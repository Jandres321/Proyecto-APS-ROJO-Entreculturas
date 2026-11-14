"""Anthropic client: request shape and reply parsing, with the SDK faked (no network)."""

import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("anthropic")

from app.config import Settings  # noqa: E402
from app.lectura import vision  # noqa: E402
from app.llm import anthropic_client  # noqa: E402
from app.llm.anthropic_client import AnthropicClient, strict_schema  # noqa: E402
from app.llm.factory import get_llm_client  # noqa: E402
from app.pipeline.classify import LLM_SCHEMA  # noqa: E402


class FakeAsyncAnthropic:
    """Stands in for `anthropic.AsyncAnthropic`: records requests and returns canned replies."""

    def __init__(self, api_key=None):
        self.api_key = api_key
        self.requests: list[dict] = []
        self.reply = {"text": "{}", "stop_reason": "end_turn"}
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(
            stop_reason=self.reply["stop_reason"],
            content=[
                SimpleNamespace(type="thinking", thinking=""),
                SimpleNamespace(type="text", text=self.reply["text"]),
            ],
            usage=SimpleNamespace(input_tokens=11, output_tokens=7),
        )


@pytest.fixture
def client(monkeypatch) -> AnthropicClient:
    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropic", FakeAsyncAnthropic)
    return AnthropicClient(api_key="clave-de-prueba")


def test_strict_schema_closes_objects_and_requires_every_property():
    strict = strict_schema(vision.SCHEMA)
    assert strict["additionalProperties"] is False
    assert strict["required"] == list(vision.SCHEMA["properties"])
    assert vision.SCHEMA["required"] != strict["required"]  # the original is not modified
    nested = strict_schema(LLM_SCHEMA)["properties"]["items"]["items"]
    assert nested["additionalProperties"] is False and "id" in nested["required"]


def test_extract_uses_structured_output_not_a_forced_tool(client):
    client._client.reply = {
        "text": json.dumps({"tipo": "factura", "importe": 1500.0}),
        "stop_reason": "end_turn",
    }
    out = asyncio.run(client.extract([b"png-inventado"], vision.SCHEMA, "Lee la página."))
    assert out == {"tipo": "factura", "importe": 1500.0}
    (request,) = client._client.requests
    assert "tool_choice" not in request and "tools" not in request
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert request["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert request["model"] == anthropic_client.DEFAULT_MODEL
    kinds = [block["type"] for block in request["messages"][0]["content"]]
    assert kinds == ["image", "text"]
    assert client.usage == [{"input": 11, "output": 7}]


def test_complete_json_sends_only_text(client):
    client._client.reply = {"text": json.dumps({"items": []}), "stop_reason": "end_turn"}
    assert asyncio.run(client.complete_json("Clasifica.", LLM_SCHEMA)) == {"items": []}
    content = client._client.requests[0]["messages"][0]["content"]
    assert content == [{"type": "text", "text": "Clasifica."}]


@pytest.mark.parametrize(
    "reply",
    [
        {"text": "", "stop_reason": "refusal"},
        {"text": '{"tipo": "fac', "stop_reason": "max_tokens"},
        {"text": "no es json", "stop_reason": "end_turn"},
        {"text": "[1, 2]", "stop_reason": "end_turn"},
    ],
)
def test_unusable_reply_is_an_empty_result(client, reply):
    client._client.reply = reply
    assert asyncio.run(client.extract([b"x"], vision.SCHEMA, "Lee.")) == {}


def test_factory_falls_back_to_the_sdk_key_variable(monkeypatch):
    """A .env with only ANTHROPIC_API_KEY (the one the command-line conciliador uses) is enough."""
    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropic", FakeAsyncAnthropic)
    settings = Settings(llm_provider="anthropic", llm_api_key="", anthropic_api_key="clave-sdk")
    assert get_llm_client(settings)._client.api_key == "clave-sdk"
    settings = Settings(
        llm_provider="anthropic",
        llm_api_key="clave-propia",
        anthropic_api_key="clave-sdk",
        llm_model="claude-sonnet-5-5",
    )
    llm = get_llm_client(settings)
    assert llm._client.api_key == "clave-propia"
    assert llm.model == "claude-sonnet-5-5"
