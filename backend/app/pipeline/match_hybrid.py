"""Hybrid matching: the name-based "conciliador" PROPOSES, the content VERIFIES.

Why hybrid: on the pilot project the names alone get 161/181 expenses right, but they know
nothing about what the PDF says; the content alone is exact when the OCR reads well and silent
when it does not. Together:

1. The conciliador (`app/pipeline/conciliador.py`) proposes one document per expense, with a
   confidence (HIGH / MEDIUM / LOW when two different documents are almost equally good).
2. HIGH / MEDIUM proposals are ACCEPTED. A proposal is marked *verified by content* when amount
   AND (issuer OR date) agree, or the invoice number does (see `is_verified`). If the document
   shows amounts and none is the expense's the link is flagged `contradicted` but kept: on the
   pilot all such cases were right (OCR noise), so only strong evidence for ANOTHER expense
   (point 3) takes a document away.
3. The content REASSIGNS a document only with strong evidence (amount + issuer, or invoice
   number) for ANOTHER expense, and only when it gives no support to the proposed one.
4. LOW (ambiguous) proposals are decided by the content when exactly one of the tied candidates
   is verified by it; otherwise the conciliador's best guess stays, with LOW confidence.
5. An expense nobody proposed gets a document only from strong content evidence.
6. ``Conflict`` (-> NAME_CONTENT_CONFLICT) exists ONLY when the content strongly points to
   another expense. A document whose OCR reads nothing is never a conflict.

`hybrid_match` is pure (no database); `match_by_segments` in match_content.py persists it.
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.enums import Confidence, MatchMethod, Severity
from app.models import Document, ExpenseDocument, ExpenseSegment, Project
from app.pipeline.conciliador import NameCandidate, NameProposal, NameReconciler
from app.pipeline.match import _sync_findings, match_name
from app.pipeline.match_config import KIND_NORMAL, MatchConfig, get_match_config
from app.pipeline.match_content import (
    ACCEPT,
    CONTENT_RULES,
    MIN_AMOUNT,
    RULE_DOC_MISSING,
    RULE_MATCH_AMBIGUOUS,
    RULE_NAME_CONTENT_CONFLICT,
    RULE_ORDER_NUMBER_MISMATCH,
    RULE_SEGMENT_ORPHAN,
    TIE,
    ContentMatchSummary,
    ExpenseView,
    Link,
    MatchResult,
    PairScore,
    SegmentView,
    best_amount,
    compute_name_claims,
    fmt_amount,
    load_expense_views,
    load_segments,
    match_segments,
)
from app.schemas import FindingIn

INVOICE_LIKE = {"factura", "recibo", "liquidacion"}

# Minimum quality (0-1) of a signal to count it as "agreeing".
AMOUNT_OK = 0.7  # the document's TOTAL equals the expense's amount (within 1 %)
ISSUER_OK = 0.6
DATE_OK = 0.6  # within 15 days
INVOICE_OK = 0.8  # invoice number equal or contained


class Origin(StrEnum):
    NAME = "name"  # proposed by the file name (conciliador)
    CONTENT = "content"  # found by the content of the document


@dataclass(frozen=True)
class HybridOptions:
    """Switches to measure each piece of the hybrid on its own (all on by default)."""

    reassign: bool = True  # strong content for another expense takes the document away
    disambiguate_low: bool = True  # the content chooses among tied (LOW) name candidates
    content_fill: bool = True  # strong content assigns expenses nobody proposed


@dataclass
class HybridLink:
    expense_id: int
    doc_key: str
    origin: Origin
    confidence: Confidence
    verified: bool  # the content confirms it (amount + issuer/date, or invoice number)
    reason: str  # plain Spanish, for the reviewer
    score: float  # name score (origin NAME) or content score (origin CONTENT)
    segment_ids: tuple[int, ...] = ()  # segments to link (the checks run on them)
    primary: bool = True  # False: an extra document (e.g. a separate proof of payment)
    contradicted: bool = False  # the document shows amounts and none is the expense's


@dataclass
class Conflict:
    """The name/number says `expense_id`, but the content strongly says other expenses."""

    expense_id: int
    doc_key: str
    other_expense_ids: list[int]
    reassigned: bool  # the document was taken away from `expense_id`


@dataclass
class HybridResult:
    links: list[HybridLink] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    content: MatchResult = field(default_factory=MatchResult)
    proposals: dict[int, NameProposal] = field(default_factory=dict)

    def primary_doc(self, expense_id: int) -> str | None:
        return next(
            (lk.doc_key for lk in self.links if lk.expense_id == expense_id and lk.primary), None
        )

    def links_of(self, expense_id: int) -> list[HybridLink]:
        return [lk for lk in self.links if lk.expense_id == expense_id]


# --- what counts as evidence --------------------------------------------------------------------


def is_verified(ps: PairScore) -> bool:
    """The content confirms the pair: the document's total equals the expense's amount AND the
    issuer or the date agree; or the invoice number agrees together with any other of the three."""
    amount = ps.amount_is_total and ps.amount_q >= AMOUNT_OK
    issuer = ps.issuer_q >= ISSUER_OK
    date = ps.date_q >= DATE_OK
    invoice = ps.invoice_q >= INVOICE_OK
    return (amount and (issuer or date)) or (invoice and (amount or issuer or date))


def is_strong(ps: PairScore) -> bool:
    """Evidence strong enough to take a document to ANOTHER expense: total + issuer, or the
    invoice number."""
    amount = ps.amount_is_total and ps.amount_q >= AMOUNT_OK
    return (amount and ps.issuer_q >= ISSUER_OK) or ps.invoice_q >= INVOICE_OK


def amounts_incompatible(expense: ExpenseView, segments: Iterable[SegmentView]) -> bool:
    """The document shows amounts (on invoice-like pages that were read) and NONE is the
    expense's. A document with no readable amount is never incompatible. Informative: the OCR
    often misreads totals, so this never vetoes an assignment."""
    shown = False
    for seg in segments:
        if not seg.legible or seg.tipo not in INVOICE_LIKE:
            continue
        figures = [v for v in (seg.importe, *seg.importes) if v and v >= MIN_AMOUNT]
        if not figures:
            continue
        shown = True
        if best_amount(seg, expense)[0] > 0:
            return False
    return shown


# --- helpers --------------------------------------------------------------------------------------


@dataclass
class _Support:
    ps: PairScore
    segment_id: int


def _verified_text(seg: SegmentView | None, ps: PairScore) -> str:
    """Why the content confirms the pair, in Spanish."""
    bits = []
    if ps.amount_is_total and ps.amount_q >= AMOUNT_OK and seg is not None:
        bits.append(
            f"el importe del documento ({fmt_amount(seg.importe)}) coincide con el del gasto"
        )
    if ps.invoice_q >= INVOICE_OK:
        bits.append("el nº de factura coincide")
    if ps.issuer_q >= ISSUER_OK:
        bits.append("el emisor coincide")
    if ps.date_q >= DATE_OK:
        bits.append("la fecha coincide")
    return "; ".join(bits)


class _Context:
    def __init__(
        self,
        expenses: Sequence[ExpenseView],
        segments: Sequence[SegmentView],
        content: MatchResult,
    ):
        self.expenses = {e.id: e for e in expenses}
        self.segments = {s.id: s for s in segments}
        self.content = content
        self.by_doc: dict[str, list[SegmentView]] = defaultdict(list)
        for s in segments:
            self.by_doc[s.doc_key].append(s)
        # Best pair (expense, document) over the document's segments.
        self.support: dict[tuple[int, str], _Support] = {}
        # document -> {expense: best content score} for STRONG pairs the engine would accept.
        self.strong: dict[str, dict[int, float]] = defaultdict(dict)
        for (eid, sid), ps in content.scores.items():
            doc = self.segments[sid].doc_key
            key = (eid, doc)
            if key not in self.support or ps.score > self.support[key].ps.score:
                self.support[key] = _Support(ps, sid)
            if ps.score >= ACCEPT and is_strong(ps):
                self.strong[doc][eid] = max(ps.score, self.strong[doc].get(eid, 0.0))

    def supported(self, eid: int, doc: str) -> bool:
        sup = self.support.get((eid, doc))
        return sup is not None and sup.ps.score >= ACCEPT

    def verified(self, eid: int, doc: str) -> bool:
        sup = self.support.get((eid, doc))
        return sup is not None and is_verified(sup.ps)

    def other_strong(self, eid: int, doc: str) -> list[int]:
        return sorted(e for e in self.strong.get(doc, {}) if e != eid)

    def segments_to_link(self, eid: int, doc: str) -> tuple[int, ...]:
        """Segments of the document that back the expense: the ones the content supports, else
        the invoice-like ones, so that the document checks have something to run on."""
        backed = [
            s.id
            for s in self.by_doc.get(doc, [])
            if (eid, s.id) in self.content.scores
            and self.content.scores[(eid, s.id)].score >= ACCEPT
        ]
        if backed:
            return tuple(backed)
        return tuple(s.id for s in self.by_doc.get(doc, []) if s.tipo in INVOICE_LIKE)


def _name_link(
    ctx: _Context, eid: int, cand: NameCandidate, confidence: Confidence, ambiguous_note: str = ""
) -> HybridLink:
    doc = cand.doc.doc_key
    sup = ctx.support.get((eid, doc))
    verified = ctx.verified(eid, doc)
    contradicted = (
        not ctx.supported(eid, doc)
        and bool(ctx.by_doc.get(doc))
        and amounts_incompatible(ctx.expenses[eid], ctx.by_doc[doc])
    )
    reason = f"Propuesto por el nombre del archivo (puntuación {cand.score:.2f}). "
    reason += " ".join(cand.reasons)
    if verified and sup is not None:
        seg = ctx.segments[sup.segment_id]
        reason += f" Verificado por contenido: {_verified_text(seg, sup.ps)}."
        if confidence == Confidence.LOW:
            confidence = Confidence.MEDIUM
    elif contradicted:
        # Informative only: on the pilot every one of these was a CORRECT assignment (the OCR
        # misreads totals), so it never lowers the confidence; the document checks report the
        # amount (AMOUNT_MISMATCH) for the reviewer.
        reason += (
            " Ojo: el importe leído en el documento no coincide con el del gasto (la lectura "
            "automática puede fallar): revisar."
        )
    elif not ctx.by_doc.get(doc):
        reason += " No se ha podido leer el contenido del documento para verificarlo."
    else:
        reason += " El contenido no contradice la propuesta, pero tampoco la confirma."
    return HybridLink(
        expense_id=eid,
        doc_key=doc,
        origin=Origin.NAME,
        confidence=confidence,
        verified=verified,
        reason=(reason + ambiguous_note).strip(),
        score=cand.score,
        segment_ids=ctx.segments_to_link(eid, doc),
        contradicted=contradicted,
    )


def _content_link(
    ctx: _Context, eid: int, doc: str, score: float, *, primary: bool, why: str
) -> HybridLink:
    sup = ctx.support[(eid, doc)]
    seg = ctx.segments[sup.segment_id]
    confidence = Confidence.HIGH if score >= 0.80 else Confidence.MEDIUM
    return HybridLink(
        expense_id=eid,
        doc_key=doc,
        origin=Origin.CONTENT,
        confidence=confidence,
        verified=True,
        reason=f"{why} {_verified_text(seg, sup.ps).capitalize()}.".strip(),
        score=round(score, 3),
        segment_ids=ctx.segments_to_link(eid, doc),
        primary=primary,
    )


def _engine_extras(result: MatchResult, ctx: _Context) -> dict[int, list[Link]]:
    """Content links of the engine that are strong, unambiguous and not LOW confidence."""
    out: dict[int, list[Link]] = defaultdict(list)
    for lk in result.links:
        ps = result.scores.get((lk.expense_id, lk.segment_id))
        if ps and not lk.ambiguous and lk.confidence != Confidence.LOW and is_strong(ps):
            out[lk.expense_id].append(lk)
    return out


# --- the hybrid ---------------------------------------------------------------------------------


def hybrid_match(
    expenses: Sequence[ExpenseView],
    segments: Sequence[SegmentView],
    names: dict[str, list[str]],
    reconciler: NameReconciler | None,
    *,
    options: HybridOptions | None = None,
    name_claims: Iterable[tuple[int, str]] = (),
) -> HybridResult:
    """Match expenses and documents.

    `segments` must carry no file name (content only). `names`: doc_key -> file names of that
    document (every supported file, also those without segments). `reconciler` None = content
    only. `name_claims`: {(expense, doc_key)} that the file's ORDER NUMBER states; never used to
    assign, only to report conflicts when the content strongly disagrees.
    """
    options = options or HybridOptions()
    content = match_segments(expenses, segments, use_name=False)
    ctx = _Context(expenses, segments, content)
    proposals = reconciler.propose(expenses, names) if reconciler else {}
    result = HybridResult(content=content, proposals=proposals)
    taken: set[tuple[int, str]] = set()  # (expense, doc) already linked

    def add(link: HybridLink) -> None:
        if (link.expense_id, link.doc_key) not in taken:
            taken.add((link.expense_id, link.doc_key))
            result.links.append(link)

    # Pass 1: the names propose. HIGH / MEDIUM are accepted (unless the content takes the
    # document away); LOW (tied) are settled by the content when it can.
    reassign_targets: dict[str, list[int]] = defaultdict(list)  # doc -> expenses it goes to
    for e in expenses:
        prop = proposals.get(e.id)
        if prop is None or not prop.assigned or prop.best is None:
            continue
        if prop.confidence in (Confidence.HIGH, Confidence.MEDIUM):
            doc = prop.best.doc.doc_key
            others = ctx.other_strong(e.id, doc)
            lost = bool(
                options.reassign
                and others
                and not ctx.supported(e.id, doc)
                and amounts_incompatible(e, ctx.by_doc.get(doc, []))
            )
            if lost:
                result.conflicts.append(Conflict(e.id, doc, others, reassigned=True))
                reassign_targets[doc] += others
            else:
                add(_name_link(ctx, e.id, prop.best, prop.confidence))
        else:
            pick = _choose_among_tied(ctx, e.id, prop) if options.disambiguate_low else None
            cand, confidence, note = pick or (prop.best, Confidence.LOW, "")
            add(_name_link(ctx, e.id, cand, confidence, note))

    # Pass 2: a document taken away from an expense goes to the expenses the content points at.
    for doc, targets in reassign_targets.items():
        for eid in sorted(set(targets)):
            if (eid, doc) not in taken and ctx.supported(eid, doc):
                add(
                    _content_link(
                        ctx,
                        eid,
                        doc,
                        ctx.support[(eid, doc)].ps.score,
                        primary=result.primary_doc(eid) is None,
                        why="Reasignado por contenido: el nombre lo asociaba a otro gasto.",
                    )
                )

    if options.content_fill:
        # Pass 3: expenses still without document (nobody proposed them, or the content took
        # their document away): only solid content assigns them.
        solid = _solid_engine_links(ctx)
        for e in expenses:
            if result.primary_doc(e.id) is not None:
                continue
            best = _best_content_doc(ctx, e.id, solid)
            if best is not None:
                doc, score = best
                add(
                    _content_link(
                        ctx,
                        e.id,
                        doc,
                        score,
                        primary=True,
                        why="Asignado por contenido (nadie lo proponía por el nombre).",
                    )
                )
        # Pass 4: extra documents of an expense that already has one (e.g. a separate proof of
        # payment found by content).
        for eid, links in _engine_extras(content, ctx).items():
            if result.primary_doc(eid) is None:
                continue
            for lk in links:
                doc = ctx.segments[lk.segment_id].doc_key
                if (eid, doc) not in taken and ctx.supported(eid, doc):
                    add(
                        _content_link(
                            ctx,
                            eid,
                            doc,
                            lk.score,
                            primary=False,
                            why="Documento adicional encontrado por contenido.",
                        )
                    )

    # Pass 5: order-number claims the content strongly contradicts: reported, never assigned.
    reported = {(c.expense_id, c.doc_key) for c in result.conflicts}
    for eid, doc in name_claims:
        if (eid, doc) in reported or (eid, doc) in taken or eid not in ctx.expenses:
            continue
        others = ctx.other_strong(eid, doc)
        if others and not ctx.supported(eid, doc):
            reported.add((eid, doc))
            result.conflicts.append(Conflict(eid, doc, others, reassigned=False))
    return result


def _choose_among_tied(
    ctx: _Context, eid: int, prop: NameProposal
) -> tuple[NameCandidate, Confidence, str] | None:
    """LOW proposal: the content picks the candidate it verifies, if exactly one."""
    verified = [c for c in prop.tied if ctx.verified(eid, c.doc.doc_key)]
    if len(verified) == 1:
        return verified[0], Confidence.MEDIUM, " El contenido ha desempatado entre candidatos."
    if len(verified) > 1:
        ranked = sorted(verified, key=lambda c: -ctx.support[(eid, c.doc.doc_key)].ps.score)
        gap = (
            ctx.support[(eid, ranked[0].doc.doc_key)].ps.score
            - ctx.support[(eid, ranked[1].doc.doc_key)].ps.score
        )
        if gap > TIE:
            return ranked[0], Confidence.MEDIUM, " El contenido ha desempatado entre candidatos."
    return None


def _best_content_doc(
    ctx: _Context, eid: int, solid: dict[int, dict[str, float]]
) -> tuple[str, float] | None:
    """The document the content backs for an expense nobody proposed: strong evidence, or a
    MEDIUM+ unambiguous link of the content engine. None when two documents are equally good."""
    scored: dict[str, float] = {}
    for doc, by_exp in ctx.strong.items():
        if eid in by_exp:
            scored[doc] = max(by_exp[eid], scored.get(doc, 0.0))
    for doc, score in solid.get(eid, {}).items():
        scored[doc] = max(score, scored.get(doc, 0.0))
    ranked = sorted(((score, doc) for doc, score in scored.items()), reverse=True)
    if not ranked:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < TIE:
        return None  # two documents equally good: nothing to choose by content
    return ranked[0][1], ranked[0][0]


def _solid_engine_links(ctx: _Context) -> dict[int, dict[str, float]]:
    """expense -> {document: score} for engine links that are unambiguous and not LOW."""
    out: dict[int, dict[str, float]] = defaultdict(dict)
    for lk in ctx.content.links:
        if lk.ambiguous or lk.confidence == Confidence.LOW or lk.group:
            continue
        doc = ctx.segments[lk.segment_id].doc_key
        out[lk.expense_id][doc] = max(lk.score, out[lk.expense_id].get(doc, 0.0))
    return out


# --- findings ---


def _doc_label(names: dict[str, list[str]], doc_key: str) -> str:
    paths = names.get(doc_key) or []
    return paths[0].rsplit("/", 1)[-1] if paths else "el documento"


def build_hybrid_findings(
    result: HybridResult,
    expenses: Sequence[ExpenseView],
    segments: Sequence[SegmentView],
    config: MatchConfig,
    names: dict[str, list[str]],
    doc_ids: dict[str, int | None],
) -> tuple[list[FindingIn], int]:
    """DOC_MISSING, NAME_CONTENT_CONFLICT, SEGMENT_ORPHAN, MATCH_AMBIGUOUS and
    ORDER_NUMBER_MISMATCH of a hybrid result, and the number of expenses without document that
    are "not included in the sample" (informative, no finding).

    - DOC_MISSING only when the folder is complete (``carpeta_completa``, default True): in a
      sample (Paraguay) an expense without document is simply not in the sample.
    - NAME_CONTENT_CONFLICT only when the content strongly points to ANOTHER expense.
    - A segment is an orphan only when its document backs no expense at all (the other pages of
      a linked PDF are part of that expense's file).
    """
    exp_by_id = {e.id: e for e in expenses}
    seg_by_id = {s.id: s for s in segments}
    complete = config.get("carpeta_completa", True)
    findings: list[FindingIn] = []
    not_in_sample = 0
    linked_expenses = {lk.expense_id for lk in result.links}
    linked_docs = {lk.doc_key for lk in result.links}

    # DOC_MISSING / "not in the sample"
    for exp in expenses:
        if exp.id in linked_expenses or exp.kind != KIND_NORMAL:
            continue
        if not complete:
            not_in_sample += 1
            continue
        label = f"nº {exp.order_no}" if exp.order_no else "sin nº de orden"
        evidence: dict[str, Any] = {"order_no": exp.order_no, "partida": exp.partida_code}
        amounts = ", ".join(f"{fmt_amount(v)} ({k})" for k, v in exp.amounts.items())
        message = (
            f"No se ha encontrado ningún documento para el gasto {label} "
            f"({(exp.concept or 'sin concepto')[:80]}; {amounts or 'sin importe'}): ni el nombre "
            "de los archivos ni su contenido lo respaldan."
        )
        best = result.content.best_for_expense.get(exp.id)
        if best:
            seg = seg_by_id[best[1]]
            message += (
                f" El candidato más cercano por contenido (puntuación {best[0]:.2f}, {seg.pages}) "
                "no llega al mínimo para asignarlo."
            )
            evidence.update(
                best_score=round(best[0], 3), best_segment=seg.id, best_document_id=seg.document_id
            )
        findings.append(
            FindingIn(
                expense_id=exp.id or None,
                rule_code=RULE_DOC_MISSING,
                severity=Severity.HIGH,
                message=message,
                evidence=evidence,
            )
        )

    # NAME_CONTENT_CONFLICT: the content strongly says ANOTHER expense.
    for c in result.conflicts:
        exp = exp_by_id[c.expense_id]
        where = ", ".join(f"nº {exp_by_id[o].order_no or 's/n'}" for o in c.other_expense_ids[:4])
        message = (
            f"«{_doc_label(names, c.doc_key)}» se asocia al gasto nº {exp.order_no} por su "
            f"nombre, pero por su contenido (importe con emisor o nº de factura) corresponde al "
            f"gasto {where}."
        )
        if c.reassigned:
            message += " Se ha reasignado a ese gasto."
        findings.append(
            FindingIn(
                expense_id=exp.id or None,
                document_id=doc_ids.get(c.doc_key),
                rule_code=RULE_NAME_CONTENT_CONFLICT,
                severity=Severity.MEDIUM,
                message=message,
                evidence={
                    "order_no": exp.order_no,
                    "content_matches": c.other_expense_ids,
                    "reassigned": c.reassigned,
                },
            )
        )

    # SEGMENT_ORPHAN: a document that backs no expense.
    for seg in segments:
        if seg.doc_key in linked_docs or seg.tipo == "otro":
            continue
        has_data = seg.importe or seg.numero_factura or seg.numero_orden
        severity = (
            Severity.LOW
            if not has_data or seg.tipo in {"contrato", "lista_asistencia"}
            else Severity.MEDIUM
        )
        details = [f"tipo {seg.tipo}", seg.pages]
        if seg.importe:
            details.append(f"importe {fmt_amount(seg.importe)} {seg.moneda or ''}".rstrip())
        if seg.numero_factura:
            details.append(f"factura {seg.numero_factura}")
        best = result.content.best_for_segment.get(seg.id)
        findings.append(
            FindingIn(
                document_id=seg.document_id,
                rule_code=RULE_SEGMENT_ORPHAN,
                severity=severity,
                message=(
                    f"Un documento ({', '.join(details)}) no corresponde a ningún gasto del "
                    "listado."
                ),
                evidence={
                    "segment_id": seg.id,
                    "tipo": seg.tipo,
                    "pagina_inicio": seg.pagina_inicio,
                    "pagina_fin": seg.pagina_fin,
                    "importe": float(seg.importe) if seg.importe else None,
                    "best_score": round(best[0], 3) if best else None,
                },
            )
        )

    # MATCH_AMBIGUOUS: names that fit several documents equally and the content did not settle.
    for lk in result.links:
        prop = result.proposals.get(lk.expense_id)
        ambiguous_name = lk.origin == Origin.NAME and prop is not None and prop.ambiguous
        if not (ambiguous_name and lk.confidence == Confidence.LOW):
            continue
        exp = exp_by_id[lk.expense_id]
        rivals = ", ".join(
            f"«{_doc_label(names, c.doc.doc_key)}»"
            for c in prop.tied
            if c.doc.doc_key != lk.doc_key
        )
        findings.append(
            FindingIn(
                expense_id=exp.id or None,
                document_id=doc_ids.get(lk.doc_key),
                rule_code=RULE_MATCH_AMBIGUOUS,
                severity=Severity.MEDIUM,
                message=(
                    f"El gasto nº {exp.order_no or 's/n'} encaja por nombre casi igual con varios "
                    f"documentos ({rivals or 'otros'}) y el contenido no permite elegir: se ha "
                    f"propuesto «{_doc_label(names, lk.doc_key)}»; revisar cuál vale."
                ),
                evidence={
                    "kind": "name",
                    "expense_ids": [exp.id],
                    "documents": [c.doc.doc_key for c in prop.tied],
                },
            )
        )
    content_pairs = {
        (lk.expense_id, lk.doc_key) for lk in result.links if lk.origin == Origin.CONTENT
    }
    for amb in result.content.ambiguities:
        docs = {seg_by_id[s].doc_key for s in amb.segment_ids}
        if not any((e, d) in content_pairs for e in amb.expense_ids for d in docs):
            continue
        seg = seg_by_id[amb.segment_ids[0]]
        orders = ", ".join(f"nº {exp_by_id[e].order_no or 's/n'}" for e in amb.expense_ids[:6])
        findings.append(
            FindingIn(
                expense_id=amb.expense_ids[0] or None,
                document_id=seg.document_id,
                rule_code=RULE_MATCH_AMBIGUOUS,
                severity=Severity.MEDIUM,
                message=(
                    f"El documento ({seg.tipo}, {seg.pages}) encaja igual de bien por contenido "
                    f"con varios gastos ({orders}); no hay nada que permita elegir uno."
                ),
                evidence={
                    "kind": amb.kind,
                    "segment_ids": amb.segment_ids,
                    "expense_ids": amb.expense_ids,
                    "score": round(amb.score, 3),
                },
            )
        )

    # ORDER_NUMBER_MISMATCH: the number written on the document is not the expense's.
    for lk in result.links:
        for sid in lk.segment_ids:
            ps = result.content.scores.get((lk.expense_id, sid))
            if ps is None or not ps.order_conflict:
                continue
            exp, seg = exp_by_id[lk.expense_id], seg_by_id[sid]
            findings.append(
                FindingIn(
                    expense_id=exp.id or None,
                    document_id=seg.document_id,
                    rule_code=RULE_ORDER_NUMBER_MISMATCH,
                    severity=Severity.MEDIUM,
                    message=(
                        f"El documento ({seg.pages}) lleva escrito el nº de orden "
                        f"{seg.numero_orden}, pero el gasto al que corresponde es el nº "
                        f"{exp.order_no}."
                    ),
                    evidence={
                        "written_order": seg.numero_orden,
                        "expense_order": exp.order_no,
                        "segment_id": seg.id,
                    },
                )
            )
    return findings, not_in_sample


# --- database ---


def match_project(
    session: Session, project_id: int, *, use_name: bool = True
) -> ContentMatchSummary:
    """Hybrid matching of a project; persists links and findings.

    Rebuilds the project's `expense_segment` rows and its filename/content `expense_document`
    links (manual links are kept), then replaces the OPEN findings of the five matching rules
    (reviewer decisions survive). `use_name=False` ignores file names completely (content only).
    """
    project = session.get(Project, project_id)
    if project is None:
        raise LookupError(f"Project {project_id} not found")
    config = get_match_config(project)
    expenses = load_expense_views(session, project_id, config)
    segments, raw_names = load_segments(session, project_id, with_names=use_name)
    content_segments = [SegmentView(**{**s.__dict__, "filename": None}) for s in segments]
    docs = {
        d.sha256: d
        for d in session.scalars(select(Document).where(Document.project_id == project_id))
    }
    doc_ids: dict[str, int | None] = {key: d.id for key, d in docs.items()}
    reconciler = (
        NameReconciler(config["name_reconciler"])
        if use_name and "name_reconciler" in config
        else None
    )
    names = {k: sorted({match_name(p, config) for p in v}) for k, v in raw_names.items()}
    claims = compute_name_claims(expenses, raw_names, config) if use_name else set()
    result = hybrid_match(expenses, content_segments, names, reconciler, name_claims=claims)

    expense_ids = [e.id for e in expenses] or [0]
    session.execute(delete(ExpenseSegment).where(ExpenseSegment.expense_id.in_(expense_ids)))
    session.execute(
        delete(ExpenseDocument).where(
            ExpenseDocument.expense_id.in_(expense_ids),
            ExpenseDocument.method.in_([MatchMethod.FILENAME, MatchMethod.CONTENT]),
        )
    )
    manual = {
        (ed.expense_id, ed.document_id)
        for ed in session.scalars(
            select(ExpenseDocument).where(
                ExpenseDocument.expense_id.in_(expense_ids),
                ExpenseDocument.method == MatchMethod.MANUAL,
            )
        )
    }
    summary = ContentMatchSummary()
    segment_links: set[tuple[int, int]] = set()
    for lk in result.links:
        document_id = doc_ids.get(lk.doc_key)
        if document_id is not None and (lk.expense_id, document_id) not in manual:
            session.add(
                ExpenseDocument(
                    expense_id=lk.expense_id,
                    document_id=document_id,
                    method=MatchMethod.FILENAME
                    if lk.origin == Origin.NAME
                    else MatchMethod.CONTENT,
                    score=lk.score,
                    confidence=lk.confidence,
                    verified=lk.verified,
                    reason=lk.reason,
                )
            )
        for sid in lk.segment_ids:
            if (lk.expense_id, sid) in segment_links:
                continue
            segment_links.add((lk.expense_id, sid))
            ps = result.content.scores.get((lk.expense_id, sid))
            session.add(
                ExpenseSegment(
                    expense_id=lk.expense_id,
                    segmento_id=sid,
                    score=ps.score if ps else lk.score,
                    confidence=lk.confidence,
                    reason=lk.reason,
                    evidence={
                        "origin": lk.origin.value,
                        "verified": lk.verified,
                        "contradicted": lk.contradicted,
                        "primary": lk.primary,
                        "parts": ps.parts if ps else None,
                    },
                )
            )
        if lk.primary:
            level = lk.confidence.value
            summary.by_confidence[level] = summary.by_confidence.get(level, 0) + 1
            summary.verified += int(lk.verified)
            if lk.origin == Origin.NAME:
                summary.from_name += 1
            else:
                summary.from_content += 1

    findings, summary.not_in_sample = build_hybrid_findings(
        result, expenses, content_segments, config, raw_names, doc_ids
    )
    _sync_findings(session, project_id, CONTENT_RULES, findings)
    session.commit()

    summary.links = len(result.links)
    summary.expenses_linked = len({lk.expense_id for lk in result.links})
    summary.segments_linked = len(segment_links)
    summary.conflicts = len(result.conflicts)
    for f in findings:
        summary.findings[f.rule_code] = summary.findings.get(f.rule_code, 0) + 1
    return summary
