"""The verdict of an expense ("Bien" / "Necesita revisión" / "No aplica") and the reviewer's
decision. The invariant the ONG asked for: nothing is "Bien" unless it was really checked."""

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select

from app.enums import Confidence, Donor, FindingStatus, ReviewDecision, Severity, TipoSegmento
from app.models import Finding
from app.pipeline import match_hybrid as mh
from app.pipeline.checks import run_checks
from app.review import (
    BY_CONTENT,
    BY_NAME,
    MANUAL,
    MISSING,
    NOT_IN_SAMPLE,
    NOT_REQUIRED_COFINANCED,
    VERIFIED,
    DocumentStatus,
)
from app.review_view import RULE_LABELS_ES, project_review
from app.samples import UGANDA_MATCH_CONFIG
from app.schemas import FindingIn
from app.services.findings import sync_findings
from app.verdict import compare_rows, decision_view, reasons_for, verdict_for
from tests.test_match_content import _doc, _segmento
from tests.test_match_pipeline import add_expense, new_project

D = Decimal


def seg(**kw):
    base = dict(
        importe=D("100"), fecha=dt.date(2024, 3, 1), emisor="Proveedor SL", numero_factura="F-1",
        legible=True, confianza_ocr=90.0, moneda="EUR", sello_donante=True, prueba_pago=True,
    )  # fmt: skip
    return SimpleNamespace(**{**base, **kw})


def finding(rule="AMOUNT_MISMATCH", status=FindingStatus.OPEN, severity=Severity.HIGH, msg="x"):
    return SimpleNamespace(rule_code=rule, status=status, severity=severity, message=msg, id=1)


def codes(status, segments=(), findings=()):
    return [r.code for r in reasons_for(status, list(segments), list(findings), RULE_LABELS_ES)]


# --- reasons and verdict (pure) ---


def test_only_a_verified_document_read_reliably_and_with_nothing_open_is_bien():
    verified = DocumentStatus(VERIFIED, Confidence.HIGH)
    assert codes(verified, [seg()]) == []
    assert verdict_for(verified, [])["label"] == "Bien"
    # found by its content is also confirmed by the content
    assert codes(DocumentStatus(BY_CONTENT, Confidence.HIGH), [seg()]) == []


def test_what_could_not_be_checked_is_a_reason_to_review_never_a_pass():
    by_name = DocumentStatus(BY_NAME, Confidence.HIGH)
    assert codes(by_name, []) == ["ONLY_BY_NAME", "NOT_READ_DOCUMENT"]
    assert codes(DocumentStatus(MANUAL), []) == ["MANUAL_UNCHECKED", "NOT_READ_DOCUMENT"]
    verified = DocumentStatus(VERIFIED, Confidence.HIGH)
    assert codes(verified, [seg(importe=None)]) == ["NOT_READ_AMOUNT"]
    assert codes(verified, [seg(fecha=None)]) == ["NOT_READ_DATE"]
    assert codes(verified, [seg(emisor=None, numero_factura=None)]) == ["NOT_READ_ISSUER"]
    # the issuer OR the invoice number is enough (the issuer is often misread)
    assert codes(verified, [seg(emisor=None)]) == []
    assert codes(verified, [seg(confianza_ocr=40.0)]) == ["DOUBTFUL_READING"]
    assert codes(verified, [seg(legible=False, confianza_ocr=None)]) == ["DOUBTFUL_READING"]


def test_missing_document_is_blocking_and_findings_become_reasons():
    (missing,) = reasons_for(DocumentStatus(MISSING), [], [], RULE_LABELS_ES)
    assert (missing.code, missing.label, missing.blocking) == (
        "DOC_MISSING",
        "Sin justificante",
        True,
    )
    verified = DocumentStatus(VERIFIED, Confidence.HIGH)
    reasons = reasons_for(
        verified,
        [seg()],
        [
            finding(),  # open, high: blocking
            finding("DATE_MISMATCH", severity=Severity.LOW),  # open, low: not blocking
            finding("NOT_AN_INVOICE", FindingStatus.FALSE_POSITIVE),  # the tool was wrong
            finding("CASH_OVER_LIMIT", FindingStatus.ACCEPTED, Severity.LOW),  # confirmed real
        ],
        RULE_LABELS_ES,
    )
    assert [(r.code, r.blocking) for r in reasons] == [
        ("AMOUNT_MISMATCH", True),
        ("DATE_MISMATCH", False),
        ("CASH_OVER_LIMIT", True),
    ]
    assert reasons[2].label.startswith("Confirmada: ")
    assert verdict_for(verified, reasons)["code"] == "review"


def test_expenses_outside_the_review_are_not_applicable_unless_something_is_open():
    for status in (NOT_IN_SAMPLE, NOT_REQUIRED_COFINANCED):
        assert verdict_for(DocumentStatus(status), []) == {"code": "na", "label": "No aplica"}
    reasons = reasons_for(DocumentStatus(NOT_IN_SAMPLE), [], [finding()], RULE_LABELS_ES)
    assert verdict_for(DocumentStatus(NOT_IN_SAMPLE), reasons)["code"] == "review"


def test_compare_puts_the_listing_next_to_what_was_read():
    expense = SimpleNamespace(
        amount_local=D("1250000"), currency="PYG", amount_eur=D("150"), amount_donor=D("150"),
        date=dt.date(2024, 3, 1), issuer="Proveedor S.L.", extra={"invoice_no": "F-1"},
    )  # fmt: skip
    rows = {r["field"]: r for r in compare_rows(expense, [seg(importe=D("1205000"), moneda="PYG")])}
    assert rows["importe"] == {
        "field": "importe", "label": "Importe", "listed": "1.250.000,00 PYG",
        "read": "1.205.000,00 PYG", "match": False,
    }  # fmt: skip
    assert rows["fecha"]["match"] is True and rows["emisor"]["match"] is True
    assert rows["factura"]["match"] is True
    unread = {r["field"]: r for r in compare_rows(expense, [])}
    assert unread["importe"]["read"] is None and unread["importe"]["match"] is None


def test_a_decision_goes_stale_when_what_the_reviewer_saw_changes():
    expense = SimpleNamespace(
        review_status=ReviewDecision.CORRECT, review_note="Visto en el PDF",
        reviewed_at=dt.datetime(2026, 11, 15, 12, 0), review_fingerprint="abc",
    )  # fmt: skip
    assert decision_view(expense, "abc")["status"] == "correct"
    stale = decision_view(expense, "otra")
    assert (stale["status"], stale["stale"], stale["note"]) == ("pending", True, "Visto en el PDF")


# --- the paths that used to end in a false "Sin incidencias" (database) ---


def _project(session):
    project = new_project(session, donor=Donor.XUNTA)
    project.match_config = UGANDA_MATCH_CONFIG
    session.flush()
    return project


def _view(session, project, expense):
    view = project_review(session, project)
    return next(e for e in view["expenses"] if e["id"] == expense.id), view


def test_a_document_that_was_never_read_needs_review(db_session):
    project = _project(db_session)
    e = add_expense(
        db_session, project, "7", 1, concept="Combustible motocicleta",
        amount_local=D("500000"), currency="UGX", date=dt.date(2024, 3, 1),
        amount_eur=D("120"), amount_donor=D("120"),
    )  # fmt: skip
    _doc(db_session, project, "7. Combustible motocicleta.jpg", "a")  # never read
    mh.match_project(db_session, project.id)
    run_checks(db_session, project.id)
    view, _ = _view(db_session, project, e)
    assert view["documents"] and view["segments"] == []
    assert view["verdict"]["code"] == "review"
    assert "NOT_READ_DOCUMENT" in [r["code"] for r in view["reasons"]]


def test_a_payslip_alone_proposed_by_name_needs_review(db_session):
    project = _project(db_session)
    e = add_expense(
        db_session, project, "12", 1, concept="Salario coordinador marzo", partida_code="A.V.1",
        amount_local=D("1800000"), currency="UGX", amount_eur=D("430"), amount_donor=D("430"),
    )  # fmt: skip
    doc = _doc(db_session, project, "12. Salario coordinador marzo.pdf", "b")
    _segmento(db_session, doc, tipo=TipoSegmento.NOMINA, confianza_ocr=80.0)
    mh.match_project(db_session, project.id)
    run_checks(db_session, project.id)
    view, _ = _view(db_session, project, e)
    assert view["verdict"]["code"] == "review"


def test_a_false_positive_does_not_hide_a_different_finding(db_session):
    project = _project(db_session)
    e = add_expense(db_session, project, "3", 1, amount_local=D("1000"))
    old = FindingIn(
        expense_id=e.id, rule_code="AMOUNT_MISMATCH", severity=Severity.LOW,
        message="difiere en 3", evidence={"diferencia": 3},
    )  # fmt: skip
    sync_findings(db_session, project.id, {"AMOUNT_MISMATCH"}, [old])
    (f,) = db_session.scalars(select(Finding).where(Finding.project_id == project.id)).all()
    f.status, f.reviewer_note = FindingStatus.FALSE_POSITIVE, "error de OCR"
    db_session.flush()
    # same finding again: the decision stands
    sync_findings(db_session, project.id, {"AMOUNT_MISMATCH"}, [old])
    (f,) = db_session.scalars(select(Finding).where(Finding.project_id == project.id)).all()
    assert f.status == FindingStatus.FALSE_POSITIVE
    # a different difference: reopened with the new message, the note kept for context
    new = FindingIn(
        expense_id=e.id, rule_code="AMOUNT_MISMATCH", severity=Severity.HIGH,
        message="difiere en 900.000", evidence={"diferencia": 900000},
    )  # fmt: skip
    sync_findings(db_session, project.id, {"AMOUNT_MISMATCH"}, [new])
    (f,) = db_session.scalars(select(Finding).where(Finding.project_id == project.id)).all()
    assert (f.status, f.message, f.severity) == (
        FindingStatus.OPEN,
        "difiere en 900.000",
        Severity.HIGH,
    )
    assert f.reviewer_note == "error de OCR"


# --- the decision through the API (database) ---


def test_deciding_on_an_expense(db_session, api):
    project = _project(db_session)
    missing = add_expense(db_session, project, "1", 1, amount_local=D("100"))
    by_name = add_expense(db_session, project, "2", 2, concept="Agua", amount_local=D("50"))
    _doc(db_session, project, "2. Agua.jpg", "c")
    mh.match_project(db_session, project.id)
    run_checks(db_session, project.id)

    # "Correcto" on a blocking reason (no document) needs a note
    refused = api.patch(f"/api/expenses/{missing.id}/decision", json={"status": "correct"})
    assert refused.status_code == 422 and "nota" in refused.json()["detail"]
    done = api.patch(
        f"/api/expenses/{missing.id}/decision",
        json={"status": "correct", "note": "El socio lo ha enviado por correo"},
    )
    assert done.status_code == 200
    body = done.json()
    assert body["decision"]["status"] == "correct" and body["decision"]["decided_at"]
    real = api.patch(f"/api/expenses/{by_name.id}/decision", json={"status": "real_issue"})
    assert real.json()["decision"]["label"] == "Incidencia real"

    counts = api.get(f"/api/projects/{project.key}/review").json()["counts"]
    assert counts == {"pending": 0, "reviewed": 2, "ok": 0, "na": 0}
    assert api.patch("/api/expenses/999999/decision", json={"status": "correct"}).status_code == 404
