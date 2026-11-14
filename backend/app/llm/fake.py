"""Deterministic offline client for tests and local development without an API key."""

from collections.abc import Sequence
from typing import Any

from app.llm.base import JsonSchema, LLMClient


class FakeLLMClient(LLMClient):
    provider = "fake"

    def __init__(
        self, model: str = "fake", responses: list[dict[str, Any]] | None = None, **kwargs: Any
    ) -> None:
        super().__init__(model=model or "fake", **kwargs)
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    def _next(self) -> dict[str, Any]:
        return self.responses.pop(0) if self.responses else {}

    async def extract(
        self, images: Sequence[bytes], schema: JsonSchema, prompt: str
    ) -> dict[str, Any]:
        self.calls.append({"method": "extract", "n_images": len(images), "prompt": prompt})
        return self._next()

    async def complete_json(self, prompt: str, schema: JsonSchema) -> dict[str, Any]:
        self.calls.append({"method": "complete_json", "prompt": prompt})
        return self._next()
