"""Inputs of the checks: segments, expenses, cases, the shared helpers and the DB loader."""

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import Severity
from app.lectura import fields
from app.lectura.classify import normalize
from app.models import Document, Expense, ExpenseDocument, ExpenseSegment, Project, Segmento
from app.pipeline.match_config import KIND_COFINANCED, expense_kind, get_match_config
from app.pipeline.match_content import fmt_amount
from app.rules import (
    RuleSet,
    load_rules,
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


def project_period(project: Any) -> tuple[dt.date | None, dt.date | None] | None:
    """The project's execution period from `start_date` / `end_date` (None when it has neither;
    the columns may not exist yet). Datetimes are reduced to dates."""
    values = []
    for name in ("start_date", "end_date"):
        value = getattr(project, name, None)
        values.append(value.date() if isinstance(value, dt.datetime) else value)
    return (values[0], values[1]) if any(values) else None


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
