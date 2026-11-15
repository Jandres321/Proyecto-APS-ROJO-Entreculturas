"""Persistence of findings: every pipeline step replaces its own OPEN findings on each run and
keeps the reviewer's decisions (accepted / false positive)."""

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.enums import FindingStatus
from app.models import Finding
from app.schemas import FindingIn


def finding_key(
    rule_code: str, expense_id: int | None, document_id: int | None, evidence: dict | None
) -> tuple:
    """Identity of a finding across runs. Findings tied to neither an expense nor a document
    (budget ones) carry `evidence["subject"]` (partida or limit code) so that a decision on one
    does not hide the others of the same rule."""
    subject = evidence.get("subject") if evidence else None
    return (rule_code, expense_id, document_id, subject)


def sync_findings(
    session: Session, project_id: int, rule_codes: set[str], new: list[FindingIn]
) -> None:
    """Replace the OPEN findings of `rule_codes` with `new`, keeping reviewer decisions.

    A finding for which the reviewer already decided (accepted / false positive) on the same
    expense + document (+ subject) is not created again. Does not commit.
    """
    session.execute(
        delete(Finding).where(
            Finding.project_id == project_id,
            Finding.rule_code.in_(rule_codes),
            Finding.status == FindingStatus.OPEN,
        )
    )
    reviewed = {
        finding_key(f.rule_code, f.expense_id, f.document_id, f.evidence)
        for f in session.scalars(
            select(Finding).where(
                Finding.project_id == project_id, Finding.rule_code.in_(rule_codes)
            )
        )
    }
    for finding in new:
        key = finding_key(
            finding.rule_code, finding.expense_id, finding.document_id, finding.evidence
        )
        if key in reviewed:
            continue
        session.add(Finding(project_id=project_id, **finding.model_dump()))
