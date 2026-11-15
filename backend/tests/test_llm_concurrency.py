"""The vision pass must finish when there are more pages than concurrent LLM calls allowed.

Regression: the reading used to hold the client's semaphore while the client took it again for
the provider call. After the first calls returned, every waiting page held one permit and waited
for another: the run hung for ever at "Leyendo el contenido de los PDF". The offline fake client
never showed it because it does not use the semaphore.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.lectura import pages as pg
from app.lectura import vision
from app.llm.base import LLMClient

PAGES = 12
TIMEOUT = 10  # seconds: a deadlock fails the test instead of hanging the suite


class ContractClient(LLMClient):
    """A provider client written as the base class asks: it bounds its own calls with
    `self.semaphore` and really waits inside (as a network call does)."""

    provider = "contract"

    def __init__(self, max_concurrency: int = 2) -> None:
        super().__init__(model="modelo", max_concurrency=max_concurrency)
        self.running = self.peak = self.calls = 0

    async def extract(self, images, schema, prompt):
        async with self.semaphore:
            self.running += 1
            self.peak = max(self.peak, self.running)
            await asyncio.sleep(0.01)
            self.running -= 1
            self.calls += 1
        return {"tipo": "factura", "importe": 100.0}

    async def complete_json(self, prompt, schema):
        return {}


def read_pages(client, tmp_path, monkeypatch) -> tuple[list[pg.Page], vision.LlmReadSummary]:
    monkeypatch.setattr(pg, "render", lambda *args, **kwargs: b"png inventado")
    settings = Settings(cache_dir=tmp_path / "cache")
    items = [
        (tmp_path / "a.pdf", "a" * 64, pg.Page(n=n, text="", source="ocr"))
        for n in range(1, PAGES + 1)
    ]
    summary = vision.LlmReadSummary()

    async def run():
        return await asyncio.wait_for(
            vision.enrich_selected(items, client, settings, summary), timeout=TIMEOUT
        )

    return asyncio.run(run()), summary


def test_more_pages_than_concurrent_calls_do_not_deadlock(tmp_path, monkeypatch):
    client = ContractClient(max_concurrency=2)
    pages, summary = read_pages(client, tmp_path, monkeypatch)
    assert len(pages) == PAGES and all(p.llm_fields for p in pages)
    assert client.calls == PAGES and summary.pages_failed == 0
    assert client.peak == 2  # the limit is still respected: never more than 2 calls at once


def test_the_anthropic_client_reads_many_pages_without_hanging(tmp_path, monkeypatch):
    anthropic = pytest.importorskip("anthropic")
    from app.llm.anthropic_client import AnthropicClient

    class FakeAsyncAnthropic:
        def __init__(self, **kwargs):
            self.options = kwargs
            self.messages = SimpleNamespace(create=self.create)

        async def create(self, **kwargs):
            await asyncio.sleep(0.01)
            return SimpleNamespace(
                stop_reason="end_turn",
                content=[SimpleNamespace(type="text", text='{"tipo": "recibo"}')],
                usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            )

    monkeypatch.setattr(anthropic, "AsyncAnthropic", FakeAsyncAnthropic)
    client = AnthropicClient(api_key="clave-de-prueba", max_concurrency=2)
    pages, summary = read_pages(client, tmp_path, monkeypatch)
    assert [p.llm_fields for p in pages] == [{"tipo": "recibo"}] * PAGES
    assert summary.pages_failed == 0 and len(client.usage) == PAGES
    # a call that never answers must end by itself instead of holding the run for ever
    assert 0 < client._client.options["timeout"] <= 300
