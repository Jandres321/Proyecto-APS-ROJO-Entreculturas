"""AecidAdapter against the real Paraguay report (skipped without DATA_DIR)."""

import datetime as dt
from collections import Counter, defaultdict
from decimal import Decimal

from app.adapters import AecidAdapter

TOLERANCE = Decimal("1")  # euros


def test_expense_count_and_prefixes(aecid_expenses):
    assert len(aecid_expenses) == 1321
    prefixes = Counter("".join(c for c in e.order_no if c.isalpha()) for e in aecid_expenses)
    assert prefixes == {"CG": 679, "FA": 601, "EC": 41}


def test_sum_by_partida_matches_executed_aecid(aecid_expenses, aecid_budget):
    spent: dict[str, Decimal] = defaultdict(Decimal)
    for e in aecid_expenses:
        spent[e.partida_code] += e.amount_donor or Decimal(0)

    leaves = {b.partida_code: b for b in aecid_budget if b.is_partida}
    assert set(spent) <= set(leaves), "expense imputed to a partida missing in the budget"
    for code, line in leaves.items():
        diff = spent.get(code, Decimal(0)) - (line.executed_donor or Decimal(0))
        assert abs(diff) <= TOLERANCE, f"{code}: expenses differ from X.A.1 by {diff}"

    total = sum(spent.values(), Decimal(0))
    general = next(b for b in aecid_budget if b.partida_code == "TOTAL_GENERAL")
    assert abs(total - general.executed_donor) <= TOLERANCE
    assert abs(total - Decimal(450000)) <= TOLERANCE


def test_budget_row_classification(aecid_budget):
    by_code = {b.partida_code: b for b in aecid_budget}
    non_partidas = {
        "A.I.6", "A.II.2", "TOTAL_A.I", "TOTAL_A.II", "TOTAL_DIRECTOS", "PCT_DIRECTOS",
        "PCT_INDIRECTOS", "B_ONGDE", "B_ONGDL", "INTERESES", "TOTAL_GENERAL",
    }  # fmt: skip
    for code in non_partidas:
        assert not by_code[code].is_partida, code
    for code in ("A.I.1", "A.I.6.1", "A.I.7", "A.II.2.1", "A.II.3", "B"):
        assert by_code[code].is_partida, code
    assert not any(
        b.is_partida and b.partida_code.startswith(("TOTAL", "PCT")) for b in aecid_budget
    )

    assert by_code["A.I.7"].approved_donor == Decimal(45616)
    assert by_code["A.I.7"].approved_total == Decimal(49723)
    assert by_code["B"].approved_donor == Decimal(53272)
    assert by_code["TOTAL_GENERAL"].approved_donor == Decimal(450000)


def test_indirect_costs_within_limit(aecid_budget):
    """Real data check of the 12 % cap the adapter exposes as a Limit."""
    (limit,) = AecidAdapter().limits()
    by_code = {b.partida_code: b for b in aecid_budget}
    grant = by_code["TOTAL_GENERAL"].approved_donor
    assert by_code["B"].approved_donor / grant <= limit.max_pct


def test_order_numbers(aecid_expenses):
    by_no = {e.order_no: e for e in aecid_expenses}
    assert by_no["CG18,1"].order_key == "CG18.1"  # decimal comma in the source
    assert by_no["FA236.3"].order_key == "FA236.3"
    assert all(" " not in e.order_key for e in aecid_expenses)
    # An order number can repeat (salary + social security of the same payslip).
    assert Counter(e.order_no for e in aecid_expenses)["CG388"] == 2


def test_expense_fields(aecid_expenses):
    first = aecid_expenses[0]
    assert first.order_no == "EC1" and first.source_row == 14
    assert first.date == dt.date(2023, 2, 25)
    assert first.partida_code == "A.I.6.3"
    assert first.partida_raw.startswith("A.I.6.3 Personal en Sede")  # nbsp prefix stripped
    assert first.currency == "EUR" and first.amount_local is None and first.fx_rate is None
    assert first.amount_donor == Decimal("340.27")

    local = next(e for e in aecid_expenses if e.order_no == "CG432")
    assert local.currency == "PYG" and local.amount_local == Decimal(583335)
    assert abs(local.fx_rate - Decimal("7655.62021211")) < Decimal("0.0001")  # PYG per EUR
    assert abs(local.amount_local / local.fx_rate - local.amount_eur) < Decimal("0.01")

    assert {e.partida_code for e in aecid_expenses} <= {
        *("A.I.1", "A.I.2", "A.I.3", "A.I.4", "A.I.5", "A.I.6.1", "A.I.6.3", "A.I.7"),
        *("A.I.9", "A.II.2.1", "A.II.3", "B"),
    }


def test_bad_date_is_flagged_not_guessed(aecid_expenses):
    bad = next(e for e in aecid_expenses if e.order_no == "CG126.9")
    assert bad.date is None
    assert bad.extra["date_raw"] == "30/9/224"
    assert sum(e.date is None for e in aecid_expenses) == 1


def test_counterpart_rows_have_no_aecid_amount(aecid_expenses):
    cp = [e for e in aecid_expenses if e.source == "CEPAG CONTRAPARTIDA"]
    assert len(cp) == 14
    assert all(e.amount_donor is None for e in cp)


def test_catalog_covers_every_partida_in_use(aecid_expenses):
    catalog = {p.code for p in AecidAdapter().partida_catalog()}
    assert {e.partida_code for e in aecid_expenses} <= catalog
