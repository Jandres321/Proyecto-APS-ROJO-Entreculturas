"""Checks per expense and segment (rules/<donor>.yaml) on invented data."""

import datetime as dt
from decimal import Decimal

import pytest
import yaml
from sqlalchemy import select

from app.enums import Confidence, Donor, Severity, TipoSegmento
from app.models import Document, ExpenseSegment, Finding, Rule, Segmento
from app.pipeline import checks as ck
from app.rules import (
    CHECK_CODES,
    RULES_DIR,
    RulesError,
    load_rules,
    partida_matches,
    sync_rules,
)
from tests.test_match_pipeline import add_expense, new_project

D = dt.date


def xunta():
    return load_rules("xunta")


def aecid():
    return load_rules("aecid")


def expense(i=1, **kw) -> ck.ExpenseFacts:
    kw.setdefault("order_no", str(i))
    kw.setdefault("partida_code", "A.VI")
    kw.setdefault("amount_local", Decimal("500000"))
    kw.setdefault("amount_eur", Decimal("120"))
    kw.setdefault("currency", "UGX")
    return ck.ExpenseFacts(id=i, **kw)


def seg(i=1, doc="d1", tipo="factura", **kw) -> ck.Seg:
    for name in ("importe",):
        if kw.get(name) is not None:
            kw[name] = Decimal(str(kw[name]))
    return ck.Seg(id=i, doc_key=doc, tipo=tipo, **kw)


def run(rules, expenses_segments, bundle_extra=(), period=None):
    """expenses_segments: [(expense, [linked segs])]; bundle = linked + bundle_extra."""
    cases, of_seg = [], {}
    for exp, linked in expenses_segments:
        cases.append(ck.Case(exp, list(linked), [*linked, *bundle_extra]))
        for s in linked:
            of_seg.setdefault(s.id, []).append(exp)
    ctx = ck.CheckContext(rules, cases, of_seg, period)
    return ck.check_documents(ctx) + ck.check_file_completeness(ctx) + ck.check_duplicates(ctx)


def codes(findings):
    return sorted(f.rule_code for f in findings)


# --- rules files ---


@pytest.mark.parametrize("donor", ["xunta", "aecid"])
def test_rules_files_declare_every_check_with_reference_and_description(donor):
    rules = load_rules(donor)
    assert set(rules.rules) == set(CHECK_CODES)
    for rule in rules.rules.values():
        assert rule.description and rule.reference and rule.severity in Severity
        assert rule.donor == Donor(donor)
    assert rules.rules["DOC_INCOMPLETE"].params["requirements"]


def test_xunta_rules_come_from_rules_xlsx_and_the_order():
    rules = xunta()
    stamp = rules.rules["DONOR_STAMP_MISSING"]
    assert "Rules.xlsx" in stamp.reference and "19.5" in stamp.reference
    assert "xunta_galicia_stamp_present" in stamp.params["sources"]
    assert rules.params("CASH_OVER_LIMIT")["limit_eur"] == 1000
    assert rules.params("CASH_OVER_LIMIT")["inclusive"] is True  # Rules.xlsx: FAIL if >= 1,000


def test_aecid_rules_cite_the_guide():
    rules = aecid()
    assert "10.7" in rules.rules["CASH_OVER_LIMIT"].reference
    assert rules.params("CASH_OVER_LIMIT")["limit_eur"] == 2500
    assert rules.params("CASH_OVER_LIMIT")["inclusive"] is False
    assert "11.1" in rules.rules["DONOR_STAMP_MISSING"].reference


def test_partida_matching_is_hierarchical():
    assert partida_matches("A.V.2", ["A.V"]) and partida_matches("A.V", ["A.V"])
    assert not partida_matches("A.VI", ["A.V"])
    assert not partida_matches(None, ["A.V"]) and partida_matches(None, [])


def _write(tmp_path, data):
    (tmp_path / "xunta.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    return tmp_path


def test_loader_rejects_bad_files(tmp_path):
    good = yaml.safe_load((RULES_DIR / "xunta.yaml").read_text(encoding="utf-8"))
    broken = {**good, "rules": good["rules"][:-1]}  # a check without rule
    with pytest.raises(RulesError, match="missing rules"):
        load_rules("xunta", _write(tmp_path, broken))
    typo = {**good, "rules": [{**good["rules"][0], "code": "AMOUNT_MISMATCHH"}, *good["rules"][1:]]}
    with pytest.raises(RulesError, match="unknown rule code"):
        load_rules("xunta", _write(tmp_path, typo))
    bad_sev = {**good, "rules": [{**good["rules"][0], "severity": "huge"}, *good["rules"][1:]]}
    with pytest.raises(RulesError):
        load_rules("xunta", _write(tmp_path, bad_sev))
    with pytest.raises(RulesError, match="No rules file"):
        load_rules("aecid", tmp_path)


def test_inactive_rule_is_skipped(tmp_path):
    good = yaml.safe_load((RULES_DIR / "xunta.yaml").read_text(encoding="utf-8"))
    for r in good["rules"]:
        if r["code"] == "ILLEGIBLE":
            r["active"] = False
    rules = load_rules("xunta", _write(tmp_path, good))
    assert rules.get("ILLEGIBLE") is None
    out = run(rules, [(expense(), [seg(legible=False)])])
    assert "ILLEGIBLE" not in codes(out)


# --- AMOUNT_MISMATCH / DATE_MISMATCH ---


def test_amount_mismatch_reports_both_amounts_in_spanish():
    out = run(xunta(), [(expense(), [seg(importe=450000, moneda="UGX")])])
    (f,) = [f for f in out if f.rule_code == "AMOUNT_MISMATCH"]
    assert f.severity == Severity.HIGH
    assert "450.000" in f.message and "500.000" in f.message and "nº 1" in f.message
    assert f.evidence["diferencia"] == -50000 and "referencia" in f.evidence


def test_amount_mismatch_is_soft_on_trusted_link_or_weak_reading():
    trusted = run(xunta(), [(expense(link_confidence="high"), [seg(importe=450000)])])
    (f,) = [f for f in trusted if f.rule_code == "AMOUNT_MISMATCH"]
    assert f.severity == Severity.LOW and f.evidence["lectura_dudosa"] is True
    assert f.message.startswith("Revisar la lectura del importe") and "450.000" in f.message
    weak = run(xunta(), [(expense(), [seg(importe=450000, confianza_ocr=40)])])
    (g,) = [f for f in weak if f.rule_code == "AMOUNT_MISMATCH"]
    assert g.severity == Severity.LOW and "40 %" in g.message
    low_link = run(xunta(), [(expense(link_confidence="low"), [seg(importe=450000)])])
    (h,) = [f for f in low_link if f.rule_code == "AMOUNT_MISMATCH"]
    assert h.severity == Severity.HIGH and h.evidence["lectura_dudosa"] is False


def test_amount_ok_when_it_matches_any_listed_amount_or_a_figure_or_a_shared_sum():
    exp = expense()
    assert "AMOUNT_MISMATCH" not in codes(run(xunta(), [(exp, [seg(importe=500000)])]))
    assert "AMOUNT_MISMATCH" not in codes(run(xunta(), [(exp, [seg(importe=120, moneda="EUR")])]))
    # OCR picked another figure as total, but the expense's amount is printed in the document
    wrong_total = seg(importe=999999, importes=(Decimal(999999), Decimal(500000)))
    assert "AMOUNT_MISMATCH" not in codes(run(xunta(), [(exp, [wrong_total])]))
    # one invoice backing two expenses: it matches the SUM
    e1, e2 = expense(1, amount_local=Decimal(300000)), expense(2, amount_local=Decimal(200000))
    shared = seg(importe=500000)
    assert "AMOUNT_MISMATCH" not in codes(run(xunta(), [(e1, [shared]), (e2, [shared])]))


def test_date_mismatch_document_and_payment_date():
    exp = expense(date=D(2024, 3, 20), extra={"payment": "02/04/2024 y con transferencia bancaria"})
    out = run(
        xunta(),
        [(exp, [seg(fecha=D(2024, 3, 12)), seg(2, tipo="transferencia", fecha=D(2024, 4, 20))])],
    )
    msgs = [f.message for f in out if f.rule_code == "DATE_MISMATCH"]
    assert len(msgs) == 2
    assert any("8 días" in m and "20/03/2024" in m for m in msgs)
    assert any("se pagó el 02/04/2024" in m and "20/04/2024" in m for m in msgs)
    ok = run(xunta(), [(exp, [seg(fecha=D(2024, 3, 21))])])
    assert "DATE_MISMATCH" not in codes(ok)


def test_aecid_does_not_check_payment_date():
    exp = expense(date=D(2024, 3, 20), extra={"payment": "02/04/2024 transferencia"})
    out = run(aecid(), [(exp, [seg(2, tipo="transferencia", fecha=D(2024, 6, 1))])])
    assert "DATE_MISMATCH" not in codes(out)


# --- soft severity for doubtful readings (same criterion as AMOUNT_MISMATCH) ---


def _by_code(out, code):
    return [f for f in out if f.rule_code == code]


def test_trusted_link_softens_stamp_payment_and_date_findings():
    exp = expense(
        date=D(2024, 3, 20), link_confidence="high", extra={"payment": "02/04/2024 transferencia"}
    )
    linked = [seg(fecha=D(2024, 3, 1))]  # no stamp, date off by 19 days, no bank proof
    out = run(xunta(), [(exp, linked)])
    for code in ("DONOR_STAMP_MISSING", "PAYMENT_EVIDENCE_MISSING", "DATE_MISMATCH"):
        [finding] = _by_code(out, code)
        assert finding.severity == Severity.LOW, code
        assert finding.message.startswith("Revisar la lectura: "), code
        assert finding.evidence["lectura_dudosa"] is True
        assert "confianza alta/media" in finding.message


def test_poor_ocr_softens_even_without_a_trusted_link():
    exp = expense(date=D(2024, 3, 20), link_confidence="low")
    poor = seg(fecha=D(2024, 3, 1), confianza_ocr=40.0)
    out = run(xunta(), [(exp, [poor])])
    for code in ("DONOR_STAMP_MISSING", "PAYMENT_EVIDENCE_MISSING", "DATE_MISMATCH"):
        [finding] = _by_code(out, code)
        assert finding.severity == Severity.LOW
        assert "poco fiable (40 %)" in finding.message


def test_reliable_reading_keeps_the_rule_severity():
    exp = expense(date=D(2024, 3, 20), link_confidence="low")
    clean = seg(fecha=D(2024, 3, 1), confianza_ocr=95.0)
    out = run(xunta(), [(exp, [clean])])
    rules = xunta()
    for code in ("DONOR_STAMP_MISSING", "PAYMENT_EVIDENCE_MISSING", "DATE_MISMATCH"):
        [finding] = _by_code(out, code)
        assert finding.severity == rules.rules[code].severity
        assert not finding.message.startswith("Revisar la lectura")
        assert finding.evidence["lectura_dudosa"] is False


def test_softening_thresholds_come_from_the_yaml():
    exp = expense(link_confidence="high")
    rules = xunta()
    for code in ("DONOR_STAMP_MISSING", "PAYMENT_EVIDENCE_MISSING"):
        rules.rules[code].params["soft_link_confidences"] = []  # switch the link criterion off
        rules.rules[code].params["soft_below_ocr_confidence"] = 0
    out = run(rules, [(exp, [seg(confianza_ocr=10.0)])])
    assert all(
        f.severity != Severity.LOW
        for f in out
        if f.rule_code in ("DONOR_STAMP_MISSING", "PAYMENT_EVIDENCE_MISSING")
    )
    for code in ("DONOR_STAMP_MISSING", "PAYMENT_EVIDENCE_MISSING", "DATE_MISMATCH"):
        params = xunta().params(code)
        assert params["soft_below_ocr_confidence"] == 70
        assert params["soft_link_confidences"] == ["high", "medium"]


# --- DATE_OUT_OF_PERIOD ---

PERIOD = (D(2024, 1, 1), D(2024, 12, 31))


def _period_findings(out):
    return [f for f in out if f.rule_code == "DATE_OUT_OF_PERIOD"]


def test_payment_after_the_closing_date_is_flagged():
    exp = expense(date=D(2024, 12, 20), extra={"payment": "15/01/2025 y con transferencia"})
    out = run(xunta(), [(exp, [seg(fecha=D(2024, 12, 20))])], period=PERIOD)
    [finding] = _period_findings(out)
    assert "de pago" in finding.message and "posterior al cierre" in finding.message
    assert "15/01/2025" in finding.message and "01/01/2024 - 31/12/2024" in finding.message
    assert finding.severity == Severity.MEDIUM
    assert finding.evidence["posicion"] == "despues"
    assert finding.evidence["referencia"]


def test_document_date_outside_the_period_and_bank_proof_date():
    exp = expense(date=D(2023, 12, 20))
    linked = [seg(fecha=D(2023, 12, 20)), seg(2, tipo="transferencia", fecha=D(2025, 2, 1))]
    out = _period_findings(run(aecid(), [(exp, linked)], period=PERIOD))
    assert {f.evidence["tipo_fecha"] for f in out} == {"del documento", "de pago"}
    assert {f.evidence["posicion"] for f in out} == {"antes", "despues"}


def test_dates_inside_the_period_and_the_grace_days_are_fine():
    exp = expense(extra={"payment": "31/12/2024 transferencia"})
    inside = run(xunta(), [(exp, [seg(fecha=D(2024, 6, 1))])], period=PERIOD)
    assert not _period_findings(inside)
    edge = expense(extra={"payment": "02/01/2025 transferencia"})
    assert _period_findings(run(xunta(), [(edge, [seg()])], period=PERIOD))
    rules = xunta()
    rules.rules["DATE_OUT_OF_PERIOD"].params["payment_grace_days"] = 5
    assert not _period_findings(run(rules, [(edge, [seg()])], period=PERIOD))


def test_without_a_period_the_rule_does_not_apply():
    exp = expense(extra={"payment": "15/01/2030 transferencia"})
    assert not _period_findings(run(xunta(), [(exp, [seg(fecha=D(2030, 1, 1))])], period=None))
    open_ended = (D(2024, 1, 1), None)  # only a start date: only "antes" can happen
    assert not _period_findings(run(xunta(), [(exp, [seg()])], period=open_ended))


def test_ocr_dates_outside_the_period_are_soft_when_the_reading_is_doubtful():
    exp = expense(date=D(2024, 5, 1), link_confidence="high")
    out = _period_findings(run(xunta(), [(exp, [seg(fecha=D(2023, 5, 1))])], period=PERIOD))
    [finding] = out
    assert finding.severity == Severity.LOW and finding.message.startswith("Revisar la lectura")


def test_project_period_comes_from_the_project_dates():
    from app.models import Project

    assert ck.project_period(Project(start_date=None, end_date=None)) is None
    open_end = Project(start_date=D(2024, 1, 1), end_date=None)
    assert ck.project_period(open_end) == (D(2024, 1, 1), None)
    assert ck.project_period(Project(start_date=D(2024, 1, 1), end_date=D(2024, 12, 31))) == PERIOD


# --- DONOR_STAMP_MISSING ---


def test_stamp_missing_incomplete_wrong_percentage_and_ok():
    exp = expense(extra={"pct_imputed": 0.5})
    none = run(xunta(), [(exp, [seg()])])
    assert (
        "no se ha detectado el sello"
        in next(f.message for f in none if f.rule_code == "DONOR_STAMP_MISSING").lower()
    )
    bare = seg(sello_donante=True)
    msg = next(
        f.message for f in run(xunta(), [(exp, [bare])]) if f.rule_code == "DONOR_STAMP_MISSING"
    )
    assert "expediente" in msg and "porcentaje" in msg
    wrong = seg(sello_donante=True, sello_expediente="PR811A", sello_porcentaje=100.0)
    msg = next(
        f.message for f in run(xunta(), [(exp, [wrong])]) if f.rule_code == "DONOR_STAMP_MISSING"
    )
    assert "100 %" in msg and "50 %" in msg
    good = seg(sello_donante=True, sello_expediente="PR811A", sello_porcentaje=50.0)
    assert "DONOR_STAMP_MISSING" not in codes(run(xunta(), [(exp, [good])]))


def test_stamp_in_another_document_of_the_same_pdf_is_a_low_finding():
    exp = expense(extra={"pct_imputed": 1.0})
    stamped_voucher = seg(
        2, tipo="otro", sello_donante=True, sello_expediente="X1", sello_porcentaje=100
    )
    out = run(xunta(), [(exp, [seg()])], bundle_extra=[stamped_voucher])
    (f,) = [f for f in out if f.rule_code == "DONOR_STAMP_MISSING"]
    assert f.severity == Severity.LOW and "otro documento del mismo PDF" in f.message


def test_aecid_stamp_needs_percentage_only_when_partial_and_cofinanced_is_skipped():
    full = expense(amount_donor=Decimal("120"))
    assert expected_ok(full)
    partial = expense(amount_donor=Decimal("60"))
    stamped = seg(sello_donante=True, sello_expediente="22-PY-136")
    out = run(aecid(), [(full, [stamped]), (expense(2, amount_donor=Decimal("60")), [stamped])])
    flagged = {f.expense_id for f in out if f.rule_code == "DONOR_STAMP_MISSING"}
    assert flagged == {2}  # partial imputation without % on the stamp
    cofin = expense(3, amount_donor=Decimal(0), cofinanced=True)
    assert "DONOR_STAMP_MISSING" not in codes(run(aecid(), [(cofin, [seg()])]))
    assert partial.amount_donor == 60


def expected_ok(exp):
    return ck.expected_percent(exp) == 100


# --- NOT_AN_INVOICE ---


def test_receipt_proforma_and_missing_invoice_fields():
    exp = expense()
    receipt = run(xunta(), [(exp, [seg(tipo="recibo")])])
    assert "es un recibo, no una factura" in next(
        f.message for f in receipt if f.rule_code == "NOT_AN_INVOICE"
    )
    proforma = run(xunta(), [(exp, [seg(extra={"proforma": True})])])
    assert any("proforma" in f.message for f in proforma if f.rule_code == "NOT_AN_INVOICE")
    only_bank = run(xunta(), [(exp, [seg(tipo="transferencia")])])
    assert "no incluyen ninguna factura" in next(
        f.message for f in only_bank if f.rule_code == "NOT_AN_INVOICE"
    )
    incomplete = run(xunta(), [(exp, [seg(emisor="ACME", fecha=D(2024, 1, 1))])])
    (f,) = [f for f in incomplete if f.rule_code == "NOT_AN_INVOICE"]
    assert f.severity == Severity.LOW and "el nº de factura" in f.message
    complete = seg(emisor="ACME", fecha=D(2024, 1, 1), numero_factura="A-123")
    assert "NOT_AN_INVOICE" not in codes(run(xunta(), [(exp, [complete])]))


def test_invoice_in_the_same_pdf_makes_a_linked_receipt_fine_and_personnel_is_exempt():
    exp = expense()
    out = run(xunta(), [(exp, [seg(tipo="recibo")])], bundle_extra=[seg(2, tipo="factura")])
    assert "NOT_AN_INVOICE" not in codes(out)
    payroll = expense(partida_code="A.V.2", concept="Salario bruto")
    assert "NOT_AN_INVOICE" not in codes(run(xunta(), [(payroll, [seg(tipo="nomina")])]))


# --- PAYMENT_EVIDENCE_MISSING / CASH_OVER_LIMIT ---


def test_payment_evidence_by_method():
    bank = seg(2, tipo="transferencia")
    by_transfer = expense(extra={"payment": "01/02/2024 y con transferencia bancaria"})
    out = run(xunta(), [(by_transfer, [seg()])])
    assert "justificante bancario" in next(
        f.message for f in out if f.rule_code == "PAYMENT_EVIDENCE_MISSING"
    )
    assert "PAYMENT_EVIDENCE_MISSING" not in codes(run(xunta(), [(by_transfer, [seg(), bank])]))
    cash = expense(extra={"payment": "01/02/2024 y en efectivo"})
    assert "sello de «pagado»" in next(
        f.message
        for f in run(xunta(), [(cash, [seg()])])
        if f.rule_code == "PAYMENT_EVIDENCE_MISSING"
    )
    paid = seg(prueba_pago=True)
    assert "PAYMENT_EVIDENCE_MISSING" not in codes(run(xunta(), [(cash, [paid])]))
    unknown = expense()
    assert "No se ha encontrado la prueba de pago" in next(
        f.message
        for f in run(xunta(), [(unknown, [seg()])])
        if f.rule_code == "PAYMENT_EVIDENCE_MISSING"
    )
    indirect = expense(partida_code="B")
    assert "PAYMENT_EVIDENCE_MISSING" not in codes(run(xunta(), [(indirect, [seg()])]))


def test_aecid_cash_without_paid_stamp_is_accepted_but_bank_proof_is_required_for_transfers():
    cash = expense(extra={"payment": "efectivo"})
    assert "PAYMENT_EVIDENCE_MISSING" not in codes(run(aecid(), [(cash, [seg()])]))
    transfer = expense(extra={"payment": "transferencia"})
    assert "PAYMENT_EVIDENCE_MISSING" in codes(run(aecid(), [(transfer, [seg()])]))


def test_cash_limit_depends_on_the_donor():
    big = expense(amount_eur=Decimal("1000"), extra={"payment": "x y en efectivo"})
    assert "CASH_OVER_LIMIT" in codes(run(xunta(), [(big, [seg(prueba_pago=True)])]))
    below = expense(amount_eur=Decimal("999.99"), extra={"payment": "en efectivo"})
    assert "CASH_OVER_LIMIT" not in codes(run(xunta(), [(below, [seg(prueba_pago=True)])]))
    # AECID: "superiores a 2.500 euros": 2.500 exactly is allowed, 2.500,01 is not
    edge = expense(amount_eur=Decimal("2500"), extra={"payment": "efectivo"})
    over = expense(2, amount_eur=Decimal("2500.01"), extra={"payment": "efectivo"})
    out = run(aecid(), [(edge, [seg()]), (over, [seg(2, doc="d2")])])
    assert [f.expense_id for f in out if f.rule_code == "CASH_OVER_LIMIT"] == [2]
    f = next(f for f in run(xunta(), [(big, [seg()])]) if f.rule_code == "CASH_OVER_LIMIT")
    assert f.severity == Severity.HIGH and "1.000 euros o más" in f.message


def test_cash_limit_also_applies_without_documents():
    big = expense(amount_eur=Decimal("5000"), extra={"payment": "en efectivo"})
    assert codes(run(xunta(), [(big, [])])) == ["CASH_OVER_LIMIT"]


# --- ILLEGIBLE / DOC_INCOMPLETE ---


def test_illegible_by_flag_or_low_confidence():
    out = run(xunta(), [(expense(), [seg(legible=False, confianza_ocr=20.0)])])
    f = next(f for f in out if f.rule_code == "ILLEGIBLE")
    assert "no se lee bien" in f.message and "20 %" in f.message
    low = run(xunta(), [(expense(), [seg(confianza_ocr=30.0)])])
    assert "ILLEGIBLE" in codes(low)
    assert "ILLEGIBLE" not in codes(run(xunta(), [(expense(), [seg(confianza_ocr=80.0)])]))


def test_payroll_needs_contract_payment_and_contribution():
    payroll = expense(partida_code="A.V.2", concept="Salarios brutos Junio", issuer="JRS")
    only_slip = run(xunta(), [(payroll, [seg(tipo="nomina")])])
    (f,) = [f for f in only_slip if f.rule_code == "DOC_INCOMPLETE"]
    assert "contrato de trabajo" in f.message and "justificante de pago" in f.message
    assert "cotización" in f.message and set(f.evidence["falta"]) == {
        "contrato",
        "pago",
        "cotizacion",
    }
    complete = [
        seg(1, tipo="nomina"),
        seg(2, tipo="contrato"),
        seg(3, tipo="transferencia"),
        seg(4, tipo="otro", extra={"cotizacion": True}),
    ]
    assert "DOC_INCOMPLETE" not in codes(run(xunta(), [(payroll, [complete[0]])], complete[1:]))
    partly = run(xunta(), [(payroll, [complete[0]])], complete[1:3])
    assert "falta: pago o boletín de cotización" in next(
        f.message for f in partly if f.rule_code == "DOC_INCOMPLETE"
    ) or "cotización" in next(f.message for f in partly if f.rule_code == "DOC_INCOMPLETE")


def test_payroll_rule_ignores_other_personnel_lines_and_refreshments_need_attendance():
    insurance = expense(partida_code="A.V.2", concept="Seguro médico NSSF Junio")
    assert "DOC_INCOMPLETE" not in codes(run(xunta(), [(insurance, [seg(tipo="recibo")])]))
    snacks = expense(partida_code="A.VIII", concept="Refrigerios diálogo comunitario")
    out = run(xunta(), [(snacks, [seg(tipo="factura")])])
    assert "lista de asistencia firmada" in next(
        f.message for f in out if f.rule_code == "DOC_INCOMPLETE"
    )
    with_list = run(xunta(), [(snacks, [seg(tipo="factura")])], [seg(2, tipo="lista_asistencia")])
    assert "DOC_INCOMPLETE" not in codes(with_list)
    aecid_snacks = expense(partida_code="A.I.5", concept="Meriendas y refrigerios del taller")
    assert "lista de participantes" in next(
        f.message
        for f in run(aecid(), [(aecid_snacks, [seg()])])
        if f.rule_code == "DOC_INCOMPLETE"
    )


# --- DUPLICATE_INVOICE ---


def test_same_invoice_in_two_pdfs_is_reported_for_both_expenses():
    a = seg(1, "pdfA", numero_factura="UGA-24-0446", emisor="Proveedor Uno S.A.")
    b = seg(2, "pdfB", numero_factura="UGA 24 0446", emisor="PROVEEDOR UNO")
    other = seg(3, "pdfC", numero_factura="UGA-24-0446", emisor="Otro Distinto Ltd")
    out = run(xunta(), [(expense(1), [a]), (expense(2), [b]), (expense(3), [other])])
    dup = [f for f in out if f.rule_code == "DUPLICATE_INVOICE"]
    assert {f.expense_id for f in dup} == {1, 2}  # same number, different issuer: not a duplicate
    assert "aparece en 2 documentos distintos" in dup[0].message


def test_one_invoice_charged_above_its_amount():
    shared = seg(1, importe=500000, tipo="factura")
    e1, e2 = expense(1, amount_local=Decimal(500000)), expense(2, amount_local=Decimal(500000))
    out = run(xunta(), [(e1, [shared]), (e2, [shared])])
    assert "doble imputación" in next(f.message for f in out if f.rule_code == "DUPLICATE_INVOICE")
    split = [expense(1, amount_local=Decimal(300000)), expense(2, amount_local=Decimal(200000))]
    ok = run(xunta(), [(split[0], [shared]), (split[1], [shared])])
    assert "DUPLICATE_INVOICE" not in codes(ok)


def test_listing_duplicates_need_same_concept_but_aecid_skips_them():
    extra = {"invoice_no": "EB UGA04 064"}
    mk = lambda i, concept: expense(  # noqa: E731
        i,
        extra=extra,
        issuer="JRS",
        concept=concept,
        amount_eur=Decimal("131.94"),
        source_row=40 + i,
    )
    same = run(xunta(), [(mk(1, "Salario Guardia"), []), (mk(2, "Salario Guardia"), [])])
    dup = [f for f in same if f.rule_code == "DUPLICATE_INVOICE"]
    assert len(dup) == 2 and "fila 41" in dup[1].message
    different = run(xunta(), [(mk(1, "Salario Guardia PI"), []), (mk(2, "Salario Guardia ST"), [])])
    assert "DUPLICATE_INVOICE" not in codes(different)
    assert "DUPLICATE_INVOICE" not in codes(run(aecid(), [(mk(1, "x"), []), (mk(2, "x"), [])]))


# --- database ---


def _doc(session, project, name, sha_char):
    doc = Document(project_id=project.id, path=name, filename=name, sha256=sha_char * 64)
    session.add(doc)
    session.flush()
    return doc


def _segmento(session, doc, **kw) -> Segmento:
    row = Segmento(
        sha256=doc.sha256,
        archivo=doc.filename,
        backend="tesseract",
        pagina_inicio=kw.pop("pagina_inicio", 1),
        pagina_fin=kw.pop("pagina_fin", 1),
        tipo=kw.pop("tipo", TipoSegmento.FACTURA),
        document_id=doc.id,
        **kw,
    )
    session.add(row)
    session.flush()
    return row


def test_sync_rules_is_idempotent_and_updates_changes(db_session):
    first = sync_rules(db_session, "xunta")
    assert first == len(CHECK_CODES)
    assert sync_rules(db_session, "xunta") == 0
    row = db_session.scalar(select(Rule).where(Rule.code == "CASH_OVER_LIMIT"))
    row.severity = Severity.LOW
    db_session.flush()
    assert sync_rules(db_session, "xunta") == 1
    assert (
        db_session.scalar(select(Rule).where(Rule.code == "CASH_OVER_LIMIT")).severity
        == Severity.HIGH
    )


def test_run_checks_end_to_end_keeps_reviewer_decisions(db_session):
    project = new_project(db_session)
    exp = add_expense(
        db_session,
        project,
        "10",
        1,
        amount_local=Decimal("500000"),
        amount_eur=Decimal("120"),
        currency="UGX",
        date=D(2024, 2, 1),
        partida_code="A.VI",
        issuer="Proveedor Uno",
        extra={"payment": "05/02/2024 y con transferencia bancaria"},
    )
    doc = _doc(db_session, project, "10. Servicio.pdf", "a")
    invoice = _segmento(
        db_session,
        doc,
        importe=Decimal("450000"),
        moneda="UGX",
        fecha=D(2024, 2, 1),
        emisor="Proveedor Uno",
        numero_factura="F-001",
        pagina_inicio=1,
        pagina_fin=1,
        extra={"importes": ["450000"]},
    )
    db_session.add(
        ExpenseSegment(
            expense_id=exp.id,
            segmento_id=invoice.id,
            score=0.9,
            confidence=Confidence.HIGH,
            reason="x.",
        )
    )
    db_session.flush()

    findings = ck.run_checks(db_session, project.id)
    rules = {f.rule_code for f in findings}
    assert {"AMOUNT_MISMATCH", "DONOR_STAMP_MISSING", "PAYMENT_EVIDENCE_MISSING"} <= rules
    stored = list(db_session.scalars(select(Finding).where(Finding.rule_code.in_(CHECK_CODES))))
    amount = next(f for f in stored if f.rule_code == "AMOUNT_MISMATCH")
    assert amount.expense_id == exp.id and amount.document_id == doc.id
    assert amount.severity == Severity.HIGH and "450.000" in amount.message

    amount.status = "false_positive"
    db_session.flush()
    ck.run_checks(db_session, project.id)
    again = list(db_session.scalars(select(Finding).where(Finding.rule_code == "AMOUNT_MISMATCH")))
    assert len(again) == 1 and again[0].status.value == "false_positive"


def test_expenses_without_segments_get_no_document_checks(db_session):
    project = new_project(db_session)
    add_expense(db_session, project, "1", 1, amount_local=Decimal("1000"), partida_code="A.VI")
    assert ck.run_checks(db_session, project.id) == []
