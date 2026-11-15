"""Tests for app/lectura with invented data (no real documents, no Tesseract needed)."""

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pymupdf
import pytest

from app.config import Settings
from app.enums import TipoSegmento
from app.lectura import fields, pages, vision
from app.lectura.classify import classify_page
from app.lectura.segment import fill_segment, split_pages


def page(n: int, text: str, **kw) -> pages.Page:
    return pages.Page(n=n, text=text, source=kw.pop("source", "native"), **kw)


# ---------------------------------------------------------------- amounts
@pytest.mark.parametrize(
    ("raw", "convention", "expected"),
    [
        ("1.250.000", "dot_thousands", "1250000"),
        ("370.000", "dot_thousands", "370000"),
        ("1.250,50", "dot_thousands", "1250.50"),
        ("1,250,000", "comma_thousands", "1250000"),
        ("1,250.50", "comma_thousands", "1250.50"),
        ("1.250,50", "auto", "1250.50"),
        ("1,250.50", "auto", "1250.50"),
        ("15.000.000", "auto", "15000000"),
        ("12,5", "auto", "12.5"),
        ("370·000", "dot_thousands", "370000"),
    ],
)
def test_parse_amount(raw, convention, expected):
    assert fields.parse_amount(raw, convention) == Decimal(expected)


def test_total_ignores_percentages_and_dates():
    text = "Fecha: 21/05/2023\nSubtotal 370.000\nIVA (10%) 33.636\nTOTAL A PAGAR 370.000"
    total, _ = fields.find_total([text], "dot_thousands")
    assert total == Decimal(370000)


def test_total_prefers_last_page():
    first = "Detalle\nTotal 1.000.000"
    last = "Resumen\nTotal 2.500.000"
    total, idx = fields.find_total([first, last], "dot_thousands")
    assert (total, idx) == (Decimal(2500000), 1)


def test_all_figures_of_a_segment_are_kept_without_noise():
    text = (
        "Tel: (021) 444 131\nFecha 21/05/2023\nIVA (10%) 33.636\nTotal 370.000\n"
        "Items 1,300,000.00\nRef 123456789\nAmount paid 4500000\nCode 9876"
    )
    figures = fields.find_all_amounts([text], "auto")
    assert Decimal(370000) in figures and Decimal(1300000) in figures
    assert Decimal(4500000) in figures  # unformatted, but on a line that talks about money
    assert Decimal(123456789) not in figures  # long bare number on a neutral line
    assert Decimal(33636) in figures and Decimal(444131) not in figures  # phone line skipped
    assert len(figures) == len(set(figures))


# ---------------------------------------------------------------- other fields
def test_dates_day_first_and_textual():
    assert fields.find_date("Fecha de emisión: 05/03/2024") == dt.date(2024, 3, 5)
    assert fields.find_date("Date: 5 March 2024") == dt.date(2024, 3, 5)
    assert fields.find_date("12 de octubre de 2023") == dt.date(2023, 10, 12)
    assert fields.find_date("Fecha Fin Vigencia: 31/03/2024") is None  # validity is not issue date


def test_tax_id_and_invoice_number():
    text = "Moviter S.A.\nR.U.C.: 80001700-5 FACTURA\n008-001 0130768\n"
    assert fields.find_tax_id(text) == "80001700-5"
    assert fields.find_invoice_number("Factura 001-005-0014228") == "001-005-0014228"
    assert fields.find_invoice_number("Invoice No: EB-UGA04-602") == "EB-UGA04-602"
    assert fields.find_invoice_number("Factura de venta") is None


def test_total_drops_small_noise_and_phone_numbers():
    form = "Tel: (021) 444 131\nTotal importe a liquidar\nSuma 0,19 EUR/km\nTotal 353.000"
    assert fields.find_total([form], "dot_thousands")[0] == Decimal(353000)
    assert fields.find_total(["Total (IVA 10%) 12"], "dot_thousands")[0] is None


def test_issuer_from_label_and_company_line():
    assert fields.find_issuer("Datos\nDon/Doña : Juan Pérez\nObjeto") == "Juan Pérez"
    assert fields.find_issuer(
        "GUÍA DE APLICACIÓN\nFerretería El Tornillo S.A.\nRUC 80000001-1"
    ) == ("Ferretería El Tornillo S.A.")


def test_currency_default_wins_over_stray_mention():
    assert fields.find_currency("pagado en euros", "PYG") == "PYG"
    assert fields.find_currency("Total UGX 1,500,000", None) == "UGX"
    assert fields.find_currency("sin moneda", "PYG") == "PYG"


def test_stamp_needs_donor_and_hint_and_ignores_experiencia():
    stamp = fields.find_stamp(
        "Financiado por: AECID\nPorcentaje de imputación a la agencia: 100 %\n"
        "Nº Expdte: 2022/PRYC/000593"
    )
    assert stamp.present
    assert stamp.expedient == "2022/PRYC/000593"
    assert stamp.percent == 100.0
    assert not fields.find_stamp("Factura de combustible 100 litros").present
    assert not fields.find_stamp("AECID aparece en un texto cualquiera sin más").present
    assert fields.find_stamp("AECID imputación 50% experiencia").expedient is None


@pytest.mark.parametrize(
    "text",
    [
        "Factura 77\nTotal 1,250,000\nForma de pago: transferencia bancaria a 30 días",
        "INVOICE 12\nPayment terms: cheque, net 30",
        "Factura 5\nCondiciones de pago: efectivo a la entrega",
        "Factura 9\nDatos bancarios para transferencia: IBAN XX00 0000 0000",
        "Bill 3\nPlease paye at the counter",  # «paye» ya no cuenta
        "Factura 4\nCon esta fecha se emite la presente factura",  # «con esta fecha» tampoco
        "Factura 8\nTotal a pagar 300.000",
    ],
)
def test_payment_terms_are_not_proof_of_payment(text):
    assert not fields.has_payment_proof(text, TipoSegmento.FACTURA)


@pytest.mark.parametrize(
    "text",
    [
        "Factura 21\nPAGADO",
        "Invoice 21\nPaid in full",
        "Invoice 22\nReceived with thanks",
        "Recibí conforme el importe de esta factura",
        "Justificante de transferencia emitida",
        "Comprobante de transferencia nº 4455",
        "Nº de operación: 000123456",
        "Referencia bancaria 99887766",
        "Factura 30\nAbonado mediante cheque nº 12",
        "Factura 31\nSe pagó en efectivo el día 3",
    ],
)
def test_real_payment_marks_still_count(text):
    assert fields.has_payment_proof(text, TipoSegmento.FACTURA)


def test_receipts_and_transfers_are_always_payment_proof():
    assert fields.has_payment_proof("", TipoSegmento.RECIBO)
    assert fields.has_payment_proof("", TipoSegmento.TRANSFERENCIA)


def test_legibility():
    assert fields.is_legible(80, None)
    assert not fields.is_legible(5, 90.0)
    assert not fields.is_legible(80, 20.0)


# ---------------------------------------------------------------- classification
def test_classify_pages():
    assert classify_page("FACTURA Nº 001-001-0000123\nRUC 800\nSubtotal IVA total a pagar") == (
        TipoSegmento.FACTURA
    )
    assert classify_page("Comprobante de transferencia SWIFT beneficiario IBAN") == (
        TipoSegmento.TRANSFERENCIA
    )
    assert classify_page("LISTA DE ASISTENCIA participantes firma") == TipoSegmento.LISTA_ASISTENCIA
    assert classify_page("PAYSLIP net pay deductions") == TipoSegmento.NOMINA
    assert classify_page("texto cualquiera sin nada") == TipoSegmento.OTRO


# ---------------------------------------------------------------- segmentation
FACTURA_A = "FACTURA 001-001-0000100\nRUC: 80000001-1\nTOTAL A PAGAR 100.000\nSubtotal IVA"
FACTURA_B = "FACTURA 001-001-0000200\nRUC: 80000002-2\nTOTAL A PAGAR 200.000\nSubtotal IVA"


def test_split_two_invoices_and_continuation():
    pgs = [
        page(1, FACTURA_A),
        page(2, FACTURA_B),
        page(3, "anverso en blanco"),  # short page: back side of the previous document
        page(4, "Comprobante de transferencia SWIFT beneficiario IBAN Total 200.000"),
    ]
    segs = split_pages(pgs)
    assert [(s.tipo, s.pagina_inicio, s.pagina_fin) for s in segs] == [
        (TipoSegmento.FACTURA, 1, 1),
        (TipoSegmento.FACTURA, 2, 3),
        (TipoSegmento.TRANSFERENCIA, 4, 4),
    ]


def test_multi_page_contract_absorbs_annexes():
    contract = "CONTRATO DE SERVICIOS\nCláusula primera: las partes acuerdan"
    annex = "Anexo con viáticos para los participantes " + "texto " * 40
    segs = split_pages([page(1, contract), page(2, contract + " cláusula segunda"), page(3, annex)])
    assert len(segs) == 1
    assert segs[0].tipo == TipoSegmento.CONTRATO
    assert (segs[0].pagina_inicio, segs[0].pagina_fin) == (1, 3)


def test_fill_segment_fields():
    text = (
        "Moviter S.A.\nR.U.C.: 80001700-5\nFACTURA 008-001-0130768\nFecha de emisión: 21/05/2023\n"
        "TOTAL A PAGAR Gs. 370.000\nFinanciado por: AECID\nimputación 100 %\n"
        "Expdte: 2022/PRYC/000593"
    )
    seg = fill_segment(split_pages([page(3, text)])[0], "dot_thousands", "PYG")
    assert seg.tipo == TipoSegmento.FACTURA
    assert seg.emisor == "Moviter S.A."
    assert seg.nif == "80001700-5"
    assert seg.numero_factura == "008-001-0130768"
    assert seg.fecha == dt.date(2023, 5, 21)
    assert seg.importe == Decimal(370000)
    assert seg.moneda == "PYG"
    assert seg.sello_donante
    assert (seg.sello_expediente, seg.sello_porcentaje) == ("2022/PRYC/000593", 100.0)
    assert seg.legible
    assert seg.pagina_inicio == 3


def test_vision_llm_fields_override_heuristics():
    pg = page(1, FACTURA_A, llm_fields={"tipo": "factura", "importe": 99, "legible": True})
    seg = fill_segment(split_pages([pg])[0], "dot_thousands")
    assert seg.importe == Decimal(99)
    assert seg.extra["llm"]


# ---------------------------------------------------------------- reading + cache
def _pdf(tmp_path: Path, text: str = "") -> Path:
    path = tmp_path / "doc.pdf"
    doc = pymupdf.open()
    for body in (text or "").split("\f"):
        pg_ = doc.new_page()
        pg_.insert_text((72, 72), body, fontsize=11)
    doc.save(path)
    return path


def test_native_text_is_read_and_cached(tmp_path):
    body = "FACTURA 001-001-0000100 TOTAL A PAGAR 100.000 emitida por Proveedor de prueba SA"
    path = _pdf(tmp_path, body)
    settings = Settings(cache_dir=tmp_path / "cache")
    sha = pages.sha256_file(path)
    first = pages.read_page(path, sha, 1, settings)
    assert first.source == "native"
    assert "FACTURA" in first.text
    cached_file = tmp_path / "cache" / "lectura" / sha / "1.tesseract.json"
    assert cached_file.is_file()
    path.unlink()  # a cache hit must not need the PDF
    assert pages.read_page(path, sha, 1, settings).text == first.text


def test_ocr_fallback_and_rotation_logic(tmp_path, monkeypatch):
    """Scanned page: poor first OCR -> OSD says 180 -> the rotated read wins."""
    path = _pdf(tmp_path, "x")
    settings = Settings(cache_dir=tmp_path / "cache")
    calls: list[int] = []

    def fake_render(_path, _n, _dpi, rotation=0):
        calls.append(rotation)
        return str(rotation).encode()

    def fake_ocr(png, _settings=None):
        rotated = png == b"180"
        return pages.ocr.OcrResult(
            text="palabra " * 40 if rotated else "x",
            confidence=90.0 if rotated else 10.0,
            quality=300.0 if rotated else 0.0,
        )

    monkeypatch.setattr(pages.ocr, "ocr_png", fake_ocr)
    monkeypatch.setattr(pages.ocr, "detect_rotation", lambda png, s=None: 180)
    result, rotation = pages.ocr_with_rotation(path, 1, settings, fake_render)
    assert rotation == 180
    assert result.confidence == 90.0
    assert calls[0] == 0


# ---------------------------------------------------------------- external LLM gate
def test_external_llm_is_blocked_unless_allowed():
    with pytest.raises(vision.ExternalLlmNotAllowed):
        vision.get_vision_client(Settings(llm_provider="anthropic", allow_external_llm=False))
    # the offline fake client never leaves the machine
    assert vision.get_vision_client(Settings(llm_provider="fake")).provider == "fake"
    # allowed but not implemented yet: fails loudly instead of silently sending nothing
    with pytest.raises(NotImplementedError):
        vision.get_vision_client(Settings(llm_provider="gemini", allow_external_llm=True))


def _tesseract_available() -> bool:
    try:
        return bool(pages.ocr.find_tesseract(Settings()))
    except pages.ocr.TesseractNotFound:
        return False


@pytest.mark.skipif(not _tesseract_available(), reason="Tesseract not installed")
def test_tesseract_reads_rendered_text(tmp_path):
    path = _pdf(tmp_path, "FACTURA 100 TOTAL")
    png = pages.render(path, 1, 200)
    result = pages.ocr.ocr_png(png, Settings(cache_dir=tmp_path))
    assert "FACTURA" in result.text.upper()
