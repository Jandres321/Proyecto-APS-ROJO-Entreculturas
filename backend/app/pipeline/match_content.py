"""Step 2 — Match by content: score every expense against every document segment.

Replaces the matching by file name. File names only contribute a weak signal (and only when
`use_name=True`); the evidence is what the PDFs actually say (see app/lectura/).

Score of an (expense, segment) pair = weighted sum of independent signals, in [0, 1]:

=========  ======  ===========================================================================
signal     weight  rule
=========  ======  ===========================================================================
importe     0.45   segment total equals the expense's local / EUR / donor amount (±0.05 %: full;
                   ±1 %: 70 %); +0.08 when that amount is unique in the expense list.
                   An amount that merely APPEARS among the document's figures counts 80 %
                   (48 % if other documents show it too): it never links on its own
factura     0.35   invoice number equal (full), contained (80 %) or same digits (70 %)
emisor      0.15   issuer fuzzy-equal (>=85: full, >=70: 60 %) or the NIF appears in the expense
fecha       0.12   ±3 days full, ±15 days 60 %, ±45 days 30 %
orden       0.12   order number written / stamped on the document equals the expense's
débil       0.06   concept words vs issuer; with names: the file name rule / concept ~ file name
=========  ======  ===========================================================================

A pair needs an anchor (amount, invoice number or order number) and a score >= ACCEPT to link.
The assignment is N:M: a segment can back several expenses (ties, or a total that is the sum of
several expenses) and an expense can be backed by several segments (invoice + payment proof).
Every link carries a confidence (alta / media / baja) and a plain-Spanish reason.
"""

import itertools
import re
import unicodedata
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Any

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import Confidence
from app.models import (
    Document,
    Expense,
    Finding,
    Segmento,
)
from app.pipeline.match import (
    _expenses_for,
    canonical_key,
    match_name,
    parse_filename,
)
from app.pipeline.match_config import (
    KIND_LABELS_ES,
    KIND_NORMAL,
    MatchConfig,
    expense_kind,
)

RULE_DOC_MISSING = "DOC_MISSING"
RULE_NAME_CONTENT_CONFLICT = "NAME_CONTENT_CONFLICT"
RULE_SEGMENT_ORPHAN = "SEGMENT_ORPHAN"
RULE_MATCH_AMBIGUOUS = "MATCH_AMBIGUOUS"
RULE_ORDER_NUMBER_MISMATCH = "ORDER_NUMBER_MISMATCH"
CONTENT_RULES = {
    RULE_DOC_MISSING,
    RULE_NAME_CONTENT_CONFLICT,
    RULE_SEGMENT_ORPHAN,
    RULE_MATCH_AMBIGUOUS,
    RULE_ORDER_NUMBER_MISMATCH,
}

WEIGHTS = {
    "importe": 0.45,
    "factura": 0.35,
    "emisor": 0.15,
    "fecha": 0.12,
    "orden": 0.12,
    "debil": 0.06,
}
UNIQUE_AMOUNT_BONUS = 0.08
SECONDARY_AMOUNT_FACTOR = 0.8  # amount found among the document's figures, not as its total
SECONDARY_SHARED_FACTOR = 0.6  # ... and also found in other documents
ACCEPT = 0.50  # minimum score to link
HIGH = 0.80
MEDIUM = 0.62
TIE = 0.04  # scores this close are indistinguishable
MIN_AMOUNT = Decimal(5)  # smaller "amounts" are OCR noise
GROUP_MAX_POOL = 16
GROUP_MAX_SIZE = 6
GROUP_DATE_DAYS = 45

AMOUNT_LABELS_ES = {
    "local": "el importe en moneda local",
    "eur": "el importe en euros",
    "donor": "el importe imputado al donante",
}
_LEGAL = re.compile(
    r"\b(?:sa|s a|srl|s r l|sl|s l|eirl|ltd|limited|sarl|inc|co|company|u|uganda|paraguay|"
    r"the|de|del|la|el|y|and|en)\b"
)


# --- views ---------------------------------------------------------------------------------


@dataclass
class ExpenseView:
    id: int
    order_no: str | None = None
    order_key: str | None = None
    date: Any = None  # datetime.date
    issuer: str | None = None
    concept: str | None = None
    amounts: dict[str, Decimal] = field(default_factory=dict)  # local / eur / donor
    invoice_no: str | None = None
    partida_code: str | None = None
    kind: str = KIND_NORMAL
    text_blob: str = ""  # issuer + concept + invoice: where a NIF may appear


@dataclass
class SegmentView:
    id: int
    doc_key: str  # sha256 of the PDF
    document_id: int | None = None
    filename: str | None = None  # only used when names are allowed
    tipo: str = "otro"
    emisor: str | None = None
    nif: str | None = None
    numero_factura: str | None = None
    fecha: Any = None
    importe: Decimal | None = None
    # Every other figure read in the segment (OCR picks the wrong 'total' often).
    importes: tuple[Decimal, ...] = ()
    moneda: str | None = None
    numero_orden: str | None = None
    legible: bool = True
    pagina_inicio: int = 1
    pagina_fin: int = 1

    @property
    def pages(self) -> str:
        if self.pagina_inicio == self.pagina_fin:
            return f"pág. {self.pagina_inicio}"
        return f"págs. {self.pagina_inicio}-{self.pagina_fin}"


def expense_view(expense: Any, config: MatchConfig) -> ExpenseView:
    """ExpenseView from an ORM `Expense` or an `ExpenseIn` (anything with the same fields)."""
    extra = getattr(expense, "extra", None) or {}
    amounts = {}
    for label, attr in (
        ("local", "amount_local"),
        ("eur", "amount_eur"),
        ("donor", "amount_donor"),
    ):
        value = getattr(expense, attr, None)
        if value is not None and value > 0:
            amounts[label] = Decimal(value)
    invoice = extra.get("invoice_no")
    blob = " ".join(
        str(x) for x in (expense.issuer, expense.concept, invoice, extra.get("payment")) if x
    )
    return ExpenseView(
        id=getattr(expense, "id", 0),
        order_no=expense.order_no,
        order_key=canonical_key(expense.order_key),
        date=expense.date,
        issuer=expense.issuer,
        concept=expense.concept,
        amounts=amounts,
        invoice_no=str(invoice) if invoice else None,
        partida_code=expense.partida_code,
        kind=expense_kind(expense, config),
        text_blob=blob,
    )


# --- signals -------------------------------------------------------------------------------


def _plain(text: str | None) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKD", text.casefold())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


def _name_tokens(text: str | None) -> set[str]:
    return {t for t in _LEGAL.sub(" ", _plain(text)).split() if len(t) >= 4}


def amount_quality(a: Decimal, b: Decimal) -> float:
    diff = abs(a - b)
    if diff <= max(Decimal(1), b * Decimal("0.0005")):
        return 1.0
    return 0.7 if diff <= b * Decimal("0.01") else 0.0


AmountHit = tuple[float, str | None, bool]  # (quality, which expense amount, is the total)


def best_amount(segment: SegmentView, expense: ExpenseView) -> AmountHit:
    """Best amount evidence of a pair. `primary` is True when the segment's TOTAL matches;
    otherwise an exact match with any other figure of the segment counts at 80 %."""
    best: AmountHit = (0.0, None, True)
    if segment.importe is not None and segment.importe >= MIN_AMOUNT:
        for label, value in expense.amounts.items():
            q = amount_quality(segment.importe, value)
            if q > best[0]:
                best = (q, label, True)
    if best[0] == 1.0:
        return best
    for other in segment.importes:
        if other < MIN_AMOUNT:
            continue
        for label, value in expense.amounts.items():
            if amount_quality(other, value) == 1.0 and best[0] < SECONDARY_AMOUNT_FACTOR:
                best = (SECONDARY_AMOUNT_FACTOR, label, False)
    return best


def _alnum(text: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def invoice_quality(segment_no: str | None, expense_no: str | None) -> float:
    a, b = _alnum(segment_no), _alnum(expense_no)
    if len(a) < 3 or len(b) < 3 or a in {"NA", "N/A"} or b in {"NA", "N/A"}:
        return 0.0
    if a == b:
        return 1.0
    if min(len(a), len(b)) >= 5 and (a in b or b in a):
        return 0.8
    da, db = re.sub(r"\D", "", a), re.sub(r"\D", "", b)
    return 0.7 if len(da) >= 4 and da == db else 0.0


def issuer_quality(segment: SegmentView, expense: ExpenseView) -> tuple[float, str]:
    """(quality, kind) with kind in {"nif", "emisor", ""}."""
    nif = re.sub(r"\D", "", segment.nif or "")
    if len(nif) >= 6 and nif in re.sub(r"\D", "", expense.text_blob):
        return 1.0, "nif"
    a, b = _plain(segment.emisor), _plain(expense.issuer)
    if len(a) < 4 or len(b) < 4:
        return 0.0, ""
    score = fuzz.token_set_ratio(_LEGAL.sub(" ", a), _LEGAL.sub(" ", b))
    if score >= 85:
        return 1.0, "emisor"
    return (0.6, "emisor") if score >= 70 else (0.0, "")


def date_quality(segment_date: Any, expense_date: Any) -> tuple[float, int | None]:
    if segment_date is None or expense_date is None:
        return 0.0, None
    days = abs((segment_date - expense_date).days)
    if days <= 3:
        return 1.0, days
    if days <= 15:
        return 0.6, days
    return (0.3, days) if days <= GROUP_DATE_DAYS else (0.0, days)


def order_quality(segment_order: str | None, expense_key: str | None) -> float:
    a, b = canonical_key(segment_order), canonical_key(expense_key)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    strip = re.compile(r"^[A-Z]+")
    return 0.7 if strip.sub("", a) == strip.sub("", b) and strip.sub("", a) else 0.0


def has_order_conflict(segment_order: str | None, expense_key: str | None) -> bool:
    return bool(
        canonical_key(segment_order)
        and canonical_key(expense_key)
        and order_quality(segment_order, expense_key) == 0.0
    )


def weak_quality(
    segment: SegmentView, expense: ExpenseView, name_hit: bool, use_name: bool
) -> tuple[float, str]:
    if use_name and name_hit:
        return 1.0, "nombre"
    best = 0.0
    if use_name and segment.filename and expense.concept:
        stem = re.sub(r"\d+", " ", PurePosixPath(segment.filename).stem)
        if fuzz.partial_ratio(_plain(expense.concept), _plain(stem)) >= 75:
            best = 0.5
    ca, cb = (
        _name_tokens(expense.concept) | _name_tokens(expense.issuer),
        _name_tokens(segment.emisor),
    )
    if ca and cb and len(ca & cb) / len(cb) >= 0.5:
        best = max(best, 0.5)
    return best, "concepto" if best else ""


# --- scoring ---------------------------------------------------------------------------------


@dataclass
class PairScore:
    score: float
    parts: dict[str, float]
    facts: list[str]
    amount_label: str | None = None
    order_conflict: bool = False
    # Quality (0-1) of each signal, before weighting: the hybrid matching reads these to decide
    # what is "strong" evidence (see app/pipeline/match_hybrid.py).
    amount_q: float = 0.0
    amount_is_total: bool = True  # False: the amount only appears among the document's figures
    issuer_q: float = 0.0
    date_q: float = 0.0
    invoice_q: float = 0.0
    order_q: float = 0.0


def fmt_amount(value: Decimal | None) -> str:
    if value is None:
        return "?"
    text = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return text[:-3] if text.endswith(",00") else text


def score_pair(
    segment: SegmentView,
    expense: ExpenseView,
    *,
    unique_amount: bool,
    name_hit: bool = False,
    use_name: bool = False,
    amount: AmountHit | None = None,
    secondary_shared: bool = False,
) -> PairScore | None:
    """Score one pair; None when there is no anchor (no amount / invoice / order evidence).

    `amount` can be passed when already computed; `secondary_shared` says the expense's amount
    is also found among the figures of other documents (weaker evidence)."""
    amount_q, amount_label, primary = (
        amount if amount is not None else best_amount(segment, expense)
    )
    if not primary and secondary_shared:
        amount_q *= SECONDARY_SHARED_FACTOR
    invoice_q = invoice_quality(segment.numero_factura, expense.invoice_no)
    order_q = order_quality(segment.numero_orden, expense.order_key)
    if not (amount_q or invoice_q or order_q):
        return None
    issuer_q, issuer_kind = issuer_quality(segment, expense)
    date_q, days = date_quality(segment.fecha, expense.date)
    weak_q, weak_kind = weak_quality(segment, expense, name_hit, use_name)

    parts = {
        "importe": WEIGHTS["importe"] * amount_q
        + (UNIQUE_AMOUNT_BONUS if amount_q == 1.0 and primary and unique_amount else 0.0),
        "factura": WEIGHTS["factura"] * invoice_q,
        "emisor": WEIGHTS["emisor"] * issuer_q,
        "fecha": WEIGHTS["fecha"] * date_q,
        "orden": WEIGHTS["orden"] * order_q,
        "debil": WEIGHTS["debil"] * weak_q,
    }
    facts: list[str] = []
    if amount_q and not primary:
        facts.append(
            f"el importe de {AMOUNT_LABELS_ES[amount_label]} del gasto "
            f"({fmt_amount(expense.amounts[amount_label])}) aparece entre las cifras del "
            "documento (no como su total)"
        )
    elif amount_q:
        how = "coincide" if amount_q == 1.0 else "es casi igual a"
        total = f"{fmt_amount(segment.importe)} {segment.moneda or ''}".strip()
        facts.append(
            f"el importe del documento ({total}) {how} {AMOUNT_LABELS_ES[amount_label]} del gasto"
            + (" y es único en el listado" if amount_q == 1.0 and unique_amount else "")
        )
    if invoice_q:
        facts.append(
            f"el nº de factura «{segment.numero_factura}» "
            + ("coincide" if invoice_q == 1.0 else "se parece")
            + f" con «{expense.invoice_no}» del listado"
        )
    if issuer_q:
        facts.append(
            "el NIF del documento aparece en el gasto"
            if issuer_kind == "nif"
            else f"el emisor «{segment.emisor}» se parece a «{expense.issuer}»"
        )
    if date_q:
        facts.append("la fecha es la misma" if days == 0 else f"la fecha difiere {days} días")
    if order_q:
        facts.append(
            f"el nº de orden escrito en el documento ({segment.numero_orden}) "
            + ("es el del gasto" if order_q == 1.0 else "casi coincide con el del gasto")
        )
    if weak_q:
        facts.append(
            "el nombre del archivo apunta a este gasto"
            if weak_kind == "nombre"
            else "el concepto comparte palabras con el documento"
        )
    return PairScore(
        score=min(1.0, sum(parts.values())),
        parts={k: round(v, 3) for k, v in parts.items()},
        facts=facts,
        amount_label=amount_label,
        order_conflict=has_order_conflict(segment.numero_orden, expense.order_key),
        amount_q=amount_q,
        amount_is_total=primary,
        issuer_q=issuer_q,
        date_q=date_q,
        invoice_q=invoice_q,
        order_q=order_q,
    )


def confidence_for(score: float, ambiguous: bool, group: bool = False) -> Confidence:
    if ambiguous or score < MEDIUM:
        return Confidence.LOW
    if group:
        return Confidence.MEDIUM
    return Confidence.HIGH if score >= HIGH else Confidence.MEDIUM


def _sentence(facts: Sequence[str]) -> str:
    text = "; ".join(facts)
    return text[:1].upper() + text[1:] + "."


# --- assignment ------------------------------------------------------------------------------


@dataclass
class Link:
    expense_id: int
    segment_id: int
    score: float
    confidence: Confidence
    reason: str
    parts: dict[str, float]
    ambiguous: bool = False
    group: bool = False


@dataclass
class Ambiguity:
    kind: str  # "segment": one segment ties between expenses | "expense": rival documents
    segment_ids: list[int]
    expense_ids: list[int]
    score: float


@dataclass
class MatchResult:
    links: list[Link] = field(default_factory=list)
    ambiguities: list[Ambiguity] = field(default_factory=list)
    order_mismatches: list[tuple[int, int]] = field(default_factory=list)  # (expense, segment)
    # best (rejected) candidate of expenses/segments without link: id -> (score, other id)
    best_for_expense: dict[int, tuple[float, int]] = field(default_factory=dict)
    best_for_segment: dict[int, tuple[float, int]] = field(default_factory=dict)
    scores: dict[tuple[int, int], PairScore] = field(default_factory=dict)  # anchored pairs

    def links_of_expense(self, expense_id: int) -> list[Link]:
        return [lk for lk in self.links if lk.expense_id == expense_id]

    def links_of_segment(self, segment_id: int) -> list[Link]:
        return [lk for lk in self.links if lk.segment_id == segment_id]


def _unique_amount_flags(
    segments: Sequence[SegmentView], expenses: Sequence[ExpenseView]
) -> dict[int, bool]:
    """segment id -> its amount matches exactly ONE expense of the list."""
    flags: dict[int, bool] = {}
    for seg in segments:
        if seg.importe is None or seg.importe < MIN_AMOUNT:
            continue
        hits = 0
        for exp in expenses:
            q, _, primary = best_amount(seg, exp)
            if q == 1.0 and primary:
                hits += 1
                if hits > 1:
                    break
        flags[seg.id] = hits == 1
    return flags


def match_segments(
    expenses: Sequence[ExpenseView],
    segments: Sequence[SegmentView],
    *,
    name_hits: set[tuple[int, str]] | None = None,
    use_name: bool = False,
    group_matching: bool = False,
) -> MatchResult:
    """Assign segments to expenses by content. `name_hits` = {(expense id, doc_key)} that the
    file-name rules claim; it is only read when `use_name` is True."""
    name_hits = (name_hits or set()) if use_name else set()
    by_id = {e.id: e for e in expenses}
    seg_by_id = {s.id: s for s in segments}
    unique = _unique_amount_flags(segments, expenses)
    result = MatchResult()

    # Amount evidence of every pair, once; how many documents show each expense's amount
    # among their figures (not as their total) tells whether that evidence is specific.
    amount_hits: dict[tuple[int, int], AmountHit] = {}
    docs_with_amount: dict[int, set[str]] = defaultdict(set)
    for seg in segments:
        for exp in expenses:
            hit = best_amount(seg, exp)
            if hit[0]:
                amount_hits[(seg.id, exp.id)] = hit
                if not hit[2]:
                    docs_with_amount[exp.id].add(seg.doc_key)

    candidates: dict[int, list[tuple[float, int, PairScore]]] = defaultdict(list)
    for seg in segments:
        for exp in expenses:
            ps = score_pair(
                seg,
                exp,
                unique_amount=unique.get(seg.id, False),
                name_hit=(exp.id, seg.doc_key) in name_hits,
                use_name=use_name,
                amount=amount_hits.get((seg.id, exp.id), (0.0, None, True)),
                secondary_shared=len(docs_with_amount.get(exp.id, ())) > 1,
            )
            if ps is None:
                continue
            result.scores[(exp.id, seg.id)] = ps
            candidates[seg.id].append((ps.score, exp.id, ps))

    linked_segments: set[int] = set()
    for seg in segments:
        ranked = sorted(candidates.get(seg.id, []), key=lambda c: -c[0])
        if ranked:
            result.best_for_segment[seg.id] = (ranked[0][0], ranked[0][1])
        accepted = [c for c in ranked if c[0] >= ACCEPT]
        if not accepted:
            continue
        top = accepted[0][0]
        tied = [c for c in accepted if c[0] >= top - TIE]
        ambiguous = len(tied) > 1
        if ambiguous:
            result.ambiguities.append(Ambiguity("segment", [seg.id], [c[1] for c in tied], top))
        for score, eid, ps in tied:
            result.links.append(_make_link(eid, seg, score, ps, ambiguous, by_id[eid]))
        linked_segments.add(seg.id)

    if group_matching:
        _group_links(result, expenses, [s for s in segments if s.id not in linked_segments])
        linked_segments |= {lk.segment_id for lk in result.links}

    _mark_rival_documents(result, seg_by_id)

    linked_expenses = {lk.expense_id for lk in result.links}
    for (eid, sid), ps in result.scores.items():  # best rejected candidate of each unlinked expense
        if eid not in linked_expenses and (
            eid not in result.best_for_expense or ps.score > result.best_for_expense[eid][0]
        ):
            result.best_for_expense[eid] = (ps.score, sid)
    for lk in result.links:
        ps = result.scores.get((lk.expense_id, lk.segment_id))
        if ps and ps.order_conflict:
            result.order_mismatches.append((lk.expense_id, lk.segment_id))
    return result


def _make_link(
    eid: int, seg: SegmentView, score: float, ps: PairScore, ambiguous: bool, exp: ExpenseView
) -> Link:
    reason = _sentence(ps.facts)
    if ambiguous:
        reason += " Otros gastos encajan igual de bien con este documento: revisar a mano."
    if ps.order_conflict:
        reason += (
            f" Ojo: el nº de orden escrito en el documento ({seg.numero_orden}) no coincide con "
            f"el del gasto ({exp.order_no})."
        )
    return Link(
        expense_id=eid,
        segment_id=seg.id,
        score=round(score, 3),
        confidence=confidence_for(score, ambiguous),
        reason=reason,
        parts=ps.parts,
        ambiguous=ambiguous,
    )


def _mark_rival_documents(result: MatchResult, seg_by_id: dict[int, SegmentView]) -> None:
    """An expense backed by two same-type segments of DIFFERENT PDFs with the same score has
    rival documents: lower the confidence of both and report the ambiguity."""
    by_expense: dict[int, list[Link]] = defaultdict(list)
    for lk in result.links:
        by_expense[lk.expense_id].append(lk)
    for eid, links in by_expense.items():
        groups: dict[str, list[Link]] = defaultdict(list)
        for lk in links:
            groups[seg_by_id[lk.segment_id].tipo].append(lk)
        for tipo_links in groups.values():
            if len({seg_by_id[lk.segment_id].doc_key for lk in tipo_links}) < 2:
                continue
            top = max(lk.score for lk in tipo_links)
            rivals = [lk for lk in tipo_links if lk.score >= top - TIE]
            if len({seg_by_id[lk.segment_id].doc_key for lk in rivals}) < 2:
                continue
            for lk in rivals:
                lk.ambiguous = True
                lk.confidence = Confidence.LOW
                if "otro documento" not in lk.reason:
                    lk.reason += " Hay otro documento igual de compatible con este gasto."
            result.ambiguities.append(
                Ambiguity("expense", [lk.segment_id for lk in rivals], [eid], top)
            )


def _group_links(
    result: MatchResult, expenses: Sequence[ExpenseView], segments: Sequence[SegmentView]
) -> None:
    """A segment whose total is the SUM of several still-unlinked expenses (payroll, a payment
    covering several invoices). Only a unique solution is accepted."""
    free = [e for e in expenses if e.kind == KIND_NORMAL]
    taken = {lk.expense_id for lk in result.links}
    for seg in segments:
        if seg.tipo == "otro" or seg.importe is None or seg.importe < MIN_AMOUNT * 10:
            continue
        pool = [e for e in free if e.id not in taken]
        if seg.fecha is not None:
            pool = [
                e
                for e in pool
                if e.date is not None and abs((e.date - seg.fecha).days) <= GROUP_DATE_DAYS
            ]
            pool.sort(key=lambda e: abs((e.date - seg.fecha).days))
        pool = pool[:GROUP_MAX_POOL]
        if len(pool) < 2:
            continue
        solutions: list[tuple[str, tuple[ExpenseView, ...]]] = []
        for label in ("local", "eur", "donor"):
            usable = [e for e in pool if label in e.amounts]
            tol = max(Decimal(1), seg.importe * Decimal("0.0005"))
            for size in range(2, min(GROUP_MAX_SIZE, len(usable)) + 1):
                for combo in itertools.combinations(usable, size):
                    if abs(sum(e.amounts[label] for e in combo) - seg.importe) <= tol:
                        solutions.append((label, combo))
                if len(solutions) > 1:
                    break
            if len(solutions) > 1:
                break
        if len(solutions) != 1:
            continue
        label, combo = solutions[0]
        orders = ", ".join(e.order_no or "s/n" for e in combo)
        for exp in combo:
            issuer_q, _ = issuer_quality(seg, exp)
            date_q, _ = date_quality(seg.fecha, exp.date)
            score = round(MEDIUM + 0.1 * issuer_q + 0.1 * date_q, 3)
            result.links.append(
                Link(
                    expense_id=exp.id,
                    segment_id=seg.id,
                    score=score,
                    confidence=confidence_for(score, False, group=True),
                    reason=(
                        f"El importe del documento ({fmt_amount(seg.importe)} "
                        f"{seg.moneda or ''}".rstrip()
                        + f") es la suma de {len(combo)} gastos del listado (órdenes {orders}): "
                        "un mismo documento justifica varios gastos."
                    ),
                    parts={"grupo": score},
                    group=True,
                )
            )
            taken.add(exp.id)


# --- file-name signal ------------------------------------------------------------------------


def compute_name_claims(
    expenses: Sequence[ExpenseView],
    names: dict[str, list[str]],
    config: MatchConfig,
) -> set[tuple[int, str]]:
    """{(expense id, doc_key)} whose ORDER NUMBER the file name states (project filename rules).

    Never used as evidence for a link, only to detect NAME_CONTENT_CONFLICT.

    `names` maps doc_key -> every path/name of that document (duplicates share a key)."""
    by_key: dict[str, list[ExpenseView]] = defaultdict(list)
    for e in expenses:
        if e.order_key:
            by_key[e.order_key].append(e)
    hits: set[tuple[int, str]] = set()
    for doc_key, paths in names.items():
        for path in paths:
            ref = parse_filename(match_name(path, config), config)
            if ref is None:
                continue
            for exp, _score in _expenses_for(ref, list(expenses), by_key):  # type: ignore[arg-type]
                hits.add((exp.id, doc_key))
    return hits


def kind_label(expense: ExpenseView) -> str:
    return KIND_LABELS_ES[expense.kind]


# --- database ----------------------------------------------------------------------------------


@dataclass
class ContentMatchSummary:
    links: int = 0
    expenses_linked: int = 0
    segments_linked: int = 0
    by_confidence: dict[str, int] = field(default_factory=dict)
    findings: dict[str, int] = field(default_factory=dict)
    # Hybrid matching (primary document of each expense):
    verified: int = 0  # confirmed by the document's content
    from_name: int = 0  # proposed by the file name (conciliador)
    from_content: int = 0  # found by content (nobody proposed it by name)
    not_in_sample: int = 0  # no document, and the folder is only a sample: informative
    conflicts: int = 0  # the content strongly says another expense (NAME_CONTENT_CONFLICT)


def load_expense_views(session: Session, project_id: int, config: MatchConfig) -> list[ExpenseView]:
    rows = session.scalars(
        select(Expense).where(Expense.project_id == project_id).order_by(Expense.source_row)
    )
    return [expense_view(e, config) for e in rows]


def load_segments(
    session: Session, project_id: int, *, with_names: bool = False
) -> tuple[list[SegmentView], dict[str, list[str]]]:
    """Segments of the project's documents (joined by sha256) + every known name per document.

    When a PDF was read with several backends the vision-LLM one (`llm-...`) wins over plain
    Tesseract. Names are only filled when `with_names` is True (content-only runs never see them).
    """
    docs = list(session.scalars(select(Document).where(Document.project_id == project_id)))
    doc_by_sha: dict[str, Document] = {}
    for d in docs:
        doc_by_sha.setdefault(d.sha256, d)
    segs = list(
        session.scalars(
            select(Segmento)
            .where(Segmento.sha256.in_(list(doc_by_sha) or [""]))
            .order_by(Segmento.sha256, Segmento.pagina_inicio)
        )
    )
    backends: dict[str, str] = {}
    for s in segs:
        current = backends.get(s.sha256)
        if current is None or (s.backend.startswith("llm") and not current.startswith("llm")):
            backends[s.sha256] = s.backend
    views = [
        SegmentView(
            id=s.id,
            doc_key=s.sha256,
            document_id=doc_by_sha[s.sha256].id,
            filename=doc_by_sha[s.sha256].filename if with_names else None,
            tipo=s.tipo.value,
            emisor=s.emisor,
            nif=s.nif,
            numero_factura=s.numero_factura,
            fecha=s.fecha,
            importe=s.importe,
            importes=tuple(Decimal(x) for x in (s.extra or {}).get("importes", ())),
            moneda=s.moneda,
            numero_orden=s.numero_orden,
            legible=s.legible,
            pagina_inicio=s.pagina_inicio,
            pagina_fin=s.pagina_fin,
        )
        for s in segs
        if s.backend == backends[s.sha256]
    ]
    names: dict[str, list[str]] = {}
    if with_names:
        for d in docs:
            names.setdefault(d.sha256, []).append(d.path)
        for f in session.scalars(
            select(Finding).where(
                Finding.project_id == project_id, Finding.rule_code == "DOC_DUPLICATE"
            )
        ):
            if f.document_id and f.evidence and f.evidence.get("duplicate_path"):
                owner = next((d for d in docs if d.id == f.document_id), None)
                if owner:
                    names.setdefault(owner.sha256, []).append(f.evidence["duplicate_path"])
    return views, names


def match_by_segments(
    session: Session, project_id: int, *, use_name: bool = True
) -> ContentMatchSummary:
    """Match a project's expenses with its documents (hybrid: the file names propose, the content
    verifies) and persist links and findings. See `app/pipeline/match_hybrid.py`.

    `use_name=False` ignores file names completely (content only)."""
    from app.pipeline.match_hybrid import match_project  # circular import: hybrid builds on this

    return match_project(session, project_id, use_name=use_name)
