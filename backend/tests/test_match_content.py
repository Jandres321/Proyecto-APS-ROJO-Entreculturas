"""Content matching (expense <-> document segments) on invented data."""

import datetime as dt
from decimal import Decimal

from sqlalchemy import select

from app.enums import Confidence, MatchMethod, TipoSegmento
from app.models import Document, ExpenseDocument, ExpenseSegment, Finding, Segmento
from app.pipeline import match_content as mc
from app.samples import UGANDA_MATCH_CONFIG
from tests.test_match_pipeline import add_expense, new_project

D = dt.date


def exp(i, amount, **kw) -> mc.ExpenseView:
    amounts = {"local": Decimal(str(amount))} if amount is not None else {}
    return mc.ExpenseView(id=i, order_no=kw.pop("order_no", str(i)), amounts=amounts, **kw)


def seg(i, doc="d1", **kw) -> mc.SegmentView:
    kw.setdefault("tipo", "factura")
    if "importe" in kw and kw["importe"] is not None:
        kw["importe"] = Decimal(str(kw["importe"]))
    return mc.SegmentView(id=i, doc_key=doc, **kw)


def link_pairs(result):
    return {(lk.expense_id, lk.segment_id) for lk in result.links}


# --- signals ---


def test_amount_invoice_issuer_date_give_high_confidence_with_reason():
    expenses = [
        exp(1, 370000, issuer="Moviter SA", date=D(2023, 5, 21), invoice_no="008-001-0130768"),
        exp(2, 100000, issuer="Otro proveedor", date=D(2023, 6, 1)),
    ]
    segments = [
        seg(
            1,
            importe=370000,
            moneda="PYG",
            emisor="MOVITER S.A.",
            fecha=D(2023, 5, 21),
            numero_factura="008-001-0130768",
        )
    ]
    result = mc.match_segments(expenses, segments)
    (link,) = result.links
    assert (link.expense_id, link.confidence) == (1, Confidence.HIGH)
    assert link.score >= 0.95
    assert "importe" in link.reason and "factura" in link.reason and "emisor" in link.reason
    assert link.reason.endswith(".")


def test_unique_amount_alone_links_with_low_confidence():
    result = mc.match_segments([exp(1, 123456), exp(2, 999)], [seg(1, importe=123456)])
    (link,) = result.links
    assert link.expense_id == 1 and link.confidence == Confidence.LOW
    assert "único" in link.reason


def test_tiny_or_wrong_amount_never_links():
    expenses = [exp(1, 3), exp(2, 5000)]
    assert not mc.match_segments(expenses, [seg(1, importe=3)]).links  # below MIN_AMOUNT
    assert not mc.match_segments(expenses, [seg(2, importe=4000)]).links  # no signal at all


def test_amount_tolerance_is_percent_not_absolute():
    expenses = [exp(1, 1000000)]
    near = mc.match_segments(expenses, [seg(1, importe=1005000, emisor="x" * 5)])
    assert not near.links  # 0.5 % apart: only 70 % of the amount weight, no anchor corroboration
    exact = mc.match_segments(expenses, [seg(1, importe=1000000)])
    assert exact.links


def test_issuer_and_nif_signals():
    e = exp(1, 500000, issuer="Estación de Servicios Yvy", text_blob="RUC 2084321-8")
    s_issuer = seg(1, importe=500000, emisor="Estacion de Servicios YVY S.A.")
    s_nif = seg(2, importe=500000, emisor="zzz", nif="2084321-8")
    assert mc.issuer_quality(s_issuer, e) == (1.0, "emisor")
    assert mc.issuer_quality(s_nif, e) == (1.0, "nif")
    assert mc.issuer_quality(seg(3, emisor="Completamente distinto"), e)[0] == 0.0


def test_order_number_signal_and_conflict():
    e = exp(1, 100000, order_no="CG34", order_key="CG34")
    assert mc.order_quality("CG34", "CG34") == 1.0
    assert mc.order_quality("34", "CG34") == 0.7  # the document shows the number without prefix
    assert mc.order_quality("35", "CG34") == 0.0
    assert mc.has_order_conflict("35", "CG34") and not mc.has_order_conflict("34", "CG34")
    assert not mc.has_order_conflict(None, "CG34")
    # a written order number that disagrees is reported, but the link stays (amount decides)
    result = mc.match_segments([e], [seg(1, importe=100000, numero_orden="35")])
    assert result.order_mismatches == [(1, 1)]


# --- amounts that are not the document's total ---


def test_amount_among_the_figures_needs_corroboration():
    e = exp(1, 1300000, issuer="Proveedor Z", date=D(2024, 10, 30))
    figures = (Decimal(900000), Decimal(1300000))
    alone = seg(1, importe=900000, importes=figures)
    assert mc.best_amount(alone, e) == (0.8, "local", False)
    # not enough by itself: 0.45 * 0.8 = 0.36 < ACCEPT
    assert not mc.match_segments([e], [alone]).links
    # with the issuer and the date it links, and the reason says the amount is not the total
    full = seg(2, importe=900000, importes=figures, emisor="Proveedor Z", fecha=D(2024, 10, 30))
    (link,) = mc.match_segments([e], [full]).links
    assert link.confidence in (Confidence.MEDIUM, Confidence.LOW)
    assert "aparece entre las cifras" in link.reason


def test_figure_shown_by_several_documents_is_weaker():
    e = exp(1, 1300000, issuer="Proveedor Z", date=D(2024, 10, 30))
    figures = (Decimal(1300000),)
    segs = [
        seg(1, "a", importe=5, importes=figures, emisor="Proveedor Z", fecha=D(2024, 10, 30)),
        seg(2, "b", importe=5, importes=figures, emisor="Proveedor Z", fecha=D(2024, 10, 30)),
    ]
    both = mc.match_segments([e], segs)
    single = mc.match_segments([e], segs[:1])
    assert single.links[0].score > max([lk.score for lk in both.links] or [0])


def test_total_match_beats_figure_match():
    e = exp(1, 500000)
    s = seg(1, importe=500000, importes=(Decimal(500000), Decimal(123456)))
    assert mc.best_amount(s, e) == (1.0, "local", True)


# --- N:M assignment, ambiguity ---


def test_one_expense_several_segments_complementary_documents():
    e = exp(1, 800000, issuer="Proveedor X", date=D(2024, 1, 10))
    segments = [
        seg(1, "pdf1", tipo="factura", importe=800000, emisor="Proveedor X", fecha=D(2024, 1, 10)),
        seg(2, "pdf1", tipo="transferencia", importe=800000, fecha=D(2024, 1, 12)),
    ]
    result = mc.match_segments([e], segments)
    assert link_pairs(result) == {(1, 1), (1, 2)}
    assert not result.ambiguities  # invoice + proof of payment are complementary, not rivals


def test_segment_tie_between_expenses_is_ambiguous_and_low():
    expenses = [exp(1, 250000, date=D(2024, 3, 1)), exp(2, 250000, date=D(2024, 3, 1))]
    result = mc.match_segments(expenses, [seg(1, importe=250000, fecha=D(2024, 3, 1))])
    assert link_pairs(result) == {(1, 1), (2, 1)}
    assert all(lk.confidence == Confidence.LOW and lk.ambiguous for lk in result.links)
    assert result.ambiguities and result.ambiguities[0].kind == "segment"
    assert "revisar" in result.links[0].reason


def test_date_breaks_a_tie_between_equal_amounts():
    expenses = [exp(1, 250000, date=D(2024, 3, 1)), exp(2, 250000, date=D(2024, 9, 1))]
    result = mc.match_segments(expenses, [seg(1, importe=250000, fecha=D(2024, 3, 2))])
    assert link_pairs(result) == {(1, 1)}


def test_rival_documents_for_one_expense_are_ambiguous():
    e = exp(1, 640000, date=D(2024, 4, 1), issuer="Proveedor Y")
    segments = [
        seg(1, "pdfA", importe=640000, emisor="Proveedor Y", fecha=D(2024, 4, 1)),
        seg(2, "pdfB", importe=640000, emisor="Proveedor Y", fecha=D(2024, 4, 1)),
    ]
    result = mc.match_segments([e], segments)
    assert link_pairs(result) == {(1, 1), (1, 2)}
    assert all(lk.confidence == Confidence.LOW for lk in result.links)
    assert any(a.kind == "expense" for a in result.ambiguities)


def test_one_document_total_is_the_sum_of_several_expenses():
    expenses = [
        exp(1, 100000, date=D(2024, 5, 1)),
        exp(2, 200000, date=D(2024, 5, 2)),
        exp(3, 300000, date=D(2024, 5, 3)),
        exp(4, 777777, date=D(2024, 5, 3)),
    ]
    segments = [seg(1, "payroll", tipo="nomina", importe=600000, fecha=D(2024, 5, 5))]
    result = mc.match_segments(expenses, segments, group_matching=True)
    assert link_pairs(result) == {(1, 1), (2, 1), (3, 1)}
    assert all(lk.group and lk.confidence == Confidence.MEDIUM for lk in result.links)
    assert "suma de 3 gastos" in result.links[0].reason
    # opt-in: on real data summing subsets of many expenses gave mostly coincidences
    assert not mc.match_segments(expenses, segments).links


def test_ambiguous_sum_is_not_guessed():
    expenses = [exp(i, a, date=D(2024, 5, 1)) for i, a in enumerate([100, 200, 300, 150, 450], 1)]
    expenses = [exp(e.id, e.amounts["local"] * 1000, date=D(2024, 5, 1)) for e in expenses]
    # 300k = 100k+200k = 150k+... several decompositions: no unique solution -> no link
    result = mc.match_segments(expenses, [seg(1, tipo="nomina", importe=300000 + 150000 * 0)])
    assert all(lk.group is False for lk in result.links)


# --- names never leak into content-only runs ---


def test_content_only_result_does_not_depend_on_file_names():
    expenses = [exp(1, 111111, concept="Combustible septiembre"), exp(2, 222222)]
    base = [seg(1, importe=111111, filename="1. Combustible septiembre.pdf")]
    renamed = [seg(1, importe=111111, filename="zzzz_random.pdf")]
    a = mc.match_segments(expenses, base, use_name=False)
    b = mc.match_segments(expenses, renamed, use_name=False)
    assert [(lk.expense_id, lk.score) for lk in a.links] == [
        (lk.expense_id, lk.score) for lk in b.links
    ]


def test_weak_name_signal_adds_at_most_its_weight():
    e = exp(1, 400000, concept="Combustible")
    s = seg(1, importe=400000)
    base = mc.score_pair(s, e, unique_amount=True).score
    with_name = mc.score_pair(s, e, unique_amount=True, name_hit=True, use_name=True).score
    assert 0 < with_name - base <= mc.WEIGHTS["debil"] + 1e-9
    ignored = mc.score_pair(s, e, unique_amount=True, name_hit=True, use_name=False).score
    assert ignored == base


def test_name_claims_come_from_the_filename_rules():
    expenses = [exp(33, 1, order_key="33"), exp(34, 1, order_key="34"), exp(70, 1, order_key="70")]
    names = {"p": ["33-34 Salarios.pdf"], "q": ["70. Combustible.pdf"], "r": ["Sin numero.pdf"]}
    assert mc.compute_name_claims(expenses, names, UGANDA_MATCH_CONFIG) == {
        (33, "p"),
        (34, "p"),
        (70, "q"),
    }


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
        backend=kw.pop("backend", "tesseract"),
        pagina_inicio=kw.pop("pagina_inicio", 1),
        pagina_fin=kw.pop("pagina_fin", 1),
        tipo=kw.pop("tipo", TipoSegmento.FACTURA),
        document_id=doc.id,
        **kw,
    )
    session.add(row)
    session.flush()
    return row


def test_match_by_segments_persists_links_and_findings(db_session):
    project = new_project(db_session)
    e1 = add_expense(
        db_session,
        project,
        "10",
        1,
        amount_local=Decimal("500000"),
        currency="UGX",
        issuer="Proveedor Uno",
        date=D(2024, 2, 1),
    )
    e2 = add_expense(db_session, project, "11", 2, amount_local=Decimal("77777"), currency="UGX")
    e3 = add_expense(  # indirect cost (partida B): needs no receipt
        db_session, project, "12", 3, amount_local=Decimal("5000"), partida_code="B"
    )
    doc_a = _doc(db_session, project, "10. Algo.pdf", "a")
    doc_b = _doc(db_session, project, "99. Huérfano.pdf", "b")
    s1 = _segmento(
        db_session, doc_a, importe=Decimal("500000"), emisor="Proveedor Uno", fecha=D(2024, 2, 1)
    )
    s_orphan = _segmento(db_session, doc_b, importe=Decimal("31415926"))

    summary = mc.match_by_segments(db_session, project.id)
    assert summary.links == 1 and summary.expenses_linked == 1

    (es,) = db_session.scalars(select(ExpenseSegment)).all()
    assert (es.expense_id, es.segmento_id) == (e1.id, s1.id)
    assert es.confidence in (Confidence.HIGH, Confidence.MEDIUM) and es.reason.endswith(".")
    (ed,) = db_session.scalars(select(ExpenseDocument)).all()
    assert (ed.expense_id, ed.document_id, ed.method) == (e1.id, doc_a.id, MatchMethod.CONTENT)

    findings = list(db_session.scalars(select(Finding).where(Finding.project_id == project.id)))
    by_rule = {f.rule_code: f for f in findings}
    assert by_rule[mc.RULE_DOC_MISSING].expense_id == e2.id  # e3 is exempt
    assert e3.id not in {f.expense_id for f in findings}
    assert by_rule[mc.RULE_SEGMENT_ORPHAN].evidence["segment_id"] == s_orphan.id

    # idempotent, and a reviewer decision survives a re-run
    by_rule[mc.RULE_DOC_MISSING].status = "false_positive"
    db_session.flush()
    mc.match_by_segments(db_session, project.id)
    assert db_session.scalars(select(ExpenseSegment)).all().__len__() == 1
    missing = list(
        db_session.scalars(select(Finding).where(Finding.rule_code == mc.RULE_DOC_MISSING))
    )
    assert len(missing) == 1 and missing[0].status.value == "false_positive"


def test_match_by_segments_keeps_manual_links_and_ignores_names_when_asked(db_session):
    project = new_project(db_session)
    e1 = add_expense(db_session, project, "1", 1, amount_local=Decimal("250000"))
    doc = _doc(db_session, project, "1. Pago.pdf", "c")
    _segmento(db_session, doc, importe=Decimal("250000"))
    db_session.add(
        ExpenseDocument(expense_id=e1.id, document_id=doc.id, method=MatchMethod.MANUAL, score=1.0)
    )
    db_session.flush()
    mc.match_by_segments(db_session, project.id, use_name=False)
    methods = {ed.method for ed in db_session.scalars(select(ExpenseDocument))}
    assert methods == {MatchMethod.MANUAL}  # the manual pair is not duplicated by a content link


def test_vision_backend_segments_win_over_tesseract(db_session):
    project = new_project(db_session)
    e1 = add_expense(
        db_session, project, "1", 1, amount_local=Decimal("250000"), issuer="Proveedor Dos"
    )
    doc = _doc(db_session, project, "x.pdf", "d")
    _segmento(db_session, doc, importe=Decimal("1"), backend="tesseract")
    good = _segmento(
        db_session, doc, importe=Decimal("250000"), emisor="Proveedor Dos", backend="llm-fake-p1"
    )
    mc.match_by_segments(db_session, project.id)
    (es,) = db_session.scalars(select(ExpenseSegment)).all()
    assert (es.expense_id, es.segmento_id) == (e1.id, good.id)
