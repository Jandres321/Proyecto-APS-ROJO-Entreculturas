"""Reviewer workflow (decisions, document/page location, API) and the Excel export, on invented
data in the (rolled back) database."""

from decimal import Decimal

import pytest
from openpyxl import load_workbook

from app.enums import Confidence, Donor, FindingStatus, MatchMethod, Severity
from app.models import BudgetLine, Document, Expense, ExpenseDocument, Finding, Project, Segmento
from app.pipeline.budget import RULE_BUDGET_DEVIATION, RULE_LIMIT_EXCEEDED, run_budget
from app.pipeline.export import export_project_xlsx
from app.review import list_findings, locate, set_decision

D = Decimal


@pytest.fixture
def project(db_session) -> Project:
    project = Project(name="review test", donor=Donor.XUNTA, code="REV-1")
    db_session.add(project)
    db_session.flush()
    return project


def add_document(session, project, name="1. Factura.pdf", pages=3) -> Document:
    doc = Document(
        project_id=project.id, path=f"x/{name}", filename=name, sha256="a" * 64, pages=pages
    )
    session.add(doc)
    session.flush()
    return doc


def add_expense(session, project, order_no="1", **fields) -> Expense:
    expense = Expense(project_id=project.id, order_no=order_no, source_row=10, **fields)
    session.add(expense)
    session.flush()
    return expense


def add_finding(session, project, rule="AMOUNT_MISMATCH", **fields) -> Finding:
    fields.setdefault("severity", Severity.MEDIUM)
    fields.setdefault("message", "Mensaje de prueba")
    finding = Finding(project_id=project.id, rule_code=rule, **fields)
    session.add(finding)
    session.flush()
    return finding


# --- decisions ---


def test_decisions_are_stored_with_a_note_and_can_be_reverted(db_session, project):
    finding = add_finding(db_session, project)
    assert finding.status == FindingStatus.OPEN

    set_decision(db_session, finding.id, FindingStatus.ACCEPTED, "Corregir con el socio")
    assert finding.status == FindingStatus.ACCEPTED
    assert finding.reviewer_note == "Corregir con el socio"

    set_decision(db_session, finding.id, FindingStatus.FALSE_POSITIVE)  # note kept
    assert finding.status == FindingStatus.FALSE_POSITIVE
    assert finding.reviewer_note == "Corregir con el socio"

    set_decision(db_session, finding.id, FindingStatus.OPEN, "")  # note cleared
    assert (finding.status, finding.reviewer_note) == (FindingStatus.OPEN, None)


def test_unknown_finding_is_an_error(db_session):
    with pytest.raises(LookupError):
        set_decision(db_session, 0, FindingStatus.ACCEPTED)


def test_list_findings_filters_and_orders_by_severity(db_session, project):
    low = add_finding(db_session, project, "ILLEGIBLE", severity=Severity.LOW)
    high = add_finding(db_session, project, "DOC_MISSING", severity=Severity.HIGH)
    mid = add_finding(db_session, project, "DATE_MISMATCH", severity=Severity.MEDIUM)
    set_decision(db_session, mid.id, FindingStatus.ACCEPTED)

    def ids(**kwargs):
        return [f.id for f in list_findings(db_session, project.id, **kwargs)]

    assert ids() == [high.id, mid.id, low.id]
    assert ids(status=FindingStatus.OPEN) == [high.id, low.id]
    assert ids(severity=Severity.LOW) == [low.id]
    assert ids(rule_codes={"DOC_MISSING"}) == [high.id]


# --- document and page of a finding ---


def test_locate_uses_the_findings_document_and_the_page_in_the_evidence(db_session, project):
    doc = add_document(db_session, project)
    finding = add_finding(
        db_session, project, document_id=doc.id, evidence={"pagina_inicio": 2, "pagina_fin": 2}
    )
    where = locate(db_session, finding)
    assert (where.document.id, where.page) == (doc.id, 2)


def test_locate_doc_missing_points_at_the_closest_candidate(db_session, project):
    doc = add_document(db_session, project)
    segment = Segmento(
        sha256="a" * 64,
        archivo=doc.filename,
        backend="tesseract",
        pagina_inicio=3,
        pagina_fin=3,
        tipo="factura",
    )
    db_session.add(segment)
    db_session.flush()
    expense = add_expense(db_session, project)
    finding = add_finding(
        db_session,
        project,
        "DOC_MISSING",
        expense_id=expense.id,
        evidence={"best_document_id": doc.id, "best_segment": segment.id},
    )
    where = locate(db_session, finding)
    assert (where.document.id, where.page) == (doc.id, 3)


def test_locate_falls_back_to_the_document_linked_to_the_expense(db_session, project):
    weak = add_document(db_session, project, "a.pdf")
    strong = add_document(db_session, project, "b.pdf")
    expense = add_expense(db_session, project)
    db_session.add_all(
        [
            ExpenseDocument(
                expense_id=expense.id, document_id=d.id, method=MatchMethod.CONTENT, score=s
            )
            for d, s in ((weak, 0.5), (strong, 0.9))
        ]
    )
    finding = add_finding(db_session, project, expense_id=expense.id)
    where = locate(db_session, finding)
    assert where.document.id == strong.id and where.page is None


def test_locate_budget_findings_have_no_document(db_session, project):
    where = locate(db_session, add_finding(db_session, project, RULE_LIMIT_EXCEEDED))
    assert where.document is None and where.page is None and where.path is None


# --- API ---


def test_findings_api_lists_and_updates(db_session, project, client):
    from app.db import get_session
    from app.main import app

    finding = add_finding(db_session, project, "DOC_MISSING", severity=Severity.HIGH)
    app.dependency_overrides[get_session] = lambda: db_session
    try:
        url = f"/api/projects/{project.key}/findings"
        assert [f["id"] for f in client.get(url, params={"status": "open"}).json()] == [finding.id]

        patched = client.patch(
            f"/api/findings/{finding.id}",
            json={"status": "false_positive", "reviewer_note": "ya consta"},
        )
        assert patched.status_code == 200
        assert patched.json()["status"] == "false_positive"
        assert client.get(url, params={"status": "open"}).json() == []
        assert client.patch("/api/findings/0", json={"status": "accepted"}).status_code == 404
    finally:
        app.dependency_overrides.clear()


# --- Excel export ---


def test_export_has_the_four_sheets(db_session, project, tmp_path):
    doc = add_document(db_session, project)
    expense = add_expense(
        db_session,
        project,
        "7",
        partida_code="A.X",
        amount_eur=D("2600"),
        amount_donor=D("2600"),
        concept="Auditoría",
        suggested_partida="A.X",
        suggested_confidence=0.9,
    )
    db_session.add(
        ExpenseDocument(
            expense_id=expense.id, document_id=doc.id, method=MatchMethod.CONTENT, score=0.87
        )
    )
    db_session.add(BudgetLine(project_id=project.id, partida_code="A.X", approved_donor=D(2500)))
    db_session.add(
        BudgetLine(project_id=project.id, partida_code="A.III.2", approved_donor=D(90000))
    )
    add_finding(
        db_session,
        project,
        "AMOUNT_MISMATCH",
        expense_id=expense.id,
        document_id=doc.id,
        evidence={"pagina_inicio": 2},
        severity=Severity.HIGH,
    )
    run_budget(db_session, project.id)

    out = export_project_xlsx(db_session, project.id, tmp_path / "sub" / "informe.xlsx")
    book = load_workbook(out)
    assert book.sheetnames == ["Incidencias", "Gastos", "Presupuesto", "Topes"]

    findings = {r[0].value: [c.value for c in r] for r in book["Incidencias"].iter_rows(min_row=2)}
    assert {"AMOUNT_MISMATCH", RULE_LIMIT_EXCEEDED, RULE_BUDGET_DEVIATION} <= set(findings)
    assert findings["AMOUNT_MISMATCH"][1:7] == ["alta", "abierta", "7", "A.X", doc.filename, 2]

    (row,) = [[c.value for c in r] for r in book["Gastos"].iter_rows(min_row=2)]
    assert row[1] == "7"
    assert "1. Factura.pdf (0.87)" in row[8]
    assert row[9] == "A.X (90%)"
    assert row[11] == 1  # open findings of the expense

    header = [c.value for c in book["Presupuesto"][1]]
    partidas = {
        r[0].value: dict(zip(header, (c.value for c in r), strict=True))
        for r in book["Presupuesto"].iter_rows(min_row=2)
    }
    assert float(partidas["A.X"]["Ejecutado (listado) €"]) == 2600
    assert partidas["A.X"]["Semáforo"] == "rojo"

    caps = {r[0].value: r[7].value for r in book["Topes"].iter_rows(min_row=2)}
    assert caps["XUNTA_AUDITORIA_MAX_2500"] == "SUPERADO"


def test_export_of_an_empty_project_still_writes_every_sheet(db_session, project, tmp_path):
    out = export_project_xlsx(db_session, project.id, tmp_path / "vacio.xlsx")
    assert load_workbook(out).sheetnames == ["Incidencias", "Gastos", "Presupuesto", "Topes"]


def test_export_unknown_project(db_session, tmp_path):
    with pytest.raises(LookupError):
        export_project_xlsx(db_session, 0, tmp_path / "x.xlsx")


# --- document status of each expense ---


def test_document_statuses_cover_every_case(db_session, project):
    from app.review import (
        BY_CONTENT,
        BY_NAME,
        MANUAL,
        MISSING,
        NOT_REQUIRED_COFINANCED,
        NOT_REQUIRED_INDIRECT,
        VERIFIED,
        document_statuses,
    )

    doc = add_document(db_session, project)
    cases = {}
    for name, fields in {
        "verified": {},
        "by_name": {},
        "by_content": {},
        "manual": {},
        "missing": {},
        "indirect": {"partida_code": "B"},
        "cofinanced": {"amount_donor": D(0)},
    }.items():
        cases[name] = add_expense(db_session, project, name, **fields)

    def link(name, method, confidence=None, verified=False):
        db_session.add(
            ExpenseDocument(
                expense_id=cases[name].id,
                document_id=doc.id,
                method=method,
                confidence=confidence,
                verified=verified,
            )
        )

    link("verified", MatchMethod.FILENAME, Confidence.HIGH, verified=True)
    link("by_name", MatchMethod.FILENAME, Confidence.LOW)
    link("by_content", MatchMethod.CONTENT, Confidence.MEDIUM)
    link("manual", MatchMethod.MANUAL)
    db_session.flush()

    got = document_statuses(db_session, project.id)
    assert {n: got[e.id].status for n, e in cases.items()} == {
        "verified": VERIFIED,
        "by_name": BY_NAME,
        "by_content": BY_CONTENT,
        "manual": MANUAL,
        "missing": MISSING,
        "indirect": NOT_REQUIRED_INDIRECT,
        "cofinanced": NOT_REQUIRED_COFINANCED,
    }
    assert got[cases["verified"].id].confidence_label == "alta"
    assert got[cases["by_name"].id].label.endswith("(sin verificar)")
    assert got[cases["missing"].id].confidence is None


def test_a_sample_folder_marks_missing_documents_as_not_in_the_sample(db_session):
    from app.review import NOT_IN_SAMPLE, document_statuses

    sample = Project(
        name="muestra", donor=Donor.AECID, code="SAMPLE-2", match_config={"carpeta_completa": False}
    )
    db_session.add(sample)
    db_session.flush()
    e = add_expense(db_session, sample, "1", amount_donor=D(10))
    status = document_statuses(db_session, sample.id)[e.id]
    assert status.status == NOT_IN_SAMPLE and status.label == "No incluido en la muestra"


def test_export_gastos_sheet_shows_the_document_status(db_session, project, tmp_path):
    doc = add_document(db_session, project)
    ok = add_expense(db_session, project, "1")
    add_expense(db_session, project, "2")
    db_session.add(
        ExpenseDocument(
            expense_id=ok.id,
            document_id=doc.id,
            method=MatchMethod.FILENAME,
            confidence=Confidence.MEDIUM,
            verified=True,
        )
    )
    db_session.flush()
    book = load_workbook(export_project_xlsx(db_session, project.id, tmp_path / "e.xlsx"))
    header = [c.value for c in book["Gastos"][1]]
    rows = [
        dict(zip(header, (c.value for c in r), strict=True))
        for r in book["Gastos"].iter_rows(min_row=2)
    ]
    assert [r["Estado del justificante"] for r in rows] == [
        "Con justificante · verificado por contenido",
        "Sin justificante",
    ]
    assert [r["Confianza del vínculo"] for r in rows] == ["media", None]
