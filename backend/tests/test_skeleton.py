"""Smoke tests for the project skeleton."""

import asyncio

import pytest

from app import models
from app.adapters import get_adapter
from app.config import Settings
from app.enums import Donor
from app.llm.factory import get_llm_client
from app.llm.fake import FakeLLMClient


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_cors_allows_frontend(client):
    resp = client.options(
        "/health",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
    )
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_metadata_has_all_tables():
    expected = {
        "project",
        "budget_line",
        "expense",
        "document",
        "expense_document",
        "extraction",
        "rule",
        "finding",
        "analysis_job",
    }
    assert expected <= set(models.Base.metadata.tables)


@pytest.mark.parametrize("donor", list(Donor))
def test_adapter_registry(donor):
    adapter = get_adapter(donor)
    assert adapter.donor == donor
    assert adapter.partida_catalog()
    assert adapter.limits()


def test_llm_factory_fake():
    client = get_llm_client(Settings(llm_provider="fake", llm_max_concurrency=2))
    assert isinstance(client, FakeLLMClient)
    assert asyncio.run(client.complete_json("hola", {"type": "object"})) == {}


def test_llm_factory_unknown_provider():
    with pytest.raises(ValueError):
        get_llm_client(Settings(llm_provider="does-not-exist"))


def test_data_dir_contains_pilot(data_dir):
    pilot = data_dir / "DocJustificativa" / "24-UG-226 Adjumani - Xunta 2024"
    if not pilot.is_dir():
        pytest.skip("Pilot project folder not found in DATA_DIR")
    assert any(pilot.iterdir())
