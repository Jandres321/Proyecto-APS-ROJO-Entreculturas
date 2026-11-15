"""Selective reading with the vision LLM (invented data, FakeLLMClient, no Tesseract)."""

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pymupdf
import pytest

from app.config import Settings
from app.lectura import pages, pipeline, vision
from app.lectura.segment import fill_segment, split_pages
from app.llm.fake import FakeLLMClient

FILLER = "detalle del servicio prestado " * 4
GOOD = "FACTURA {n}\nRUC: 80000001-1\nFecha: 05/03/2024\nTOTAL A PAGAR Gs. 100.000\n" + FILLER
NO_DATE = "FACTURA {n}\nRUC: 80000001-1\nTOTAL A PAGAR Gs. 100.000\n" + FILLER


def _page(n: int, template: str, confidence: float | None = 90.0) -> pages.Page:
    return pages.Page(
        n=n,
        text=template.format(n=f"001-001-000{n}00"),
        source="ocr" if confidence is not None else "native",
        confidence=confidence,
    )


def _pdf(tmp_path: Path, n_pages: int) -> Path:
    path = tmp_path / "doc.pdf"
    doc = pymupdf.open()
    for i in range(n_pages):
        doc.new_page().insert_text((72, 72), f"page {i}", fontsize=11)
    doc.save(path)
    return path


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """A 4-page PDF whose OCR reading is controlled, and a fake LLM that records its calls."""
    settings = Settings(cache_dir=tmp_path / "cache", llm_provider="fake")
    ocr_pages = {
        1: _page(1, GOOD),  # fine
        2: _page(2, NO_DATE),  # no date -> doubtful (doubt 1)
        3: _page(3, GOOD, confidence=30.0),  # poor OCR -> doubtful (doubt 1.5)
        4: _page(4, GOOD),  # fine
    }
    monkeypatch.setattr(pages, "read_page", lambda path, sha, n, settings=None: ocr_pages[n])
    client = FakeLLMClient(
        responses=[{"tipo": "factura", "fecha": "2024-03-05", "importe": 7}] * 10
    )
    monkeypatch.setattr(vision, "get_vision_client", lambda settings=None: client)
    return settings, _pdf(tmp_path, 4), client


def _sent_pages(results) -> set[int]:
    return {p.n for r in results for p in r.pages if p.llm_fields}


def test_mode_off_never_calls_the_llm(setup):
    settings, path, client = setup
    results = pipeline.process_pdfs([path], settings, llm=vision.LlmReadOptions("off", 10, 60.0))
    assert client.calls == []
    assert results.llm.pages_sent == 0 and results.llm.mode == "off"
    assert results.llm.estimated_cost_usd == 0
    assert len(results[0].segments) == 4
    # no options at all behaves the same
    pipeline.process_pdfs([path], settings)
    assert client.calls == []


def test_dudosas_sends_only_doubtful_pages(setup):
    settings, path, client = setup
    results = pipeline.process_pdfs(
        [path], settings, llm=vision.LlmReadOptions("dudosas", 10, 60.0)
    )
    assert len(client.calls) == 2
    assert _sent_pages(results) == {2, 3}
    assert results.llm.pages_candidate == 2 and results.llm.pages_over_limit == 0
    assert results[0].backend.startswith("llm")


def test_max_pages_caps_the_run_and_keeps_the_most_doubtful(setup):
    settings, path, client = setup
    results = pipeline.process_pdfs([path], settings, llm=vision.LlmReadOptions("todas", 1, 60.0))
    assert len(client.calls) == 1
    assert _sent_pages(results) == {3}  # poor OCR beats a missing date
    assert results.llm.pages_sent == 1 and results.llm.pages_over_limit == 3

    client.calls.clear()
    results = pipeline.process_pdfs([path], settings, llm=vision.LlmReadOptions("todas", 2, 60.0))
    # page 3 is now cached (free); the cap counts pages SENT
    assert len(client.calls) == 2
    assert _sent_pages(results) == {3, 2, 1}  # 1 is the first of the equally good pages


def test_todas_without_cap_sends_every_page(setup):
    settings, path, client = setup
    pipeline.process_pdfs([path], settings, llm=vision.LlmReadOptions("todas", 100, 60.0))
    assert len(client.calls) == 4


def test_cache_means_no_second_charge(setup):
    settings, path, client = setup
    options = vision.LlmReadOptions("dudosas", 10, 60.0)
    pipeline.process_pdfs([path], settings, llm=options)
    client.calls.clear()
    again = pipeline.process_pdfs([path], settings, llm=options)
    assert client.calls == []
    assert again.llm.pages_cached == 2 and again.llm.pages_sent == 0


def test_summary_has_tokens_and_estimated_cost(setup):
    settings, path, _client = setup
    results = pipeline.process_pdfs(
        [path], settings, llm=vision.LlmReadOptions("dudosas", 10, 60.0)
    )
    summary = results.llm
    assert (summary.input_tokens, summary.output_tokens) == (2000, 200)
    expected = 2000 * settings.llm_price_input_per_mtok / 1e6
    expected += 200 * settings.llm_price_output_per_mtok / 1e6
    assert summary.estimated_cost_usd == pytest.approx(expected)
    assert summary.pages_total == 4


def test_external_provider_needs_the_flag_and_fails_before_reading(tmp_path):
    path = _pdf(tmp_path, 1)
    settings = Settings(
        cache_dir=tmp_path / "c", llm_provider="anthropic", allow_external_llm=False
    )
    with pytest.raises(vision.ExternalLlmNotAllowed, match="ALLOW_EXTERNAL_LLM"):
        pipeline.process_pdfs([path], settings, llm=vision.LlmReadOptions("dudosas", 5, 60.0))
    # "off" never needs the flag
    pipeline.process_pdfs([path], settings, llm=vision.LlmReadOptions("off", 5, 60.0))


def test_options_are_validated():
    with pytest.raises(ValueError):
        vision.LlmReadOptions("sometimes", 5, 60.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        vision.LlmReadOptions("dudosas", -1, 60.0)


# ------------------------------------------------------------- merge + provenance
def _segment(text: str, confidence: float | None, llm: dict, min_conf: float | None = 60.0):
    pg = pages.Page(1, text, "ocr", confidence=confidence, llm_fields=llm)
    return fill_segment(split_pages([pg])[0], "dot_thousands", None, min_conf)


def test_llm_fills_what_the_ocr_did_not_read_but_not_what_it_read_well():
    seg = _segment(NO_DATE.format(n="001-001-0000100"), 90.0, {"fecha": "2024-03-05", "importe": 7})
    assert seg.fecha == dt.date(2024, 3, 5)  # the OCR had no date
    assert seg.importe == Decimal(100000)  # good OCR keeps its amount
    assert seg.extra["campo_fuente"]["fecha"] == "llm"
    assert seg.extra["campo_fuente"]["importe"] == "ocr"


def test_llm_replaces_values_when_the_ocr_is_unreliable():
    seg = _segment(GOOD.format(n="001-001-0000100"), 30.0, {"fecha": "2024-04-01", "importe": 7})
    assert seg.importe == Decimal(7)
    assert seg.fecha == dt.date(2024, 4, 1)
    assert seg.extra["campo_fuente"]["importe"] == "llm"
    assert seg.extra["campo_fuente"]["nif"] == "ocr"  # untouched fields keep their source


def test_without_a_threshold_the_llm_always_wins():
    seg = _segment(GOOD.format(n="001-001-0000100"), 95.0, {"importe": 7}, min_conf=None)
    assert seg.importe == Decimal(7)
