"""Segmentation of a PDF into logical documents (consecutive pages) + field extraction."""

import contextlib
import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.enums import TipoSegmento
from app.lectura import fields
from app.lectura.classify import classify_scored, is_proforma
from app.lectura.pages import Page

# Types whose continuation pages often carry no keyword (annexes, signature sheets, tables).
MULTI_PAGE = {
    TipoSegmento.CONTRATO,
    TipoSegmento.LISTA_ASISTENCIA,
    TipoSegmento.LIQUIDACION,
    TipoSegmento.NOMINA,
}
# One physical document per number: a different number starts a new segment.
NUMBERED = {TipoSegmento.FACTURA, TipoSegmento.RECIBO, TipoSegmento.TRANSFERENCIA}
SHORT_PAGE_WORDS = 25
# A page of another type scoring below this inside a multi-page document is just an annex.
WEAK_SCORE = 5.0


@dataclass
class Segment:
    tipo: TipoSegmento
    pages: list[Page] = field(default_factory=list)
    emisor: str | None = None
    nif: str | None = None
    numero_factura: str | None = None
    fecha: dt.date | None = None
    importe: Decimal | None = None
    importes: list[Decimal] = field(default_factory=list)  # every figure read (see fields)
    moneda: str | None = None
    numero_orden: str | None = None
    sello_donante: bool = False
    sello_expediente: str | None = None
    sello_porcentaje: float | None = None
    prueba_pago: bool = False
    legible: bool = True
    confianza_ocr: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def pagina_inicio(self) -> int:
        return self.pages[0].n

    @property
    def pagina_fin(self) -> int:
        return self.pages[-1].n


def _page_type(page: Page) -> tuple[TipoSegmento, float]:
    if page.llm_fields and page.llm_fields.get("tipo"):
        try:
            return TipoSegmento(page.llm_fields["tipo"]), 99.0
        except ValueError:
            pass
    return classify_scored(page.text)


def split_pages(pages: list[Page]) -> list[Segment]:
    """Group consecutive pages into logical documents."""
    segments: list[Segment] = []
    for page in pages:
        tipo, score = _page_type(page)
        number = fields.find_invoice_number(page.text) if tipo in NUMBERED else None
        current = segments[-1] if segments else None
        if current is None:
            segments.append(Segment(tipo, [page], extra={"_number": number}))
            continue
        if tipo == current.tipo:
            previous = current.extra.get("_number")
            new_document = tipo in NUMBERED and number and previous and number != previous
            if not new_document:
                current.pages.append(page)
                current.extra["_number"] = previous or number
                continue
        elif (
            tipo == TipoSegmento.OTRO
            and current.tipo != TipoSegmento.OTRO
            and (current.tipo in MULTI_PAGE or page.n_words < SHORT_PAGE_WORDS)
        ) or (current.tipo in MULTI_PAGE and score < WEAK_SCORE):
            current.pages.append(page)  # continuation / back side / annex
            continue
        segments.append(Segment(tipo, [page]))
        segments[-1].extra["_number"] = number
    for segment in segments:
        segment.extra.pop("_number", None)
    return segments


def _first(values: list[Any]) -> Any:
    return next((v for v in values if v), None)


def fill_segment(
    segment: Segment,
    convention: str = "auto",
    default_currency: str | None = None,
    min_ocr_confidence: float | None = None,
) -> Segment:
    """Extract the fields of a segment from the text of its pages (+ LLM fields if any).

    LLM values replace the OCR ones only where the OCR had none or the segment's OCR confidence is
    below `min_ocr_confidence`. With `min_ocr_confidence=None` (direct callers) the LLM always wins.
    `extra["campo_fuente"]` records, per field, which source gave the value ("ocr" or "llm").
    """
    texts = [p.text for p in segment.pages]
    joined = "\n".join(texts)
    tipo = segment.tipo

    segment.emisor = fields.find_issuer(texts[0]) or _first([fields.find_issuer(t) for t in texts])
    segment.nif = _first([fields.find_tax_id(t) for t in texts])
    segment.numero_factura = (
        _first([fields.find_invoice_number(t) for t in texts]) if tipo in NUMBERED else None
    )
    segment.fecha = _first([fields.find_date(t) for t in texts])
    if tipo not in (TipoSegmento.LISTA_ASISTENCIA, TipoSegmento.OTRO):
        total, _ = fields.find_total(texts, convention)
        segment.importe = total
    if tipo != TipoSegmento.LISTA_ASISTENCIA:  # attendance lists are full of phone numbers
        segment.importes = fields.find_all_amounts(texts, convention)
        segment.extra["importes"] = [str(v) for v in segment.importes]
    segment.moneda = fields.find_currency(joined, default_currency)
    segment.numero_orden = _first([fields.find_order_number(t) for t in texts])
    stamp = fields.find_stamp(joined)
    segment.sello_donante = stamp.present
    segment.sello_expediente = stamp.expedient
    segment.sello_porcentaje = stamp.percent
    segment.prueba_pago = fields.has_payment_proof(joined, tipo)

    ocr_conf = [p.confidence for p in segment.pages if p.confidence is not None]
    segment.confianza_ocr = round(sum(ocr_conf) / len(ocr_conf), 1) if ocr_conf else None
    words = sum(p.n_words for p in segment.pages)
    segment.legible = fields.is_legible(words, segment.confianza_ocr)
    if is_proforma(joined) and tipo in (TipoSegmento.FACTURA, TipoSegmento.RECIBO):
        segment.extra["proforma"] = True
    if fields.has_social_security(joined):
        segment.extra["cotizacion"] = True
    segment.extra["paginas"] = [p.n for p in segment.pages]
    segment.extra["fuente"] = sorted({p.source for p in segment.pages})
    rotated = [p.n for p in segment.pages if p.rotation]
    if rotated:
        segment.extra["paginas_giradas"] = rotated

    _record_sources(segment)
    _merge_llm(segment, min_ocr_confidence)
    return segment


_LLM_SCALARS = ("emisor", "nif", "numero_factura", "moneda", "numero_orden", "sello_expediente")
# Fields whose absence is a falsy value rather than None.
_SOURCE_FIELDS = (
    *_LLM_SCALARS,
    "fecha",
    "importe",
    "sello_donante",
    "sello_porcentaje",
    "prueba_pago",
)


def _record_sources(segment: Segment) -> None:
    """Initial provenance: every field the heuristics found comes from the OCR / native text."""
    segment.extra["campo_fuente"] = {
        name: "ocr" for name in _SOURCE_FIELDS if getattr(segment, name) not in (None, "", False)
    }


def _merge_llm(segment: Segment, min_ocr_confidence: float | None = None) -> None:
    """Vision-LLM values replace the heuristics where those had nothing or the OCR is unreliable."""
    extracted = [p.llm_fields for p in segment.pages if p.llm_fields]
    if not extracted:
        return
    sources: dict[str, str] = segment.extra.setdefault("campo_fuente", {})
    low_confidence = min_ocr_confidence is None or not segment.legible
    if min_ocr_confidence is not None and segment.confianza_ocr is not None:
        low_confidence = low_confidence or segment.confianza_ocr < min_ocr_confidence

    def may_replace(name: str, current: Any) -> bool:
        return low_confidence or current in (None, "", False)

    def take(name: str, value: Any) -> None:
        setattr(segment, name, value)
        sources[name] = "llm"

    for name in _LLM_SCALARS:
        value = _first([f.get(name) for f in extracted])
        if value and may_replace(name, getattr(segment, name)):
            take(name, str(value))
    if (raw := _first([f.get("fecha") for f in extracted])) is not None and may_replace(
        "fecha", segment.fecha
    ):
        with contextlib.suppress(ValueError):
            take("fecha", dt.date.fromisoformat(str(raw)))
    if (raw := _first([f.get("importe") for f in extracted])) is not None and may_replace(
        "importe", segment.importe
    ):
        with contextlib.suppress(ArithmeticError):
            take("importe", Decimal(str(raw)))
    for name in ("sello_donante", "prueba_pago"):
        values = [f.get(name) for f in extracted if f.get(name) is not None]
        if values and may_replace(name, getattr(segment, name)):
            take(name, any(values))
    legible = [f.get("legible") for f in extracted if f.get("legible") is not None]
    if legible and (low_confidence or not segment.legible):
        take("legible", all(legible))
    if (pct := _first([f.get("sello_porcentaje") for f in extracted])) is not None and may_replace(
        "sello_porcentaje", segment.sello_porcentaje
    ):
        take("sello_porcentaje", float(pct))
    segment.extra["llm"] = True
