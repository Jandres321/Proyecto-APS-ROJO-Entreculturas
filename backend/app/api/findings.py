"""Finding routes: list and reviewer decisions."""

from fastapi import APIRouter, HTTPException, status

from app.api.deps import SessionDep
from app.enums import FindingStatus, Severity
from app.review import list_findings as _list_findings
from app.review import set_decision
from app.schemas import FindingOut, FindingUpdate
from app.services.projects import get_project

router = APIRouter(tags=["findings"])


@router.get("/projects/{key}/findings", response_model=list[FindingOut])
def list_findings(
    key: str,
    session: SessionDep,
    status: FindingStatus | None = None,
    severity: Severity | None = None,
) -> list[FindingOut]:
    try:
        project = get_project(session, key)
    except LookupError:
        raise HTTPException(404, "Proyecto no encontrado") from None
    return [
        FindingOut.model_validate(f)
        for f in _list_findings(session, project.id, status=status, severity=severity)
    ]


@router.patch("/findings/{finding_id}", response_model=FindingOut)
def update_finding(finding_id: int, payload: FindingUpdate, session: SessionDep) -> FindingOut:
    """Accept / mark as false positive, with an optional reviewer note."""
    try:
        finding = set_decision(session, finding_id, payload.status, payload.reviewer_note)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Incidencia no encontrada") from None
    return FindingOut.model_validate(finding)
