"""Routes of the web review app (app/web/index.html): pilots, launching the pipeline with
progress, the expense-centred review, budget, PDF pages and the Excel download.

Decisions on findings use the existing `PATCH /api/findings/{id}`.
"""

import tempfile
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Response, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select

from app import db
from app.api.deps import SessionDep
from app.config import get_settings
from app.models import Document, Expense, Project
from app.pipeline.export import export_project_xlsx
from app.pipeline.run import PILOTS, STEPS, PilotProject, run_pipeline
from app.review_view import document_path, donor_rules, project_budget, project_review

router = APIRouter(prefix="/ui", tags=["ui"])

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# --- pipeline runs (one per pilot, in a background thread) -------------------------------------

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def _job(key: str) -> dict[str, Any]:
    with _jobs_lock:
        return dict(_jobs.get(key) or {"state": "idle"})


def _set_job(key: str, **fields: Any) -> None:
    with _jobs_lock:
        _jobs[key] = {**_jobs.get(key, {}), **fields}


def _summary_text(summary: Any) -> str:
    m = summary.matching
    text = (
        f"Análisis terminado: {summary.expenses} gastos y {summary.documents} documentos. "
        f"{m.expenses_linked} gastos con justificante ({m.verified} verificados por contenido, "
        f"{m.from_name} propuestos por el nombre, {m.from_content} encontrados por contenido)"
    )
    if m.not_in_sample:
        text += f"; {m.not_in_sample} no incluidos en la muestra"
    return text + "." + (" Se ha releído el informe." if summary.report_reread else "")


def _run(pilot: PilotProject, reread: bool) -> None:
    order = {key: i for i, (key, _) in enumerate(STEPS)}

    def on_progress(step: str, fraction: float, message: str) -> None:
        _set_job(
            pilot.key,
            step=order[step] + 1,
            steps=len(STEPS),
            fraction=min((order[step] + fraction) / len(STEPS), 1.0),
            message=message,
        )

    try:
        with db.SessionLocal() as session:
            summary = run_pipeline(session, pilot, reread_report=reread, on_progress=on_progress)
        _set_job(pilot.key, state="done", fraction=1.0, message=_summary_text(summary))
    except FileNotFoundError as error:
        _set_job(
            pilot.key,
            state="error",
            message=f"Faltan datos: {error}. Comprueba DATA_DIR en el archivo .env.",
        )
    except Exception as error:  # surface it: a silent failure would look like "no findings"
        _set_job(pilot.key, state="error", message=f"El análisis ha fallado: {error}")


def _pilot(key: str) -> PilotProject:
    if key not in PILOTS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Proyecto no encontrado")
    return PILOTS[key]


def _project(session: SessionDep, pilot: PilotProject) -> Project:
    project = session.scalar(select(Project).where(Project.code == pilot.code))
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "El proyecto aún no se ha analizado")
    return project


# --- routes ------------------------------------------------------------------------------------


@router.get("/pilots")
def list_pilots(session: SessionDep) -> dict[str, Any]:
    """Projects that can be reviewed, whether each is already loaded, and the app settings the
    page shows (data folder available, external LLM on/off)."""
    settings = get_settings()
    pilots = []
    for pilot in PILOTS.values():
        project = session.scalar(select(Project).where(Project.code == pilot.code))
        expenses = (
            session.scalar(
                select(func.count()).select_from(Expense).where(Expense.project_id == project.id)
            )
            if project
            else 0
        )
        pilots.append(
            {
                "key": pilot.key,
                "title": pilot.title,
                "donor": pilot.donor.value,
                "code": pilot.code,
                "expenses": expenses,
                "loaded": bool(expenses),
                "job": _job(pilot.key),
            }
        )
    external_llm = settings.llm_provider.strip().lower() != "fake" and settings.allow_external_llm
    return {
        "pilots": pilots,
        "data_dir_available": settings.data_dir_available,
        "external_llm": external_llm,
        "llm_provider": settings.llm_provider if external_llm else None,
        "llm_model": settings.llm_model if external_llm else None,
    }


@router.post("/pilots/{key}/run", status_code=status.HTTP_202_ACCEPTED)
def launch(key: str, reread: bool = False) -> dict[str, Any]:
    """Start the whole pipeline for a pilot in the background (poll `GET .../run`)."""
    pilot = _pilot(key)
    with _jobs_lock:
        if (_jobs.get(key) or {}).get("state") == "running":
            raise HTTPException(status.HTTP_409_CONFLICT, "Ya hay un análisis en marcha")
        _jobs[key] = {
            "state": "running",
            "step": 0,
            "steps": len(STEPS),
            "fraction": 0.0,
            "message": "Preparando…",
        }
    threading.Thread(target=_run, args=(pilot, reread), daemon=True).start()
    return _job(key)


@router.get("/pilots/{key}/run")
def run_status(key: str) -> dict[str, Any]:
    _pilot(key)
    return _job(key)


@router.get("/pilots/{key}/review")
def review(key: str, session: SessionDep) -> dict[str, Any]:
    project = _project(session, _pilot(key))
    return {**project_review(session, project), "rules": donor_rules(session, project)}


@router.get("/pilots/{key}/budget")
def budget(key: str, session: SessionDep) -> dict[str, Any]:
    return project_budget(session, _project(session, _pilot(key)))


@router.get("/pilots/{key}/export.xlsx")
def export(key: str, session: SessionDep) -> Response:
    """The review workbook, with the decisions taken so far."""
    pilot = _pilot(key)
    project = _project(session, pilot)
    with tempfile.TemporaryDirectory() as tmp:
        content = export_project_xlsx(session, project.id, Path(tmp) / "revision.xlsx").read_bytes()
    name = f"{pilot.code}_revision.xlsx"
    return Response(
        content, media_type=XLSX, headers={"Content-Disposition": f'attachment; filename="{name}"'}
    )


def _pdf(session: SessionDep, document_id: int) -> Path:
    document = session.get(Document, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Documento no encontrado")
    path = document_path(document)
    if path is None or path.suffix.lower() != ".pdf":
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No se puede abrir el archivo: no está en DATA_DIR, está dentro de un .zip o no es "
            "un PDF.",
        )
    return path


@router.get("/documents/{document_id}/page/{page}.png")
def document_page(document_id: int, page: int, session: SessionDep, rotation: int = 0) -> Response:
    """One page of a registered PDF as PNG (many scans come upside down: `rotation` turns it)."""
    import pymupdf

    path = _pdf(session, document_id)
    try:
        with pymupdf.open(path) as pdf:
            pages = pdf.page_count
            selected = pdf[min(max(page, 1), pages) - 1]
            if rotation % 360:
                selected.set_rotation((selected.rotation + rotation) % 360)
            image = selected.get_pixmap(dpi=110).tobytes("png")
    except Exception as error:  # damaged or protected PDF
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"No se ha podido abrir el PDF: {error}"
        ) from None
    return Response(image, media_type="image/png", headers={"X-Pages": str(pages)})


@router.get("/documents/{document_id}/file")
def document_file(document_id: int, session: SessionDep) -> FileResponse:
    """The original PDF, to open it in a browser tab."""
    path = _pdf(session, document_id)
    return FileResponse(path, media_type="application/pdf", filename=path.name)
