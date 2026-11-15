"""Read the PDFs registered for a project and store their segments (`segmento`)."""

from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.lectura.pipeline import process_pdfs
from app.lectura.store import save_segments
from app.lectura.vision import LlmReadOptions, LlmReadSummary
from app.models import Document, Project
from app.pipeline.ingest import ZIP_MEMBER_SEPARATOR
from app.pipeline.match_config import get_match_config


def read_project_documents(
    session: Session,
    project_id: int,
    *,
    settings: Settings | None = None,
    use_llm: bool = False,
    workers: int | None = None,
    progress: Callable[[int, int], None] | None = None,
    llm: LlmReadOptions | None = None,
    on_llm_summary: Callable[[LlmReadSummary], None] | None = None,
) -> int:
    """OCR / read every PDF registered for the project and replace its segments.

    Paths in `document` are relative to DATA_DIR. PDFs inside .zip files are skipped (they are
    matched through the archive's name). The amount convention and default currency come from the
    project's match config. Returns the number of segments stored.

    `llm` selects which pages go to the vision LLM (None / mode "off": none, unless the legacy
    `use_llm` is set); `on_llm_summary` receives the run's summary (pages sent, tokens, estimated
    cost) so callers such as the API can report it.
    """
    settings = settings or get_settings()
    project = session.get(Project, project_id)
    if project is None:
        raise LookupError(f"Project {project_id} not found")
    if settings.data_dir is None:
        raise RuntimeError("DATA_DIR is not set: cannot locate the documents")
    config = get_match_config(project)
    base = settings.data_dir
    docs = [
        d
        for d in session.scalars(select(Document).where(Document.project_id == project_id))
        if ZIP_MEMBER_SEPARATOR not in d.path and d.path.lower().endswith(".pdf")
    ]
    paths = [base / d.path for d in docs if (base / d.path).is_file()]
    results = process_pdfs(
        paths,
        settings,
        config.get("amount_convention", "auto"),
        config.get("default_currency"),
        use_llm=use_llm,
        workers=workers,
        progress=progress,
        llm=llm,
    )
    if on_llm_summary is not None:
        on_llm_summary(results.llm)
    total = sum(save_segments(session, r, Path(base)) for r in results)
    session.commit()
    return total
