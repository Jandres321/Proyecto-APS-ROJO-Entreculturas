"""Step 3 — Extract: structured data from each (scanned) PDF via the multimodal LLM.

Every result is cached by PDF sha256 + model + PROMPT_VERSION (in CACHE_DIR and in the
`extraction` table), so re-runs never pay twice for the same document.
"""

from pathlib import Path

from sqlalchemy.orm import Session

from app.llm.base import LLMClient
from app.models import Extraction

# Bump whenever the extraction prompt or schema changes to invalidate the cache.
PROMPT_VERSION = "0"


def render_pages(pdf_path: Path, dpi: int = 150) -> list[bytes]:
    """Render each page to PNG with PyMuPDF, fixing 180º rotations when detectable."""
    raise NotImplementedError


def cache_key(sha256: str, model: str, prompt_version: str = PROMPT_VERSION) -> str:
    """Key for the on-disk cache of LLM results."""
    raise NotImplementedError


async def extract_document(session: Session, document_id: int, llm: LLMClient) -> Extraction:
    """Return the cached extraction or call the LLM.

    Extracted fields: document type, issuer, invoice no., date, amounts, currency,
    donor stamp present, proof of payment present, legibility, per-page notes.
    """
    raise NotImplementedError


async def extract_project(session: Session, project_id: int, llm: LLMClient) -> None:
    """Extract every document of the project, bounded by LLM_MAX_CONCURRENCY, updating
    project.progress."""
    raise NotImplementedError
