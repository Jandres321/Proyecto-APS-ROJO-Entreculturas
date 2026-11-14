"""Full reading pipeline: PDFs -> pages (parallel, cached) -> segments with fields."""

import asyncio
import os
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from app.config import Settings, get_settings
from app.lectura import pages as pg
from app.lectura import vision
from app.lectura.segment import Segment, fill_segment, split_pages


@dataclass
class PdfResult:
    path: Path
    sha256: str
    pages: list[pg.Page] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    backend: str = pg.BACKEND_TESSERACT
    error: str | None = None

    @property
    def n_pages(self) -> int:
        return len(self.pages)


def process_pdfs(
    paths: Iterable[Path],
    settings: Settings | None = None,
    convention: str = "auto",
    default_currency: str | None = None,
    use_llm: bool = False,
    workers: int | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> list[PdfResult]:
    """Read every page of every PDF in one flat pool (so many small PDFs also parallelise)."""
    settings = settings or get_settings()
    workers = workers or settings.ocr_workers or os.cpu_count() or 2
    client = vision.get_vision_client(settings) if use_llm else None  # fail early if not allowed

    results: list[PdfResult] = []
    tasks: list[tuple[PdfResult, int]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for path in paths:
            try:
                sha, count = pg.sha256_file(path), pg.page_count(path)
            except Exception as exc:  # corrupt / encrypted PDF
                results.append(PdfResult(path, "", error=f"{type(exc).__name__}: {exc}"))
                continue
            result = PdfResult(path, sha)
            results.append(result)
            tasks.extend((result, n) for n in range(1, count + 1))

        futures = [
            (result, pool.submit(pg.read_page, result.path, result.sha256, n, settings))
            for result, n in tasks
        ]
        for done, (result, future) in enumerate(futures, 1):
            result.pages.append(future.result())
            if progress:
                progress(done, len(futures))

    for result in results:
        if result.error:
            continue
        result.pages.sort(key=lambda p: p.n)
        if client is not None:
            result.pages = asyncio.run(
                vision.enrich_pages(result.path, result.sha256, result.pages, client, settings)
            )
            result.backend = vision.backend_name(client)
        result.segments = [
            fill_segment(s, convention, default_currency) for s in split_pages(result.pages)
        ]
    return results


def list_pdfs(folder: Path) -> list[Path]:
    """PDFs of a folder (recursive), sorted by name."""
    found = {p for p in folder.rglob("*") if p.suffix.lower() == ".pdf" and p.is_file()}
    return sorted(found, key=lambda p: p.name.lower())
