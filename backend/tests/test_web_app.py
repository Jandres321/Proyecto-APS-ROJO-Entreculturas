"""Web review app: the static page, the expense-centred view and the /api/ui routes, on invented
data (database tests are rolled back and skipped without PostgreSQL)."""

import threading
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api import ui
from app.enums import Confidence, Donor, FindingStatus, MatchMethod, Severity, TipoSegmento
from app.models import (
    Document,
    Expense,
    ExpenseDocument,
    ExpenseSegment,
    Finding,
    Project,
    Segmento,
)
from app.pipeline.run import PilotProject
from app.review import MISSING, NOT_REQUIRED_INDIRECT, VERIFIED, DocumentStatus
from app.review_view import expense_verdict, project_review

D = Decimal
TEST_PILOT = PilotProject("web", "Proyecto web de prueba", Donor.XUNTA, "WEB-1", "carpeta", "i.xls")


@pytest.fixture
def pilots(monkeypatch, tmp_path):
    from app import projects
    from app.config import get_settings

    monkeypatch.setattr(projects, "PILOTS", {TEST_PILOT.key: TEST_PILOT})
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)  # no connected Drive folders
    monkeypatch.setattr(ui, "_jobs", {})
    return TEST_PILOT


# --- the page ---


def test_the_page_is_served_and_carries_no_data_or_external_resources(client):
    response = client.get("/")
    assert response.status_code == 200 and "text/html" in response.headers["content-type"]
    html = response.text
    assert "Revisor de justificación" in html and "/api" in html
    # Nothing pre-analysed is embedded and nothing is fetched from the internet.
    assert "ALL_PROJECT_RECORDS" not in html
    assert "https://" not in html and "http://" not in html
    assert "localStorage" not in html and "sk-ant" not in html


# --- verdict (pure) ---


def finding(status=FindingStatus.OPEN, severity=Severity.MEDIUM):
    return SimpleNamespace(status=status, severity=severity)


def test_verdict_comes_from_the_document_status_and_the_findings():
    linked = DocumentStatus(VERIFIED, Confidence.HIGH)
    assert expense_verdict(linked, [])["code"] == "ok"
    assert expense_verdict(linked, [finding()]) == {
        "code": "review",
        "label": "Revisar · 1 incidencia",
    }
    two = [finding(), finding(severity=Severity.HIGH)]
    assert expense_verdict(linked, two) == {"code": "critical", "label": "Revisar · 2 incidencias"}
    # nothing open: an accepted finding is a confirmed problem, a false positive is not
    assert expense_verdict(linked, [finding(FindingStatus.ACCEPTED)])["code"] == "confirmed"
    assert expense_verdict(linked, [finding(FindingStatus.FALSE_POSITIVE)])["code"] == "ok"
    assert expense_verdict(DocumentStatus(MISSING), [])["code"] == "nodoc"
    indirect = expense_verdict(DocumentStatus(NOT_REQUIRED_INDIRECT), [])
    assert indirect == {"code": "na", "label": "No requiere justificante (coste indirecto)"}


# --- expense-centred view (database) ---


@pytest.fixture
def project(db_session) -> Project:
    project = Project(name="web test", donor=Donor.XUNTA, code=TEST_PILOT.code)
    db_session.add(project)
    db_session.flush()
    return project


def fill(session, project):
    """One expense with a verified document, a segment that read only the amount and an open
    finding; one expense without document; and a finding that hangs from no expense."""
    doc = Document(
        project_id=project.id, path="carpeta/1. Factura.pdf", filename="1. Factura.pdf",
        sha256="c" * 64, pages=3,
    )  # fmt: skip
    with_doc = Expense(
        project_id=project.id, order_no="1", source_row=10, issuer="Proveedor Inventado",
        concept="Material de oficina", amount_local=D("1500"), currency="UGX",
        amount_eur=D("0.40"), amount_donor=D("0.40"), partida_code="A.1",
        extra={"invoice_no": "F-42", "payment": "Transferencia 10/05/2024"},
    )  # fmt: skip
    without = Expense(project_id=project.id, order_no="2", source_row=11, amount_eur=D("9"))
    session.add_all([doc, with_doc, without])
    session.flush()
    session.add(
        ExpenseDocument(
            expense_id=with_doc.id,
            document_id=doc.id,
            method=MatchMethod.FILENAME,
            score=0.9,
            confidence=Confidence.HIGH,
            verified=True,
            reason="El nombre describe el gasto.",
        )  # fmt: skip
    )
    segment = Segmento(
        document_id=doc.id, sha256="c" * 64, archivo="1. Factura.pdf", backend="tesseract",
        pagina_inicio=2, pagina_fin=2, tipo=TipoSegmento.FACTURA, importe=D("1500"), moneda="UGX",
    )  # fmt: skip
    session.add(segment)
    session.flush()
    session.add(
        ExpenseSegment(
            expense_id=with_doc.id,
            segmento_id=segment.id,
            score=0.8,
            confidence=Confidence.MEDIUM,
            reason="El importe coincide.",
        )  # fmt: skip
    )
    own = Finding(
        project_id=project.id, expense_id=with_doc.id, document_id=doc.id,
        rule_code="DONOR_STAMP_MISSING", severity=Severity.MEDIUM, message="Falta el sello.",
        evidence={"pagina_inicio": 2},
    )  # fmt: skip
    general = Finding(
        project_id=project.id, rule_code="BUDGET_DEVIATION", severity=Severity.LOW,
        message="Desviación en la partida A.1.", evidence={"subject": "A.1"},
    )  # fmt: skip
    session.add_all([own, general])
    session.flush()
    return with_doc, without, doc, own, general


def test_review_groups_documents_readings_and_findings_by_expense(db_session, project):
    with_doc, without, doc, own, general = fill(db_session, project)
    view = project_review(db_session, project)

    assert view["project"]["expenses"] == 2 and view["project"]["findings"]["open"] == 2
    first, second = view["expenses"]  # in the order of the report
    assert (first["id"], first["invoice_no"], first["payment"]) == (
        with_doc.id,
        "F-42",
        "Transferencia 10/05/2024",
    )
    assert first["verdict"] == {"code": "review", "label": "Revisar · 1 incidencia"}
    (linked,) = first["documents"]
    assert linked["filename"] == "1. Factura.pdf" and linked["verified"] is True
    assert linked["method_label"] == "propuesto por el nombre del archivo"
    (read,) = first["segments"]
    assert (read["importe"], read["pagina_inicio"], read["reason"]) == (
        1500.0,
        2,
        "El importe coincide.",
    )
    # What was not read inside the PDF stays empty: it is never filled in from the listing.
    assert read["emisor"] is None and read["fecha"] is None and read["numero_factura"] is None
    (shown,) = first["findings"]
    assert (shown["id"], shown["label"], shown["page"]) == (own.id, "Falta el sello del donante", 2)
    assert shown["document_id"] == doc.id and shown["document_openable"] is False  # no DATA_DIR

    assert second["id"] == without.id and second["documents"] == [] and second["segments"] == []
    assert second["doc_status"] == "missing" and second["verdict"]["code"] == "nodoc"
    assert [f["id"] for f in view["general_findings"]] == [general.id]


# --- analysis report (database) ---


def test_the_report_has_one_row_per_expense_with_what_was_really_read(
    db_session, project, tmp_path
):
    from openpyxl import load_workbook

    from app.pipeline.report import export_report_xlsx
    from app.review import set_decision

    with_doc, without, doc, own, general = fill(db_session, project)
    set_decision(db_session, own.id, FindingStatus.ACCEPTED, "Pedir el sello al socio")
    book = load_workbook(export_report_xlsx(db_session, project.id, tmp_path / "informe.xlsx"))
    assert book.sheetnames == [
        "Resumen",
        "Análisis por gasto",
        "Incidencias",
        "Presupuesto",
        "Topes",
    ]

    sheet = book["Análisis por gasto"]
    header = [c.value for c in sheet[1]]
    first, second = (
        dict(zip(header, [c.value for c in row], strict=True)) for row in sheet.iter_rows(min_row=2)
    )
    assert (first["Nº orden"], first["Semáforo"], first["Veredicto"]) == (
        "1",
        "rojo",
        "1 incidencia confirmada",
    )
    assert first["Documentos vinculados"] == "1. Factura.pdf"
    assert first["Verificado por contenido"] == "sí"
    assert first["Leído: importe"] == "1,500.00 UGX"
    # not read inside the PDF: empty, never the listing's value
    assert first["Leído: emisor"] is None and first["Leído: fecha"] is None
    assert first["Leído: sello del donante"] == "no"
    assert (first["Incidencias abiertas"], first["Incidencias aceptadas"]) == (0, 1)
    assert "Falta el sello del donante: Falta el sello." in first["Detalle de las incidencias"]
    assert "nota: Pedir el sello al socio" in first["Detalle de las incidencias"]

    assert (second["Nº orden"], second["Semáforo"], second["Veredicto"]) == (
        "2",
        "gris",
        "Sin justificante",
    )
    # nothing was read for an expense without document: the cells stay empty, not "no"
    assert second["Leído: sello del donante"] is None and second["Documentos vinculados"] is None

    summary = {row[0].value: row[1].value for row in book["Resumen"].iter_rows(min_row=2)}
    assert summary["Gastos del informe"] == 2 and summary["Gastos sin justificante"] == 1
    assert summary["Incidencias aceptadas (reales)"] == 1 and summary["Incidencias abiertas"] == 1
    assert summary["   · Desviación respecto al presupuesto"] == 1


# --- /api/ui (database) ---


@pytest.fixture
def api(db_session, client):
    from app.db import get_session
    from app.main import app

    app.dependency_overrides[get_session] = lambda: db_session
    yield client
    app.dependency_overrides.clear()


def test_api_serves_pilots_review_budget_and_excel(db_session, project, pilots, api):
    fill(db_session, project)

    listing = api.get("/api/ui/pilots").json()
    (pilot,) = listing["pilots"]
    assert (pilot["key"], pilot["expenses"], pilot["loaded"]) == ("web", 2, True)
    assert pilot["job"] == {"state": "idle"} and pilot["drive"] is False
    assert {d["value"] for d in listing["donors"]} == {"xunta", "aecid"}
    assert set(listing["drive"]) == {"available", "message"}

    review = api.get("/api/ui/pilots/web/review").json()
    assert len(review["expenses"]) == 2 and "rules" in review
    assert set(api.get("/api/ui/pilots/web/budget").json()) == {"partidas", "limits"}

    excel = api.get("/api/ui/pilots/web/export.xlsx")
    assert excel.status_code == 200 and excel.content[:2] == b"PK"
    assert "WEB-1_revision.xlsx" in excel.headers["content-disposition"]
    report = api.get("/api/ui/pilots/web/report.xlsx")
    assert report.status_code == 200 and report.content[:2] == b"PK"
    assert "WEB-1_informe_analisis_" in report.headers["content-disposition"]

    assert api.get("/api/ui/pilots/otro/review").status_code == 404


def test_a_pilot_that_was_never_analysed_is_not_loaded(db_session, pilots, api):
    (pilot,) = api.get("/api/ui/pilots").json()["pilots"]
    assert pilot["loaded"] is False
    assert api.get("/api/ui/pilots/web/review").status_code == 404


def test_pdf_pages_are_rendered_only_for_files_inside_data_dir(
    db_session, project, api, tmp_path, monkeypatch
):
    import pymupdf

    from app.config import get_settings

    root = tmp_path / "datos"
    (root / "carpeta").mkdir(parents=True)
    pdf = pymupdf.open()
    for n in range(1, 4):
        pdf.new_page().insert_text((72, 72), f"Pagina {n}")
    pdf.save(root / "carpeta" / "f.pdf")
    pdf.close()
    (tmp_path / "secreto.pdf").write_bytes((root / "carpeta" / "f.pdf").read_bytes())
    monkeypatch.setattr(get_settings(), "data_dir", root)

    def add(path):
        doc = Document(
            project_id=project.id, path=path, filename=Path(path).name, sha256="d" * 64, pages=3
        )
        db_session.add(doc)
        db_session.flush()
        return doc.id

    inside, outside, sheet = add("carpeta/f.pdf"), add("../secreto.pdf"), add("carpeta/x.xlsx")

    page = api.get(f"/api/ui/documents/{inside}/page/2.png", params={"rotation": 180})
    assert page.status_code == 200 and page.content[:4] == b"\x89PNG"
    assert page.headers["x-pages"] == "3"
    assert api.get(f"/api/ui/documents/{inside}/page/99.png").status_code == 200  # clamped
    original = api.get(f"/api/ui/documents/{inside}/file")
    assert original.status_code == 200 and original.content[:4] == b"%PDF"

    # a path that escapes DATA_DIR, a file that is not a PDF and an unknown document
    for document_id in (outside, sheet, 0):
        assert api.get(f"/api/ui/documents/{document_id}/page/1.png").status_code == 404
        assert api.get(f"/api/ui/documents/{document_id}/file").status_code == 404


# --- launching the pipeline (no database) ---


class FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def summary():
    matching = SimpleNamespace(
        expenses_linked=3, verified=1, from_name=2, from_content=0, not_in_sample=0
    )
    return SimpleNamespace(expenses=5, documents=4, matching=matching, report_reread=True)


def wait_for(client, state):
    for _ in range(200):
        job = client.get("/api/ui/pilots/web/run").json()
        if job["state"] == state:
            return job
        time.sleep(0.02)
    raise AssertionError(f"the run never reached {state!r}: {job}")


def test_launching_runs_in_the_background_and_reports_progress(pilots, client, monkeypatch):
    release = threading.Event()
    calls = []

    def fake_pipeline(session, pilot, *, reread_report, on_progress):
        calls.append((pilot.key, reread_report))
        on_progress("reading", 0.5, "Leyendo el contenido de los PDF")
        release.wait(5)
        return summary()

    monkeypatch.setattr(ui, "run_pipeline", fake_pipeline)
    monkeypatch.setattr(ui.db, "SessionLocal", FakeSession)

    started = client.post("/api/ui/pilots/web/run", params={"reread": True})
    assert started.status_code == 202 and started.json()["state"] == "running"
    assert client.post("/api/ui/pilots/web/run").status_code == 409  # one run at a time
    running = wait_for(client, "running")
    assert running["steps"] == len(ui.STEPS)

    release.set()
    done = wait_for(client, "done")
    assert (
        done["fraction"] == 1.0 and "Análisis terminado: 5 gastos y 4 documentos" in done["message"]
    )
    assert "Se ha releído el informe" in done["message"]
    assert calls == [("web", True)]
    assert client.post("/api/ui/pilots/otro/run").status_code == 404


def test_a_failed_run_is_reported_not_hidden(pilots, client, monkeypatch):
    def missing(session, pilot, **kwargs):
        raise FileNotFoundError("No se encuentra el informe")

    monkeypatch.setattr(ui, "run_pipeline", missing)
    monkeypatch.setattr(ui.db, "SessionLocal", FakeSession)
    assert client.post("/api/ui/pilots/web/run").status_code == 202
    failed = wait_for(client, "error")
    assert "Faltan datos" in failed["message"] and "DATA_DIR" in failed["message"]
