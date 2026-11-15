"""The name-based conciliador (app/pipeline/conciliador.py) on invented data."""

import datetime as dt

import pytest

from app.enums import Confidence
from app.pipeline import conciliador as c
from app.pipeline.match_content import ExpenseView
from app.samples import UGANDA_MATCH_CONFIG

CONFIG = UGANDA_MATCH_CONFIG["name_reconciler"]
D = dt.date


def exp(i, concept, *, issuer=None, date=None, invoice=None) -> ExpenseView:
    return ExpenseView(
        id=i, order_no=str(i), concept=concept, issuer=issuer, date=date, invoice_no=invoice
    )


@pytest.fixture
def rec() -> c.NameReconciler:
    return c.NameReconciler(CONFIG)


def propose(rec, expenses, names):
    return rec.propose(expenses, {k: [v] if isinstance(v, str) else v for k, v in names.items()})


# --- text helpers ---


def test_months_are_read_from_names_and_dates():
    assert c.extract_months("Combustible Septiembre") == {9}
    assert c.extract_months("NSSF oct 2024 y november") == {10, 11}
    assert c.extract_months("Pago 12.03.24") == {3}
    assert c.extract_months("Factura 31.13.24") == set()  # not a date


def test_references_are_normalized_without_leading_zeros(rec):
    pattern = rec.reference_pattern
    assert c.extract_references("EB UGA04 0602", pattern) == {"EB#602"}
    assert c.extract_references("CV UGA04-113 y EBUGA04-060", pattern) == {"CV#113", "EB#60"}
    assert c.extract_references("Factura 46681", pattern, long_numbers=True) == {"#46681"}
    assert c.extract_references("Factura 46681", pattern) == set()


def test_concept_text_drops_noise_and_unifies_synonyms(rec):
    text = c.concept_text("Pago de Nóminas Septiembre 2024", rec.reference_pattern, rec.synonyms)
    assert text == "pago salario"  # months, years and stop words gone; nomina -> salario
    assert c.root("comisiones") == "comision" and c.root("vehiculos") == "vehiculo"


# --- proposals ---


def test_the_manual_order_number_is_hidden(rec):
    expenses = [
        exp(1, "Combustible septiembre vehículo", issuer="Total Energies"),
        exp(2, "Alquiler sala formación", issuer="Hotel Sol"),
    ]
    names = {
        "d1": "12. Combustible septiembre vehículo.pdf",  # "12" is NOT the expense's number
        "d2": "2. .pdf",  # only the manual number: no information
        "d3": "99. Otro.pdf",
    }
    out = propose(rec, expenses, names)
    assert out[1].doc_key == "d1" and out[1].confidence == Confidence.HIGH
    assert out[2].doc_key is None and not out[2].assigned


def test_prepare_doc_hides_ranges_suffixes_and_extension(rec):
    for name in ("33-39 Salario bruto.pdf", "10.A Salario bruto.xlsx", "49A Salario bruto.pdf"):
        doc = rec.prepare_doc("k", name)
        assert doc.text == "salario bruto", name
    assert rec.prepare_doc("k", "Copia de 5 Salario bruto.pdf").text == "salario bruto"


def test_invoice_reference_in_the_name_is_enough_when_it_identifies_one_document(rec):
    expenses = [exp(1, "Material de oficina", invoice="EB UGA04 602")]
    names = {"d1": "3. Material EB UGA04 602.pdf", "d2": "4. Otra cosa EB UGA04 700.pdf"}
    best = propose(rec, expenses, names)[1]
    assert best.doc_key == "d1"
    assert best.best.score >= 0.62
    assert any("EB-602" in r for r in best.best.reasons)


def test_reference_shared_by_several_documents_does_not_decide_alone(rec):
    expenses = [exp(1, "Seguro médico", invoice="EB UGA04 602")]
    names = {"d1": "1. Seguro médico EB UGA04 602.pdf", "d2": "2. Pension EB UGA04 602.pdf"}
    best = propose(rec, expenses, names)[1].best
    assert any("también en otros documentos" in r for r in best.reasons)


def test_a_different_invoice_number_penalizes(rec):
    expenses = [exp(1, "Material de oficina", invoice="EB UGA04 602")]
    same = propose(rec, expenses, {"d": "Material EB UGA04 602.pdf"})[1].best.score
    other = propose(rec, expenses, {"d": "Material EB UGA04 999.pdf"})[1].best
    assert other.score < same - 0.4
    assert any("distinto" in r for r in other.reasons)


def test_month_of_the_file_must_match_the_expense(rec):
    expenses = [exp(1, "Combustible", date=D(2024, 9, 15))]
    names = {"sep": "Combustible Septiembre.pdf", "oct": "Combustible Octubre.pdf"}
    out = propose(rec, expenses, names)[1]
    assert out.best.doc.doc_key == "sep"
    assert (
        out.best.score - out.second.score >= 0.3
    )  # the other month is penalized, not just ranked lower


def test_social_security_is_paid_the_month_after_it_covers(rec):
    expenses = [exp(1, "NSSF contribution", date=D(2024, 10, 5))]
    names = {"sep": "NSSF Septiembre.pdf", "oct": "NSSF Octubre.pdf"}
    out = propose(rec, expenses, names)[1]
    assert out.best.doc.doc_key == "sep"
    assert any(
        "mes contiguo" not in r and "coincide con el del gasto" in r for r in out.best.reasons
    )


# --- confidence ---


def test_copies_of_the_same_document_are_not_rivals(rec):
    expenses = [exp(1, "Combustible septiembre vehículo", issuer="Total")]
    names = {
        "pdf": "1. Combustible septiembre vehículo.pdf",
        "xlsx": "Copia de 1. Combustible septiembre vehículo.xlsx",
    }
    out = propose(rec, expenses, names)[1]
    assert out.assigned and not out.ambiguous
    assert out.confidence == Confidence.HIGH


def test_two_different_documents_almost_equally_good_are_ambiguous(rec):
    expenses = [exp(1, "Combustible Mombasa Nairobi")]
    names = {"a": "Combustible Mombasa.pdf", "b": "Combustible Nairobi.pdf"}
    out = propose(rec, expenses, names)[1]
    assert out.assigned and out.ambiguous and out.confidence == Confidence.LOW
    assert {cand.doc.doc_key for cand in out.tied} == {"a", "b"}


def test_below_the_threshold_nothing_is_proposed(rec):
    out = propose(rec, [exp(1, "Alquiler de sala")], {"d": "Combustible.pdf"})[1]
    assert not out.assigned and out.confidence is None and out.doc_key is None
    assert out.best is not None  # the best guess is still available for the reviewer


def test_medium_confidence_between_threshold_and_high(rec):
    expenses = [exp(1, "Material de oficina y papelería varia")]
    out = propose(rec, expenses, {"d": "Material oficina.pdf"})[1]
    assert out.assigned and out.confidence == Confidence.MEDIUM
    assert 0.60 <= out.best.score < 0.80


def test_params_can_be_overridden_per_project():
    strict = c.NameReconciler({**CONFIG, "params": {"threshold": 0.95}})
    out = strict.propose([exp(1, "Material de oficina varia")], {"d": ["Material oficina.pdf"]})
    assert not out[1].assigned


def test_names_without_information_are_not_copies_of_each_other(rec):
    """Bare order numbers ("7.pdf") leave no text: such files are distinct documents, while a
    .pdf and a .xlsx with the same descriptive name are still the same one."""
    a, b = rec.prepare_doc("sha-a", "7.pdf"), rec.prepare_doc("sha-b", "8.pdf")
    assert a.text == b.text == "" and a.signature != b.signature
    pdf = rec.prepare_doc("sha-c", "12. Fondo pensiones.pdf")
    xlsx = rec.prepare_doc("sha-d", "12. Fondo pensiones.xlsx")
    assert pdf.signature == xlsx.signature
