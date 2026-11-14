"""Step 6 — Budget (Process 2a): executed vs approved per partida + donor limits.

Executed amounts are ALWAYS recomputed from the expense list (never trusted from the report):

- *donor* amount = sum of `expense.amount_donor` (what is charged to the donor);
- *total* (project) amount = donor amount + the other funders imputed to the project
  (`expense.extra`: Xunta `other_funders`, AECID `own_funds` / `other_funders`). It is NOT
  `amount_eur`, which is the whole invoice even when most of it is paid outside the project.

Three kinds of finding are produced (all numeric, in code; the LLM is never involved):

- ``LIMIT_EXCEEDED``: a donor cap from the adapter (`DonorAdapter.limits()`, scope ``budget``) is
  exceeded. Percentages are a fraction of the *approved* base (donor grant or project total).
- ``BUDGET_DEVIATION``: executed differs from approved by more than the donor policy tolerates
  (traffic light yellow / red). For AECID a transfer between partidas is an "accidental
  modification": it needs no authorization but must be communicated in the report.
- ``EXECUTED_REPORT_MISMATCH``: the executed figure declared in the report does not match the sum
  of the expense list.

`analyze_budget` is pure (no database) and is what the tests exercise; `compute_execution` and
`run_budget` wrap it with the database.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters import get_adapter
from app.enums import Donor, Severity
from app.models import BudgetLine, Expense, Project
from app.pipeline.match import _sync_findings
from app.rules import partida_matches
from app.schemas import FindingIn, Limit, LimitBase, LimitScope

RULE_LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
RULE_BUDGET_DEVIATION = "BUDGET_DEVIATION"
RULE_EXECUTED_REPORT_MISMATCH = "EXECUTED_REPORT_MISMATCH"
BUDGET_CODES = {RULE_LIMIT_EXCEEDED, RULE_BUDGET_DEVIATION, RULE_EXECUTED_REPORT_MISMATCH}

NO_PARTIDA = "SIN_PARTIDA"  # expenses with no partida still count: they are reported, not lost
CENT = Decimal("0.01")


class Traffic(StrEnum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


TRAFFIC_LABELS_ES = {Traffic.GREEN: "verde", Traffic.YELLOW: "amarillo", Traffic.RED: "rojo"}


@dataclass(frozen=True)
class BudgetPolicy:
    """Per-donor tolerances. NOTE: the thresholds are working assumptions, not donor rules:
    confirm them with Entreculturas."""

    green_max: Decimal  # |deviation| up to this fraction of the approved amount: green
    yellow_max: Decimal  # up to this fraction: yellow; above: red
    report_tolerance: Decimal  # EUR of difference tolerated against the executed figure declared
    deviation_note: str
    deviation_severity: dict[Traffic, Severity]


POLICIES: dict[Donor, BudgetPolicy] = {
    Donor.XUNTA: BudgetPolicy(
        green_max=Decimal("0.05"),
        yellow_max=Decimal("0.10"),
        report_tolerance=Decimal(1),
        deviation_note=(
            "Revisar si la convocatoria permite la desviación y justificarla en el informe."
        ),
        deviation_severity={Traffic.YELLOW: Severity.MEDIUM, Traffic.RED: Severity.HIGH},
    ),
    Donor.AECID: BudgetPolicy(
        green_max=Decimal("0.05"),
        yellow_max=Decimal("0.10"),
        report_tolerance=Decimal(1),
        deviation_note=(
            "Modificación accidental respecto al presupuesto aprobado: no requiere autorización "
            "previa, pero hay que comunicarla y explicarla en el informe."
        ),
        # A deviation is not an error for AECID, only something to communicate.
        deviation_severity={Traffic.YELLOW: Severity.LOW, Traffic.RED: Severity.MEDIUM},
    ),
}


# --- result structures --------------------------------------------------------------------------


@dataclass
class PartidaExecution:
    """One budget partida: approved vs recomputed executed (donor share) and the report figure."""

    code: str
    name: str | None
    approved: Decimal  # approved donor amount
    approved_total: Decimal  # approved amount, every funder
    executed: Decimal = Decimal(0)  # recomputed donor amount
    executed_total: Decimal = Decimal(0)  # recomputed project amount (donor + other funders)
    declared: Decimal | None = None  # executed donor amount declared in the report
    expenses: int = 0
    traffic: Traffic = Traffic.GREEN
    in_budget: bool = True  # False: expenses imputed to a partida the budget does not have

    @property
    def deviation(self) -> Decimal:
        return self.executed - self.approved

    @property
    def deviation_pct(self) -> Decimal | None:
        """Deviation as a fraction of the approved amount (None when nothing was approved)."""
        return self.deviation / self.approved if self.approved else None

    @property
    def report_difference(self) -> Decimal | None:
        return None if self.declared is None else self.executed - self.declared


@dataclass
class LimitCheck:
    limit: Limit
    executed: Decimal  # measured amount of the limit's partidas
    base: Decimal | None  # approved base the percentage refers to
    pct: Decimal | None  # executed / base
    exceeded: bool

    @property
    def max_allowed(self) -> Decimal | None:
        """Tightest ceiling in EUR (percentage of the base and/or fixed amount)."""
        caps = []
        if self.limit.max_pct is not None and self.base:
            caps.append(self.limit.max_pct * self.base)
        if self.limit.max_amount_eur is not None:
            caps.append(self.limit.max_amount_eur)
        return min(caps) if caps else None


@dataclass
class BudgetAnalysis:
    partidas: list[PartidaExecution] = field(default_factory=list)
    limits: list[LimitCheck] = field(default_factory=list)
    findings: list[FindingIn] = field(default_factory=list)


# --- pure analysis ------------------------------------------------------------------------------


def project_amount(amount_donor: Decimal | None, extra: dict[str, Any] | None) -> Decimal:
    """Amount of an expense imputed to the project: donor + other funders of the project.

    `extra` may come straight from an adapter (Decimals) or from the JSONB column (numbers).
    """
    total = Decimal(amount_donor or 0)
    for key in ("other_funders", "own_funds"):
        value = (extra or {}).get(key)
        for item in value.values() if isinstance(value, dict) else [value]:
            if item is not None:
                total += Decimal(str(item))
    return total


def _eur(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return f"{value.quantize(CENT):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _pct(value: Decimal, places: int = 2) -> str:
    """Fraction -> Spanish percentage ("0.1184" -> "11,84 %")."""
    return f"{value * 100:.{places}f} %".replace(".", ",")


def _money(value: Decimal | None) -> str | None:
    """Evidence is stored as JSON: money goes in as a plain decimal string."""
    return None if value is None else str(value.quantize(CENT))


def _traffic_for(partida: PartidaExecution, policy: BudgetPolicy) -> Traffic:
    deviation = partida.deviation
    if abs(deviation) <= policy.report_tolerance:
        return Traffic.GREEN
    pct = partida.deviation_pct
    if pct is None:  # spending on a partida with nothing approved
        return Traffic.RED
    if abs(pct) <= policy.green_max:
        return Traffic.GREEN
    return Traffic.YELLOW if abs(pct) <= policy.yellow_max else Traffic.RED


def _deviation_finding(p: PartidaExecution, policy: BudgetPolicy) -> FindingIn:
    label = f"Partida {p.code}" + (f" ({p.name})" if p.name else "")
    if not p.in_budget:
        what = (
            f"{label}: hay gasto imputado ({_eur(p.executed)} €) pero la partida no figura "
            "en el presupuesto aprobado."
        )
    else:
        direction = "por encima" if p.deviation > 0 else "por debajo"
        pct = f", {_pct(abs(p.deviation_pct), 1)}" if p.deviation_pct is not None else ""
        what = (
            f"{label}: presupuestado {_eur(p.approved)} €, ejecutado {_eur(p.executed)} € "
            f"({_eur(abs(p.deviation))} € {direction}{pct}; semáforo "
            f"{TRAFFIC_LABELS_ES[p.traffic]})."
        )
    return FindingIn(
        rule_code=RULE_BUDGET_DEVIATION,
        severity=policy.deviation_severity[p.traffic],
        message=f"{what} {policy.deviation_note}",
        evidence={
            "subject": p.code,
            "partida": p.code,
            "approved": _money(p.approved),
            "executed": _money(p.executed),
            "deviation": _money(p.deviation),
            "deviation_pct": None if p.deviation_pct is None else float(p.deviation_pct),
            "traffic": p.traffic.value,
        },
    )


def _mismatch_finding(p: PartidaExecution) -> FindingIn:
    assert p.declared is not None and p.report_difference is not None
    label = f"Partida {p.code}" + (f" ({p.name})" if p.name else "")
    return FindingIn(
        rule_code=RULE_EXECUTED_REPORT_MISMATCH,
        severity=Severity.HIGH,
        message=(
            f"{label}: el informe declara {_eur(p.declared)} € ejecutados, pero la suma del "
            f"listado de gastos es {_eur(p.executed)} € (diferencia de "
            f"{_eur(p.report_difference)} €). Hay que corregir el listado o el resumen del "
            "informe."
        ),
        evidence={
            "subject": p.code,
            "partida": p.code,
            "declared": _money(p.declared),
            "recomputed": _money(p.executed),
            "difference": _money(p.report_difference),
        },
    )


def _check_limit(limit: Limit, partidas: Sequence[PartidaExecution]) -> LimitCheck:
    """Compare what the limit's partidas executed with its percentage and/or fixed ceiling.

    The measure follows the base: a cap over the project total uses the project amount (donor +
    other funders); a cap over the donor grant (and fixed-amount caps) uses the donor share.
    """
    total_basis = limit.base == LimitBase.PROJECT_TOTAL
    covered = [p for p in partidas if partida_matches(p.code, limit.partida_codes)]
    executed = sum((p.executed_total if total_basis else p.executed for p in covered), Decimal(0))
    base = sum(
        (p.approved_total if total_basis else p.approved for p in partidas if p.in_budget),
        Decimal(0),
    )
    pct = executed / base if base else None

    exceeded = False
    if limit.max_pct is not None and pct is not None and pct > limit.max_pct:
        exceeded = True
    if limit.max_amount_eur is not None and executed > limit.max_amount_eur:
        exceeded = True
    return LimitCheck(limit, executed, base or None, pct, exceeded)


def _limit_finding(check: LimitCheck) -> FindingIn:
    limit = check.limit
    parts = []
    if limit.max_pct is not None and check.pct is not None:
        base_name = (
            "el presupuesto total del proyecto"
            if limit.base == LimitBase.PROJECT_TOTAL
            else "la subvención aprobada"
        )
        parts.append(
            f"{_pct(check.pct)} de {base_name} ({_eur(check.base)} €), máximo {_pct(limit.max_pct)}"
        )
    if limit.max_amount_eur is not None:
        parts.append(f"máximo {_eur(limit.max_amount_eur)} €")
    return FindingIn(
        rule_code=RULE_LIMIT_EXCEEDED,
        severity=limit.severity,
        message=(
            f"Se supera el tope {limit.code}: {limit.description} "
            f"Ejecutado: {_eur(check.executed)} € ({'; '.join(parts)})."
        ),
        evidence={
            "subject": limit.code,
            "limit_code": limit.code,
            "partidas": limit.partida_codes,
            "executed": _money(check.executed),
            "base": _money(check.base),
            "pct": None if check.pct is None else float(check.pct),
            "max_pct": None if limit.max_pct is None else float(limit.max_pct),
            "max_amount_eur": _money(limit.max_amount_eur),
            "referencia": limit.reference,
        },
    )


def analyze_budget(
    lines: Iterable[Any],
    spends: Iterable[tuple[str | None, Decimal | None, Decimal | None]],
    limits: Iterable[Limit],
    donor: Donor,
) -> BudgetAnalysis:
    """Executed vs approved per partida, donor caps and the findings they raise.

    `lines`: budget lines (anything with the `BudgetLineIn` attributes); subtotal rows
    (`is_partida=False`) are ignored. `spends`: one ``(partida_code, amount_donor,
    amount_project)`` per expense (see `project_amount`).
    """
    policy = POLICIES[donor]
    by_code: dict[str, PartidaExecution] = {}
    for line in lines:
        if not line.is_partida:
            continue
        by_code[line.partida_code] = PartidaExecution(
            code=line.partida_code,
            name=line.partida_name,
            approved=line.approved_donor or Decimal(0),
            approved_total=line.approved_total or Decimal(0),
            declared=line.executed_donor,
        )
    for code, amount_donor, amount_project in spends:
        key = code or NO_PARTIDA
        if key not in by_code:
            by_code[key] = PartidaExecution(
                key, "partida no presupuestada", Decimal(0), Decimal(0), in_budget=False
            )
        row = by_code[key]
        row.executed += amount_donor or Decimal(0)
        row.executed_total += amount_project or Decimal(0)
        row.expenses += 1

    analysis = BudgetAnalysis(partidas=sorted(by_code.values(), key=lambda p: p.code))

    # 1. Traffic light from the deviation alone (a cap or a mismatch can only turn it red later).
    for p in analysis.partidas:
        p.traffic = _traffic_for(p, policy)
        if p.traffic != Traffic.GREEN:
            analysis.findings.append(_deviation_finding(p, policy))

    # 2. Donor caps.
    for limit in limits:
        if limit.scope != LimitScope.BUDGET:
            continue
        check = _check_limit(limit, analysis.partidas)
        analysis.limits.append(check)
        if check.exceeded:
            analysis.findings.append(_limit_finding(check))
            for p in analysis.partidas:
                if partida_matches(p.code, limit.partida_codes):
                    p.traffic = Traffic.RED

    # 3. Executed figure declared in the report vs the recomputed one.
    for p in analysis.partidas:
        diff = p.report_difference
        if diff is not None and p.in_budget and abs(diff) > policy.report_tolerance:
            analysis.findings.append(_mismatch_finding(p))
            p.traffic = Traffic.RED
    return analysis


# --- database -----------------------------------------------------------------------------------


def compute_execution(session: Session, project_id: int) -> BudgetAnalysis:
    """Recompute the execution of a project from its expenses and compare it with its budget lines
    and the donor's limits. Reads only; see `run_budget` to persist the findings."""
    project = session.get(Project, project_id)
    if project is None:
        raise LookupError(f"Project {project_id} not found")
    lines = list(session.scalars(select(BudgetLine).where(BudgetLine.project_id == project_id)))
    spends = [
        (code, donor, project_amount(donor, extra))
        for code, donor, extra in session.execute(
            select(Expense.partida_code, Expense.amount_donor, Expense.extra).where(
                Expense.project_id == project_id
            )
        )
    ]
    adapter = get_adapter(project.donor)
    return analyze_budget(lines, spends, adapter.limits(), project.donor)


def check_limits(session: Session, project_id: int) -> list[FindingIn]:
    """The budget findings (caps, deviations, report mismatches) without persisting them."""
    return compute_execution(session, project_id).findings


def run_budget(session: Session, project_id: int) -> BudgetAnalysis:
    """Compute the execution and persist its findings: the OPEN ones of a previous run are
    replaced, the reviewer's decisions (accepted / false positive) are kept."""
    analysis = compute_execution(session, project_id)
    _sync_findings(session, project_id, BUDGET_CODES, analysis.findings)
    session.commit()
    return analysis
