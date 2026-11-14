"""XuntaAdapter against the real Uganda pilot report (skipped without DATA_DIR)."""

from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import pytest
import xlrd

from app.adapters import XuntaAdapter

TOLERANCE = Decimal("0.05")


def _sheet_total(report: Path, label_prefix: str, column: int) -> Decimal:
    """Independent read of a total cell of 'Relación de facturas' (does not use the adapter)."""
    sheet = xlrd.open_workbook(report).sheet_by_name("Relación de facturas")
    for r in range(sheet.nrows):
        if str(sheet.cell_value(r, 0)).upper().startswith(label_prefix):
            return Decimal(str(sheet.cell_value(r, column)))
    raise AssertionError(f"Row '{label_prefix}' not found")


def test_expense_count(xunta_expenses):
    assert len(xunta_expenses) == 203


def test_sum_donor_amount_matches_report_total(xunta_expenses, xunta_report):
    total_cell = _sheet_total(xunta_report, "TOTAL XERAL", column=11)
    assert total_cell == pytest.approx(Decimal("71711.65"), abs=Decimal("0.01"))
    total = sum((e.amount_donor or Decimal(0) for e in xunta_expenses), Decimal(0))
    assert abs(total - total_cell) <= Decimal("0.01")


def test_expense_fields(xunta_expenses):
    first = xunta_expenses[0]
    assert first.source_row == 6
    assert (first.order_no, first.order_key) == ("1", "1")
    assert str(first.date) == "2024-10-24"
    assert first.issuer == "Albert Alumbgi"
    assert first.partida_code == "A.I"
    assert first.currency == "UGX"
    assert first.amount_local == Decimal("300345.0")
    assert first.amount_eur == pytest.approx(Decimal("77.90293502"), abs=Decimal("0.0001"))
    assert first.amount_donor == Decimal("77.9")
    assert first.fx_rate == pytest.approx(Decimal("3855.37464"), abs=Decimal("0.001"))
    assert first.extra and first.extra["invoice_no"] == "FP UGA04 108"

    for e in xunta_expenses:
        assert e.partida_code, e
        assert e.date is not None, e
        assert e.amount_eur is not None, e
        assert e.concept and e.issuer, e


def test_order_numbers_are_kept_as_written(xunta_expenses):
    na = [e for e in xunta_expenses if e.order_no == "N/A"]
    assert na and all(e.order_key is None for e in na)
    suffixed = [e for e in xunta_expenses if e.order_no and e.order_no.endswith(".A")]
    assert suffixed and all(e.order_key == e.order_no for e in suffixed)
    assert all(" " not in (e.order_key or "") for e in xunta_expenses)
    assert any(e.order_no == "5" and e.order_key == "5" for e in xunta_expenses)


def test_expenses_charged_to_other_funders_have_no_xunta_amount(xunta_expenses):
    jrs = [e for e in xunta_expenses if e.extra and "JRS" in e.extra.get("other_funders", {})]
    assert jrs
    assert all(e.amount_donor is None for e in jrs)


def test_budget_partidas(xunta_budget):
    by_code = {b.partida_code: b for b in xunta_budget}
    assert by_code["A.V.2"].approved_donor == Decimal("18075.27")  # "Local" despite the sheet label
    assert by_code["A.X"].executed_donor == Decimal("2000.0")
    assert by_code["B"].approved_donor == Decimal("4000.0")
    # Parent rows and totals are not partidas.
    assert not by_code["A.III"].is_partida
    assert not by_code["A.V"].is_partida
    assert not by_code["TOTAL_DIRECTOS"].is_partida
    assert not by_code["TOTAL_GENERAL"].is_partida
    assert by_code["A.III.2"].is_partida and by_code["B"].is_partida
    assert by_code["TOTAL_GENERAL"].executed_donor == pytest.approx(
        Decimal("71711.65"), abs=Decimal("0.01")
    )


def test_executed_per_partida_matches_expenses(xunta_expenses, xunta_budget):
    spent: dict[str, Decimal] = defaultdict(Decimal)
    for e in xunta_expenses:
        spent[e.partida_code] += e.amount_donor or Decimal(0)

    leaves = {b.partida_code: b for b in xunta_budget if b.is_partida}
    assert set(spent) <= set(leaves), "expense imputed to a partida missing in the budget"
    for code, line in leaves.items():
        assert (
            abs(spent.get(code, Decimal(0)) - (line.executed_donor or Decimal(0))) <= TOLERANCE
        ), code


def test_catalog_covers_every_partida_in_use(xunta_expenses, xunta_budget):
    catalog = {p.code for p in XuntaAdapter().partida_catalog()}
    used = {e.partida_code for e in xunta_expenses} | {
        b.partida_code for b in xunta_budget if b.is_partida
    }
    assert used <= catalog, used - catalog
