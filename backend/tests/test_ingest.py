"""ingest_project against PostgreSQL (rolled back) with the real reports."""

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.enums import Donor
from app.models import BudgetLine, Expense, Project
from app.pipeline.ingest import ingest_project


def _project(session, donor: Donor, code: str) -> Project:
    project = Project(name=f"test {code}", donor=donor, code=code)
    session.add(project)
    session.flush()
    return project


def test_unknown_project_raises(db_session, tmp_path):
    with pytest.raises(LookupError):
        ingest_project(db_session, 999_999, tmp_path / "x.xlsx")


def test_ingest_xunta(db_session, xunta_report):
    project = _project(db_session, Donor.XUNTA, "24-UG-226")
    result = ingest_project(db_session, project.id, xunta_report)
    assert result.expenses == 203 and result.budget_lines > 15

    rows = db_session.scalars(select(Expense).where(Expense.project_id == project.id)).all()
    assert len(rows) == 203
    assert sum(r.amount_donor or Decimal(0) for r in rows) == pytest.approx(
        Decimal("71711.65"),
        abs=Decimal("0.5"),  # each amount is rounded to cents by Numeric(14,2)
    )
    first = min(rows, key=lambda r: r.source_row)
    assert first.order_key == "1" and first.currency == "UGX"
    assert first.extra["invoice_no"] == "FP UGA04 108"  # JSONB round trip
    assert isinstance(first.extra["pct_imputed"], float)

    flags = {
        b.partida_code: b.is_partida
        for b in db_session.scalars(select(BudgetLine).where(BudgetLine.project_id == project.id))
    }
    assert flags["A.III.2"] and flags["B"] and not flags["A.V"] and not flags["TOTAL_GENERAL"]


def test_ingest_aecid_is_idempotent(db_session, aecid_report):
    project = _project(db_session, Donor.AECID, "22-PY-136")
    first = ingest_project(db_session, project.id, aecid_report, aecid_report)
    second = ingest_project(db_session, project.id, aecid_report)
    assert first == second
    assert first.expenses == 1321

    count = db_session.scalar(
        select(func.count()).select_from(Expense).where(Expense.project_id == project.id)
    )
    assert count == 1321  # re-ingesting replaces, never duplicates
    total = db_session.scalar(
        select(func.sum(Expense.amount_donor)).where(Expense.project_id == project.id)
    )
    assert abs(total - Decimal(450000)) < Decimal("1")
    bad_date = db_session.scalar(
        select(Expense).where(Expense.project_id == project.id, Expense.order_no == "CG126.9")
    )
    assert bad_date.date is None and bad_date.extra["date_raw"] == "30/9/224"


def test_failed_parse_keeps_previous_data(db_session, xunta_report, tmp_path):
    project = _project(db_session, Donor.XUNTA, "24-UG-226")
    ingest_project(db_session, project.id, xunta_report)
    broken = tmp_path / "broken.xls"
    broken.write_bytes(b"not an excel file")
    with pytest.raises(Exception):  # noqa: B017  (xlrd raises its own error type)
        ingest_project(db_session, project.id, broken)
    count = db_session.scalar(
        select(func.count()).select_from(Expense).where(Expense.project_id == project.id)
    )
    assert count == 203
