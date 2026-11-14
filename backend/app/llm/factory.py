"""Build the configured LLM client from settings (LLM_PROVIDER, LLM_MODEL, ...)."""

from app.config import Settings, get_settings
from app.llm.base import LLMClient
from app.llm.fake import FakeLLMClient

# Providers we plan to support; each will get its own module in app/llm/.
PLANNED_PROVIDERS = {"openai", "gemini"}


def get_llm_client(settings: Settings | None = None) -> LLMClient:
    settings = settings or get_settings()
    provider = settings.llm_provider.strip().lower()
    common = {
        "model": settings.llm_model,
        "api_key": settings.llm_api_key,
        "max_concurrency": settings.llm_max_concurrency,
    }

    if provider == "fake":
        return FakeLLMClient(**common)
    if provider == "anthropic":
        from app.llm.anthropic_client import AnthropicClient

        common["api_key"] = settings.llm_api_key or settings.anthropic_api_key
        return AnthropicClient(**common)
    if provider in PLANNED_PROVIDERS:
        raise NotImplementedError(f"LLM provider '{provider}' is not implemented yet")
    raise ValueError(f"Unknown LLM_PROVIDER '{settings.llm_provider}'")
