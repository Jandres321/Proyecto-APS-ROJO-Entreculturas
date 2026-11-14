"""Application settings, read from environment variables / .env."""

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # backend/.env first, then the repo-root .env (when running from backend/).
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://revisor:revisor@localhost:5432/revisor"

    # Root of the confidential dataset. Never committed. None/missing => real-data
    # features and tests are disabled.
    data_dir: Path | None = None

    llm_provider: str = "fake"
    llm_model: str = ""
    llm_api_key: str = ""
    # Standard variable of the Anthropic SDK; used when LLM_PROVIDER=anthropic and LLM_API_KEY is
    # empty, so a .env written for the command-line conciliador works here too.
    anthropic_api_key: str = ""
    llm_max_concurrency: int = 4

    # Sending documents to an external LLM is OFF unless the team explicitly allows it
    # (Entreculturas has not yet confirmed which provider is permitted).
    allow_external_llm: bool = False

    cache_dir: Path = Path("./cache")

    # PDF reading (app/lectura): Tesseract binary (None = look it up), tessdata folder with
    # the spa/eng/fra/osd models (None = Tesseract's default) and OCR settings.
    tesseract_cmd: str | None = None
    tessdata_dir: Path | None = None
    ocr_langs: str = "spa+eng+fra"
    ocr_dpi: int = 200
    ocr_workers: int = 4

    # Comma-separated list of allowed origins for the frontend.
    cors_origins: str = "http://localhost:5173"

    @field_validator("data_dir", "tessdata_dir", "tesseract_cmd", mode="before")
    @classmethod
    def _empty_data_dir_is_none(cls, value: object) -> object:
        # DATA_DIR= (empty) would otherwise become Path("."), i.e. the current directory.
        return None if isinstance(value, str) and not value.strip() else value

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def data_dir_available(self) -> bool:
        return self.data_dir is not None and self.data_dir.is_dir()


@lru_cache
def get_settings() -> Settings:
    return Settings()
