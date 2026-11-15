"""Checks on the expense listing itself (no OCR): exchange rate, share imputed to the donor,
partida outside the budget, three offers above the donor's threshold."""

from decimal import Decimal
from types import SimpleNamespace

from app.enums import Donor, Severity
from app.pipeline.listing import check_listing
from app.pipeline.match_config import DONOR_DEFAULTS, KIND_COFINANCED, KIND_NORMAL, expense_kind
from app.schemas import Limit, LimitScope

D = Decimal
QUOTES = Limit(
    code="XUNTA_3_OFERTAS_SUMINISTROS_SERVICIOS",
    description="Deben constar tres ofertas.",
    partida_codes=["A.III.1", "A.VI"],
    scope=LimitScope.EXPENSE,
    max_amount_eur=D("15000"),
    reference="Orden AH 2024",
)


def expense(**kw):
    base = dict(
        id=1, order_no="5", source_row=10, amount_local=D("4300000"), currency="UGX",
        fx_rate=D("4300"), amount_eur=D("1000"), amount_donor=D("1000"),
        extra={"pct_imputed": 1}, partida_code="A.V.1",
    )  # fmt: skip
    return SimpleNamespace(**{**base, **kw})


def codes(findings):
    return [f.rule_code for f in findings]


def test_a_coherent_line_raises_nothing():
    assert check_listing([expense()], ["A.V", "A.III"], [QUOTES]) == []
    # rounding is not an error
    assert check_listing([expense(amount_eur=D("1000.40"))], [], []) == []


def test_euros_that_do_not_follow_from_the_exchange_rate():
    (f,) = check_listing([expense(amount_eur=D("1200"), amount_donor=D("1200"))], [], [])
    assert f.rule_code == "FX_RATE_MISMATCH" and f.severity == Severity.MEDIUM
    assert "1.000,00 €" in f.message and "1.200,00 €" in f.message


def test_donor_share_that_does_not_follow_from_the_percentage():
    line = expense(amount_donor=D("900"), extra={"pct_imputed": 0.5})
    (f,) = check_listing([line], [], [])
    assert f.rule_code == "DONOR_SHARE_MISMATCH" and "50 %" in f.message and "500,00 €" in f.message


def test_partida_outside_the_approved_budget():
    (f,) = check_listing([expense(partida_code="A.XI")], ["A.V", "A.III"], [])
    assert (f.rule_code, f.severity) == ("PARTIDA_NOT_IN_BUDGET", Severity.HIGH)
    # a more specific or a more general code of a budget partida is fine; no budget, no check
    assert check_listing([expense(partida_code="A.V.2")], ["A.V"], []) == []
    assert check_listing([expense(partida_code="A.V")], ["A.V.1", "A.V.2"], []) == []
    assert check_listing([expense(partida_code="A.XI")], [], []) == []


def test_three_offers_above_the_threshold_of_the_partida():
    big = expense(partida_code="A.III.1", amount_eur=D("20000"), amount_local=None, extra={})
    (f,) = check_listing([big], [], [QUOTES])
    assert f.rule_code == "THREE_QUOTES_REQUIRED" and "15.000,00 €" in f.message
    assert f.evidence["subject"] == QUOTES.code
    # another partida, or under the threshold: nothing
    other = expense(partida_code="A.V.1", amount_eur=D("20000"), amount_local=None, extra={})
    assert codes(check_listing([other], [], [QUOTES])) == []
    small = expense(partida_code="A.VI", amount_eur=D("14000"), amount_local=None, extra={})
    assert check_listing([small], [], [QUOTES]) == []


def test_xunta_justifies_cofinanced_expenses_too():
    cofinanced = expense(amount_donor=D("0"), extra={"pct_imputed": 0})
    assert expense_kind(cofinanced, DONOR_DEFAULTS[Donor.XUNTA]) == KIND_NORMAL
    assert expense_kind(cofinanced, DONOR_DEFAULTS[Donor.AECID]) == KIND_COFINANCED
