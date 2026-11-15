from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.adapters import AecidAdapter, XuntaAdapter
from app.config import get_settings
from app.main import app
from app.schemas import BudgetLineIn, ExpenseIn

XUNTA_REPORT = (
    "DocJustificativa/24-UG-226 Adjumani - Xunta 2024/"
    "Informe_economico_final_Adjuamni_def 14.10.25 v1.xls"
)
AECID_REPORT = "DocJustificativa/22-PY-136 - AECID/Informe seguimiento y final_31_10.xlsx"


@pytest.fixture(scope="session")
def data_dir() -> Path:
    """Root of the confidential dataset; skips the test when it is not available."""
    settings = get_settings()
    if not settings.data_dir_available:
        pytest.skip("DATA_DIR not set or not found: skipping test that needs real data")
    assert settings.data_dir is not None
    return settings.data_dir


def _report(data_dir: Path, relative: str) -> Path:
    path = data_dir / relative
    if not path.is_file():
        pytest.skip(f"Report not found in DATA_DIR: {relative}")
    return path


@pytest.fixture(scope="session")
def xunta_report(data_dir: Path) -> Path:
    return _report(data_dir, XUNTA_REPORT)


@pytest.fixture(scope="session")
def aecid_report(data_dir: Path) -> Path:
    return _report(data_dir, AECID_REPORT)


@pytest.fixture(scope="session")
def xunta_docs(xunta_report: Path) -> Path:
    return xunta_report.parent


@pytest.fixture(scope="session")
def aecid_docs(aecid_report: Path) -> Path:
    return aecid_report.parent


@pytest.fixture(scope="session")
def xunta_expenses(xunta_report: Path) -> list[ExpenseIn]:
    return XuntaAdapter().parse_expenses(xunta_report)


@pytest.fixture(scope="session")
def xunta_budget(xunta_report: Path) -> list[BudgetLineIn]:
    return XuntaAdapter().parse_budget(xunta_report)


@pytest.fixture(scope="session")
def aecid_expenses(aecid_report: Path) -> list[ExpenseIn]:
    return AecidAdapter().parse_expenses(aecid_report)


@pytest.fixture(scope="session")
def aecid_budget(aecid_report: Path) -> list[BudgetLineIn]:
    return AecidAdapter().parse_budget(aecid_report)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def db_session() -> Iterator[Session]:
    """Session on the configured PostgreSQL inside a transaction that is always rolled back.

    Skips when the database is not reachable or not migrated (`alembic upgrade head`).
    """
    from app.db import engine

    try:
        inspector = inspect(engine)  # own short-lived connection, not the test transaction
        columns = (
            {c["name"] for c in inspector.get_columns("expense")}
            if inspector.has_table("expense")
            else set()
        )
    except OperationalError:
        pytest.skip("PostgreSQL not reachable (start it with `docker compose up -d db`)")
    if "extra" not in columns:
        pytest.skip("Database not migrated: run `alembic upgrade head`")

    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def api(db_session, client):
    """Test client whose requests use the test transaction (`db_session`)."""
    from app.db import get_session
    from app.main import app

    app.dependency_overrides[get_session] = lambda: db_session
    yield client
    app.dependency_overrides.clear()


class _SameSession:
    """`with SessionLocal() as s` that hands out the test session and does not close it."""

    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self

    def __enter__(self):
        return self.session

    def __exit__(self, *args):
        return False


@pytest.fixture
def sync_jobs(db_session, monkeypatch):
    """Analysis runs execute synchronously, in the test transaction."""
    from app.api import projects as projects_api
    from app.services import jobs

    monkeypatch.setattr(jobs.db, "SessionLocal", _SameSession(db_session))
    monkeypatch.setattr(projects_api, "launcher", jobs.execute_job)
    return jobs


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    """DATA_DIR pointing to an empty temporary folder."""
    root = tmp_path / "datos"
    root.mkdir()
    monkeypatch.setattr(get_settings(), "data_dir", root)
    return root
