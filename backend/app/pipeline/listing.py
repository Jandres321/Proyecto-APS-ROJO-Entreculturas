"""Checks on the expense listing itself: no OCR involved, so they are reliable from day one.

- ``FX_RATE_MISMATCH``: the euro amount does not follow from the local amount and the exchange
  rate of the listing (local / rate).
- ``DONOR_SHARE_MISMATCH``: the amount imputed to the donor is not the euro amount times the
  imputed percentage.
- ``PARTIDA_NOT_IN_BUDGET``: the expense is imputed to a partida that is not in the approved
  budget.
- ``THREE_QUOTES_REQUIRED``: the expense is above the donor's threshold for three offers (the
  adapter's ``scope=expense`` limits); the reviewer must check that they are in the file.

Each finding explains the figures; the tolerances are generous (rounding in the listing is not an
error).
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters import get_adapter
from app.enums import Severity
from app.models import BudgetLine, Expense, Project
from app.rules import partida_matches
from app.schemas import FindingIn, LimitScope
from app.services.findings import sync_findings

FX_RATE_MISMATCH = "FX_RATE_MISMATCH"
DONOR_SHARE_MISMATCH = "DONOR_SHARE_MISMATCH"
PARTIDA_NOT_IN_BUDGET = "PARTIDA_NOT_IN_BUDGET"
THREE_QUOTES_REQUIRED = "THREE_QUOTES_REQUIRED"
LISTING_CODES = {
    FX_RATE_MISMATCH,
    DONOR_SHARE_MISMATCH,
    PARTIDA_NOT_IN_BUDGET,
    THREE_QUOTES_REQUIRED,
}

# Relative tolerance and minimum difference in euros before a figure counts as wrong.
TOLERANCE_PCT = Decimal("0.01")
TOLERANCE_EUR = Decimal("1")
# Limits of scope=expense that are not about three offers (cash has its own rule).
_NOT_QUOTES = ("EFECTIVO", "CASH")


def _eur(value: Decimal) -> str:
    text = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{text} €"


def _differs(actual: Decimal, expected: Decimal) -> bool:
    diff = abs(actual - expected)
    return diff > TOLERANCE_EUR and diff > abs(expected) * TOLERANCE_PCT


def _in_budget(code: str, budget_codes: list[str]) -> bool:
    """A.V.2 is in a budget with A.V, and A.V in a budget broken down into A.V.1, A.V.2..."""
    return partida_matches(code, budget_codes) or any(
        b.startswith(code + ".") for b in budget_codes
    )


def _ref(expense: Expense) -> str:
    return f"nº {expense.order_no}" if expense.order_no else f"de la fila {expense.source_row}"


def check_listing(
    expenses: list[Expense], budget_codes: list[str], quote_limits: list
) -> list[FindingIn]:
    """Pure: the findings for a project's expenses (see the module docstring)."""
    findings: list[FindingIn] = []
    for e in expenses:
        ref = _ref(e)
        if e.amount_local is not None and e.fx_rate and e.amount_eur is not None:
            expected = (e.amount_local / e.fx_rate).quantize(Decimal("0.01"))
            if _differs(e.amount_eur, expected):
                findings.append(
                    FindingIn(
                        expense_id=e.id,
                        rule_code=FX_RATE_MISMATCH,
                        severity=Severity.MEDIUM,
                        message=(
                            f"El gasto {ref} figura con {_eur(e.amount_eur)}, pero "
                            f"{e.amount_local:,.2f} {e.currency or ''} al tipo de cambio "
                            f"{e.fx_rate:.4f} son {_eur(expected)}."
                        ),
                        evidence={
                            "importe_local": str(e.amount_local),
                            "tipo_cambio": str(e.fx_rate),
                            "importe_eur": str(e.amount_eur),
                            "esperado_eur": str(expected),
                        },
                    )
                )
        pct = (e.extra or {}).get("pct_imputed")
        if pct is not None and e.amount_eur is not None and e.amount_donor is not None:
            expected = (e.amount_eur * Decimal(str(pct))).quantize(Decimal("0.01"))
            if _differs(e.amount_donor, expected):
                findings.append(
                    FindingIn(
                        expense_id=e.id,
                        rule_code=DONOR_SHARE_MISMATCH,
                        severity=Severity.MEDIUM,
                        message=(
                            f"El gasto {ref} imputa {_eur(e.amount_donor)} al donante, pero el "
                            f"{float(pct) * 100:.0f} % de {_eur(e.amount_eur)} son "
                            f"{_eur(expected)}."
                        ),
                        evidence={
                            "importe_eur": str(e.amount_eur),
                            "porcentaje": pct,
                            "imputado": str(e.amount_donor),
                            "esperado": str(expected),
                        },
                    )
                )
        if budget_codes and e.partida_code and not _in_budget(e.partida_code, budget_codes):
            findings.append(
                FindingIn(
                    expense_id=e.id,
                    rule_code=PARTIDA_NOT_IN_BUDGET,
                    severity=Severity.HIGH,
                    message=(
                        f"El gasto {ref} está imputado a la partida {e.partida_code}, que no "
                        "figura en el presupuesto aprobado."
                    ),
                    evidence={"partida": e.partida_code},
                )
            )
        for limit in quote_limits:
            if (
                e.amount_eur is not None
                and e.amount_eur > limit.max_amount_eur
                and partida_matches(e.partida_code, limit.partida_codes)
            ):
                findings.append(
                    FindingIn(
                        expense_id=e.id,
                        rule_code=THREE_QUOTES_REQUIRED,
                        severity=Severity.MEDIUM,
                        message=(
                            f"El gasto {ref} es de {_eur(e.amount_eur)}, por encima de "
                            f"{_eur(limit.max_amount_eur)}: {limit.description} Comprueba que "
                            "están en el expediente."
                        ),
                        evidence={"subject": limit.code, "referencia": limit.reference},
                    )
                )
                break
    return findings


def run_listing_checks(session: Session, project_id: int) -> list[FindingIn]:
    """Run the listing checks and persist the findings (keeping the reviewer's decisions)."""
    project = session.get(Project, project_id)
    if project is None:
        raise LookupError(f"Project {project_id} not found")
    expenses = list(session.scalars(select(Expense).where(Expense.project_id == project_id)))
    budget_codes = list(
        session.scalars(
            select(BudgetLine.partida_code).where(
                BudgetLine.project_id == project_id, BudgetLine.is_partida.is_(True)
            )
        )
    )
    quote_limits = [
        limit
        for limit in get_adapter(project.donor).limits()
        if limit.scope == LimitScope.EXPENSE
        and limit.max_amount_eur is not None
        and not any(word in limit.code.upper() for word in _NOT_QUOTES)
    ]
    findings = check_listing(expenses, budget_codes, quote_limits)
    sync_findings(session, project_id, LISTING_CODES, findings)
    session.commit()
    return findings
