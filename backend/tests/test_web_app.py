"""Web review app: the static page, the expense-centred view and the project API, on invented
data (database tests are rolled back and skipped without PostgreSQL)."""

import io
import json
import re
import shutil
import subprocess
import zipfile
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.enums import (
    Confidence,
    Donor,
    FindingStatus,
    JobState,
    MatchMethod,
    Severity,
    SourceType,
    TipoSegmento,
)
from app.models import (
    AnalysisJob,
    Document,
    Expense,
    ExpenseDocument,
    ExpenseSegment,
    Finding,
    Project,
    Segmento,
)
from app.pipeline.match_content import ContentMatchSummary
from app.pipeline.run import RunSummary
from app.review import MISSING, NOT_REQUIRED_INDIRECT, VERIFIED, DocumentStatus
from app.review_view import expense_verdict, project_review

D = Decimal
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
    project = Project(
        key="web",
        name="web test",
        donor=Donor.XUNTA,
        code="WEB-1",
        source_type=SourceType.LOCAL,
        folder="carpeta",
        report_file="i.xls",
    )
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


# --- project API (database) ---


def test_settings_tell_the_page_what_is_available(client):
    data = client.get("/api/settings").json()
    assert {d["value"] for d in data["donors"]} == {"xunta", "aecid"}
    assert {m["value"] for m in data["llm_modes"]} == {"off", "dudosas", "todas"}
    assert set(data["drive"]) == {"available", "message"}
    assert isinstance(data["external_llm"], bool)


def test_api_serves_projects_review_budget_and_excel(db_session, project, api):
    fill(db_session, project)

    listing = api.get("/api/projects").json()
    (shown,) = [p for p in listing if p["key"] == "web"]
    assert (shown["expenses"], shown["loaded"], shown["source_type"]) == (2, True, "local")
    assert shown["job"] == {"state": "idle"} and shown["llm_mode"] == "off"

    review = api.get("/api/projects/web/review").json()
    assert len(review["expenses"]) == 2 and "rules" in review
    assert set(api.get("/api/projects/web/budget").json()) == {"partidas", "limits"}

    excel = api.get("/api/projects/web/export.xlsx")
    assert excel.status_code == 200 and excel.content[:2] == b"PK"
    assert "WEB-1_revision.xlsx" in excel.headers["content-disposition"]
    report = api.get("/api/projects/web/report.xlsx")
    assert report.status_code == 200 and report.content[:2] == b"PK"
    assert "WEB-1_informe_analisis_" in report.headers["content-disposition"]

    assert api.get("/api/projects/otro/review").status_code == 404


def test_a_project_that_was_never_analysed_is_not_loaded(db_session, project, api):
    assert api.get("/api/projects/web").json()["loaded"] is False
    assert api.get("/api/projects/web/review").status_code == 404


def test_settings_of_a_project_can_be_changed(db_session, project, api):
    changed = api.patch(
        "/api/projects/web",
        json={"start_date": "2024-06-01", "end_date": "2024-12-31", "llm_mode": "dudosas"},
    ).json()
    assert (changed["start_date"], changed["end_date"], changed["llm_mode"]) == (
        "2024-06-01",
        "2024-12-31",
        "dudosas",
    )
    backwards = api.patch("/api/projects/web", json={"end_date": "2024-01-01"})
    assert backwards.status_code == 400 and "anterior" in backwards.json()["detail"]
    cleared = api.patch("/api/projects/web", json={"start_date": None, "end_date": None}).json()
    assert cleared["start_date"] is None


def test_sample_projects_cannot_be_deleted(db_session, project, api):
    project.is_sample = True
    db_session.flush()
    refused = api.delete("/api/projects/web")
    assert refused.status_code == 400 and "ejemplo" in refused.json()["detail"]


def _zip(**files: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buffer.getvalue()


def test_a_project_can_be_created_by_uploading_its_files(data_root, api):
    response = api.post(
        "/api/projects/upload",
        data={"name": "Proyecto subido", "donor": "aecid", "start_date": "2023-02-01"},
        files=[
            ("report", ("Informe final.xlsx", b"PK-fake", "application/octet-stream")),
            ("documents", ("1. Factura.pdf", b"%PDF-1", "application/pdf")),
            (
                "documents",
                ("lote.zip", _zip(**{"sub/2. Recibo.pdf": b"%PDF-2"}), "application/zip"),
            ),
        ],
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert (created["key"], created["source_type"], created["report_file"]) == (
        "proyecto-subido",
        "upload",
        "Informe final.xlsx",
    )
    assert created["saved"] == {"documentos": 2, "informe": 1}
    folder = data_root / "_proyectos" / "proyecto-subido"
    assert (folder / "Informe final.xlsx").is_file()
    assert (folder / "justificantes" / "1. Factura.pdf").is_file()
    assert (folder / "justificantes" / "sub" / "2. Recibo.pdf").is_file()
    assert not (folder / "justificantes" / "lote.zip").exists()  # unpacked

    more = api.post(
        "/api/projects/proyecto-subido/files",
        files=[("documents", ("3. Nomina.pdf", b"%PDF-3", "application/pdf"))],
    )
    assert more.json()["saved"] == {"documentos": 1, "informe": 0}

    assert (
        api.delete("/api/projects/proyecto-subido", params={"delete_files": True}).status_code
        == 204
    )
    assert not folder.exists()


def test_uploads_are_validated(data_root, api):
    def upload(report_name, doc_name, doc=b"%PDF"):
        return api.post(
            "/api/projects/upload",
            data={"name": "X", "donor": "xunta"},
            files=[
                ("report", (report_name, b"x", "application/octet-stream")),
                ("documents", (doc_name, doc, "application/octet-stream")),
            ],
        )

    assert "Excel" in upload("informe.pdf", "a.pdf").json()["detail"]
    assert "no admitido" in upload("informe.xlsx", "virus.exe").json()["detail"]
    escape = upload("informe.xlsx", "malo.zip", _zip(**{"../../fuera.pdf": b"x"}))
    assert escape.status_code == 400 and "rutas no válidas" in escape.json()["detail"]
    assert not (data_root.parent / "fuera.pdf").exists()


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

    page = api.get(f"/api/documents/{inside}/page/2.png", params={"rotation": 180})
    assert page.status_code == 200 and page.content[:4] == b"\x89PNG"
    assert page.headers["x-pages"] == "3"
    assert api.get(f"/api/documents/{inside}/page/99.png").status_code == 200  # clamped
    original = api.get(f"/api/documents/{inside}/file")
    assert original.status_code == 200 and original.content[:4] == b"%PDF"

    # a path that escapes DATA_DIR, a file that is not a PDF and an unknown document
    for document_id in (outside, sheet, 0):
        assert api.get(f"/api/documents/{document_id}/page/1.png").status_code == 404
        assert api.get(f"/api/documents/{document_id}/file").status_code == 404


# --- launching the pipeline (database) ---


def summary():
    matching = ContentMatchSummary(expenses_linked=3, verified=1, from_name=2, from_content=0)
    return RunSummary(project_id=0, expenses=5, documents=4, matching=matching, report_reread=True)


def fraction_seen(session, project):
    session.expire_all()
    return sync_state(session, project)


def sync_state(session, project):
    from app.services.jobs import latest_job

    return latest_job(session, project).state.value


def test_a_run_is_persisted_with_its_progress_and_outcome(
    db_session, project, api, sync_jobs, data_root, monkeypatch
):
    (data_root / "carpeta").mkdir()
    progress = []

    def fake_pipeline(session, proj, *, reread_report, on_progress):
        on_progress("reading", 0.5, "Leyendo el contenido de los PDF")
        progress.append(fraction_seen(session, proj))
        assert reread_report is True
        return summary()

    monkeypatch.setattr(sync_jobs, "run_pipeline", fake_pipeline)
    started = api.post("/api/projects/web/run", params={"reread": True})
    assert started.status_code == 202
    assert progress == ["running"]
    done = api.get("/api/projects/web/run").json()
    assert done["state"] == "done" and done["fraction"] == 1.0
    assert "Análisis terminado: 5 gastos y 4 documentos" in done["message"]
    assert "Se ha releído el informe" in done["message"]
    assert api.post("/api/projects/otro/run").status_code == 404


def test_one_run_at_a_time_and_interrupted_runs_are_closed(db_session, project, api, sync_jobs):
    running = AnalysisJob(project_id=project.id, state=JobState.RUNNING, steps=7)
    db_session.add(running)
    db_session.flush()
    assert api.post("/api/projects/web/run").status_code == 409
    assert sync_jobs.mark_interrupted_jobs(db_session) == 1
    closed = api.get("/api/projects/web/run").json()
    assert closed["state"] == "error" and "reinició" in closed["message"]


def test_a_failed_run_is_reported_not_hidden(db_session, project, api, sync_jobs, data_root):
    # the folder exists but the report does not: the pipeline refuses to start
    (data_root / "carpeta").mkdir()
    assert api.post("/api/projects/web/run").status_code == 202
    failed = api.get("/api/projects/web/run").json()
    assert failed["state"] == "error"
    assert "Faltan datos" in failed["message"] and "DATA_DIR" in failed["message"]


# --- the page's modules (web/js) and its example data (?mock=1) ---

WEB = Path(__file__).resolve().parents[1] / "app" / "web"
JS_FILES = sorted((WEB / "js").glob("*.js"))
# U+1F000-U+1FAFF pictographs, U+2600-U+27BF symbols and dingbats, U+2B00-U+2BFF arrows/stars
# and the emoji variation selector.
EMOJI = re.compile("[\U0001f000-\U0001faff\u2600-\u27bf\u2b00-\u2bff\ufe0f]")
SVG_NAMESPACE = "http://www.w3.org/2000/svg"  # an identifier inside the drawn example page


def test_the_page_loads_its_modules_from_the_server(client):
    html = client.get("/").text
    assert '<script type="module" src="/js/app.js"></script>' in html
    assert {p.name for p in JS_FILES} >= {"app.js", "api.js", "mock.js", "review.js", "viewer.js"}
    for path in JS_FILES:
        served = client.get(f"/js/{path.name}")
        assert served.status_code == 200, path.name
        assert served.headers["content-type"].startswith("text/javascript")
    for name in ("nada.js", "..%2Fmain.py", "index.html"):
        assert client.get(f"/js/{name}").status_code == 404


def test_the_modules_load_nothing_from_outside_and_store_nothing():
    for path in [WEB / "index.html", *JS_FILES]:
        text = path.read_text(encoding="utf-8").replace(SVG_NAMESPACE, "")
        assert "http://" not in text and "https://" not in text, path.name
        for storage in ("localStorage", "sessionStorage", "indexedDB", "document.cookie"):
            assert storage not in text, f"{storage} in {path.name}"


def test_the_page_has_no_emojis_and_no_positive_decision_in_bulk():
    for path in [WEB / "index.html", *JS_FILES]:
        text = path.read_text(encoding="utf-8")
        assert not EMOJI.search(text), f"emoji in {path.name}: {EMOJI.search(text).group()!r}"
        # the verdict comes from the server; nothing marks several expenses as fine at once
        for gone in ("verdictOf", "Dar por bueno", "bulk("):
            assert gone not in text, f"{gone} in {path.name}"


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js no está instalado")
    return node


def test_every_module_is_valid_javascript():
    node = _node()
    for path in JS_FILES:
        result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, f"{path.name}: {result.stderr}"


MOCK_PROBE = """
globalThis.location = { search: "?mock=1" };
const mock = await import(process.argv[2]);  // the URL of mock.js
const call = async (path, options) => {
  const r = await mock.respond(path, options);
  return { status: r.status, body: r.status === 204 ? null : await r.json() };
};
const json = (method, body) => ({ method, headers: { "Content-Type": "application/json" },
                                  body: JSON.stringify(body) });
const projects = await call("/projects");
const review = await call(`/projects/${projects.body[0].key}/review`);
const blocking = review.body.expenses.find((e) => e.reasons.some((r) => r.blocking));
const refused = await call(`/expenses/${blocking.id}/decision`,
                           json("PATCH", { status: "correct" }));
const pending = review.body.expenses.find((e) => e.verdict.code === "review");
const decided = await call(`/expenses/${pending.id}/decision`,
                           json("PATCH", { status: "real_issue", note: "Nota" }));
const page = await mock.respond(`/documents/${pending.documents[0]?.id ?? 0}/page/1.png?dpi=150`);
console.log(JSON.stringify({ projects, review, refused, decided,
                             page: { status: page.status, pages: page.headers.get("X-Pages") } }));
"""


def test_the_example_data_follows_the_api_contract(tmp_path):
    """The shape the page expects from GET /review and PATCH /decision (docs/pendiente_web.md)."""
    node = _node()
    probe = tmp_path / "probe.mjs"
    probe.write_text(MOCK_PROBE, encoding="utf-8")
    mock_url = (WEB / "js" / "mock.js").as_uri()
    result = subprocess.run(
        [node, str(probe), mock_url], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)

    review = out["review"]["body"]
    assert set(review["counts"]) == {"pending", "reviewed", "ok", "na"}
    assert sum(review["counts"].values()) == len(review["expenses"])
    for e in review["expenses"]:
        assert e["verdict"]["code"] in {"ok", "review", "na"}
        assert e["verdict"]["label"] in {"Bien", "Necesita revisión", "No aplica"}
        assert all(set(r) == {"code", "label", "blocking"} for r in e["reasons"])
        assert e["decision"]["status"] in {"pending", "correct", "real_issue"}
        assert {"status", "note", "decided_at"} <= set(e["decision"])
        assert all(set(c) == {"field", "label", "listed", "read", "match"} for c in e["compare"])
        # as in app/verdict.py: there are reasons exactly when the expense needs review
        assert (e["verdict"]["code"] == "review") == bool(e["reasons"])
    assert {e["verdict"]["code"] for e in review["expenses"]} == {"ok", "review", "na"}

    # "Correcto" needs a note when a reason is blocking; a decision returns the expense
    assert out["refused"]["status"] == 422 and "bloqueante" in out["refused"]["body"]["detail"]
    assert out["decided"]["status"] == 200
    assert out["decided"]["body"]["decision"]["status"] == "real_issue"
    assert out["decided"]["body"]["decision"]["note"] == "Nota"
    assert out["page"]["status"] == 200 and int(out["page"]["pages"]) >= 1
