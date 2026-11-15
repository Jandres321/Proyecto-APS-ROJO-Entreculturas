"""Per-expense document checks: amount, date, stamp, invoice validity, proof of payment, cash
limit and legibility."""

from decimal import Decimal
from typing import Any

from app.enums import Severity
from app.pipeline.checks.context import (
    FIELD_LABELS_ES,
    FULL_PERCENT,
    INVOICE_LIKE,
    Case,
    CheckContext,
    Seg,
    _date,
    _finding,
    _money,
    _types,
    _within,
    expected_percent,
    payment_date,
    payment_method,
    soften,
    unreliable_reading,
)
from app.pipeline.checks.period import _check_period
from app.pipeline.match_content import fmt_amount
from app.rules import (
    AMOUNT_MISMATCH,
    CASH_OVER_LIMIT,
    DATE_MISMATCH,
    DONOR_STAMP_MISSING,
    ILLEGIBLE,
    NOT_AN_INVOICE,
    PAYMENT_EVIDENCE_MISSING,
)
from app.schemas import FindingIn


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

    def wanted(seg: Seg) -> list[Decimal]:
        out = list(amounts.values())
        shared = ctx.expenses_of_segment.get(seg.id, [])
        for label in amounts:  # a document shared by several expenses: compare the sum
            if len(shared) > 1 and all(label in e.amounts for e in shared):
                out.append(sum(e.amounts[label] for e in shared))
        return out

    def agrees(seg: Seg) -> bool:
        # Solo cuenta el total del documento. Una cifra cualquiera (precio unitario, subtotal,
        # anticipo...) que coincida con el listado no basta para dar el importe por bueno.
        return any(_within(seg.importe, w, pct, absolute) for w in wanted(seg))

    def other_figure_agrees(seg: Seg) -> bool:
        return any(
            _within(f, w, pct, absolute)
            for f in seg.importes
            if f != seg.importe
            for w in wanted(seg)
        )

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
    other = other_figure_agrees(seg)
    if other:
        message = (
            f"{message[:-1]}. Otra cifra del documento sí coincide con el listado, pero no es el "
            f"total: comprueba qué importe se ha imputado."
        )
    # Solo se rebaja a "revisar la lectura" si la lectura es dudosa (OCR por debajo del umbral
    # o vínculos listados en soft_link_confidences, vacío por defecto).
    why = unreliable_reading(exp, seg, params)
    soft = why is not None
    if soft:
        message = (
            f"Revisar la lectura del importe: {message[0].lower()}{message[1:-1]}. La lectura es "
            f"dudosa porque {why}; compruébalo en el PDF."
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
            otra_cifra_coincide=other,
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


MIN_EXPEDIENTE_CHARS = 4


def _code_key(code: str | None) -> str:
    """Código de expediente comparable: sin espacios, guiones ni barras, en minúsculas."""
    return "".join(ch for ch in str(code or "") if ch not in " -/\t").lower()


def _same_expediente(stamp: str | None, project: str | None) -> bool | None:
    """True/False si se pueden comparar; None si falta alguno de los dos códigos."""
    a, b = _code_key(stamp), _code_key(project)
    # Too short to compare (a misread "1" would be contained in almost any code): no verdict.
    if len(a) < MIN_EXPEDIENTE_CHARS or len(b) < MIN_EXPEDIENTE_CHARS:
        return None
    return a in b or b in a


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
    out: list[FindingIn] = []
    if _same_expediente(seg.sello_expediente, ctx.project_code) is False:
        out.append(
            _finding(
                rule,
                case,
                f"El sello es de otro expediente ({seg.sello_expediente}); el proyecto es "
                f"{ctx.project_code}. Gasto {exp.ref} ({seg.where}).",
                seg=seg,
                severity=Severity.HIGH,
                subject="sello_expediente",
                sello_expediente=seg.sello_expediente,
                expediente_proyecto=ctx.project_code,
            )
        )
    if not issues:
        return out
    only_elsewhere = elsewhere and len(issues) == 1
    message = (
        f"El sello del donante del gasto {exp.ref} ({seg.where}) no es correcto: "
        + "; ".join(issues)
        + "."
    )
    why = unreliable_reading(exp, seg, params)
    out.append(
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
    )
    return out


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
    limit and legibility. Cash limit and the execution period also run on expenses without
    documents (the listing alone is enough for them)."""
    findings: list[FindingIn] = []
    for case in ctx.cases:
        findings += _check_cash_limit(ctx, case)
        # El periodo se comprueba también sin documentos: la fecha del listado basta.
        findings += _check_period(ctx, case)
        if not case.linked:
            continue
        for check in (
            _check_amount,
            _check_date,
            _check_stamp,
            _check_not_invoice,
            _check_payment,
            _check_illegible,
        ):
            findings += check(ctx, case)
    return findings
