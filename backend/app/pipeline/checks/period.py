"""DATE_OUT_OF_PERIOD: document and payment dates inside the project's execution period."""

import datetime as dt

from app.enums import Severity
from app.pipeline.checks.context import (
    Case,
    CheckContext,
    Seg,
    _date,
    _finding,
    _types,
    payment_date,
    soften,
    unreliable_reading,
)
from app.rules import DATE_OUT_OF_PERIOD
from app.schemas import FindingIn


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
