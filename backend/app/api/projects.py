"""Project routes (stubs: return 501 until the pipeline is implemented)."""

from fastapi import APIRouter, status
from fastapi.responses import FileResponse

from app.api.deps import SessionDep, not_implemented
from app.schemas import BudgetLineOut, DocumentOut, ExpenseOut, ProjectIn, ProjectOut

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("", response_model=list[ProjectOut])
def list_projects(session: SessionDep) -> list[ProjectOut]:
    raise not_implemented()


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(payload: ProjectIn, session: SessionDep) -> ProjectOut:
    raise not_implemented()


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: int, session: SessionDep) -> ProjectOut:
    raise not_implemented()


@router.post("/{project_id}/run", response_model=ProjectOut, status_code=status.HTTP_202_ACCEPTED)
def run_project(project_id: int, session: SessionDep) -> ProjectOut:
    """Launch the review pipeline in the background."""
    raise not_implemented()


@router.get("/{project_id}/expenses", response_model=list[ExpenseOut])
def list_expenses(project_id: int, session: SessionDep) -> list[ExpenseOut]:
    raise not_implemented()


@router.get("/{project_id}/documents", response_model=list[DocumentOut])
def list_documents(project_id: int, session: SessionDep) -> list[DocumentOut]:
    raise not_implemented()


@router.get("/{project_id}/budget", response_model=list[BudgetLineOut])
def list_budget(project_id: int, session: SessionDep) -> list[BudgetLineOut]:
    raise not_implemented()


@router.get("/{project_id}/export.xlsx", response_class=FileResponse)
def export_project(project_id: int, session: SessionDep) -> FileResponse:
    """Download the review workbook (main deliverable)."""
    raise not_implemented()
