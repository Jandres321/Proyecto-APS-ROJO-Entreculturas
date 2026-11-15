"""Reviewer workflow (decisions, document/page location, API), the technical Excel export and the
analysis report (`report.py`), on invented data. Database tests run in a rolled-back transaction;
the report's sheets are also checked without a database, from a hand-made review view."""

import datetime as dt
from decimal import Decimal

import pandas as pd
import pytest
from openpyxl import load_workbook

from app.enums import Confidence, Donor, FindingStatus, MatchMethod, Severity
from app.models import BudgetLine, Document, Expense, ExpenseDocument, Finding, Project, Segmento
from app.pipeline import report
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

    # a donor that does not justify cofinanced expenses (Xunta does: see test_listing.py)
    project.match_config = {**(project.match_config or {}), "cofinanced_needs_document": False}
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


# --- analysis report (report.py) ---
#
# The view is built by hand with the fields of the new review criterion (verdict Bien / Necesita
# revisión / No aplica, reasons and a decision per expense), so these tests do not depend on how
# `review_view.project_review` computes them.

BLOCKING = {"code": "DOC_MISSING", "label": "Sin justificante", "blocking": True}
NOT_READ = {"code": "NOT_READ_AMOUNT", "label": "No leído: importe", "blocking": False}
NAME_ONLY = {"code": "NAME_ONLY", "label": "Solo emparejado por el nombre", "blocking": False}
VERDICTS = {"ok": "Bien", "review": "Necesita revisión", "na": "No aplica"}


def report_expense(order_no, verdict="review", reasons=(), status="pending", note=None, **kw):
    """One expense of `project_review(...)["expenses"]` with the new fields."""
    expense = {
        "id": kw.pop("id", hash(order_no) % 10_000),
        "order_no": order_no,
        "source_row": kw.pop("source_row", 10),
        "date": "2024-03-01",
        "issuer": kw.pop("issuer", f"Proveedor {order_no}"),
        "concept": "Concepto inventado",
        "partida_code": "A.III",
        "partida_raw": "A.III",
        "amount_local": None,
        "currency": "EUR",
        "amount_eur": kw.pop("amount_eur", 100.0),
        "amount_donor": None,
        "invoice_no": None,
        "payment": None,
        "suggested_partida": None,
        "suggested_reason": None,
        "doc_status": "missing",
        "doc_status_label": "Sin justificante",
        "confidence_label": None,
        "documents": [],
        "segments": [],
        "findings": [],
        "verdict": {"code": verdict, "label": VERDICTS[verdict]},
        "reasons": list(reasons),
        "decision": {
            "status": status,
            "note": note,
            "decided_at": kw.pop(
                "decided_at", None if status == "pending" else "2026-11-15T12:00:00"
            ),
        },
    }
    expense.update(kw)
    return expense


def report_view(expenses, general_findings=()):
    review = [e for e in expenses if e["verdict"]["code"] == "review"]
    return {
        "project": {
            "id": 1, "name": "Proyecto inventado", "code": "INV-1", "donor": "xunta",
            "expenses": len(expenses), "documents": 3,
            "findings": {"open": 0, "accepted": 0, "false_positive": 0},
        },
        "expenses": list(expenses),
        "general_findings": list(general_findings),
        "rule_labels": {},
        "counts": {
            "pending": sum(e["decision"]["status"] == "pending" for e in review),
            "reviewed": sum(e["decision"]["status"] != "pending" for e in review),
            "ok": sum(e["verdict"]["code"] == "ok" for e in expenses),
            "na": sum(e["verdict"]["code"] == "na" for e in expenses),
        },
    }  # fmt: skip


def sample_expenses():
    """Two partners (CG, FA), an expense without partner and every state / decision."""
    return [
        report_expense("FA10", "review", [BLOCKING, NOT_READ], amount_eur=40.0, id=1),
        report_expense("CG12.3", "review", [NOT_READ], "real_issue", "Pedir el original", id=2,
                       amount_eur=25.5),
        report_expense("FA2", "review", [NAME_ONLY], "correct", "Revisado en el PDF", id=3,
                       amount_eur=10.0),
        report_expense("7", "review", [BLOCKING], id=4, amount_eur=5.0),
        report_expense("FA236.3", "ok", id=5, amount_eur=1000.0),
        report_expense("CG1", "na", id=6, amount_eur=300.0),
        report_expense("FA3", "review", [BLOCKING], "correct", "Consta en otra carpeta", id=7,
                       amount_eur=2.0),
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("order_no", "partner"),
    [
        ("CG12.3", "CG"), ("FA236.3", "FA"), ("ec-5", "EC"), (" CG 14", "CG"),
        ("12", "Sin socio"), ("N/A", "Sin socio"), ("s/n", "Sin socio"), (None, "Sin socio"),
        ("", "Sin socio"),
    ],
)  # fmt: skip
def test_the_partner_is_the_letter_prefix_of_the_order_number(order_no, partner):
    assert report.partner_of(order_no) == partner


def test_reasons_are_joined_and_blocking_ones_marked():
    assert report.reasons_text([BLOCKING, NOT_READ]) == (
        "[bloqueante] Sin justificante; No leído: importe"
    )
    assert report.reasons_text([]) is None and report.reasons_text(None) is None


def test_report_filename_uses_the_code_and_the_date():
    day = dt.date(2026, 11, 15)
    project = Project(name="x", donor=Donor.AECID, code="22-PY-136", key="paraguay")
    assert report.report_filename(project, day) == "22-PY-136_revision_2026-11-15.xlsx"
    project.code = "2019/PRYC/1234"  # not valid in a file name
    assert report.report_filename(project, day) == "2019-PRYC-1234_revision_2026-11-15.xlsx"
    project.code = None
    assert report.report_filename(project, day) == "paraguay_revision_2026-11-15.xlsx"
    assert report.report_filename(project).endswith(f"_{dt.date.today().isoformat()}.xlsx")


def test_expense_sheet_starts_with_state_reasons_and_decision():
    table = report.expense_table(sample_expenses())
    assert list(table.columns[:5]) == [
        "Estado",
        "Motivos",
        "Decisión del revisor",
        "Nota del revisor",
        "Fecha de la decisión",
    ]
    assert "Semáforo" not in table.columns and "Veredicto" not in table.columns
    assert {"Nº orden", "Fecha", "Proveedor", "Importe €", "Documentos vinculados",
            "Leído: importe", "Archivo y página"} <= set(table.columns)  # fmt: skip
    rows = table.set_index("Nº orden")
    assert rows.loc["FA10", "Estado"] == "Necesita revisión"
    assert rows.loc["FA10", "Motivos"] == "[bloqueante] Sin justificante; No leído: importe"
    assert rows.loc["FA10", "Decisión del revisor"] == "Pendiente"
    assert rows.loc["CG12.3", "Decisión del revisor"] == "Incidencia real"
    assert rows.loc["CG12.3", "Nota del revisor"] == "Pedir el original"
    assert rows.loc["CG12.3", "Fecha de la decisión"] == dt.datetime(2026, 11, 15, 12, 0)
    assert rows.loc["FA236.3", "Estado"] == "Bien"
    assert pd.isna(rows.loc["FA236.3", "Decisión del revisor"])  # nothing pending on a «Bien»
    assert rows.loc["CG1", "Estado"] == "No aplica"


def test_expense_sheet_shows_where_to_look():
    expense = report_expense(
        "FA1",
        documents=[{"filename": "FA1.pdf", "verified": False, "reason": "Por el nombre"}],
        segments=[
            {"archivo": "carpeta/FA1.pdf", "pagina_inicio": 2, "pagina_fin": 3, "importe": 50.0,
             "moneda": "PYG", "emisor": None, "numero_factura": None, "fecha": None,
             "sello_donante": False, "prueba_pago": True, "legible": True},
        ],
    )  # fmt: skip
    (row,) = report.expense_table([expense]).to_dict("records")
    assert row["Archivo y página"] == "FA1.pdf, págs. 2-3"
    assert row["Leído: importe"] == "50.00 PYG" and row["Leído: emisor"] is None
    assert row["Verificado por contenido"] == "no"
    (row,) = report.expense_table(
        [
            report_expense(
                "FA2", documents=[{"filename": "FA2.pdf", "verified": True, "reason": None}]
            )
        ]
    ).to_dict("records")
    assert row["Archivo y página"] == "FA2.pdf"  # nothing read: the linked file


def test_summary_counts_expenses_and_euros_per_state():
    general = [{"status": "open"}, {"status": "accepted"}]
    table = report.summary_table(report_view(sample_expenses(), general))
    rows = {r["Indicador"]: (r["Valor"], r["Importe €"]) for r in table.to_dict("records")}
    assert rows["Gastos del informe"] == (7, 1382.5)
    assert rows["Bien"] == (1, 1000.0)
    assert rows["Necesita revisión"] == (5, 82.5)
    assert rows["   · pendientes de decisión"] == (2, 45.0)
    assert rows["   · revisados"] == (3, 37.5)
    assert rows["      · Correcto"] == (2, 12.0)
    assert rows["      · Incidencia real"] == (1, 25.5)
    assert rows["No aplica"] == (1, 300.0)
    assert rows["Gastos con algún motivo bloqueante"] == (3, 47.0)
    assert rows["   · Sin justificante"][0] == 3 and rows["   · No leído: importe"][0] == 2
    assert rows["Incidencias generales abiertas (presupuesto, topes, documentos sin gasto)"][0] == 1


def test_an_unknown_verdict_is_never_counted_as_fine():
    odd = report_expense("FA9", "review")
    odd["verdict"] = {"code": "critical", "label": "Revisar · 1 incidencia"}
    odd.pop("decision")
    odd.pop("reasons")
    rows = {r["Indicador"]: r["Valor"] for r in report.summary_table(report_view([odd]))
            .to_dict("records")}  # fmt: skip
    assert (rows["Necesita revisión"], rows["   · pendientes de decisión"], rows["Bien"]) == (
        1,
        1,
        0,
    )


def test_requirements_are_real_issues_and_blocking_reasons_grouped_by_partner():
    table = report.requirements_table(sample_expenses())
    assert list(table.columns) == [
        "Socio",
        "Nº orden",
        "Proveedor",
        "Importe €",
        "Qué falta o hay que corregir",
    ]
    rows = table.to_dict("records")
    # CG first, then FA, "Sin socio" last. Not included: Bien (FA236.3), No aplica (CG1), a
    # "Correcto" without blocking reasons (FA2) and a blocking reason judged "Correcto" (FA3).
    assert [(r["Socio"], r["Nº orden"]) for r in rows] == [
        ("CG", "CG12.3"),
        ("FA", "FA10"),
        ("Sin socio", "7"),
    ]
    assert rows[0]["Qué falta o hay que corregir"] == (
        "No leído: importe. Nota del revisor: Pedir el original"
    )
    assert rows[1]["Qué falta o hay que corregir"] == (
        "[bloqueante] Sin justificante; No leído: importe"
    )
    assert rows[1]["Proveedor"] == "Proveedor FA10" and rows[1]["Importe €"] == 40.0


def test_requirements_follow_the_natural_order_of_the_order_numbers():
    expenses = [report_expense(n, reasons=[BLOCKING]) for n in ("FA10", "FA9", "FA236.10",
                                                               "FA236.2")]  # fmt: skip
    orders = list(report.requirements_table(expenses)["Nº orden"])
    assert orders == ["FA9", "FA10", "FA236.2", "FA236.10"]


def test_requirements_of_a_project_with_nothing_to_ask_is_an_empty_sheet():
    table = report.requirements_table([report_expense("FA1", "ok")])
    assert table.empty and list(table.columns)[0] == "Socio"


def test_report_workbook_has_the_new_sheets(db_session, project, tmp_path, monkeypatch):
    monkeypatch.setattr(report, "project_review", lambda session, p: report_view(sample_expenses()))
    out = report.export_report_xlsx(db_session, project.id, tmp_path / "sub" / "informe.xlsx")
    book = load_workbook(out)
    assert book.sheetnames == [
        "Resumen",
        "Análisis por gasto",
        "Requerimientos",
        "Incidencias",
        "Presupuesto",
        "Topes",
    ]

    def records(name):
        sheet = book[name]
        header = [c.value for c in sheet[1]]
        return header, [
            dict(zip(header, [c.value for c in row], strict=True))
            for row in sheet.iter_rows(min_row=2)
        ]

    header, rows = records("Análisis por gasto")
    assert header[:5] == [
        "Estado",
        "Motivos",
        "Decisión del revisor",
        "Nota del revisor",
        "Fecha de la decisión",
    ]
    assert "Semáforo" not in header and "Veredicto" not in header and len(rows) == 7
    real = next(r for r in rows if r["Nº orden"] == "CG12.3")
    assert real["Fecha de la decisión"] == dt.datetime(2026, 11, 15, 12, 0)
    pending = next(r for r in rows if r["Nº orden"] == "FA10")
    assert (pending["Decisión del revisor"], pending["Fecha de la decisión"]) == ("Pendiente", None)

    header, rows = records("Requerimientos")
    assert header == ["Socio", "Nº orden", "Proveedor", "Importe €", "Qué falta o hay que corregir"]
    assert [(r["Socio"], r["Nº orden"]) for r in rows] == [
        ("CG", "CG12.3"),
        ("FA", "FA10"),
        ("Sin socio", "7"),
    ]

    summary = {r[0].value: (r[1].value, r[2].value) for r in book["Resumen"].iter_rows(min_row=2)}
    assert summary["Bien"] == (1, 1000) and summary["No aplica"] == (1, 300)
    assert summary["   · pendientes de decisión"] == (2, 45)
    assert summary["      · Incidencia real"] == (1, 25.5)


def test_report_of_a_project_without_expenses_still_writes_every_sheet(
    db_session, project, tmp_path, monkeypatch
):
    monkeypatch.setattr(report, "project_review", lambda session, p: report_view([]))
    book = load_workbook(report.export_report_xlsx(db_session, project.id, tmp_path / "v.xlsx"))
    assert "Requerimientos" in book.sheetnames and book["Requerimientos"].max_row == 1


def test_report_unknown_project(db_session, tmp_path):
    with pytest.raises(LookupError):
        report.export_report_xlsx(db_session, 0, tmp_path / "x.xlsx")
