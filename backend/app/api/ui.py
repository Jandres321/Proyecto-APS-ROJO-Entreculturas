"""Routes of the web review app (app/web/index.html): projects (built-in pilots and connected
Google Drive folders), launching the pipeline with progress, the expense-centred review, budget,
PDF pages and the Excel downloads.

Decisions on findings use the existing `PATCH /api/findings/{id}`.
"""

import datetime as dt
import tempfile
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func, select

from app import db, drive
from app import projects as registry
from app.api.deps import SessionDep
from app.config import get_settings
from app.enums import Donor
from app.models import Document, Expense, Project
from app.pipeline.export import export_project_xlsx
from app.pipeline.report import export_report_xlsx
from app.pipeline.run import STEPS, PilotProject, run_pipeline
from app.review_view import document_path, donor_rules, project_budget, project_review

router = APIRouter(prefix="/ui", tags=["ui"])

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DONOR_LABELS_ES = {Donor.XUNTA: "Xunta de Galicia", Donor.AECID: "AECID"}

# --- pipeline runs (one per project, in a background thread) -----------------------------------

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


def _sync_drive(project: registry.DriveProject, steps: int) -> tuple[PilotProject, str]:
    """Mirror the Drive folder of a connected project and locate its report. Returns the project
    ready for the pipeline and a sentence on what was downloaded."""

    def on_progress(done: int, total: int) -> None:
        _set_job(
            project.key,
            step=1,
            steps=steps,
            fraction=(done / max(total, 1)) / steps,
            message=f"Descargando la carpeta de Google Drive ({done} de {total})",
        )

    _set_job(project.key, step=1, steps=steps, message="Conectando con Google Drive")
    result = drive.sync_folder(
        drive.build_service(),
        project.drive_folder,
        registry.local_folder(project),
        registry.manifest_path(project),
        on_progress,
    )
    report = registry.find_report(registry.local_folder(project), project.report)
    note = (
        f" Drive: {result.total} archivos ({result.downloaded} descargados, "
        f"{result.unchanged} sin cambios"
        + (f", {result.removed} eliminados" if result.removed else "")
        + (f", {len(result.skipped)} documentos de Google omitidos" if result.skipped else "")
        + ")."
    )
    return project.as_pilot(report), note


def _run(pilot: PilotProject, reread: bool) -> None:
    connected = registry.drive_projects().get(pilot.key)
    offset = 1 if connected else 0  # a Drive project has one step more: the download
    steps = len(STEPS) + offset
    order = {key: i for i, (key, _) in enumerate(STEPS)}

    def on_progress(step: str, fraction: float, message: str) -> None:
        _set_job(
            pilot.key,
            step=order[step] + 1 + offset,
            steps=steps,
            fraction=min((order[step] + offset + fraction) / steps, 1.0),
            message=message,
        )

    try:
        note = ""
        if connected:
            pilot, note = _sync_drive(connected, steps)
        with db.SessionLocal() as session:
            summary = run_pipeline(session, pilot, reread_report=reread, on_progress=on_progress)
        _set_job(pilot.key, state="done", fraction=1.0, message=_summary_text(summary) + note)
    except (drive.DriveError, registry.ProjectError) as error:
        _set_job(pilot.key, state="error", message=str(error))
    except FileNotFoundError as error:
        _set_job(
            pilot.key,
            state="error",
            message=f"Faltan datos: {error}. Comprueba DATA_DIR en el archivo .env.",
        )
    except Exception as error:  # surface it: a silent failure would look like "no findings"
        _set_job(pilot.key, state="error", message=f"El análisis ha fallado: {error}")


def _pilot(key: str) -> PilotProject:
    pilots = registry.all_projects()
    if key not in pilots:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Proyecto no encontrado")
    return pilots[key]


def _project(session: SessionDep, pilot: PilotProject) -> Project:
    project = session.scalar(select(Project).where(Project.code == pilot.code))
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "El proyecto aún no se ha analizado")
    return project


# --- projects ----------------------------------------------------------------------------------


@router.get("/pilots")
def list_pilots(session: SessionDep) -> dict[str, Any]:
    """Projects that can be reviewed, whether each is already loaded, and the app settings the
    page shows (data folder available, Drive available, external LLM on/off)."""
    settings = get_settings()
    connected = registry.drive_projects()
    pilots = []
    for pilot in registry.all_projects().values():
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
                "drive": pilot.key in connected,
                "job": _job(pilot.key),
            }
        )
    external_llm = settings.llm_provider.strip().lower() != "fake" and settings.allow_external_llm
    return {
        "pilots": pilots,
        "donors": [{"value": d.value, "label": DONOR_LABELS_ES.get(d, d.value)} for d in Donor],
        "data_dir_available": settings.data_dir_available,
        "drive": drive.status(settings),
        "external_llm": external_llm,
        "llm_provider": settings.llm_provider if external_llm else None,
        "llm_model": settings.llm_model if external_llm else None,
    }


class DriveConnection(BaseModel):
    url: str
    title: str
    donor: str
    report: str = ""


@router.post("/drive/projects", status_code=status.HTTP_201_CREATED)
def connect_drive(payload: DriveConnection) -> dict[str, Any]:
    """Connect a Google Drive folder as a project. Checks that the folder can be opened; the
    files are downloaded when the analysis is launched."""
    try:
        folder = drive.folder_id(payload.url)
        name = drive.folder_name(drive.build_service(), folder)
        project = registry.connect_drive_folder(
            folder, payload.title or name, payload.donor, payload.report
        )
    except (drive.DriveError, registry.ProjectError) as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from None
    return {"key": project.key, "title": project.title, "folder_name": name}


@router.delete("/drive/projects/{key}", status_code=status.HTTP_204_NO_CONTENT)
def disconnect_drive(key: str) -> Response:
    """Forget a connected Drive folder (downloaded files and reviewed data are kept)."""
    try:
        registry.disconnect_drive_folder(key)
    except registry.ProjectError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- running and reviewing ---------------------------------------------------------------------


@router.post("/pilots/{key}/run", status_code=status.HTTP_202_ACCEPTED)
def launch(key: str, reread: bool = False) -> dict[str, Any]:
    """Start the whole pipeline for a project in the background (poll `GET .../run`). A Drive
    project first downloads what is new or changed in its folder."""
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


def _workbook(writer: Any, session: SessionDep, key: str, name: str) -> Response:
    pilot = _pilot(key)
    project = _project(session, pilot)
    with tempfile.TemporaryDirectory() as tmp:
        content = writer(session, project.id, Path(tmp) / "libro.xlsx").read_bytes()
    filename = f"{pilot.code}_{name}.xlsx"
    return Response(
        content,
        media_type=XLSX,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/pilots/{key}/report.xlsx")
def report(key: str, session: SessionDep) -> Response:
    """Analysis report: a summary, one row per expense (verdict, document, what was read inside
    it, findings) and the findings, budget and caps, with the decisions taken so far."""
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    return _workbook(export_report_xlsx, session, key, f"informe_analisis_{stamp}")


@router.get("/pilots/{key}/export.xlsx")
def export(key: str, session: SessionDep) -> Response:
    """The review workbook of the Streamlit app (findings, expenses, budget, caps)."""
    return _workbook(export_project_xlsx, session, key, "revision")


# --- documents ---------------------------------------------------------------------------------


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
