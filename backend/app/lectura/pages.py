"""Reading one page: native text if present, otherwise OCR with rotation correction.

Disk cache keyed by sha256 + page + backend:
CACHE_DIR/lectura/<sha256>/<page>.<backend>.json
"""

import hashlib
import json
import re
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pymupdf

from app.config import Settings, get_settings
from app.lectura import ocr

# Bump when the way a page is read changes, to invalidate the cache.
READ_VERSION = "2"
BACKEND_TESSERACT = "tesseract"

MIN_NATIVE_CHARS = 50
MIN_NATIVE_WORDS = 8
GOOD_CONFIDENCE = 70.0
GOOD_WORDS = 30

# PyMuPDF is not thread-safe: rendering runs under a lock, OCR (a subprocess) outside it.
_PDF_LOCK = threading.Lock()


@dataclass
class Page:
    n: int  # 1-based
    text: str
    source: str  # "native" | "ocr"
    rotation: int = 0
    confidence: float | None = None  # OCR only
    backend: str = BACKEND_TESSERACT
    llm_fields: dict[str, Any] | None = None
    error: str | None = None

    @property
    def n_words(self) -> int:
        return len(re.findall(r"\w{2,}", self.text))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def page_count(path: Path) -> int:
    with _PDF_LOCK, pymupdf.open(path) as doc:
        return doc.page_count


def _cache_path(sha256: str, n: int, backend: str, settings: Settings) -> Path:
    safe = re.sub(r"[^\w.-]", "_", backend)
    return settings.cache_dir / "lectura" / sha256 / f"{n}.{safe}.json"


def _load_cache(path: Path) -> Page | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.pop("version", None) != READ_VERSION:
        return None
    try:
        return Page(**data)
    except TypeError:
        return None


def _save_cache(path: Path, page: Page) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
    tmp.write_text(
        json.dumps({**asdict(page), "version": READ_VERSION}, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(path)


def render(path: Path, n: int, dpi: int, rotation: int = 0) -> bytes:
    """PNG of page `n` (1-based), rotated `rotation` degrees."""
    with _PDF_LOCK, pymupdf.open(path) as doc:
        matrix = pymupdf.Matrix(dpi / 72, dpi / 72).prerotate(rotation)
        return doc[n - 1].get_pixmap(matrix=matrix, alpha=False).tobytes("png")


def _native_text(path: Path, n: int) -> str:
    with _PDF_LOCK, pymupdf.open(path) as doc:
        return doc[n - 1].get_text("text").strip()


def _is_good(result: ocr.OcrResult) -> bool:
    return (
        result.confidence >= GOOD_CONFIDENCE
        and len(re.findall(r"\w{2,}", result.text)) >= GOOD_WORDS
    )


def ocr_with_rotation(
    path: Path, n: int, settings: Settings, render_fn: Callable[..., bytes] = render
) -> tuple[ocr.OcrResult, int]:
    """OCR a page fixing its rotation. Returns (result, rotation applied).

    The page is read as is; only when the result is poor is the orientation detected (OSD)
    and the candidate rotations tried, keeping the one that reads best.
    """
    png = render_fn(path, n, settings.ocr_dpi, 0)
    best, best_rotation = ocr.ocr_png(png, settings), 0
    if _is_good(best):
        return best, 0
    osd = ocr.detect_rotation(png, settings)
    if osd:
        candidates = [osd, (360 - osd) % 360]
    elif osd == 0:
        candidates = []  # detector says it is upright: the text is simply poor
    else:
        candidates = [180, 90, 270]
    for rotation in candidates:
        attempt = ocr.ocr_png(render_fn(path, n, settings.ocr_dpi, rotation), settings)
        if attempt.quality > best.quality * 1.2 + 1:
            best, best_rotation = attempt, rotation
        if _is_good(best):
            break
    return best, best_rotation


def read_page(
    path: Path,
    sha256: str,
    n: int,
    settings: Settings | None = None,
    backend: str = BACKEND_TESSERACT,
) -> Page:
    """Read one page (cached): native text if present, otherwise OCR."""
    settings = settings or get_settings()
    cache = _cache_path(sha256, n, backend, settings)
    cached = _load_cache(cache)
    if cached is not None:
        return cached
    try:
        native = _native_text(path, n)
        if len(native) >= MIN_NATIVE_CHARS and len(re.findall(r"\w{2,}", native)) >= (
            MIN_NATIVE_WORDS
        ):
            page = Page(n=n, text=native, source="native", backend=backend)
        else:
            result, rotation = ocr_with_rotation(path, n, settings)
            page = Page(
                n=n,
                text=result.text,
                source="ocr",
                rotation=rotation,
                confidence=round(result.confidence, 1),
                backend=backend,
            )
    except Exception as exc:  # a broken page must not sink the whole PDF (and is not cached)
        return Page(
            n=n, text="", source="ocr", backend=backend, error=f"{type(exc).__name__}: {exc}"
        )
    _save_cache(cache, page)
    return page
