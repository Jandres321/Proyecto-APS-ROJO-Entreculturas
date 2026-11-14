"""Anthropic (Claude) client. Only used when LLM_PROVIDER=anthropic.

Sending listing text or documents to this provider stays behind ALLOW_EXTERNAL_LLM (callers
check it). JSON output is forced with a single tool whose input schema is the expected schema.
"""

import base64
from collections.abc import Sequence
from typing import Any

from app.llm.base import JsonSchema, LLMClient

DEFAULT_MODEL = "claude-haiku-5-5"  # cheapest model: the API budget is small


class AnthropicClient(LLMClient):
    provider = "anthropic"

    def __init__(self, model: str = "", api_key: str = "", max_concurrency: int = 2, **kw: Any):
        super().__init__(model=model or DEFAULT_MODEL, api_key=api_key, max_concurrency=max_concurrency)
        from anthropic import AsyncAnthropic  # optional dependency (extra "llm")

        self._client = AsyncAnthropic(api_key=api_key)
        self.usage: list[dict[str, int]] = []

    async def _call(self, content: list[dict[str, Any]], schema: JsonSchema, max_tokens: int) -> dict[str, Any]:
        tool = {"name": "responder", "description": "Devuelve la respuesta.", "input_schema": schema}
        async with self.semaphore:
            msg = await self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                tools=[tool],
                tool_choice={"type": "tool", "name": "responder"},
                messages=[{"role": "user", "content": content}],
            )
        self.usage.append({"input": msg.usage.input_tokens, "output": msg.usage.output_tokens})
        for block in msg.content:
            if block.type == "tool_use":
                return dict(block.input)
        return {}

    async def complete_json(self, prompt: str, schema: JsonSchema) -> dict[str, Any]:
        return await self._call([{"type": "text", "text": prompt}], schema, max_tokens=1024)

    async def extract(self, images: Sequence[bytes], schema: JsonSchema, prompt: str) -> dict[str, Any]:
        content: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(img).decode()},
            }
            for img in images
        ]
        content.append({"type": "text", "text": prompt})
        return await self._call(content, schema, max_tokens=2048)
