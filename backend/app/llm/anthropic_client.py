"""Anthropic (Claude) client. Only used when LLM_PROVIDER=anthropic.

Sending listing text or documents to this provider stays behind ALLOW_EXTERNAL_LLM (callers
check it). JSON output uses structured outputs (`output_config.format` with the expected JSON
schema): the reply is a JSON document that validates against it. Forcing a tool call
(`tool_choice`) is not an option: current models (Claude Sonnet 5.5, Opus 5.5...) reject it.
"""

import base64
import json
from collections.abc import Sequence
from typing import Any

from app.llm.base import JsonSchema, LLMClient

DEFAULT_MODEL = "claude-haiku-5-5"  # cheapest model: the API budget is small
# Thinking is on by default in current models and counts against max_tokens: leave room so the
# JSON is never cut, and keep the effort low (these are small extraction tasks).
MAX_TOKENS = 16000
EFFORT = "low"


def strict_schema(schema: JsonSchema) -> JsonSchema:
    """Copy of `schema` in the shape structured outputs require: every object closed
    (`additionalProperties: false`) and all its properties required. Optional fields must
    therefore allow null in their type, which is how the schemas of this app are written."""
    if isinstance(schema, dict):
        out = {key: strict_schema(value) for key, value in schema.items()}
        if out.get("type") == "object" and isinstance(out.get("properties"), dict):
            out["required"] = list(out["properties"])
            out["additionalProperties"] = False
        return out
    if isinstance(schema, list):
        return [strict_schema(item) for item in schema]
    return schema


class AnthropicClient(LLMClient):
    provider = "anthropic"

    def __init__(self, model: str = "", api_key: str = "", max_concurrency: int = 2, **kw: Any):
        super().__init__(
            model=model or DEFAULT_MODEL, api_key=api_key, max_concurrency=max_concurrency
        )
        from anthropic import AsyncAnthropic  # optional dependency (extra "llm")

        # Without an explicit key the SDK takes it from the environment (ANTHROPIC_API_KEY).
        self._client = AsyncAnthropic(api_key=api_key or None)

    async def _call(self, content: list[dict[str, Any]], schema: JsonSchema) -> dict[str, Any]:
        async with self.semaphore:
            msg = await self._client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                messages=[{"role": "user", "content": content}],
                output_config={
                    "effort": EFFORT,
                    "format": {"type": "json_schema", "schema": strict_schema(schema)},
                },
            )
        self.usage.append({"input": msg.usage.input_tokens, "output": msg.usage.output_tokens})
        # A refusal or a cut reply carries no usable JSON: same as "nothing read".
        if msg.stop_reason in ("refusal", "max_tokens"):
            return {}
        text = next((block.text for block in msg.content if block.type == "text"), "")
        try:
            data = json.loads(text)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    async def complete_json(self, prompt: str, schema: JsonSchema) -> dict[str, Any]:
        return await self._call([{"type": "text", "text": prompt}], schema)

    async def extract(
        self, images: Sequence[bytes], schema: JsonSchema, prompt: str
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.b64encode(img).decode(),
                },
            }
            for img in images
        ]
        content.append({"type": "text", "text": prompt})
        return await self._call(content, schema)
