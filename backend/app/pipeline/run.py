"""Whole review pipeline for one project, step by step (launched by app/services/jobs.py).

ingest report -> register documents -> read PDFs -> match (names propose, content verifies) ->
checks -> partida of each expense -> budget and caps

Every step persists its findings in the `finding` table, replacing the OPEN ones of the previous
run and keeping the reviewer's decisions. The exception is the report ingestion: it recreates the
expenses (and with them the findings hanging from them), so it only runs when the project has no
expenses yet or when `reread_report=True`.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.lectura.project import read_project_documents
from app.lectura.vision import LlmReadOptions, LlmReadSummary
from app.models import Document, Expense, Project
from app.pipeline.budget import BudgetAnalysis, run_budget
from app.pipeline.checks import run_checks
from app.pipeline.classify import flag_doubtful_imputations, suggest_partidas
from app.pipeline.ingest import ingest_project
from app.pipeline.match import register_documents
from app.pipeline.match_content import ContentMatchSummary, match_by_segments
from app.rules import sync_rules

STEPS = (
    ("ingest", "Leyendo el informe económico"),
    ("register", "Inventariando los justificantes"),
    ("reading", "Leyendo el contenido de los PDF"),
    ("matching", "Cruzando gastos y justificantes por contenido"),
    ("checks", "Comprobando los justificantes"),
    ("partidas", "Revisando la partida de cada gasto"),
    ("budget", "Revisando presupuesto y topes"),
)

# step key, fraction of that step done (0-1), human message
Progress = Callable[[str, float, str], None]


@dataclass
class RunSummary:
    project_id: int
    expenses: int = 0
    documents: int = 0
    segments: int = 0
    matching: ContentMatchSummary = field(default_factory=ContentMatchSummary)
    check_findings: int = 0
    partida_findings: int = 0
    budget: BudgetAnalysis = field(default_factory=BudgetAnalysis)
    report_reread: bool = False
    llm: LlmReadSummary = field(default_factory=LlmReadSummary)


def run_pipeline(
    session: Session,
    project: Project,
    *,
    reread_report: bool = False,
    on_progress: Progress | None = None,
) -> RunSummary:
    """Run every step for a project whose files are already in its folder under DATA_DIR (see
    `app/services/sources.py`) and return a summary. Raises `FileNotFoundError` when the report
    or the folder are missing."""
    data_dir = get_settings().data_dir
    if data_dir is None or not data_dir.is_dir():
        raise FileNotFoundError("DATA_DIR no está definido o no existe")
    if not project.folder or not project.report_file:
        raise FileNotFoundError("el proyecto no tiene carpeta o informe económico")
    folder = data_dir / project.folder
    report = folder / project.report_file
    if not report.is_file():
        raise FileNotFoundError(f"No se encuentra el informe: {report}")

    def notify(step: str, fraction: float = 0.0) -> None:
        if on_progress:
            on_progress(step, fraction, dict(STEPS)[step])

    summary = RunSummary(project.id)

    notify("ingest")
    has_expenses = session.scalar(
        select(func.count()).select_from(Expense).where(Expense.project_id == project.id)
    )
    if reread_report or not has_expenses:
        ingest_project(session, project.id, report)
        summary.report_reread = True
    summary.expenses = session.scalar(
        select(func.count()).select_from(Expense).where(Expense.project_id == project.id)
    )

    notify("register")
    register_documents(session, project.id, folder)
    summary.documents = session.scalar(
        select(func.count()).select_from(Document).where(Document.project_id == project.id)
    )

    notify("reading")
    summary.segments = read_project_documents(
        session,
        project.id,
        llm=LlmReadOptions(mode=project.llm_mode.value, max_pages=project.llm_max_pages),
        on_llm_summary=lambda llm: setattr(summary, "llm", llm),
        progress=lambda done, total: notify("reading", done / max(total, 1)),
    )

    notify("matching")
    summary.matching = match_by_segments(session, project.id)

    notify("checks")
    sync_rules(session, project.donor)
    summary.check_findings = len(run_checks(session, project.id))

    notify("partidas")
    asyncio.run(suggest_partidas(session, project.id))  # keyword layer only (no network)
    summary.partida_findings = len(flag_doubtful_imputations(session, project.id))

    notify("budget")
    summary.budget = run_budget(session, project.id)
    notify("budget", 1.0)
    return summary
