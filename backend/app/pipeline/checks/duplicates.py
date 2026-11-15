"""DUPLICATE_INVOICE: the same invoice twice, over-charged, or repeated in the listing."""

import re
from collections import defaultdict
from decimal import Decimal
from typing import Any

from rapidfuzz import fuzz

from app.lectura.classify import normalize
from app.pipeline.checks.context import (
    Case,
    CheckContext,
    ExpenseFacts,
    Seg,
    _finding,
    _issuer_of,
    _money,
)
from app.pipeline.match_content import fmt_amount
from app.rules import DUPLICATE_INVOICE
from app.schemas import FindingIn


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
