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


class ReadResults(list[PdfResult]):
    """The PDFs read in one run (a plain list) + what the LLM was asked (`llm`)."""

    llm: vision.LlmReadSummary

    def __init__(self, items: Iterable[PdfResult] = (), llm: vision.LlmReadSummary | None = None):
        super().__init__(items)
        self.llm = llm or vision.LlmReadSummary()


def _segments(
    result: PdfResult, convention: str, currency: str | None, min_conf: float | None
) -> list[Segment]:
    return [fill_segment(s, convention, currency, min_conf) for s in split_pages(result.pages)]


def _llm_pass(
    results: list[PdfResult],
    client: vision.LLMClient,
    options: vision.LlmReadOptions,
    settings: Settings,
    convention: str,
    currency: str | None,
) -> vision.LlmReadSummary:
    """Choose which pages go to the LLM, read them and put the enriched pages back in `results`.

    1. Pages the LLM already read in an earlier run come from the cache (free, not counted).
    2. Of the rest, the candidates are ranked by doubt (`vision.page_doubt`); "dudosas" keeps those
       with doubt > 0 and "todas" all of them; never more than `options.max_pages` are sent.
    """
    summary = vision.LlmReadSummary(mode=options.mode, pages_total=sum(r.n_pages for r in results))
    candidates: list[tuple[float, int, int, PdfResult, pg.Page]] = []
    for r_index, result in enumerate(results):
        by_page = {}
        for segment in _segments(result, convention, currency, None):
            for page in segment.pages:
                by_page[page.n] = segment
        for p_index, page in enumerate(result.pages):
            cached = vision.load_cached(result.sha256, page, client, settings)
            if cached is not None:
                result.pages[p_index] = cached
                summary.pages_cached += cached.llm_fields is not None
                continue
            if page.error:
                continue
            doubt = vision.page_doubt(page, by_page.get(page.n), options.min_ocr_confidence)
            if options.mode == "todas" or doubt > 0:
                candidates.append((doubt, r_index, p_index, result, page))
    candidates.sort(key=lambda c: (-c[0], c[1], c[2]))
    chosen = candidates[: options.max_pages]
    summary.pages_candidate = len(candidates)
    summary.pages_over_limit = len(candidates) - len(chosen)
    summary.pages_sent = len(chosen)
    if chosen:
        enriched = asyncio.run(
            vision.enrich_selected(
                [(r.path, r.sha256, page) for _, _, _, r, page in chosen], client, settings, summary
            )
        )
        for (_, _, p_index, result, _), page in zip(chosen, enriched, strict=True):
            result.pages[p_index] = page
    vision.fill_usage(summary, client, settings)
    return summary


def process_pdfs(
    paths: Iterable[Path],
    settings: Settings | None = None,
    convention: str = "auto",
    default_currency: str | None = None,
    use_llm: bool = False,
    workers: int | None = None,
    progress: Callable[[int, int], None] | None = None,
    llm: vision.LlmReadOptions | None = None,
) -> ReadResults:
    """Read every page of every PDF in one flat pool (so many small PDFs also parallelise).

    `llm` decides which pages go to the vision LLM (see `vision.LlmReadOptions`); `use_llm=True`
    without options means every page. The returned list carries `.llm`, the summary of what was
    sent (pages, tokens, estimated cost).
    """
    settings = settings or get_settings()
    workers = workers or settings.ocr_workers or os.cpu_count() or 2
    options = (
        llm
        if llm is not None
        else (vision.LlmReadOptions("todas", vision.UNLIMITED_PAGES) if use_llm else None)
    )
    active = options is not None and options.active
    client = vision.get_vision_client(settings) if active else None  # fail early if not allowed

    results = ReadResults()
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

    readable = [r for r in results if not r.error]
    for result in readable:
        result.pages.sort(key=lambda p: p.n)
    if active and client is not None and options is not None:
        results.llm = _llm_pass(readable, client, options, settings, convention, default_currency)
        for result in readable:
            if any(p.llm_fields for p in result.pages):
                result.backend = vision.backend_name(client)
    min_conf = options.min_ocr_confidence if active and options is not None else None
    for result in readable:
        result.segments = _segments(result, convention, default_currency, min_conf)
    return results


def list_pdfs(folder: Path) -> list[Path]:
    """PDFs of a folder (recursive), sorted by name."""
    found = {p for p in folder.rglob("*") if p.suffix.lower() == ".pdf" and p.is_file()}
    return sorted(found, key=lambda p: p.name.lower())
