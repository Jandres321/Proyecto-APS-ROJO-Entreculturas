"""Step 4 — Checks: per expense and its document segments, driven by `rules/<donor>.yaml`.

Numeric comparisons are done here in code; the thresholds, tolerances, normative references and
document requirements come from the donor's rules (app/rules.py). Segments come from
`app/lectura/` and are linked to expenses by `app/pipeline/match_content.py`.

Two views of an expense's documents are used:

- ``linked``: the segments linked to the expense by content (amount / date / stamp checks are
  made on these: they ARE its justificante);
- ``bundle``: every segment of the PDFs those links belong to (payment proof, contract,
  attendance list... usually sit next to the invoice in the same PDF).

Checks (rule codes in app/rules.py): AMOUNT_MISMATCH, DATE_MISMATCH, DONOR_STAMP_MISSING,
NOT_AN_INVOICE, PAYMENT_EVIDENCE_MISSING, CASH_OVER_LIMIT, ILLEGIBLE (`check_documents`),
DATE_OUT_OF_PERIOD (when the project has a period), DOC_INCOMPLETE (`check_file_completeness`)
and DUPLICATE_INVOICE (`check_duplicates`).
An expense without any linked segment gets none of the document checks: DOC_MISSING (content
matching) already reports it.
"""

import datetime as dt
import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import Severity
from app.lectura import fields
from app.lectura.classify import normalize
from app.models import Document, Expense, ExpenseDocument, ExpenseSegment, Project, Segmento
from app.pipeline.match import _sync_findings
from app.pipeline.match_config import KIND_COFINANCED, expense_kind, get_match_config
from app.pipeline.match_content import fmt_amount
from app.rules import (
    AMOUNT_MISMATCH,
    CASH_OVER_LIMIT,
    CHECK_CODES,
    DATE_MISMATCH,
    DATE_OUT_OF_PERIOD,
    DOC_INCOMPLETE,
    DONOR_STAMP_MISSING,
    DUPLICATE_INVOICE,
    ILLEGIBLE,
    NOT_AN_INVOICE,
    PAYMENT_EVIDENCE_MISSING,
    RuleSet,
    load_rules,
    partida_matches,
)
from app.schemas import FindingIn, RuleIn

INVOICE_LIKE = ("factura", "recibo", "liquidacion")
FULL_PERCENT = 99.5  # an expected imputation at or above this counts as 100 %

TIPO_LABELS_ES = {
    "factura": "factura",
    "recibo": "recibo",
    "nomina": "nómina",
    "transferencia": "justificante de transferencia",
    "lista_asistencia": "lista de asistencia",
    "liquidacion": "liquidación",
    "contrato": "contrato",
    "otro": "otro documento",
}
FIELD_LABELS_ES = {
    "numero_factura": "el nº de factura",
    "emisor": "el emisor",
    "fecha": "la fecha",
}


# --- inputs ----------------------------------------------------------------------------------


@dataclass
class Seg:
    """A document segment as the checks see it."""

    id: int
    doc_key: str  # sha256 of the PDF
    tipo: str = "otro"
    document_id: int | None = None
    filename: str | None = None
    emisor: str | None = None
    nif: str | None = None
    numero_factura: str | None = None
    fecha: dt.date | None = None
    importe: Decimal | None = None
    importes: tuple[Decimal, ...] = ()
    moneda: str | None = None
    sello_donante: bool = False
    sello_expediente: str | None = None
    sello_porcentaje: float | None = None
    prueba_pago: bool = False
    legible: bool = True
    confianza_ocr: float | None = None
    pagina_inicio: int = 1
    pagina_fin: int = 1
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def where(self) -> str:
        pages = (
            f"pág. {self.pagina_inicio}"
            if self.pagina_inicio == self.pagina_fin
            else f"págs. {self.pagina_inicio}-{self.pagina_fin}"
        )
        return f"«{self.filename}», {pages}" if self.filename else pages

    @property
    def label(self) -> str:
        return TIPO_LABELS_ES.get(self.tipo, self.tipo)


@dataclass
class ExpenseFacts:
    id: int
    order_no: str | None = None
    date: dt.date | None = None
    issuer: str | None = None
    concept: str | None = None
    partida_code: str | None = None
    currency: str | None = None
    amount_local: Decimal | None = None
    amount_eur: Decimal | None = None
    amount_donor: Decimal | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    cofinanced: bool = False
    source_row: int | None = None  # row of the listing, to tell apart repeated order numbers
    # Best confidence of the hybrid (filename + content) link to a document: "high" / "medium" /
    # "low" / None. A trusted link turns an amount disagreement into "check the OCR reading".
    link_confidence: str | None = None

    @property
    def ref(self) -> str:
        return f"nº {self.order_no}" if self.order_no else "sin nº de orden"

    @property
    def ref_row(self) -> str:
        return f"{self.ref} (fila {self.source_row})" if self.source_row else self.ref

    @property
    def amounts(self) -> dict[str, Decimal]:
        found = {
            "local": self.amount_local,
            "eur": self.amount_eur,
            "donor": self.amount_donor,
        }
        return {k: v for k, v in found.items() if v is not None and v > 0}

    @property
    def invoice_no(self) -> str | None:
        value = (self.extra or {}).get("invoice_no")
        return str(value) if value else None


@dataclass
class Case:
    expense: ExpenseFacts
    linked: list[Seg] = field(default_factory=list)
    bundle: list[Seg] = field(default_factory=list)


@dataclass
class CheckContext:
    rules: RuleSet
    cases: list[Case]
    # segment id -> every expense linked to it (a document can back several expenses)
    expenses_of_segment: dict[int, list[ExpenseFacts]] = field(default_factory=dict)
    # Execution period of the project (start, end); either end may be None. None = no period known,
    # so DATE_OUT_OF_PERIOD does not apply.
    period: tuple[dt.date | None, dt.date | None] | None = None


# --- small helpers --------------------------------------------------------------------------


def _money(value: Decimal | None, currency: str | None = None) -> str:
    return f"{fmt_amount(value)} {currency}".strip() if value is not None else "importe desconocido"


def _date(value: dt.date | None) -> str:
    return value.strftime("%d/%m/%Y") if value else "sin fecha"


def _issuer_of(seg: Seg) -> str:
    """' de <emisor>' when the issuer looks like a name (OCR often returns codes)."""
    name = (seg.emisor or "").strip()
    return f" de {name}" if sum(c.isalpha() for c in name) >= 4 else ""


def _types(params: dict[str, Any], key: str = "document_types") -> set[str]:
    return set(params.get(key) or [])


def _finding(
    rule: RuleIn,
    case: Case,
    message: str,
    *,
    seg: Seg | None = None,
    severity: Severity | None = None,
    **evidence: Any,
) -> FindingIn:
    evidence["referencia"] = rule.reference
    evidence["order_no"] = case.expense.order_no
    if seg is not None:
        evidence.setdefault("segment_id", seg.id)
        evidence.setdefault("pagina_inicio", seg.pagina_inicio)
        evidence.setdefault("pagina_fin", seg.pagina_fin)
    return FindingIn(
        expense_id=case.expense.id or None,
        document_id=seg.document_id if seg is not None else None,
        rule_code=rule.code,
        severity=severity or rule.severity,
        message=message,
        evidence=evidence,
    )


def _within(a: Decimal, b: Decimal, pct: float, absolute: float) -> bool:
    return abs(a - b) <= max(Decimal(str(absolute)), abs(b) * Decimal(str(pct)))


def unreliable_reading(exp: ExpenseFacts, seg: Seg, params: dict[str, Any]) -> str | None:
    """Why a value read from `seg` is probably an OCR misreading rather than a real discrepancy
    (None = no reason to doubt it): the document is linked to the expense with high/medium
    confidence, or the OCR confidence is low. Thresholds come from the rule's params."""
    floor = params.get("soft_below_ocr_confidence", 70)
    trusted = set(params.get("soft_link_confidences", ["high", "medium"]))
    if exp.link_confidence in trusted:
        return "el documento está asignado con confianza alta/media"
    if seg.confianza_ocr is not None and seg.confianza_ocr < floor:
        return f"la lectura del documento es poco fiable ({seg.confianza_ocr:.0f} %)"
    return None


def soften(message: str, why: str) -> str:
    """Turn a finding message into a "check the reading" one (used with `unreliable_reading`)."""
    return (
        f"Revisar la lectura: {message[0].lower()}{message[1:]} Probablemente sea un error de "
        f"lectura del OCR, porque {why}; compruébalo en el PDF."
    )


def payment_method(expense: ExpenseFacts, params: dict[str, Any]) -> str | None:
    """'efectivo' / 'transferencia' / None, from the listing's free-text payment column."""
    text = normalize(str((expense.extra or {}).get("payment") or ""))
    if not text:
        return None
    if any(w in text for w in params.get("cash_words") or []):
        return "efectivo"
    if any(w in text for w in params.get("transfer_words") or ["transferencia"]):
        return "transferencia"
    return None


def payment_date(expense: ExpenseFacts) -> dt.date | None:
    text = str((expense.extra or {}).get("payment") or "")
    return fields.find_date(text) if text else None


def expected_percent(expense: ExpenseFacts) -> float | None:
    """% of the expense imputed to the donor, from the listing (0-100)."""
    extra = expense.extra or {}
    if extra.get("pct_imputed") is not None:
        return float(extra["pct_imputed"]) * 100
    if expense.amount_donor is not None and expense.amount_eur:
        return float(expense.amount_donor / expense.amount_eur) * 100
    return None


# --- document checks -----------------------------------------------------------------------


def _check_amount(ctx: CheckContext, case: Case) -> list[FindingIn]:
    rule = ctx.rules.applies(AMOUNT_MISMATCH, case.expense.partida_code)
    if rule is None:
        return []
    params = rule.params or {}
    pct, absolute = params.get("tolerance_pct", 0.01), params.get("tolerance_abs", 1)
    exp = case.expense
    amounts = exp.amounts
    segs = [s for s in case.linked if s.tipo in _types(params) and s.importe]
    if not amounts or not segs:
        return []

    def agrees(seg: Seg) -> bool:
        wanted = list(amounts.values())
        shared = ctx.expenses_of_segment.get(seg.id, [])
        for label in amounts:  # a document shared by several expenses: compare the sum
            if len(shared) > 1 and all(label in e.amounts for e in shared):
                wanted.append(sum(e.amounts[label] for e in shared))
        figures = [seg.importe, *seg.importes]  # OCR may have picked the wrong "total"
        return any(_within(f, w, pct, absolute) for f in figures for w in wanted)

    if any(agrees(s) for s in segs):
        return []
    seg = min(
        segs,
        key=lambda s: min(abs(s.importe - v) / v for v in amounts.values()),
    )
    label = (
        "eur"
        if seg.moneda == "EUR" and "eur" in amounts
        else ("local" if "local" in amounts else next(iter(amounts)))
    )
    listed = amounts[label]
    unit = {"eur": "EUR", "local": exp.currency or seg.moneda or "", "donor": "EUR"}[label]
    diff = seg.importe - listed
    message = (
        f"El importe del documento ({_money(seg.importe, seg.moneda)}; {seg.where}) no "
        f"coincide con el del gasto {exp.ref} en el listado ({_money(listed, unit)}): "
        f"difiere en {_money(abs(diff), unit)}."
    )
    # Most disagreements on trusted links are OCR misreadings (handwriting, stamps over the
    # total): in Uganda all 37 "contradicted" links were right. Report them as a low-severity
    # "check the reading" instead of an error.
    why = unreliable_reading(exp, seg, params)
    soft = why is not None
    if soft:
        message = (
            f"Revisar la lectura del importe: {message[0].lower()}{message[1:-1]}. Probablemente "
            f"sea un error de lectura del OCR, porque {why}; compruébalo en el PDF."
        )
    return [
        _finding(
            rule,
            case,
            message,
            seg=seg,
            severity=Severity.LOW if soft else None,
            importe_documento=float(seg.importe),
            importe_listado=float(listed),
            diferencia=float(diff),
            moneda_listado=unit,
            lectura_dudosa=soft,
        )
    ]


def _check_date(ctx: CheckContext, case: Case) -> list[FindingIn]:
    rule = ctx.rules.applies(DATE_MISMATCH, case.expense.partida_code)
    if rule is None:
        return []
    params = rule.params or {}
    exp = case.expense
    out: list[FindingIn] = []
    tolerance = params.get("tolerance_days", 3)
    dated = [s for s in case.linked if s.tipo in _types(params) and s.fecha]
    if exp.date and dated and all(abs((s.fecha - exp.date).days) > tolerance for s in dated):
        seg = min(dated, key=lambda s: abs((s.fecha - exp.date).days))
        days = abs((seg.fecha - exp.date).days)
        message = (
            f"La fecha del documento ({_date(seg.fecha)}; {seg.where}) no coincide con la del "
            f"gasto {exp.ref} en el listado ({_date(exp.date)}): {days} días de diferencia."
        )
        why = unreliable_reading(exp, seg, params)
        out.append(
            _finding(
                rule,
                case,
                soften(message, why) if why else message,
                seg=seg,
                severity=Severity.LOW if why else None,
                lectura_dudosa=bool(why),
                fecha_documento=seg.fecha.isoformat(),
                fecha_listado=exp.date.isoformat(),
                dias=days,
            )
        )
    paid = payment_date(exp) if params.get("check_payment_date") else None
    proofs = [s for s in case.bundle if s.tipo == "transferencia" and s.fecha]
    pay_tol = params.get("payment_tolerance_days", 5)
    if paid and proofs and all(abs((s.fecha - paid).days) > pay_tol for s in proofs):
        seg = min(proofs, key=lambda s: abs((s.fecha - paid).days))
        days = abs((seg.fecha - paid).days)
        message = (
            f"El listado dice que el gasto {exp.ref} se pagó el {_date(paid)}, pero el "
            f"justificante bancario ({seg.where}) es del {_date(seg.fecha)}: {days} días de "
            "diferencia."
        )
        why = unreliable_reading(exp, seg, params)
        out.append(
            _finding(
                rule,
                case,
                soften(message, why) if why else message,
                seg=seg,
                severity=Severity.LOW if why else None,
                lectura_dudosa=bool(why),
                fecha_pago_listado=paid.isoformat(),
                fecha_justificante_pago=seg.fecha.isoformat(),
                dias=days,
            )
        )
    return out


def project_period(project: Any) -> tuple[dt.date | None, dt.date | None] | None:
    """The project's execution period from `start_date` / `end_date` (None when it has neither;
    the columns may not exist yet). Datetimes are reduced to dates."""
    values = []
    for name in ("start_date", "end_date"):
        value = getattr(project, name, None)
        values.append(value.date() if isinstance(value, dt.datetime) else value)
    return (values[0], values[1]) if any(values) else None


def _outside(day: dt.date, period: tuple[dt.date | None, dt.date | None], grace: int) -> str | None:
    """'antes' / 'despues' when `day` falls outside the period (± `grace` days), else None."""
    start, end = period
    if start and day < start - dt.timedelta(days=grace):
        return "antes"
    if end and day > end + dt.timedelta(days=grace):
        return "despues"
    return None


def _period_text(period: tuple[dt.date | None, dt.date | None]) -> str:
    start, end = period
    if start and end:
        return f"{_date(start)} - {_date(end)}"
    return f"desde el {_date(start)}" if start else f"hasta el {_date(end)}"


def _check_period(ctx: CheckContext, case: Case) -> list[FindingIn]:
    """DATE_OUT_OF_PERIOD: document date and payment date inside the project's period."""
    rule = ctx.rules.applies(DATE_OUT_OF_PERIOD, case.expense.partida_code)
    if rule is None or ctx.period is None:
        return []
    params = rule.params or {}
    period, exp = ctx.period, case.expense
    verdict = {"antes": "anterior al inicio", "despues": "posterior al cierre"}
    out: list[FindingIn] = []

    def report(kind: str, day: dt.date, side: str, seg: Seg | None, source: str) -> None:
        why = unreliable_reading(exp, seg, params) if seg is not None else None
        message = (
            f"La fecha {kind} del gasto {exp.ref} ({_date(day)}; {source}) es {verdict[side]} "
            f"del periodo del proyecto ({_period_text(period)})."
        )
        if why:
            message = soften(message, why)
        out.append(
            _finding(
                rule,
                case,
                message,
                seg=seg,
                severity=Severity.LOW if why else None,
                fecha=day.isoformat(),
                tipo_fecha=kind,
                posicion=side,
                periodo_inicio=period[0].isoformat() if period[0] else None,
                periodo_fin=period[1].isoformat() if period[1] else None,
                lectura_dudosa=bool(why),
            )
        )

    grace = params.get("document_grace_days", 0)
    dated = [s for s in case.linked if s.tipo in _types(params) and s.fecha]
    outside = [(s, _outside(s.fecha, period, grace)) for s in dated]
    if dated and all(side for _, side in outside):
        seg, side = outside[0]
        report("del documento", seg.fecha, side or "", seg, seg.where)

    if not params.get("check_payment_date", True):
        return out
    pay_grace = params.get("payment_grace_days", 0)
    paid = payment_date(exp)
    if paid and (side := _outside(paid, period, pay_grace)):
        report("de pago", paid, side, None, "según el listado")
    else:
        proofs = [s for s in case.bundle if s.tipo == "transferencia" and s.fecha]
        late = [(s, _outside(s.fecha, period, pay_grace)) for s in proofs]
        if proofs and all(side for _, side in late):
            seg, side = late[0]
            report("de pago", seg.fecha, side or "", seg, f"justificante bancario, {seg.where}")
    return out


def _check_stamp(ctx: CheckContext, case: Case) -> list[FindingIn]:
    rule = ctx.rules.applies(DONOR_STAMP_MISSING, case.expense.partida_code)
    if rule is None:
        return []
    params = rule.params or {}
    exp = case.expense
    if params.get("skip_cofinanced", True) and exp.cofinanced:
        return []
    types = _types(params)
    docs = [s for s in case.linked if s.tipo in types] or [
        s for s in case.bundle if s.tipo in types
    ]
    if not docs:
        return []
    stamped = [s for s in docs if s.sello_donante]
    elsewhere = False
    if not stamped:
        # local partners often stamp the payment voucher, not the invoice: look in the whole file
        stamped = [s for s in case.bundle if s.sello_donante and s not in docs]
        elsewhere = bool(stamped)
    if not stamped:
        names = ", ".join(s.where for s in docs[:3])
        message = (
            f"No se ha detectado el sello del donante en el justificante del gasto {exp.ref} "
            f"({names}). Si el sello está pero no se lee bien, descarta la incidencia."
        )
        why = unreliable_reading(exp, docs[0], params)
        return [
            _finding(
                rule,
                case,
                soften(message, why) if why else message,
                seg=docs[0],
                severity=Severity.LOW if why else None,
                segmentos=[s.id for s in docs],
                lectura_dudosa=bool(why),
            )
        ]
    seg = max(stamped, key=lambda s: (bool(s.sello_expediente), s.sello_porcentaje is not None))
    required = set(params.get("required_fields") or [])
    issues: list[str] = []
    if elsewhere:
        issues.append(
            f"el sello figura en otro documento del mismo PDF ({seg.label}) pero no en la propia "
            f"{docs[0].label}"
        )
    if "expediente" in required and not seg.sello_expediente:
        issues.append("no se lee el nº de expediente (o el código del proyecto)")
    expected = expected_percent(exp)
    mode = params.get("percentage", "always")  # always | when_partial | never
    need_pct = mode == "always" or (
        mode == "when_partial" and expected is not None and expected < FULL_PERCENT
    )
    if need_pct and seg.sello_porcentaje is None:
        issues.append("no se lee el porcentaje de imputación")
    elif seg.sello_porcentaje is not None and expected is not None:
        tol = params.get("percentage_tolerance_points", 1.0)
        if abs(seg.sello_porcentaje - expected) > tol:
            issues.append(
                f"indica un {seg.sello_porcentaje:g} % pero el listado imputa un {expected:.4g} %"
            )
    if not issues:
        return []
    only_elsewhere = elsewhere and len(issues) == 1
    message = (
        f"El sello del donante del gasto {exp.ref} ({seg.where}) no es correcto: "
        + "; ".join(issues)
        + "."
    )
    why = unreliable_reading(exp, seg, params)
    return [
        _finding(
            rule,
            case,
            soften(message, why) if why else message,
            seg=seg,
            severity=Severity.LOW if only_elsewhere or why else None,
            lectura_dudosa=bool(why),
            sello_expediente=seg.sello_expediente,
            sello_porcentaje=seg.sello_porcentaje,
            porcentaje_listado=expected,
        )
    ]


def _check_not_invoice(ctx: CheckContext, case: Case) -> list[FindingIn]:
    rule = ctx.rules.applies(NOT_AN_INVOICE, case.expense.partida_code)
    if rule is None:
        return []
    params = rule.params or {}
    exp = case.expense
    invoice_types = set(params.get("invoice_types") or ["factura"])
    receipt_types = set(params.get("receipt_types") or ["recibo"])
    out: list[FindingIn] = []

    proformas = [
        s
        for s in case.linked
        if s.tipo in invoice_types | receipt_types and s.extra.get("proforma")
    ]
    if params.get("flag_proforma", True) and proformas:
        seg = proformas[0]
        out.append(
            _finding(
                rule,
                case,
                f"El documento del gasto {exp.ref} ({seg.where}) parece una proforma o un "
                "presupuesto, no una factura: no acredita el gasto.",
                seg=seg,
                proforma=True,
            )
        )

    invoices = [s for s in case.linked if s.tipo in invoice_types]
    if not invoices:
        in_bundle = any(s.tipo in invoice_types for s in case.bundle)
        receipts = [s for s in case.linked if s.tipo in receipt_types]
        if in_bundle:
            return out
        if receipts and params.get("flag_receipts", True):
            seg = receipts[0]
            out.append(
                _finding(
                    rule,
                    case,
                    f"El justificante del gasto {exp.ref} ({seg.where}) es un recibo, no una "
                    "factura. Solo vale con autorización previa del donante o si la normativa "
                    "local exime de facturar.",
                    seg=seg,
                    tipo=seg.tipo,
                )
            )
        elif not receipts and not any(s.tipo in receipt_types for s in case.bundle):
            kinds = sorted({s.label for s in case.linked})
            out.append(
                _finding(
                    rule,
                    case,
                    f"Los documentos vinculados al gasto {exp.ref} no incluyen ninguna factura "
                    f"(solo: {', '.join(kinds)}).",
                    seg=case.linked[0],
                    tipos=kinds,
                )
            )
        return out

    wanted = params.get("required_fields") or []
    if wanted:
        best = max(invoices, key=lambda s: sum(bool(getattr(s, f)) for f in wanted))
        missing = [f for f in wanted if not getattr(best, f)]
        if missing:
            names = ", ".join(FIELD_LABELS_ES.get(f, f) for f in missing)
            severity = (
                Severity(params["missing_fields_severity"])
                if params.get("missing_fields_severity")
                else None
            )
            out.append(
                _finding(
                    rule,
                    case,
                    f"En la factura del gasto {exp.ref} ({best.where}) no se identifica {names}. "
                    "Puede ser un fallo de lectura: revisa el original.",
                    seg=best,
                    severity=severity,
                    campos_que_faltan=missing,
                )
            )
    return out


def _check_payment(ctx: CheckContext, case: Case) -> list[FindingIn]:
    rule = ctx.rules.applies(PAYMENT_EVIDENCE_MISSING, case.expense.partida_code)
    if rule is None:
        return []
    params = rule.params or {}
    exp = case.expense
    if exp.partida_code in set(params.get("skip_partidas") or []):
        return []
    method = payment_method(exp, params)
    proof_types = set(params.get("proof_types") or ["transferencia"])
    bank_proofs = [s for s in case.bundle if s.tipo in proof_types]
    paid_stamps = [s for s in case.bundle if s.tipo in INVOICE_LIKE and s.prueba_pago]
    ref_seg = (case.linked or case.bundle)[0]

    message: str | None = None
    evidence: dict[str, Any] = {}
    if method == "efectivo":
        if params.get("cash_requires_paid_stamp", True) and not paid_stamps:
            message = (
                f"El gasto {exp.ref} se pagó en efectivo, pero la factura no lleva sello de "
                "«pagado» ni firma del proveedor que lo acredite."
            )
            evidence = {"forma_pago": "efectivo"}
    elif method == "transferencia":
        if not bank_proofs:
            message = (
                f"El listado dice que el gasto {exp.ref} se pagó por transferencia, pero no "
                "hay justificante bancario en el expediente."
            )
            evidence = {"forma_pago": "transferencia"}
    elif not (bank_proofs or (params.get("accept_paid_stamp", True) and paid_stamps)):
        message = (
            f"No se ha encontrado la prueba de pago del gasto {exp.ref}: ni justificante "
            "bancario ni sello de «pagado»."
        )
    if message is None:
        return []
    # The proof may sit on a page the OCR could not read: any doubtful document of the file counts.
    why = next(
        (w for seg in (ref_seg, *case.bundle) if (w := unreliable_reading(exp, seg, params))), None
    )
    return [
        _finding(
            rule,
            case,
            soften(message, why) if why else message,
            seg=ref_seg,
            severity=Severity.LOW if why else None,
            lectura_dudosa=bool(why),
            **evidence,
        )
    ]


def _check_cash_limit(ctx: CheckContext, case: Case) -> list[FindingIn]:
    rule = ctx.rules.applies(CASH_OVER_LIMIT, case.expense.partida_code)
    if rule is None:
        return []
    params = rule.params or {}
    exp = case.expense
    if payment_method(exp, params) != "efectivo":
        return []
    amount = exp.amount_eur
    limit = Decimal(str(params.get("limit_eur", 1000)))
    if amount is None or (amount < limit if params.get("inclusive", True) else amount <= limit):
        return []
    sign = "o más" if params.get("inclusive", True) else ""
    return [
        _finding(
            rule,
            case,
            f"El gasto {exp.ref} ({_money(amount, 'EUR')}) se pagó en efectivo, y los pagos de "
            f"{fmt_amount(limit)} euros {sign}".rstrip()
            + " deben hacerse por transferencia salvo excepción justificada.",
            seg=(case.linked or [None])[0],
            importe_eur=float(amount),
            limite_eur=float(limit),
        )
    ]


def _check_illegible(ctx: CheckContext, case: Case) -> list[FindingIn]:
    rule = ctx.rules.applies(ILLEGIBLE, case.expense.partida_code)
    if rule is None:
        return []
    params = rule.params or {}
    types = _types(params)
    threshold = params.get("min_ocr_confidence", 45)
    out = []
    for seg in case.linked:
        if types and seg.tipo not in types:
            continue
        low = seg.confianza_ocr is not None and seg.confianza_ocr < threshold
        if seg.legible and not low:
            continue
        detail = (
            f" (confianza de lectura {seg.confianza_ocr:.0f} %)"
            if seg.confianza_ocr is not None
            else ""
        )
        out.append(
            _finding(
                rule,
                case,
                f"El documento del gasto {case.expense.ref} ({seg.where}) no se lee bien{detail}: "
                "pide al socio una copia legible.",
                seg=seg,
                confianza_ocr=seg.confianza_ocr,
            )
        )
    return out


def check_documents(ctx: CheckContext) -> list[FindingIn]:
    """Per expense with documents: amount, date, stamp, invoice validity, proof of payment, cash
    limit and legibility. Cash limit also runs on expenses without documents (it is a property of
    the listing)."""
    findings: list[FindingIn] = []
    for case in ctx.cases:
        findings += _check_cash_limit(ctx, case)
        if not case.linked:
            continue
        for check in (
            _check_amount,
            _check_date,
            _check_period,
            _check_stamp,
            _check_not_invoice,
            _check_payment,
            _check_illegible,
        ):
            findings += check(ctx, case)
    return findings


# --- file completeness ---------------------------------------------------------------------------


def _requirement_applies(req: dict[str, Any], exp: ExpenseFacts) -> bool:
    if not partida_matches(exp.partida_code, req.get("applies_to_partidas") or []):
        return False
    keywords = req.get("keywords") or []
    if not keywords:
        return True
    text = normalize(f"{exp.concept or ''} {exp.issuer or ''}")
    return any(re.search(rf"\b{re.escape(normalize(k))}", text) for k in keywords)


def _has(item: dict[str, Any], bundle: list[Seg]) -> bool:
    types, flags = set(item.get("segment_types") or []), item.get("flags") or []
    for seg in bundle:
        if seg.tipo in types:
            return True
        for flag in flags:
            if seg.prueba_pago if flag == "prueba_pago" else seg.extra.get(flag):
                return True
    return False


def check_file_completeness(ctx: CheckContext) -> list[FindingIn]:
    """DOC_INCOMPLETE: the expense's file has the supporting documents its type requires."""
    findings: list[FindingIn] = []
    for case in ctx.cases:
        exp = case.expense
        rule = ctx.rules.applies(DOC_INCOMPLETE, exp.partida_code)
        if rule is None or not case.linked or exp.cofinanced:
            continue
        for req in (rule.params or {}).get("requirements") or []:
            if not _requirement_applies(req, exp):
                continue
            missing = [item for item in req.get("requires") or [] if not _has(item, case.bundle)]
            if not missing:
                continue
            present = sorted({s.label for s in case.bundle})
            findings.append(
                _finding(
                    rule,
                    case,
                    f"Al expediente del gasto {exp.ref} ({req.get('label', req['name'])}) le "
                    f"falta: {', '.join(i['label'] for i in missing)}. "
                    f"En sus documentos solo hay: {', '.join(present) or 'nada'}.",
                    seg=case.linked[0],
                    requisito=req["name"],
                    falta=[i["id"] for i in missing],
                    documentos_presentes=present,
                )
            )
    return findings


# --- duplicates --------------------------------------------------------------------------------


def _issuer_key(seg: Seg) -> str:
    nif = re.sub(r"\D", "", seg.nif or "")
    return f"nif:{nif}" if len(nif) >= 6 else f"emisor:{normalize(seg.emisor or '')}"


def _same_issuer(a: Seg, b: Seg) -> bool:
    ka, kb = _issuer_key(a), _issuer_key(b)
    if ka == kb:
        return not ka.endswith(":")
    if ka.startswith("emisor:") and kb.startswith("emisor:") and len(ka) > 12 and len(kb) > 12:
        return fuzz.token_set_ratio(ka, kb) >= 85
    return False


def _invoice_key(value: str | None, params: dict[str, Any]) -> str | None:
    key = re.sub(r"[^A-Z0-9]", "", (value or "").upper())
    ignore = {
        re.sub(r"[^A-Z0-9]", "", str(v).upper()) for v in params.get("ignore_invoice_numbers") or []
    }
    if len(key) < params.get("min_invoice_chars", 4) or key in ignore:
        return None
    return key


def check_duplicates(ctx: CheckContext) -> list[FindingIn]:
    """DUPLICATE_INVOICE: the same invoice in two documents, charged to several expenses above
    its amount, or repeated in the listing."""
    findings: list[FindingIn] = []
    by_id = {c.expense.id: c for c in ctx.cases}

    # (a) same invoice number + issuer in segments of different PDFs.
    segs: dict[int, Seg] = {}
    owners: dict[int, list[Case]] = defaultdict(list)
    for case in ctx.cases:
        rule = ctx.rules.applies(DUPLICATE_INVOICE, case.expense.partida_code)
        if rule is None:
            continue
        for s in case.linked:
            if s.tipo in ("factura", "recibo"):
                segs[s.id] = s
                owners[s.id].append(case)
    rule_of = lambda c: ctx.rules.applies(DUPLICATE_INVOICE, c.expense.partida_code)  # noqa: E731
    groups: dict[str, list[Seg]] = defaultdict(list)
    for s in segs.values():
        params = ctx.rules.params(DUPLICATE_INVOICE)
        key = _invoice_key(s.numero_factura, params)
        if key:
            groups[key].append(s)
    reported: set[tuple[int, str]] = set()
    for key, group in groups.items():
        docs = {s.doc_key for s in group}
        if len(docs) < 2:
            continue
        # keep only the segments whose issuer matches another document's
        dup = [
            s for s in group if any(o.doc_key != s.doc_key and _same_issuer(s, o) for o in group)
        ]
        if len({s.doc_key for s in dup}) < 2:
            continue
        where = "; ".join(s.where for s in dup[:4])
        for s in dup:
            for case in owners[s.id]:
                if (case.expense.id, key) in reported:
                    continue
                reported.add((case.expense.id, key))
                rule = rule_of(case)
                if rule is None:
                    continue
                findings.append(
                    _finding(
                        rule,
                        case,
                        f"La factura nº {s.numero_factura}{_issuer_of(s)} aparece "
                        f"en {len({x.doc_key for x in dup})} documentos distintos ({where}): "
                        "puede estar imputada dos veces.",
                        seg=s,
                        numero_factura=s.numero_factura,
                        segmentos=[x.id for x in dup],
                    )
                )

    # (b) one invoice charged to several expenses for more than its amount.
    for sid, seg in segs.items():
        linked = ctx.expenses_of_segment.get(sid, [])
        if len(linked) < 2 or not seg.importe:
            continue
        params = ctx.rules.params(DUPLICATE_INVOICE)
        tol = Decimal(str(params.get("amount_tolerance_pct", 0.01)))
        for label in ("local", "eur"):
            if not all(label in e.amounts for e in linked):
                continue
            total = sum(e.amounts[label] for e in linked)
            if total > seg.importe * (1 + tol):
                orders = ", ".join(e.ref for e in linked[:6])
                for case in owners[sid]:
                    rule = rule_of(case)
                    if rule is None:
                        continue
                    findings.append(
                        _finding(
                            rule,
                            case,
                            f"Un mismo documento ({seg.where}, {_money(seg.importe, seg.moneda)}) "
                            f"se imputa a {len(linked)} gastos ({orders}) que suman "
                            f"{fmt_amount(total)}, más de lo que vale: posible doble imputación "
                            "(o el cruce es ambiguo).",
                            seg=seg,
                            gastos=[e.id for e in linked],
                            suma_gastos=float(total),
                        )
                    )
                break

    # (c) the listing itself: same invoice no. + issuer + amount + concept in several lines
    # (the concept tells two payees of one payroll reference apart).
    listing: dict[tuple[str, str, str, Decimal], list[ExpenseFacts]] = defaultdict(list)
    for case in ctx.cases:
        rule = ctx.rules.applies(DUPLICATE_INVOICE, case.expense.partida_code)
        if rule is None or not (rule.params or {}).get("check_listing"):
            continue
        key = _invoice_key(case.expense.invoice_no, rule.params or {})
        amount = case.expense.amount_eur
        if key and amount and case.expense.issuer:
            concept = normalize(case.expense.concept or "")
            listing[(key, normalize(case.expense.issuer), concept, amount)].append(case.expense)
    for (_key, _issuer, _concept, amount), group in listing.items():
        if len(group) < 2:
            continue
        for exp in group:
            case = by_id[exp.id]
            rule = ctx.rules.applies(DUPLICATE_INVOICE, exp.partida_code)
            if rule is None:
                continue
            others = ", ".join(e.ref_row for e in group if e.id != exp.id)
            findings.append(
                _finding(
                    rule,
                    case,
                    f"El gasto {exp.ref_row} repite factura, emisor, concepto e importe "
                    f"({_money(amount, 'EUR')}) "
                    f"con el gasto {others} del listado: posible gasto duplicado.",
                    numero_factura=exp.invoice_no,
                    gastos=[e.id for e in group],
                )
            )
    return findings


# --- database ------------------------------------------------------------------------------------


def _seg_from_row(row: Segmento, document: Document | None) -> Seg:
    extra = dict(row.extra or {})
    return Seg(
        id=row.id,
        doc_key=row.sha256,
        tipo=row.tipo.value,
        document_id=document.id if document else row.document_id,
        filename=document.filename if document else row.archivo,
        emisor=row.emisor,
        nif=row.nif,
        numero_factura=row.numero_factura,
        fecha=row.fecha,
        importe=row.importe,
        importes=tuple(Decimal(x) for x in extra.get("importes", ())),
        moneda=row.moneda,
        sello_donante=row.sello_donante,
        sello_expediente=row.sello_expediente,
        sello_porcentaje=row.sello_porcentaje,
        prueba_pago=row.prueba_pago,
        legible=row.legible,
        confianza_ocr=row.confianza_ocr,
        pagina_inicio=row.pagina_inicio,
        pagina_fin=row.pagina_fin,
        extra=extra,
    )


def build_context(session: Session, project_id: int) -> CheckContext:
    """Expenses, their content links and the segments of the PDFs they belong to."""
    project = session.get(Project, project_id)
    if project is None:
        raise LookupError(f"Project {project_id} not found")
    config = get_match_config(project)
    docs = list(session.scalars(select(Document).where(Document.project_id == project_id)))
    doc_by_sha: dict[str, Document] = {}
    for d in docs:
        doc_by_sha.setdefault(d.sha256, d)

    rows = list(
        session.scalars(
            select(Segmento)
            .where(Segmento.sha256.in_(list(doc_by_sha) or [""]))
            .order_by(Segmento.sha256, Segmento.pagina_inicio)
        )
    )
    backend: dict[str, str] = {}
    for r in rows:  # the vision-LLM backend wins over plain Tesseract
        if r.sha256 not in backend or (
            r.backend.startswith("llm") and not backend[r.sha256].startswith("llm")
        ):
            backend[r.sha256] = r.backend
    segs = {
        r.id: _seg_from_row(r, doc_by_sha.get(r.sha256))
        for r in rows
        if r.backend == backend[r.sha256]
    }
    by_doc: dict[str, list[Seg]] = defaultdict(list)
    for s in segs.values():
        by_doc[s.doc_key].append(s)

    expenses = list(
        session.scalars(
            select(Expense).where(Expense.project_id == project_id).order_by(Expense.source_row)
        )
    )
    facts = {
        e.id: ExpenseFacts(
            id=e.id,
            order_no=e.order_no,
            date=e.date,
            issuer=e.issuer,
            concept=e.concept,
            partida_code=e.partida_code,
            currency=e.currency,
            amount_local=e.amount_local,
            amount_eur=e.amount_eur,
            amount_donor=e.amount_donor,
            extra=dict(e.extra or {}),
            cofinanced=expense_kind(e, config) == KIND_COFINANCED,
            source_row=e.source_row,
        )
        for e in expenses
    }
    linked: dict[int, list[Seg]] = defaultdict(list)
    expenses_of: dict[int, list[ExpenseFacts]] = defaultdict(list)
    for link in session.scalars(
        select(ExpenseSegment).where(ExpenseSegment.expense_id.in_(list(facts) or [0]))
    ):
        seg = segs.get(link.segmento_id)
        if seg is None:
            continue
        linked[link.expense_id].append(seg)
        expenses_of[seg.id].append(facts[link.expense_id])

    rank = {"high": 3, "medium": 2, "low": 1}
    for doc_link in session.scalars(
        select(ExpenseDocument).where(ExpenseDocument.expense_id.in_(list(facts) or [0]))
    ):
        conf = doc_link.confidence.value if doc_link.confidence is not None else None
        exp_facts = facts.get(doc_link.expense_id)
        if (
            exp_facts is not None
            and conf
            and rank.get(conf, 0) > rank.get(exp_facts.link_confidence or "", 0)
        ):
            exp_facts.link_confidence = conf

    cases = []
    for eid, exp in facts.items():
        own = sorted(linked.get(eid, []), key=lambda s: s.id)
        bundle: dict[int, Seg] = {}
        for s in own:
            for sib in by_doc[s.doc_key]:
                bundle[sib.id] = sib
        cases.append(Case(exp, own, sorted(bundle.values(), key=lambda s: s.id)))
    return CheckContext(
        load_rules(project.donor), cases, dict(expenses_of), project_period(project)
    )


def run_checks(session: Session, project_id: int) -> list[FindingIn]:
    """Run every check and persist the findings, replacing the open ones of a previous run and
    keeping the reviewer's decisions (accepted / false positive)."""
    ctx = build_context(session, project_id)
    findings = check_documents(ctx) + check_file_completeness(ctx) + check_duplicates(ctx)
    _sync_findings(session, project_id, set(CHECK_CODES), findings)
    session.commit()
    return findings
