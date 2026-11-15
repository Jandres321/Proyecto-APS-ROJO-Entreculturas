"""Project routes: list, create (from a folder, an upload or a Google Drive folder), settings,
delete, launch the analysis and follow it, and the review data and Excel downloads.

Projects are addressed by their `key` (a slug). Decisions on findings use
`PATCH /api/findings/{id}`.
"""

import datetime as dt
import tempfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, Field

from app import drive
from app.api.deps import SessionDep
from app.enums import Donor, LlmMode, SourceType
from app.models import Project
from app.pipeline.export import export_project_xlsx
from app.pipeline.report import export_report_xlsx
from app.review_view import donor_rules, project_budget, project_review
from app.services import jobs
from app.services import projects as service
from app.services.sources import store_upload

router = APIRouter(prefix="/projects", tags=["projects"])

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# The function that runs a job; tests replace it to run synchronously.
launcher: jobs.Launcher = jobs.launch_in_thread


def _get(session: SessionDep, key: str) -> Project:
    try:
        return service.get_project(session, key)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Proyecto no encontrado") from None


def _bad_request(error: Exception) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(error))


def project_view(session: SessionDep, project: Project) -> dict[str, Any]:
    expenses = service.expense_count(session, project)
    return {
        "key": project.key,
        "name": project.name,
        "donor": project.donor.value,
        "code": project.code,
        "currency": project.currency,
        "source_type": project.source_type.value,
        "report_file": project.report_file,
        "start_date": project.start_date.isoformat() if project.start_date else None,
        "end_date": project.end_date.isoformat() if project.end_date else None,
        "llm_mode": project.llm_mode.value,
        "llm_max_pages": project.llm_max_pages,
        "is_sample": project.is_sample,
        "expenses": expenses,
        "loaded": bool(expenses),
        "job": jobs.job_view(jobs.latest_job(session, project)),
    }


# --- list and create ---------------------------------------------------------------------------


@router.get("")
def list_projects(session: SessionDep) -> list[dict[str, Any]]:
    return [project_view(session, p) for p in service.list_projects(session)]


class ProjectCreate(BaseModel):
    name: str = Field(max_length=200)
    donor: Donor
    source_type: SourceType
    code: str | None = Field(default=None, max_length=50)
    currency: str = Field(default="EUR", min_length=3, max_length=3)
    folder: str | None = None  # `local`: folder relative to DATA_DIR
    drive_url: str | None = None  # `drive`: URL or id of the Drive folder
    report_file: str | None = None
    start_date: dt.date | None = None
    end_date: dt.date | None = None
    llm_mode: LlmMode = LlmMode.OFF
    llm_max_pages: int = Field(default=20, ge=0, le=1000)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_project(payload: ProjectCreate, session: SessionDep) -> dict[str, Any]:
    """Create a project from a folder under DATA_DIR (`local`), a Google Drive folder (`drive`,
    checked now, downloaded when the analysis starts) or an empty upload project (`upload`; send
    its files to `POST /api/projects/{key}/files`)."""
    source_ref = None
    try:
        if payload.source_type is SourceType.DRIVE:
            source_ref = drive.folder_id(payload.drive_url or "")
            drive.folder_name(drive.build_service(), source_ref)
        project = service.create_project(
            session,
            name=payload.name,
            donor=payload.donor.value,
            source_type=payload.source_type,
            code=payload.code,
            currency=payload.currency,
            folder=payload.folder,
            source_ref=source_ref,
            report_file=payload.report_file,
            start_date=payload.start_date,
            end_date=payload.end_date,
            llm_mode=payload.llm_mode,
            llm_max_pages=payload.llm_max_pages,
        )
    except (drive.DriveError, service.ProjectError) as error:
        raise _bad_request(error) from None
    return project_view(session, project)


@router.post("/{key}/files")
def upload_files(
    key: str,
    session: SessionDep,
    report: Annotated[UploadFile | None, File(description="Excel del informe económico")] = None,
    documents: Annotated[
        list[UploadFile] | None, File(description="Justificantes: PDF, Excel o .zip")
    ] = None,
) -> dict[str, Any]:
    """Add files to an upload project: the report spreadsheet and the documents (zips are
    unpacked). Can be called several times; files with the same name are replaced."""
    project = _get(session, key)
    try:
        saved = store_upload(
            project,
            (report.filename, report.file) if report is not None else None,
            [(d.filename, d.file) for d in documents or []],
        )
    except service.ProjectError as error:
        raise _bad_request(error) from None
    session.commit()
    return {**project_view(session, project), "saved": saved}


@router.post("/upload", status_code=status.HTTP_201_CREATED)
def create_uploaded_project(
    session: SessionDep,
    name: Annotated[str, Form()],
    donor: Annotated[Donor, Form()],
    report: Annotated[UploadFile, File(description="Excel del informe económico")],
    documents: Annotated[list[UploadFile], File(description="Justificantes: PDF o .zip")],
    code: Annotated[str | None, Form()] = None,
    currency: Annotated[str, Form()] = "EUR",
    start_date: Annotated[dt.date | None, Form()] = None,
    end_date: Annotated[dt.date | None, Form()] = None,
    llm_mode: Annotated[LlmMode, Form()] = LlmMode.OFF,
) -> dict[str, Any]:
    """Create a project from files sent by the browser in one request (multipart form)."""
    try:
        project = service.create_project(
            session,
            name=name,
            donor=donor.value,
            source_type=SourceType.UPLOAD,
            code=code,
            currency=currency,
            start_date=start_date or None,
            end_date=end_date or None,
            llm_mode=llm_mode,
        )
        saved = store_upload(
            project, (report.filename, report.file), [(d.filename, d.file) for d in documents]
        )
        session.commit()
    except service.ProjectError as error:
        session.rollback()
        raise _bad_request(error) from None
    return {**project_view(session, project), "saved": saved}


# --- one project ---------------------------------------------------------------------------------


@router.get("/{key}")
def get_project(key: str, session: SessionDep) -> dict[str, Any]:
    return project_view(session, _get(session, key))


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    report_file: str | None = None
    start_date: dt.date | None = None
    end_date: dt.date | None = None
    llm_mode: LlmMode | None = None
    llm_max_pages: int | None = Field(default=None, ge=0, le=1000)


@router.patch("/{key}")
def update_project(key: str, payload: ProjectUpdate, session: SessionDep) -> dict[str, Any]:
    """Change the project's settings. Only the fields sent are changed (send null to clear a
    date)."""
    project = _get(session, key)
    try:
        service.update_project(session, project, **payload.model_dump(exclude_unset=True))
    except service.ProjectError as error:
        session.rollback()
        raise _bad_request(error) from None
    return project_view(session, project)


@router.delete("/{key}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(key: str, session: SessionDep, delete_files: bool = False) -> Response:
    """Delete a project and its review. Its uploaded / downloaded files only with
    `delete_files=true`. Sample projects cannot be deleted."""
    try:
        service.delete_project(session, _get(session, key), delete_files=delete_files)
    except service.ProjectError as error:
        raise _bad_request(error) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- analysis ------------------------------------------------------------------------------------


@router.post("/{key}/run", status_code=status.HTTP_202_ACCEPTED)
def launch(key: str, session: SessionDep, reread: bool = False) -> dict[str, Any]:
    """Start the whole review in the background; follow it with `GET .../run`."""
    project = _get(session, key)
    try:
        job = jobs.start_job(session, project, reread_report=reread)
    except jobs.JobAlreadyRunning as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
    launcher(job.id, project.id)
    session.expire_all()
    return jobs.job_view(jobs.latest_job(session, project))


@router.get("/{key}/run")
def run_status(key: str, session: SessionDep) -> dict[str, Any]:
    return jobs.job_view(jobs.latest_job(session, _get(session, key)))


# --- review data and downloads -------------------------------------------------------------------


def _loaded(session: SessionDep, key: str) -> Project:
    project = _get(session, key)
    if not service.expense_count(session, project):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "El proyecto aún no se ha analizado")
    return project


@router.get("/{key}/review")
def review(key: str, session: SessionDep) -> dict[str, Any]:
    project = _loaded(session, key)
    return {**project_review(session, project), "rules": donor_rules(session, project)}


@router.get("/{key}/budget")
def budget(key: str, session: SessionDep) -> dict[str, Any]:
    return project_budget(session, _loaded(session, key))


def _workbook(writer: Any, session: SessionDep, key: str, name: str) -> Response:
    project = _loaded(session, key)
    with tempfile.TemporaryDirectory() as tmp:
        content = writer(session, project.id, Path(tmp) / "libro.xlsx").read_bytes()
    filename = f"{project.code or project.key}_{name}.xlsx"
    return Response(
        content,
        media_type=XLSX,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{key}/report.xlsx")
def report(key: str, session: SessionDep) -> Response:
    """Analysis report: a summary, one row per expense (verdict, document, what was read inside
    it, findings) and the findings, budget and caps, with the decisions taken so far."""
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    return _workbook(export_report_xlsx, session, key, f"informe_analisis_{stamp}")


@router.get("/{key}/export.xlsx")
def export(key: str, session: SessionDep) -> Response:
    """Technical review workbook (findings, expenses, budget, caps)."""
    return _workbook(export_project_xlsx, session, key, "revision")
