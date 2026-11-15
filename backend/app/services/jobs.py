"""Analysis runs: the whole review pipeline of a project, in the background, with its state and
progress persisted in `analysis_job` (survives a restart of the API and can be read by any
worker). One run at a time per project."""

import datetime as dt
import threading
from collections.abc import Callable
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import db, drive
from app.enums import JobState, SourceType
from app.lectura.vision import ExternalLlmNotAllowed
from app.models import AnalysisJob, Project
from app.pipeline.run import STEPS, RunSummary, run_pipeline
from app.services.projects import ProjectError
from app.services.sources import prepare_files

INTERRUPTED = "El análisis se interrumpió (la aplicación se reinició). Vuelve a lanzarlo."


class JobAlreadyRunning(RuntimeError):
    pass


def latest_job(session: Session, project: Project) -> AnalysisJob | None:
    return session.scalar(
        select(AnalysisJob)
        .where(AnalysisJob.project_id == project.id)
        .order_by(AnalysisJob.id.desc())
        .limit(1)
    )


def job_view(job: AnalysisJob | None) -> dict[str, Any]:
    if job is None:
        return {"state": "idle"}
    return {
        "state": job.state.value,
        "step": job.step,
        "steps": job.steps,
        "fraction": job.fraction,
        "message": job.message,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def mark_interrupted_jobs(session: Session) -> int:
    """On startup: runs left 'running' by a previous process will never finish."""
    result = session.execute(
        update(AnalysisJob)
        .where(AnalysisJob.state == JobState.RUNNING)
        .values(state=JobState.ERROR, message=INTERRUPTED, finished_at=dt.datetime.now(dt.UTC))
    )
    session.commit()
    return result.rowcount or 0


def _total_steps(project: Project) -> int:
    return len(STEPS) + (1 if project.source_type is SourceType.DRIVE else 0)


def start_job(session: Session, project: Project, *, reread_report: bool = False) -> AnalysisJob:
    """Create the run record (raises JobAlreadyRunning if one is in progress)."""
    current = latest_job(session, project)
    if current is not None and current.state is JobState.RUNNING:
        raise JobAlreadyRunning("Ya hay un análisis en marcha para este proyecto.")
    job = AnalysisJob(
        project_id=project.id,
        state=JobState.RUNNING,
        steps=_total_steps(project),
        message="Preparando…",
        reread_report=reread_report,
    )
    session.add(job)
    session.commit()
    return job


def _update(job_id: int, **fields: Any) -> None:
    with db.SessionLocal() as session:
        session.execute(update(AnalysisJob).where(AnalysisJob.id == job_id).values(**fields))
        session.commit()


def summary_text(summary: RunSummary) -> str:
    m = summary.matching
    text = (
        f"Análisis terminado: {summary.expenses} gastos y {summary.documents} documentos. "
        f"{m.expenses_linked} gastos con justificante ({m.verified} verificados por contenido, "
        f"{m.from_name} propuestos por el nombre, {m.from_content} encontrados por contenido)"
    )
    if m.not_in_sample:
        text += f"; {m.not_in_sample} no incluidos en la muestra"
    text += "."
    if summary.llm.pages_sent or summary.llm.pages_cached:
        text += (
            f" Lectura con IA: {summary.llm.pages_sent} páginas enviadas"
            f" ({summary.llm.pages_cached} desde caché), coste estimado"
            f" {summary.llm.estimated_cost_usd:.2f} USD."
        )
    return text + (" Se ha releído el informe." if summary.report_reread else "")


def execute_job(job_id: int, project_id: int) -> None:
    """Body of a run (in its own DB session): get the files, run the pipeline, record the
    outcome. Errors the user can fix are reported in Spanish; nothing fails silently."""
    with db.SessionLocal() as session:
        project = session.get(Project, project_id)
        job = session.get(AnalysisJob, job_id)
        offset = 1 if project.source_type is SourceType.DRIVE else 0
        steps = job.steps
        order = {key: i for i, (key, _) in enumerate(STEPS)}

        def on_download(done: int, total: int) -> None:
            _update(
                job_id,
                step=1,
                fraction=(done / max(total, 1)) / steps,
                message=f"Descargando la carpeta de Google Drive ({done} de {total})",
            )

        def on_progress(step: str, fraction: float, message: str) -> None:
            _update(
                job_id,
                step=order[step] + 1 + offset,
                fraction=min((order[step] + offset + fraction) / steps, 1.0),
                message=message,
            )

        try:
            if offset:
                _update(job_id, step=1, message="Conectando con Google Drive")
            note = prepare_files(project, on_download if offset else None)
            session.commit()
            summary = run_pipeline(
                session, project, reread_report=job.reread_report, on_progress=on_progress
            )
            message = summary_text(summary) + (f" {note}" if note else "")
            state = JobState.DONE
        except (drive.DriveError, ProjectError) as error:
            message, state = str(error), JobState.ERROR
        except ExternalLlmNotAllowed as error:
            message = (
                f"{error} Pon la lectura con IA en 'No' en los ajustes del proyecto "
                "o activa ALLOW_EXTERNAL_LLM en .env."
            )
            state = JobState.ERROR
        except FileNotFoundError as error:
            message, state = f"Faltan datos: {error}. Comprueba DATA_DIR en .env.", JobState.ERROR
        except Exception as error:  # surface it: a silent failure would look like "no findings"
            session.rollback()
            message, state = f"El análisis ha fallado: {error}", JobState.ERROR
    outcome: dict[str, Any] = {"state": state, "message": message}
    if state is JobState.DONE:
        outcome["fraction"] = 1.0
    _update(job_id, **outcome, finished_at=dt.datetime.now(dt.UTC))


Launcher = Callable[[int, int], None]


def launch_in_thread(job_id: int, project_id: int) -> None:
    """Default launcher: a daemon thread in the API process. The run state is in the database,
    so moving to a task queue (RQ, Celery…) only means replacing this function."""
    threading.Thread(target=execute_job, args=(job_id, project_id), daemon=True).start()
