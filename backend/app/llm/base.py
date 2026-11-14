"""Provider-agnostic LLM interface.

The LLM only extracts data from documents and judges qualitative rules; numeric checks
are always done in code. Every call returns JSON validated against `schema`.
"""

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

JsonSchema = dict[str, Any]


class LLMClient(ABC):
    provider: str = "base"

    def __init__(self, model: str, api_key: str = "", max_concurrency: int = 4) -> None:
        self.model = model
        self.api_key = api_key
        # Subclasses should wrap provider calls in `async with self.semaphore:`.
        self.semaphore = asyncio.Semaphore(max_concurrency)

    @abstractmethod
    async def extract(
        self, images: Sequence[bytes], schema: JsonSchema, prompt: str
    ) -> dict[str, Any]:
        """Multimodal extraction: page images (PNG bytes) + prompt -> JSON matching `schema`."""

    @abstractmethod
    async def complete_json(self, prompt: str, schema: JsonSchema) -> dict[str, Any]:
        """Text-only completion -> JSON matching `schema`."""
