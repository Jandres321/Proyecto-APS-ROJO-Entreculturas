"""The review app (ui/streamlit_app.py) driven with Streamlit's AppTest.

It talks to the real (rolled back) database: `db.SessionLocal` is replaced by the test session.
"""

from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from app import db  # noqa: E402
from app.enums import Donor, FindingStatus, Severity  # noqa: E402
from app.models import BudgetLine, Expense, Finding, Project  # noqa: E402
from app.pipeline import run as run_module  # noqa: E402
from app.pipeline.run import PILOTS, PilotProject  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "ui" / "streamlit_app.py"
TEST_PILOT = PilotProject("test", "Proyecto de prueba", Donor.XUNTA, "UI-TEST", "x", "y.xls")


class SharedSession:
    """Stand-in for `SessionLocal`: always hands out the test session and never closes it."""

    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self

    def __enter__(self):
        return self.session

    def __exit__(self, *exc):
        return False


@pytest.fixture
def ui_session(db_session, monkeypatch):
    monkeypatch.setattr(db, "SessionLocal", SharedSession(db_session))
    monkeypatch.setitem(run_module.PILOTS, "test", TEST_PILOT)
    return db_session


def open_app(timeout: int = 20) -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=timeout).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def choose(at: AppTest, key: str) -> AppTest:
    at.sidebar.selectbox[0].select(key).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def button(at: AppTest, label: str):
    return next(b for b in at.button if b.label == label)


def project_with_findings(session) -> tuple[Project, dict[str, Finding]]:
    project = Project(name="ui test", donor=Donor.XUNTA, code=TEST_PILOT.code)
    session.add(project)
    session.flush()
    expense = Expense(
        project_id=project.id,
        order_no="12",
        source_row=20,
        partida_code="A.X",
        concept="Auditoría",
        amount_donor=Decimal("2600"),
    )
    session.add(expense)
    session.add(BudgetLine(project_id=project.id, partida_code="A.X", approved_donor=Decimal(2500)))
    session.flush()
    findings = {
        "missing": Finding(
            project_id=project.id,
            expense_id=expense.id,
            rule_code="DOC_MISSING",
            severity=Severity.HIGH,
            message="No se ha encontrado ningún documento para el gasto nº 12.",
            evidence={"order_no": "12"},
        ),
        "limit": Finding(
            project_id=project.id,
            rule_code="LIMIT_EXCEEDED",
            severity=Severity.HIGH,
            message="Se supera el tope XUNTA_AUDITORIA_MAX_2500.",
            evidence={"subject": "XUNTA_AUDITORIA_MAX_2500"},
        ),
    }
    session.add_all(findings.values())
    session.flush()
    return project, findings


def test_app_starts_and_asks_to_launch_an_analysis(ui_session):
    at = choose(open_app(), "test")
    assert any("Lanzar análisis" in i.value for i in at.info)
    assert button(at, "Lanzar análisis").proto.type == "primary"


def test_app_lists_findings_budget_and_metrics(ui_session):
    project_with_findings(ui_session)
    at = choose(open_app(), "test")

    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Incidencias abiertas"] == "2" and metrics["Aceptadas"] == "0"
    assert at.header[0].value == "Proyecto de prueba"
    assert [t.label for t in at.tabs] == ["Incidencias", "Presupuesto y topes"]
    assert any("Selecciona una fila" in i.value for i in at.info)
    # Findings table + budget table + caps table.
    assert len(at.dataframe) == 3
    budget = at.dataframe[1].value
    assert budget["Partida"].tolist() == ["A.X"] and budget["Semáforo"].tolist() == ["rojo"]


def test_selecting_a_finding_and_accepting_it_stores_the_decision(ui_session):
    _, findings = project_with_findings(ui_session)
    at = choose(open_app(), "test")

    at.session_state["selected_finding"] = findings["missing"].id
    at.run()
    assert not at.exception
    assert at.subheader[0].value == "Gasto sin justificante"
    # No document for this finding: the right-hand panel says so instead of failing.
    assert any("no apunta a un documento" in i.value for i in at.info)

    next(t for t in at.text_input if t.label == "Nota (opcional)").input("Pedir factura al socio")
    button(at, "✅ Aceptar").click().run()
    assert not at.exception

    ui_session.refresh(findings["missing"])
    assert findings["missing"].status == FindingStatus.ACCEPTED
    assert findings["missing"].reviewer_note == "Pedir factura al socio"
    assert {m.label: m.value for m in at.metric}["Aceptadas"] == "1"
    # Accepted findings drop out of the default ("abierta") filter but stay on screen.
    assert any("Ya no aparece en la tabla" in c.value for c in at.caption)


def test_false_positive_and_reopen(ui_session):
    _, findings = project_with_findings(ui_session)
    at = choose(open_app(), "test")
    at.session_state["selected_finding"] = findings["limit"].id
    at.run()

    button(at, "🚫 Falso positivo").click().run()
    ui_session.refresh(findings["limit"])
    assert findings["limit"].status == FindingStatus.FALSE_POSITIVE

    button(at, "↺ Reabrir").click().run()
    ui_session.refresh(findings["limit"])
    assert findings["limit"].status == FindingStatus.OPEN


def test_excel_is_prepared_and_offered_for_download(ui_session):
    project_with_findings(ui_session)
    at = choose(open_app(), "test")
    assert not at.sidebar.get("download_button")

    button(at, "Preparar Excel").click().run()
    assert not at.exception
    assert at.session_state["excel_bytes"][:2] == b"PK"  # an .xlsx is a zip
    assert at.sidebar.get("download_button")


def test_missing_database_is_reported_not_crashed(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(
        "postgresql+psycopg://x:x@127.0.0.1:1/x", connect_args={"connect_timeout": 2}
    )
    broken = sessionmaker(bind=engine)
    monkeypatch.setattr(db, "SessionLocal", broken)
    at = AppTest.from_file(str(APP), default_timeout=30).run()
    assert not at.exception
    assert any("No hay conexión con PostgreSQL" in e.value for e in at.error)


# --- full pipeline on the real data (skipped without DATA_DIR) ---


def test_launching_the_analysis_runs_the_whole_pipeline(ui_session, data_dir):
    pilot = PILOTS["aecid"]
    if not (data_dir / pilot.folder / pilot.report).is_file():
        pytest.skip("AECID report not found in DATA_DIR")

    at = choose(open_app(120), "aecid")
    button(at, "Lanzar análisis").click().run(timeout=180)
    assert not at.exception, [e.value for e in at.exception]
    assert not at.error, [e.value for e in at.error]
    assert any("Análisis terminado" in s.value for s in at.success)

    rules = {f.rule_code for f in ui_session.query(Finding).all()}
    # Content matching, document checks and budget all land in the same table.
    assert "SEGMENT_ORPHAN" in rules
    # Paraguay's folder is a sample (carpeta_completa: false): expenses without document are
    # "no incluidos en la muestra", not DOC_MISSING findings.
    assert "DOC_MISSING" not in rules
    assert rules & {
        "AMOUNT_MISMATCH",
        "DATE_MISMATCH",
        "PAYMENT_EVIDENCE_MISSING",
        "DOC_INCOMPLETE",
    }
    assert "BUDGET_DEVIATION" in rules
    assert "LIMIT_EXCEEDED" not in rules  # Paraguay: indirect costs at 11,84 %
    assert {m.label for m in at.metric} >= {"Gastos", "Incidencias abiertas"}
    assert {m.label: m.value for m in at.metric}["Gastos"] == "1321"


# --- PDF beside the finding ---


def make_pdf(path: Path, pages: int = 3) -> None:
    import pymupdf

    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = pymupdf.open()
    for n in range(1, pages + 1):
        pdf.new_page().insert_text((72, 72), f"Pagina {n}")
    pdf.save(path)
    pdf.close()


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    return tmp_path


def finding_on(session, relative_path: str, page: int | None = None):
    from app.models import Document

    project = Project(name="pdf test", donor=Donor.XUNTA, code=TEST_PILOT.code)
    session.add(project)
    session.flush()
    doc = Document(
        project_id=project.id,
        path=relative_path,
        filename=Path(relative_path).name,
        sha256="b" * 64,
        pages=3,
    )
    session.add(doc)
    session.flush()
    finding = Finding(
        project_id=project.id,
        document_id=doc.id,
        rule_code="AMOUNT_MISMATCH",
        severity=Severity.HIGH,
        message="El importe no coincide.",
        evidence={"pagina_inicio": page} if page else {"order_no": "3"},
    )
    session.add(finding)
    session.flush()
    return finding


def open_finding(finding) -> AppTest:
    at = choose(open_app(), "test")
    at.session_state["selected_finding"] = finding.id
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_pdf_opens_on_the_page_of_the_problem(ui_session, data_root):
    make_pdf(data_root / "docs" / "3. Factura.pdf")
    at = open_finding(finding_on(ui_session, "docs/3. Factura.pdf", page=2))

    page = next(n for n in at.number_input if n.label == "Página (de 3)")
    assert page.value == 2
    assert len(at.get("image")) == 1
    assert any(c.value == "Página 2 del problema." for c in at.caption)

    page.set_value(3).run()  # the reviewer can look at the rest of the document
    assert not at.exception and len(at.get("image")) == 1


def test_pdf_without_page_in_the_evidence_shows_the_first_one(ui_session, data_root):
    make_pdf(data_root / "docs" / "3. Factura.pdf")
    at = open_finding(finding_on(ui_session, "docs/3. Factura.pdf"))
    assert next(n for n in at.number_input if n.label == "Página (de 3)").value == 1
    assert any("no indica página" in c.value for c in at.caption)


def test_pdf_panel_explains_when_the_file_is_missing(ui_session, data_root):
    at = open_finding(finding_on(ui_session, "docs/no-existe.pdf", page=1))
    assert any("No se puede abrir el archivo" in w.value for w in at.warning)
    assert not at.get("image")


def test_pdf_panel_explains_when_the_file_is_not_a_pdf(ui_session, data_root):
    (data_root / "docs").mkdir()
    (data_root / "docs" / "hoja.xlsx").write_bytes(b"PK")
    at = open_finding(finding_on(ui_session, "docs/hoja.xlsx"))
    assert any("no se puede previsualizar" in w.value for w in at.warning)
