"""Budget step (executed vs approved, donor caps, traffic light, findings).

The first block is pure (invented figures, no database). The second needs the migrated
PostgreSQL (rolled back) and the third needs the real reports in DATA_DIR.
"""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.adapters import AecidAdapter, XuntaAdapter
from app.enums import Donor, FindingStatus, Severity
from app.models import BudgetLine, Expense, Finding, Project
from app.pipeline.budget import (
    NO_PARTIDA,
    RULE_BUDGET_DEVIATION,
    RULE_EXECUTED_REPORT_MISMATCH,
    RULE_LIMIT_EXCEEDED,
    Traffic,
    analyze_budget,
    check_limits,
    compute_execution,
    project_amount,
    run_budget,
)
from app.review import set_decision
from app.schemas import BudgetLineIn

D = Decimal


def line(code, approved, *, total=None, declared=None, is_partida=True, name=None):
    return BudgetLineIn(
        partida_code=code,
        partida_name=name,
        approved_donor=D(approved),
        approved_total=D(total if total is not None else approved),
        executed_donor=None if declared is None else D(declared),
        is_partida=is_partida,
    )


def spend(code, donor, project=None):
    """(partida, donor amount, project amount)."""
    return (code, D(donor), D(project if project is not None else donor))


def rules(analysis):
    return [f.rule_code for f in analysis.findings]


def by_rule(analysis, rule):
    return [f for f in analysis.findings if f.rule_code == rule]


def partida(analysis, code):
    return next(p for p in analysis.partidas if p.code == code)


# --- AECID cap: indirect costs <= 12 % of the approved grant ---


def aecid(lines, spends):
    return analyze_budget(lines, spends, AecidAdapter().limits(), Donor.AECID)


def test_aecid_indirect_costs_at_11_84_percent_are_ok():
    # Paraguay: 53.272 EUR of indirect costs over a 450.000 EUR grant = 11,84 %.
    lines = [line("A.I.1", 396728), line("B", 53272)]
    analysis = aecid(lines, [spend("A.I.1", 396728), spend("B", 53272)])
    (check,) = analysis.limits
    assert round(check.pct, 4) == D("0.1184")
    assert not check.exceeded
    assert RULE_LIMIT_EXCEEDED not in rules(analysis)
    assert all(p.traffic == Traffic.GREEN for p in analysis.partidas)


def test_aecid_indirect_costs_above_12_percent_exceed_the_cap():
    lines = [line("A.I.1", 396728), line("B", 53272)]
    analysis = aecid(lines, [spend("A.I.1", 395000), spend("B", 54500)])  # 12,11 % of 450.000
    (finding,) = by_rule(analysis, RULE_LIMIT_EXCEEDED)
    assert finding.severity == Severity.HIGH
    assert finding.evidence["subject"] == "AECID_INDIRECTOS_MAX_12"
    assert "12,11 %" in finding.message and "450.000,00 €" in finding.message  # Spanish formatting
    assert partida(analysis, "B").traffic == Traffic.RED


def test_aecid_cap_is_measured_against_the_approved_grant_not_the_executed_total():
    # Executing little overall must not make a modest indirect cost look excessive.
    lines = [line("A.I.1", 396728), line("B", 53272)]
    analysis = aecid(lines, [spend("A.I.1", 100000), spend("B", 53272)])
    assert RULE_LIMIT_EXCEEDED not in rules(analysis)


# --- Xunta caps ---


def xunta(lines, spends):
    return analyze_budget(lines, spends, XuntaAdapter().limits(), Donor.XUNTA)


def xunta_lines():
    # Project total (every funder) = 100.000 EUR, of which 80.000 from the Xunta.
    return [
        line("A.V.2", 30000, total=50000),
        line("A.III.2", 30000, total=37500),
        line("A.IX", 5000),
        line("A.X", 2500),
        line("B", 2500, total=5000),
        line("A.VIII", 0),
    ]


def limits_exceeded(analysis):
    return {f.evidence["subject"] for f in by_rule(analysis, RULE_LIMIT_EXCEEDED)}


def test_xunta_all_caps_respected():
    # Exactly AT a cap is valid: 70 % personal, 5 % indirect, 2.500 audit, 5.000 evaluation.
    spends = [
        spend("A.V.2", 30000, 70000),
        spend("A.X", 2500),
        spend("A.IX", 5000),
        spend("B", 2500, 5000),
    ]
    assert limits_exceeded(xunta(xunta_lines(), spends)) == set()


def test_xunta_caps_exceeded():
    spends = [
        spend("A.V.2", 30000, 70001),  # personal above 70 % of the project total
        spend("A.X", 2500.01),  # audit above 2.500
        spend("A.IX", 5000.01),  # evaluation above 5.000
        spend("B", 2500, 5000.01),  # indirect above 5 % of the project total
    ]
    analysis = xunta(xunta_lines(), spends)
    assert limits_exceeded(analysis) == {
        "XUNTA_PERSONAL_MAX_70",
        "XUNTA_AUDITORIA_MAX_2500",
        "XUNTA_EVALUACION_MAX_5000",
        "XUNTA_INDIRECTOS_MAX_5",
    }
    # Every partida of an exceeded cap turns red, even those without deviation.
    assert partida(analysis, "A.IX").traffic == Traffic.RED


def test_xunta_personal_cap_counts_other_funders_of_the_project():
    # 40.000 charged to the Xunta + 31.000 from another funder on personal partidas.
    spends = [spend("A.V.2", 40000, 71000)]
    assert "XUNTA_PERSONAL_MAX_70" in limits_exceeded(xunta(xunta_lines(), spends))


def test_expense_scope_limits_are_not_budget_checks():
    # 3-offers / cash thresholds are per expense, not a budget cap: no LimitCheck for them.
    analysis = xunta(xunta_lines(), [])
    codes = {c.limit.code for c in analysis.limits}
    assert "XUNTA_EFECTIVO_MAX_1000" not in codes and "XUNTA_3_OFERTAS_OBRAS" not in codes


# --- deviation and traffic light ---


@pytest.mark.parametrize(
    ("executed", "traffic"),
    [(1000, Traffic.GREEN), (1050, Traffic.GREEN), (1080, Traffic.YELLOW), (1100, Traffic.YELLOW)]
    + [(1101, Traffic.RED), (920, Traffic.YELLOW), (850, Traffic.RED)],
)
def test_traffic_light_thresholds(executed, traffic):
    analysis = xunta([line("A.VII", 1000)], [spend("A.VII", executed)])
    assert partida(analysis, "A.VII").traffic == traffic
    assert (RULE_BUDGET_DEVIATION in rules(analysis)) == (traffic != Traffic.GREEN)


def test_deviation_under_report_tolerance_is_green_even_on_tiny_partidas():
    analysis = xunta([line("A.VII", 10)], [spend("A.VII", D("10.80"))])  # +8 % but < 1 EUR
    assert partida(analysis, "A.VII").traffic == Traffic.GREEN


def test_deviation_severity_depends_on_donor():
    spends = [spend("A.VII", 1080), spend("A.VIII", 1500)]
    lines = [line("A.VII", 1000), line("A.VIII", 1000)]
    x = xunta(lines, spends)
    a = aecid(lines, spends)
    assert [f.severity for f in by_rule(x, RULE_BUDGET_DEVIATION)] == [
        Severity.MEDIUM,
        Severity.HIGH,
    ]
    assert [f.severity for f in by_rule(a, RULE_BUDGET_DEVIATION)] == [
        Severity.LOW,
        Severity.MEDIUM,
    ]


def test_aecid_deviation_is_an_accidental_modification_to_communicate():
    analysis = aecid([line("A.I.1", 1000)], [spend("A.I.1", 1300)])
    (finding,) = by_rule(analysis, RULE_BUDGET_DEVIATION)
    assert "Modificación accidental" in finding.message
    assert "comunicarla" in finding.message
    assert "30,0 %" in finding.message


def test_deviation_evidence_is_json_safe():
    import json

    analysis = xunta([line("A.VII", 1000)], [spend("A.VII", 1300)])
    evidence = by_rule(analysis, RULE_BUDGET_DEVIATION)[0].evidence
    assert json.loads(json.dumps(evidence)) == evidence
    assert evidence["subject"] == "A.VII" and evidence["traffic"] == "red"


def test_partida_without_approved_amount_but_with_spending_is_red():
    analysis = xunta([line("A.V.1", 0)], [spend("A.V.1", 500)])
    assert partida(analysis, "A.V.1").traffic == Traffic.RED
    (finding,) = by_rule(analysis, RULE_BUDGET_DEVIATION)
    assert ", ;" not in finding.message


def test_partida_missing_from_the_budget_is_reported():
    analysis = xunta([line("A.VII", 1000)], [spend("A.VII", 1000), spend("Z.9", 100)])
    z = partida(analysis, "Z.9")
    assert not z.in_budget and z.traffic == Traffic.RED
    (finding,) = by_rule(analysis, RULE_BUDGET_DEVIATION)
    assert "no figura en el presupuesto aprobado" in finding.message


def test_expense_without_partida_is_not_lost():
    analysis = xunta([line("A.VII", 1000)], [spend("A.VII", 1000), (None, D(300), D(300))])
    assert partida(analysis, NO_PARTIDA).executed == 300


def test_subtotal_rows_are_not_partidas():
    lines = [line("A.III", 99999, is_partida=False), line("A.III.2", 1000)]
    analysis = xunta(lines, [spend("A.III.2", 1000)])
    assert [p.code for p in analysis.partidas] == ["A.III.2"]
    # ...and they do not inflate the base the caps are measured against.
    (check,) = [c for c in analysis.limits if c.limit.code == "XUNTA_INDIRECTOS_MAX_5"]
    assert check.base == 1000


# --- executed figure declared in the report ---


def test_report_mismatch_when_declared_differs_from_the_expense_list():
    lines = [line("A.VII", 1000, declared=1000), line("A.VIII", 500, declared=500)]
    analysis = xunta(lines, [spend("A.VII", 990), spend("A.VIII", D("500.40"))])
    (finding,) = by_rule(analysis, RULE_EXECUTED_REPORT_MISMATCH)
    assert finding.severity == Severity.HIGH
    assert finding.evidence["subject"] == "A.VII"
    assert finding.evidence["declared"] == "1000.00" and finding.evidence["recomputed"] == "990.00"
    assert "diferencia de -10,00 €" in finding.message
    assert partida(analysis, "A.VII").traffic == Traffic.RED  # within 1 EUR (A.VIII): no finding


def test_no_mismatch_when_the_report_does_not_declare_executed():
    analysis = xunta([line("A.VII", 1000)], [spend("A.VII", 1000)])
    assert RULE_EXECUTED_REPORT_MISMATCH not in rules(analysis)


# --- project amount ---


def test_project_amount_adds_other_funders_in_both_donor_layouts():
    assert project_amount(D(100), {"other_funders": {"JRS": D("30.5"), "X": D("0.5")}}) == D(131)
    assert project_amount(D(100), {"other_funders": 20.0, "own_funds": 5}) == D(125)  # AECID, JSONB
    assert project_amount(None, {"other_funders": {"JRS": 430.89}}) == D("430.89")  # co-financed
    assert project_amount(D(100), None) == D(100)


# --- database ---


def make_project(session, donor=Donor.XUNTA, code="BUD-1"):
    project = Project(name="budget test", donor=donor, code=code)
    session.add(project)
    session.flush()
    return project


def add_lines(session, project, lines):
    session.add_all(BudgetLine(project_id=project.id, **ln.model_dump()) for ln in lines)
    session.flush()


def add_expense(session, project, code, donor, **extra):
    session.add(
        Expense(
            project_id=project.id,
            order_no="1",
            partida_code=code,
            amount_donor=D(donor),
            extra=extra or None,
        )
    )
    session.flush()


def findings_of(session, project, rule=None):
    query = select(Finding).where(Finding.project_id == project.id)
    if rule:
        query = query.where(Finding.rule_code == rule)
    return list(session.scalars(query.order_by(Finding.id)))


def test_compute_execution_reads_budget_lines_and_expenses(db_session):
    project = make_project(db_session)
    add_lines(db_session, project, [line("A.VII", 1000, total=1500, declared=1000)])
    add_expense(db_session, project, "A.VII", 600, other_funders={"JRS": 100})
    add_expense(db_session, project, "A.VII", 400)

    analysis = compute_execution(db_session, project.id)
    p = partida(analysis, "A.VII")
    assert (p.executed, p.executed_total, p.expenses) == (D(1000), D(1100), 2)
    assert check_limits(db_session, project.id) == analysis.findings == []
    assert findings_of(db_session, project) == []  # reading does not persist


def test_run_budget_persists_findings_and_is_idempotent(db_session):
    project = make_project(db_session, Donor.AECID)
    add_lines(db_session, project, [line("A.I.1", 396728), line("B", 53272)])
    add_expense(db_session, project, "A.I.1", 394000)
    add_expense(db_session, project, "B", 56000)  # 12,44 % of the grant

    run_budget(db_session, project.id)
    first = {(f.rule_code, f.evidence["subject"]) for f in findings_of(db_session, project)}
    assert (RULE_LIMIT_EXCEEDED, "AECID_INDIRECTOS_MAX_12") in first
    assert (RULE_BUDGET_DEVIATION, "B") in first

    run_budget(db_session, project.id)
    again = findings_of(db_session, project)
    assert {(f.rule_code, f.evidence["subject"]) for f in again} == first
    assert len(again) == len(first)  # replaced, not duplicated
    assert all(f.status == FindingStatus.OPEN for f in again)


def test_reviewer_decision_on_one_cap_does_not_hide_another(db_session):
    project = make_project(db_session)
    add_lines(
        db_session,
        project,
        [line("A.X", 2500), line("A.IX", 5000), line("A.III.2", 90000)],
    )
    add_expense(db_session, project, "A.X", 2600)
    add_expense(db_session, project, "A.IX", 5100)
    run_budget(db_session, project.id)

    caps = findings_of(db_session, project, RULE_LIMIT_EXCEEDED)
    subjects = {f.evidence["subject"]: f for f in caps}
    assert set(subjects) == {"XUNTA_AUDITORIA_MAX_2500", "XUNTA_EVALUACION_MAX_5000"}

    set_decision(
        db_session, subjects["XUNTA_AUDITORIA_MAX_2500"].id, FindingStatus.FALSE_POSITIVE, "ok"
    )
    run_budget(db_session, project.id)

    caps = {f.evidence["subject"]: f for f in findings_of(db_session, project, RULE_LIMIT_EXCEEDED)}
    assert caps["XUNTA_AUDITORIA_MAX_2500"].status == FindingStatus.FALSE_POSITIVE
    assert caps["XUNTA_AUDITORIA_MAX_2500"].reviewer_note == "ok"
    assert caps["XUNTA_EVALUACION_MAX_5000"].status == FindingStatus.OPEN
    assert len(caps) == 2  # the decided one is not created again


def test_findings_resolved_by_the_data_disappear_on_rerun(db_session):
    project = make_project(db_session)
    add_lines(db_session, project, [line("A.X", 2500), line("A.III.2", 90000)])
    add_expense(db_session, project, "A.X", 2600)
    run_budget(db_session, project.id)
    assert findings_of(db_session, project, RULE_LIMIT_EXCEEDED)

    expense = db_session.scalars(select(Expense).where(Expense.project_id == project.id)).one()
    expense.amount_donor = D(2500)
    db_session.flush()
    run_budget(db_session, project.id)
    assert findings_of(db_session, project, RULE_LIMIT_EXCEEDED) == []


# --- real reports (skipped without DATA_DIR) ---


def spends_of(expenses):
    return [
        (e.partida_code, e.amount_donor, project_amount(e.amount_donor, e.extra)) for e in expenses
    ]


def test_real_paraguay_indirect_costs_11_84_percent_are_ok(aecid_budget, aecid_expenses):
    analysis = analyze_budget(
        aecid_budget, spends_of(aecid_expenses), AecidAdapter().limits(), Donor.AECID
    )
    (check,) = analysis.limits
    assert round(check.pct, 4) == D("0.1184")
    assert check.base == D(450000)  # the sum of the partidas is the approved grant
    assert not check.exceeded
    assert RULE_LIMIT_EXCEEDED not in rules(analysis)
    # The report's executed figures agree with the expense list (the adapter test checks it too).
    assert RULE_EXECUTED_REPORT_MISMATCH not in rules(analysis)


def test_real_xunta_caps_are_respected_and_report_matches(xunta_budget, xunta_expenses):
    analysis = analyze_budget(
        xunta_budget, spends_of(xunta_expenses), XuntaAdapter().limits(), Donor.XUNTA
    )
    checks = {c.limit.code: c for c in analysis.limits}
    assert checks["XUNTA_INDIRECTOS_MAX_5"].pct <= D("0.05")
    assert checks["XUNTA_PERSONAL_MAX_70"].pct <= D("0.70")
    assert checks["XUNTA_AUDITORIA_MAX_2500"].executed <= D(2500)
    assert RULE_LIMIT_EXCEEDED not in rules(analysis)
    assert RULE_EXECUTED_REPORT_MISMATCH not in rules(analysis)
    # The Xunta total of the report is the donor share + the other funders of the project.
    declared_total = {b.partida_code: b.executed_total for b in xunta_budget if b.is_partida}
    for p in analysis.partidas:
        if declared_total.get(p.code) is not None:
            assert abs(p.executed_total - declared_total[p.code]) <= D(1), p.code
