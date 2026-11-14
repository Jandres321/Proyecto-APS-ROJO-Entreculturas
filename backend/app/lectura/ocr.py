"""Tesseract OCR (run as a subprocess) and page-orientation detection."""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings, get_settings

_WINDOWS_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)


class TesseractNotFound(RuntimeError):
    pass


@dataclass
class OcrResult:
    text: str
    confidence: float  # mean 0-100 over the recognised words
    quality: float  # sum of reliable characters: used to compare rotations


def find_tesseract(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    if settings.tesseract_cmd:
        return settings.tesseract_cmd
    found = shutil.which("tesseract")
    if found:
        return found
    for path in _WINDOWS_PATHS:
        if Path(path).is_file():
            return path
    raise TesseractNotFound(
        "No se encuentra Tesseract. Instálalo (Windows: winget install UB-Mannheim.TesseractOCR; "
        "Linux: apt install tesseract-ocr tesseract-ocr-spa tesseract-ocr-fra) o define "
        "TESSERACT_CMD."
    )


def _env(settings: Settings) -> dict[str, str]:
    env = dict(os.environ)
    env["OMP_THREAD_LIMIT"] = "1"  # parallelism comes from the page pool
    if settings.tessdata_dir:
        env["TESSDATA_PREFIX"] = str(settings.tessdata_dir)
    return env


def _run(args: list[str], png: bytes, settings: Settings, timeout: int = 180) -> str:
    cmd = [find_tesseract(settings), "stdin", "stdout", *args]
    proc = subprocess.run(
        cmd, input=png, capture_output=True, timeout=timeout, env=_env(settings), check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip()[:300])
    return proc.stdout.decode("utf-8", "replace")


def ocr_png(png: bytes, settings: Settings | None = None) -> OcrResult:
    """OCR one image: text (one line per text line), mean confidence and quality score."""
    settings = settings or get_settings()
    tsv = _run(
        ["-l", settings.ocr_langs, "--psm", "3", "-c", "tessedit_create_tsv=1"], png, settings
    )
    lines: dict[tuple[str, str, str], list[str]] = {}
    confs: list[float] = []
    quality = 0.0
    for row in tsv.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or cols[0] != "5":
            continue
        try:
            conf = float(cols[10])
        except ValueError:
            continue
        word = cols[11].strip()
        if conf < 0 or not word:
            continue
        lines.setdefault((cols[2], cols[3], cols[4]), []).append(word)
        confs.append(conf)
        if conf >= 50 and re.search(r"[^\W\d_]", word):
            quality += len(word) * conf / 100
    text = "\n".join(" ".join(words) for words in lines.values())
    mean = sum(confs) / len(confs) if confs else 0.0
    return OcrResult(text=text, confidence=mean, quality=quality)


def detect_rotation(png: bytes, settings: Settings | None = None) -> int | None:
    """Rotation (0/90/180/270) proposed by Tesseract's OSD, or None when it is not reliable."""
    settings = settings or get_settings()
    try:
        out = _run(["--psm", "0", "-l", "osd"], png, settings, timeout=60)
    except (RuntimeError, subprocess.TimeoutExpired):
        return None
    rotate = re.search(r"Rotate:\s*(\d+)", out)
    conf = re.search(r"Orientation confidence:\s*([\d.]+)", out)
    if not rotate or not conf or float(conf.group(1)) < 1.5:
        return None
    return int(rotate.group(1)) % 360
