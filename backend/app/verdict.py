"""Verdict of an expense: "Bien", "Necesita revisión" or "No aplica", with the reasons.

The rule the ONG asked for: no expense may look correct unless it was really checked. So "Bien"
is not "no finding was raised" but "a document confirmed by its content, with the key fields read
reliably, and nothing open". Anything that could not be checked is a reason to review, never a
pass. This is the only place where the verdict is decided; the web app and the Excel show it.
"""

import datetime as dt
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.enums import REVIEW_DECISION_LABELS_ES, FindingStatus, ReviewDecision, Severity
from app.models import Expense, Finding, Segmento
from app.review import (
    BY_NAME,
    MANUAL,
    MISSING,
    NOT_IN_SAMPLE,
    NOT_REQUIRED_COFINANCED,
    NOT_REQUIRED_INDIRECT,
    DocumentStatus,
)

OK, REVIEW, NOT_APPLICABLE = "ok", "review", "na"
VERDICT_LABELS_ES = {OK: "Bien", REVIEW: "Necesita revisión", NOT_APPLICABLE: "No aplica"}
NOT_REVIEWED_STATUSES = {NOT_IN_SAMPLE, NOT_REQUIRED_COFINANCED, NOT_REQUIRED_INDIRECT}

# Below this OCR confidence (0-100) a reading is not reliable enough to call an expense "Bien".
MIN_OCR_CONFIDENCE = 60.0
# Relative tolerance when comparing the amount read with the listing (same as the checks).
AMOUNT_TOLERANCE = Decimal("0.01")

REASON_LABELS_ES = {
    "DOC_MISSING": "Sin justificante",
    "ONLY_BY_NAME": "Solo emparejado por el nombre del archivo",
    "MANUAL_UNCHECKED": "Vinculado a mano: contenido sin comprobar",
    "NOT_READ_DOCUMENT": "No se ha leído el contenido del justificante",
    "NOT_READ_AMOUNT": "No leído: importe",
    "NOT_READ_DATE": "No leído: fecha",
    "NOT_READ_ISSUER": "No leído: emisor ni nº de factura",
    "DOUBTFUL_READING": "Lectura dudosa",
}
BLOCKING_REASONS = {"DOC_MISSING"}


@dataclass(frozen=True)
class Reason:
    code: str
    label: str
    blocking: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "label": self.label, "blocking": self.blocking}


def _reason(code: str, label: str | None = None) -> Reason:
    return Reason(code, label or REASON_LABELS_ES[code], code in BLOCKING_REASONS)


def reasons_for(
    status: DocumentStatus,
    segments: list[Segmento],
    findings: list[Finding],
    rule_labels: dict[str, str],
) -> list[Reason]:
    """Why an expense needs review (empty = nothing to review). Findings the reviewer marked as
    false positives are not reasons; accepted ones are (they are real problems)."""
    reasons: list[Reason] = []
    if status.status == MISSING:
        reasons.append(_reason("DOC_MISSING"))
    elif status.status not in NOT_REVIEWED_STATUSES:
        # There is a document: say what could not be checked about it.
        if status.status == BY_NAME:
            reasons.append(_reason("ONLY_BY_NAME"))
        elif status.status == MANUAL and not segments:
            reasons.append(_reason("MANUAL_UNCHECKED"))
        if not segments:
            reasons.append(_reason("NOT_READ_DOCUMENT"))
        else:
            if all(s.importe is None for s in segments):
                reasons.append(_reason("NOT_READ_AMOUNT"))
            if all(s.fecha is None for s in segments):
                reasons.append(_reason("NOT_READ_DATE"))
            if all(not s.emisor and not s.numero_factura for s in segments):
                reasons.append(_reason("NOT_READ_ISSUER"))
            doubtful = [
                s
                for s in segments
                if not s.legible
                or (s.confianza_ocr is not None and s.confianza_ocr < MIN_OCR_CONFIDENCE)
            ]
            if doubtful:
                worst = min(
                    (s.confianza_ocr for s in doubtful if s.confianza_ocr is not None), default=None
                )
                detail = "ilegible" if worst is None else f"confianza de lectura {worst:.0f} %"
                reasons.append(_reason("DOUBTFUL_READING", f"Lectura dudosa ({detail})"))
    seen = {r.code for r in reasons}
    for f in sorted(findings, key=lambda f: (f.severity != Severity.HIGH, f.id or 0)):
        if f.status == FindingStatus.FALSE_POSITIVE or f.rule_code in seen:
            continue
        label = rule_labels.get(f.rule_code, f.rule_code)
        if f.status == FindingStatus.ACCEPTED:
            reasons.append(Reason(f.rule_code, f"Confirmada: {label}", True))
        else:
            reasons.append(Reason(f.rule_code, label, f.severity == Severity.HIGH))
        seen.add(f.rule_code)
    return reasons


def verdict_for(status: DocumentStatus, reasons: list[Reason]) -> dict[str, str]:
    if status.status in NOT_REVIEWED_STATUSES and not reasons:
        code = NOT_APPLICABLE
    else:
        code = OK if not reasons else REVIEW
    return {"code": code, "label": VERDICT_LABELS_ES[code]}


# --- "Informe frente a PDF" ----------------------------------------------------------------


def _norm(text: str | None) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _fmt_amount(value: Decimal | None, currency: str | None) -> str | None:
    if value is None:
        return None
    text = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{text} {currency}".strip() if currency else text


def _fmt_date(value: dt.date | None) -> str | None:
    return value.strftime("%d/%m/%Y") if value else None


def _amount_matches(read: Decimal, expense: Expense) -> bool:
    for listed in (expense.amount_local, expense.amount_eur, expense.amount_donor):
        if listed and abs(read - listed) <= abs(listed) * AMOUNT_TOLERANCE:
            return True
    return False


def _yes_no(value: bool | None) -> str | None:
    return None if value is None else ("sí" if value else "no")


def compare_rows(expense: Expense, segments: list[Segmento]) -> list[dict[str, Any]]:
    """What the listing says next to what was read in the document (None = not read)."""
    seg = next((s for s in segments if s.importe is not None), segments[0] if segments else None)
    extra = expense.extra or {}
    listed_amount = (
        _fmt_amount(expense.amount_local, expense.currency)
        if expense.amount_local is not None
        else _fmt_amount(expense.amount_eur, "EUR")
    )

    def row(field: str, label: str, listed: Any, read: Any, match: bool | None) -> dict[str, Any]:
        return {"field": field, "label": label, "listed": listed, "read": read, "match": match}

    if seg is None:
        return [
            row("importe", "Importe", listed_amount, None, None),
            row("fecha", "Fecha", _fmt_date(expense.date), None, None),
            row("emisor", "Emisor", expense.issuer, None, None),
            row("factura", "Nº de factura", extra.get("invoice_no"), None, None),
            row("sello", "Sello del donante", None, None, None),
            row("pago", "Prueba de pago", extra.get("payment"), None, None),
        ]
    invoice = extra.get("invoice_no")
    issuer_match = None
    if expense.issuer and seg.emisor:
        a, b = _norm(expense.issuer), _norm(seg.emisor)
        issuer_match = bool(a and b and (a in b or b in a))
    invoice_match = None
    if invoice and seg.numero_factura:
        invoice_match = _norm(str(invoice)) == _norm(seg.numero_factura)
    return [
        row(
            "importe",
            "Importe",
            listed_amount,
            _fmt_amount(seg.importe, seg.moneda),
            None if seg.importe is None else _amount_matches(seg.importe, expense),
        ),
        row(
            "fecha",
            "Fecha",
            _fmt_date(expense.date),
            _fmt_date(seg.fecha),
            None if not (seg.fecha and expense.date) else seg.fecha == expense.date,
        ),
        row("emisor", "Emisor", expense.issuer, seg.emisor, issuer_match),
        row("factura", "Nº de factura", invoice, seg.numero_factura, invoice_match),
        row("sello", "Sello del donante", None, _yes_no(seg.sello_donante), seg.sello_donante),
        row(
            "pago",
            "Prueba de pago",
            extra.get("payment"),
            _yes_no(seg.prueba_pago),
            seg.prueba_pago,
        ),
    ]


# --- the reviewer's decision ---------------------------------------------------------------


def fingerprint(
    reasons: list[Reason],
    findings: list[Finding],
    compare: list[dict[str, Any]],
    document_ids: list[int],
) -> str:
    """What the reviewer sees when deciding. If it changes (another reading, another document,
    a new finding), an earlier decision no longer applies."""
    payload = {
        "reasons": sorted(r.code for r in reasons if not r.label.startswith("Confirmada")),
        "findings": sorted((f.rule_code, f.message) for f in findings),
        "read": [(c["field"], c["read"]) for c in compare],
        "documents": sorted(document_ids),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def decision_view(expense: Expense, current_fingerprint: str) -> dict[str, Any]:
    status = expense.review_status or ReviewDecision.PENDING
    stale = status != ReviewDecision.PENDING and expense.review_fingerprint != current_fingerprint
    shown = ReviewDecision.PENDING if stale else status
    return {
        "status": shown.value,
        "label": REVIEW_DECISION_LABELS_ES[shown],
        "note": expense.review_note,
        "decided_at": expense.reviewed_at.isoformat() if expense.reviewed_at else None,
        # The reviewer decided, but what was read or found has changed since then.
        "stale": stale,
    }
