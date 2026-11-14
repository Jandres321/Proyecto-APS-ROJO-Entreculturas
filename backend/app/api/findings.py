"""Finding routes: list and reviewer decisions."""

from fastapi import APIRouter, HTTPException, status

from app.api.deps import SessionDep
from app.enums import FindingStatus, Severity
from app.review import list_findings as _list_findings
from app.review import set_decision
from app.schemas import FindingOut, FindingUpdate

router = APIRouter(tags=["findings"])


@router.get("/projects/{project_id}/findings", response_model=list[FindingOut])
def list_findings(
    project_id: int,
    session: SessionDep,
    status: FindingStatus | None = None,
    severity: Severity | None = None,
) -> list[FindingOut]:
    return [
        FindingOut.model_validate(f)
        for f in _list_findings(session, project_id, status=status, severity=severity)
    ]


@router.patch("/findings/{finding_id}", response_model=FindingOut)
def update_finding(finding_id: int, payload: FindingUpdate, session: SessionDep) -> FindingOut:
    """Accept / mark as false positive, with an optional reviewer note."""
    try:
        finding = set_decision(session, finding_id, payload.status, payload.reviewer_note)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Incidencia no encontrada") from None
    return FindingOut.model_validate(finding)
