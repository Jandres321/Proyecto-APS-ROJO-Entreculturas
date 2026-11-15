"""Hybrid matching: the names propose, the content verifies (invented data).

Pure block first (hybrid_match, build_hybrid_findings, validation metric), then the database.
"""

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.enums import Confidence, Donor, MatchMethod
from app.models import ExpenseDocument, ExpenseSegment, Finding
from app.pipeline import match_content as mc
from app.pipeline import match_hybrid as mh
from app.pipeline.conciliador import NameReconciler
from app.pipeline.match_config import KIND_COFINANCED, KIND_INDIRECT
from app.pipeline.match_eval import truth_by_expense, validate
from app.samples import UGANDA_MATCH_CONFIG
from tests.test_match_content import _doc, _segmento
from tests.test_match_pipeline import add_expense, new_project

D = dt.date
REC = NameReconciler(UGANDA_MATCH_CONFIG["name_reconciler"])
OFF = mh.HybridOptions(reassign=False, disambiguate_low=False, content_fill=False)


def exp(i, concept, amount, **kw) -> mc.ExpenseView:
    return mc.ExpenseView(
        id=i,
        order_no=str(i),
        order_key=kw.pop("order_key", str(i)),
        concept=concept,
        amounts={"local": Decimal(str(amount))},
        **kw,
    )


def seg(i, doc, **kw) -> mc.SegmentView:
    kw.setdefault("tipo", "factura")
    if kw.get("importe") is not None:
        kw["importe"] = Decimal(str(kw["importe"]))
    return mc.SegmentView(id=i, doc_key=doc, **kw)


def run(expenses, segments, names, *, reconciler=REC, options=None, claims=()):
    names = {k: [v] if isinstance(v, str) else v for k, v in names.items()}
    return mh.hybrid_match(
        expenses, segments, names, reconciler, options=options, name_claims=claims
    )


def link(result, expense_id, doc=None):
    found = [
        lk
        for lk in result.links
        if lk.expense_id == expense_id and (doc is None or lk.doc_key == doc)
    ]
    assert len(found) == 1, found
    return found[0]


FUEL = "Combustible motocicleta"
FUEL_NAME = "1. Combustible motocicleta.pdf"


# --- 1. the name proposes, the content verifies ---


def test_a_name_proposal_is_accepted_when_the_ocr_read_nothing():
    res = run([exp(1, FUEL, 500000)], [], {"d1": FUEL_NAME})
    lk = link(res, 1)
    assert (lk.doc_key, lk.origin, lk.primary) == ("d1", mh.Origin.NAME, True)
    assert lk.confidence == Confidence.HIGH and not lk.verified and not lk.contradicted
    assert "No se ha podido leer el contenido" in lk.reason
    assert res.conflicts == []  # an unreadable document is never a conflict


def test_a_document_with_nothing_useful_neither_confirms_nor_contradicts():
    segments = [seg(1, "d1", tipo="otro")]
    lk = link(run([exp(1, FUEL, 500000)], segments, {"d1": FUEL_NAME}), 1)
    assert not lk.verified and not lk.contradicted
    assert "no contradice la propuesta, pero tampoco la confirma" in lk.reason


def test_verified_by_amount_and_issuer():
    e = exp(1, FUEL, 500000, issuer="Total Energies", date=D(2024, 9, 3))
    s = seg(1, "d1", importe=500000, emisor="Total Energies")
    lk = link(run([e], [s], {"d1": FUEL_NAME}), 1)
    assert lk.verified and lk.origin == mh.Origin.NAME
    assert "Verificado por contenido" in lk.reason and "el emisor coincide" in lk.reason
    assert lk.segment_ids == (1,)


def test_verified_by_amount_and_date_when_the_issuer_is_unreadable():
    e = exp(1, FUEL, 500000, date=D(2024, 9, 3))
    s = seg(1, "d1", importe=500000, fecha=D(2024, 9, 4))
    assert link(run([e], [s], {"d1": FUEL_NAME}), 1).verified


def test_the_amount_alone_does_not_verify():
    s = seg(1, "d1", importe=500000)
    lk = link(run([exp(1, FUEL, 500000)], [s], {"d1": FUEL_NAME}), 1)
    assert not lk.verified and lk.confidence == Confidence.HIGH  # still accepted


def test_invoice_number_with_the_amount_verifies():
    e = exp(1, FUEL, 500000, invoice_no="FV-20481")
    s = seg(1, "d1", importe=500000, numero_factura="FV-20481")
    assert link(run([e], [s], {"d1": FUEL_NAME}), 1).verified


def test_incompatible_amounts_are_flagged_but_never_veto_the_proposal():
    # The OCR misreads totals: on the pilot every such case was a correct assignment.
    s = seg(1, "d1", importe=987654)
    res = run([exp(1, FUEL, 500000)], [s], {"d1": FUEL_NAME})
    lk = link(res, 1)
    assert lk.doc_key == "d1" and lk.contradicted and not lk.verified
    assert lk.confidence == Confidence.HIGH  # not demoted
    assert "Ojo: el importe leído" in lk.reason
    assert res.conflicts == []


def test_a_figure_among_the_documents_numbers_is_compatible():
    s = seg(1, "d1", importe=987654, importes=(Decimal(500000),))
    assert not link(run([exp(1, FUEL, 500000)], [s], {"d1": FUEL_NAME}), 1).contradicted


def test_without_a_reconciler_names_are_ignored():
    res = run([exp(1, FUEL, 500000)], [], {"d1": FUEL_NAME}, reconciler=None)
    assert res.links == [] and res.proposals == {}


# --- 2. the content reassigns only with strong evidence for ANOTHER expense ---


def two_expenses():
    return [
        exp(1, FUEL, 500000, issuer="Total Energies"),
        exp(2, "Cemento obra", 900000, issuer="Cementos SA"),
    ]


def test_strong_content_for_another_expense_takes_the_document_away():
    segments = [seg(1, "d1", importe=900000, emisor="Cementos SA")]
    res = run(two_expenses(), segments, {"d1": FUEL_NAME})
    (conflict,) = res.conflicts
    assert (conflict.expense_id, conflict.doc_key, conflict.other_expense_ids) == (1, "d1", [2])
    assert conflict.reassigned
    assert res.links_of(1) == []  # the name was wrong
    moved = link(res, 2, "d1")
    assert moved.origin == mh.Origin.CONTENT and moved.verified
    assert "Reasignado por contenido" in moved.reason


def test_the_amount_alone_for_another_expense_is_not_strong_enough():
    segments = [seg(1, "d1", importe=900000)]  # no issuer, no invoice number
    res = run(two_expenses(), segments, {"d1": FUEL_NAME})
    assert res.conflicts == []
    assert link(res, 1).doc_key == "d1" and link(res, 1).contradicted
    assert res.links_of(2) == []


def test_a_document_that_also_backs_the_proposed_expense_is_kept_for_both():
    segments = [
        seg(1, "d1", importe=500000, emisor="Total Energies"),
        seg(2, "d1", importe=900000, emisor="Cementos SA"),
    ]
    res = run(two_expenses(), segments, {"d1": FUEL_NAME})
    assert res.conflicts == []
    assert link(res, 1, "d1").verified
    assert link(res, 2, "d1").origin == mh.Origin.CONTENT


def test_reassignment_can_be_switched_off():
    segments = [seg(1, "d1", importe=900000, emisor="Cementos SA")]
    res = run(two_expenses(), segments, {"d1": FUEL_NAME}, options=OFF)
    assert res.conflicts == [] and link(res, 1).doc_key == "d1"


# --- 3. ambiguous (LOW) proposals: the content may choose ---


AMBIGUOUS = "Combustible Mombasa Nairobi"
TIED_NAMES = {"a": "Combustible Mombasa.pdf", "b": "Combustible Nairobi.pdf"}


def test_the_content_breaks_a_tie_between_name_candidates():
    e = exp(1, AMBIGUOUS, 700000, issuer="Shell", date=D(2024, 5, 2))
    segments = [seg(1, "b", importe=700000, emisor="Shell")]
    res = run([e], segments, TIED_NAMES)
    assert res.proposals[1].confidence == Confidence.LOW  # by name alone it is a coin toss
    lk = link(res, 1)
    assert lk.doc_key == "b" and lk.confidence == Confidence.MEDIUM and lk.verified
    assert "desempatado" in lk.reason


def test_a_tie_the_content_cannot_settle_stays_low():
    e = exp(1, AMBIGUOUS, 700000)
    res = run([e], [], TIED_NAMES)
    lk = link(res, 1)
    assert lk.confidence == Confidence.LOW and not lk.verified
    assert lk.doc_key == res.proposals[1].best.doc.doc_key  # the conciliador's best guess


def test_the_tie_break_can_be_switched_off():
    e = exp(1, AMBIGUOUS, 700000, issuer="Shell")
    segments = [seg(1, "b", importe=700000, emisor="Shell")]
    res = run([e], segments, TIED_NAMES, options=OFF)
    lk = link(res, 1)
    assert lk.doc_key == res.proposals[1].best.doc.doc_key and lk.confidence != Confidence.MEDIUM


# --- 4. nobody proposed it: only solid content assigns ---


def test_strong_content_assigns_an_expense_nobody_proposed():
    e = exp(1, "Gasto sin parecido alguno", 345000, issuer="Proveedor Raro")
    segments = [seg(1, "x", importe=345000, emisor="Proveedor Raro", fecha=D(2024, 1, 1))]
    res = run([e], segments, {"x": "Escaneo 0001.pdf"})
    lk = link(res, 1)
    assert lk.origin == mh.Origin.CONTENT and lk.verified and lk.doc_key == "x"
    assert "Asignado por contenido" in lk.reason


def test_weak_content_does_not_assign():
    e = exp(1, "Gasto sin parecido alguno", 345000)
    res = run([e], [seg(1, "x", importe=345000)], {"x": "Escaneo 0001.pdf"})
    assert res.links == []


def test_content_assignment_can_be_switched_off():
    e = exp(1, "Gasto sin parecido alguno", 345000, issuer="Proveedor Raro")
    segments = [seg(1, "x", importe=345000, emisor="Proveedor Raro")]
    assert run([e], segments, {"x": "Escaneo.pdf"}, options=OFF).links == []


def test_two_equally_good_documents_are_not_guessed():
    e = exp(1, "Gasto sin parecido alguno", 345000, issuer="Proveedor Raro")
    segments = [
        seg(1, "x", importe=345000, emisor="Proveedor Raro"),
        seg(2, "y", importe=345000, emisor="Proveedor Raro"),
    ]
    assert run([e], segments, {"x": "A.pdf", "y": "B.pdf"}).links == []


def test_a_separate_proof_of_payment_is_an_extra_document():
    e = exp(1, FUEL, 500000, issuer="Total Energies")
    segments = [
        seg(1, "d1", importe=500000, emisor="Total Energies"),
        seg(2, "d2", tipo="transferencia", importe=500000, emisor="Total Energies"),
    ]
    res = run([e], segments, {"d1": FUEL_NAME, "d2": "Escaneo 0002.pdf"})
    assert link(res, 1, "d1").primary and not link(res, 1, "d2").primary
    assert res.primary_doc(1) == "d1"


# --- 5. which segments are linked (the document checks run on them) ---


def test_the_segments_that_back_the_expense_are_the_ones_linked():
    segments = [
        seg(1, "d1", importe=500000, emisor="Total Energies"),
        seg(2, "d1", importe=123456, emisor="Otro"),  # another invoice in the same PDF
    ]
    e = exp(1, FUEL, 500000, issuer="Total Energies")
    assert link(run([e], segments, {"d1": FUEL_NAME}), 1).segment_ids == (1,)


def test_without_content_support_the_invoice_like_segments_are_linked():
    segments = [seg(1, "d1", importe=123456), seg(2, "d1", tipo="contrato")]
    lk = link(run([exp(1, FUEL, 500000)], segments, {"d1": FUEL_NAME}), 1)
    assert lk.segment_ids == (1,)  # so AMOUNT_MISMATCH & co. have something to look at


# --- 6. conflicts: only when the content strongly says another expense ---


def test_order_number_claims_are_reported_only_against_strong_content():
    expenses = two_expenses()
    claims = {(1, "d1")}
    strong = [seg(1, "d1", importe=900000, emisor="Cementos SA")]
    res = run(expenses, strong, {"d1": "Escaneo.pdf"}, reconciler=None, claims=claims)
    (conflict,) = res.conflicts
    assert (conflict.expense_id, conflict.reassigned) == (1, False)
    assert res.links_of(1) == []  # a claim by order number is never used to assign

    unreadable = run(expenses, [], {"d1": "Escaneo.pdf"}, reconciler=None, claims=claims)
    assert unreadable.conflicts == []  # the OCR read nothing: no basis for a conflict
    weak = run(
        expenses, [seg(1, "d1", importe=900000)], {"d1": "x.pdf"}, reconciler=None, claims=claims
    )
    assert weak.conflicts == []


# --- findings ---


def findings(result, expenses, segments, config=None, names=None):
    return mh.build_hybrid_findings(
        result, expenses, segments, config or UGANDA_MATCH_CONFIG, names or {}, {}
    )


def rules(fs):
    return sorted(f.rule_code for f in fs)


def test_doc_missing_when_the_folder_is_complete():
    expenses = [exp(1, "Gasto uno", 111111), exp(2, "Gasto dos", 222222)]
    res = run(expenses, [], {})
    fs, not_in_sample = findings(res, expenses, [])
    assert [f.expense_id for f in fs if f.rule_code == mc.RULE_DOC_MISSING] == [1, 2]
    assert not_in_sample == 0


def test_doc_missing_becomes_informative_when_the_folder_is_only_a_sample():
    expenses = [exp(1, "Gasto uno", 111111), exp(2, "Gasto dos", 222222)]
    res = run(expenses, [], {})
    sample = {**UGANDA_MATCH_CONFIG, "carpeta_completa": False}
    fs, not_in_sample = findings(res, expenses, [], sample)
    assert mc.RULE_DOC_MISSING not in rules(fs)
    assert not_in_sample == 2


def test_doc_missing_ignores_indirect_and_cofinanced_expenses():
    expenses = [
        exp(1, "Normal", 111111),
        exp(2, "Indirecto", 222222, kind=KIND_INDIRECT),
        exp(3, "Cofinanciado", 333333, kind=KIND_COFINANCED),
    ]
    fs, _ = findings(run(expenses, [], {}), expenses, [])
    assert [f.expense_id for f in fs] == [1]
    sample = {**UGANDA_MATCH_CONFIG, "carpeta_completa": False}
    assert findings(run(expenses, [], {}), expenses, [], sample)[1] == 1  # only the normal one


def test_name_content_conflict_finding_names_the_other_expense():
    expenses = two_expenses()
    segments = [seg(1, "d1", importe=900000, emisor="Cementos SA")]
    res = run(expenses, segments, {"d1": FUEL_NAME})
    fs, _ = findings(res, expenses, segments, names={"d1": [FUEL_NAME]})
    (conflict,) = [f for f in fs if f.rule_code == mc.RULE_NAME_CONTENT_CONFLICT]
    assert conflict.expense_id == 1 and "nº 2" in conflict.message
    assert "Se ha reasignado" in conflict.message and conflict.evidence["reassigned"]


def test_an_unreadable_document_never_raises_a_name_content_conflict():
    expenses = two_expenses()
    res = run(expenses, [], {"d1": FUEL_NAME})
    fs, _ = findings(res, expenses, [])
    assert mc.RULE_NAME_CONTENT_CONFLICT not in rules(fs)


def test_orphan_only_when_the_document_backs_no_expense():
    expenses = [exp(1, FUEL, 500000, issuer="Total Energies")]
    segments = [
        seg(1, "d1", importe=500000, emisor="Total Energies"),
        seg(2, "d1", tipo="lista_asistencia"),  # another page of a linked PDF: not an orphan
        seg(3, "d2", importe=4242424, emisor="Nadie"),  # backs nothing
        seg(4, "d2", tipo="otro"),  # cover sheet: never an orphan
    ]
    res = run(expenses, segments, {"d1": FUEL_NAME, "d2": "Huerfano.pdf"})
    fs, _ = findings(res, expenses, segments)
    orphans = [f for f in fs if f.rule_code == mc.RULE_SEGMENT_ORPHAN]
    assert [f.evidence["segment_id"] for f in orphans] == [3]


def test_unresolved_name_ties_are_reported_as_ambiguous():
    expenses = [exp(1, AMBIGUOUS, 700000)]
    res = run(expenses, [], TIED_NAMES)
    fs, _ = findings(
        res,
        expenses,
        [],
        names={"a": ["Combustible Mombasa.pdf"], "b": ["Combustible Nairobi.pdf"]},
    )
    (amb,) = [f for f in fs if f.rule_code == mc.RULE_MATCH_AMBIGUOUS]
    assert amb.expense_id == 1 and amb.evidence["kind"] == "name"
    assert "Combustible" in amb.message


def test_a_tie_settled_by_the_content_raises_no_ambiguity():
    e = exp(1, AMBIGUOUS, 700000, issuer="Shell")
    segments = [seg(1, "b", importe=700000, emisor="Shell")]
    res = run([e], segments, TIED_NAMES)
    fs, _ = findings(res, [e], segments)
    assert mc.RULE_MATCH_AMBIGUOUS not in rules(fs)


def test_order_number_written_on_the_document_that_is_not_the_expenses():
    e = exp(1, FUEL, 500000, issuer="Total Energies")
    segments = [seg(1, "d1", importe=500000, emisor="Total Energies", numero_orden="7")]
    res = run([e], segments, {"d1": FUEL_NAME})
    fs, _ = findings(res, [e], segments)
    (mismatch,) = [f for f in fs if f.rule_code == mc.RULE_ORDER_NUMBER_MISMATCH]
    assert mismatch.evidence["written_order"] == "7"


# --- validation metric (same as the conciliador's --validar) ---


def test_validation_verdicts_and_accuracy():
    expenses = [exp(i, "x", 1) for i in range(1, 7)] + [exp(7, "sin orden", 1, order_key=None)]
    truth = truth_by_expense({(1, "a"), (2, "b"), (3, "c"), (4, "d")})
    assigned = {1: "a", 2: "WRONG", 3: None, 4: "d", 5: "z", 6: None}
    v = validate(expenses, assigned, truth, {1: "high", 2: "low", 4: "medium", 5: "low"})
    assert v.evaluable == 6  # the expense without order number is not evaluable
    assert dict(v.counts) == {
        "correcto": 2,
        "incorrecto": 1,
        "no_encontrado": 1,
        "falso_positivo": 1,
        "correcto_sin_documento": 1,
    }
    assert v.correct == 3 and v.accuracy == 0.5
    assert v.assigned == 4 and v.precision == 0.5
    assert v.by_confidence == {"high": [1, 0], "low": [0, 2], "medium": [1, 0]}


# --- database ---


def link_rows(session):
    return list(session.scalars(select(ExpenseDocument).order_by(ExpenseDocument.expense_id)))


def rule_findings(session, rule):
    return list(session.scalars(select(Finding).where(Finding.rule_code == rule)))


def test_the_hybrid_is_persisted_with_confidence_verification_and_reason(db_session):
    project = new_project(db_session)
    e = add_expense(
        db_session, project, "10", 1, concept=FUEL, issuer="Total Energies",
        amount_local=Decimal("500000"), currency="UGX",
    )  # fmt: skip
    doc = _doc(db_session, project, "10. Combustible motocicleta.pdf", "a")
    s = _segmento(db_session, doc, importe=Decimal("500000"), emisor="Total Energies")

    summary = mc.match_by_segments(db_session, project.id)

    (row,) = link_rows(db_session)
    assert (row.expense_id, row.document_id, row.method) == (e.id, doc.id, MatchMethod.FILENAME)
    assert row.confidence == Confidence.HIGH and row.verified
    assert (
        row.reason.startswith("Propuesto por el nombre")
        and "Verificado por contenido" in row.reason
    )
    (es,) = db_session.scalars(select(ExpenseSegment)).all()
    assert es.segmento_id == s.id and es.evidence["origin"] == "name" and es.evidence["verified"]
    assert (summary.expenses_linked, summary.verified, summary.from_name) == (1, 1, 1)
    assert not rule_findings(db_session, mc.RULE_DOC_MISSING)


def test_a_name_link_to_an_unreadable_document_is_kept_and_is_not_a_missing_document(db_session):
    project = new_project(db_session)
    add_expense(db_session, project, "10", 1, concept=FUEL, amount_local=Decimal("500000"))
    doc = _doc(db_session, project, "10. Combustible motocicleta.pdf", "a")  # no segments at all
    mc.match_by_segments(db_session, project.id)
    (row,) = link_rows(db_session)
    assert row.document_id == doc.id and not row.verified and row.confidence == Confidence.HIGH
    assert not db_session.scalars(select(ExpenseSegment)).all()
    assert not rule_findings(db_session, mc.RULE_DOC_MISSING)
    assert not rule_findings(db_session, mc.RULE_NAME_CONTENT_CONFLICT)


def test_content_reassignment_is_stored_as_a_content_link_and_a_conflict(db_session):
    project = new_project(db_session)
    e1 = add_expense(
        db_session, project, "1", 1, concept=FUEL, issuer="Total Energies",
        amount_local=Decimal("500000"),
    )  # fmt: skip
    e2 = add_expense(
        db_session, project, "2", 2, concept="Cemento obra", issuer="Cementos SA",
        amount_local=Decimal("900000"),
    )  # fmt: skip
    doc = _doc(db_session, project, "1. Combustible motocicleta.pdf", "a")
    _segmento(db_session, doc, importe=Decimal("900000"), emisor="Cementos SA")

    summary = mc.match_by_segments(db_session, project.id)

    (row,) = link_rows(db_session)
    assert (row.expense_id, row.method, row.verified) == (e2.id, MatchMethod.CONTENT, True)
    (conflict,) = rule_findings(db_session, mc.RULE_NAME_CONTENT_CONFLICT)
    assert conflict.expense_id == e1.id and conflict.evidence["content_matches"] == [e2.id]
    assert summary.conflicts == 1
    # e1 lost its document and the folder is complete: it is a missing document now
    assert [f.expense_id for f in rule_findings(db_session, mc.RULE_DOC_MISSING)] == [e1.id]


def test_a_sample_folder_reports_no_doc_missing_but_counts_the_expenses(db_session):
    project = new_project(db_session, donor=Donor.AECID, code="SAMPLE-1")
    project.match_config = {"carpeta_completa": False}  # the folder is only a sample
    for i in (1, 2, 3):
        add_expense(db_session, project, str(i), i, amount_local=Decimal(i * 1000))
    summary = mc.match_by_segments(db_session, project.id)
    assert not rule_findings(db_session, mc.RULE_DOC_MISSING)
    assert summary.not_in_sample == 3 and summary.expenses_linked == 0


def test_rerunning_is_idempotent_and_keeps_the_reviewers_decisions(db_session):
    project = new_project(db_session)
    add_expense(db_session, project, "1", 1, concept="Uno", amount_local=Decimal("111111"))
    add_expense(db_session, project, "2", 2, concept="Dos", amount_local=Decimal("222222"))
    mc.match_by_segments(db_session, project.id)
    missing = rule_findings(db_session, mc.RULE_DOC_MISSING)
    assert len(missing) == 2
    missing[0].status = "false_positive"
    db_session.flush()

    mc.match_by_segments(db_session, project.id)
    again = rule_findings(db_session, mc.RULE_DOC_MISSING)
    assert sorted(f.status.value for f in again) == ["false_positive", "open"]
    assert len(again) == 2


def test_content_only_run_ignores_the_file_names(db_session):
    project = new_project(db_session)
    add_expense(db_session, project, "10", 1, concept=FUEL, amount_local=Decimal("500000"))
    _doc(db_session, project, "10. Combustible motocicleta.pdf", "a")
    mc.match_by_segments(db_session, project.id, use_name=False)
    assert link_rows(db_session) == []


def test_manual_links_survive_and_are_not_duplicated(db_session):
    project = new_project(db_session)
    e = add_expense(db_session, project, "10", 1, concept=FUEL, amount_local=Decimal("500000"))
    doc = _doc(db_session, project, "10. Combustible motocicleta.pdf", "a")
    db_session.add(
        ExpenseDocument(expense_id=e.id, document_id=doc.id, method=MatchMethod.MANUAL, score=1.0)
    )
    db_session.flush()
    mc.match_by_segments(db_session, project.id)
    assert {r.method for r in link_rows(db_session)} == {MatchMethod.MANUAL}


@pytest.mark.parametrize("use_name", [True, False])
def test_every_finding_rule_of_the_matching_is_replaced_not_accumulated(db_session, use_name):
    project = new_project(db_session)
    add_expense(db_session, project, "1", 1, concept="Uno", amount_local=Decimal("111111"))
    for _ in range(3):
        mc.match_by_segments(db_session, project.id, use_name=use_name)
    assert len(rule_findings(db_session, mc.RULE_DOC_MISSING)) == 1
