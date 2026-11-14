"""Reviewer workflow on findings: list, filter, decide (accept / false positive) and locate the
document and page a finding is about. Shared by the API and the Streamlit app."""

from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.enums import CONFIDENCE_LABELS_ES, Confidence, FindingStatus, MatchMethod, Severity
from app.models import Document, Expense, ExpenseDocument, Finding, Project, Segmento
from app.pipeline.match_config import (
    KIND_COFINANCED,
    KIND_INDIRECT,
    expense_kind,
    get_match_config,
)


def list_findings(
    session: Session,
    project_id: int,
    *,
    status: FindingStatus | None = None,
    severity: Severity | None = None,
    rule_codes: Collection[str] | None = None,
) -> list[Finding]:
    """Findings of a project, most severe first, then by rule and creation order."""
    query = select(Finding).where(Finding.project_id == project_id)
    if status is not None:
        query = query.where(Finding.status == status)
    if severity is not None:
        query = query.where(Finding.severity == severity)
    if rule_codes:
        query = query.where(Finding.rule_code.in_(list(rule_codes)))
    order = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}
    return sorted(
        session.scalars(query.order_by(Finding.id)), key=lambda f: (order[f.severity], f.rule_code)
    )


def set_decision(
    session: Session,
    finding_id: int,
    status: FindingStatus | None = None,
    note: str | None = None,
) -> Finding:
    """Record the reviewer's decision. `status` None leaves it as is; a note of None keeps the
    previous one (pass "" to clear it). Decisions survive re-running the pipeline."""
    finding = session.get(Finding, finding_id)
    if finding is None:
        raise LookupError(f"Finding {finding_id} not found")
    if status is not None:
        finding.status = status
    if note is not None:
        finding.reviewer_note = note or None
    session.commit()
    return finding


@dataclass
class FindingLocation:
    """Where to look at a finding: a PDF (path relative to DATA_DIR) and a 1-based page."""

    document: Document | None
    page: int | None

    @property
    def path(self) -> Path | None:
        """Absolute path of the PDF, when DATA_DIR is set and the file exists."""
        data_dir = get_settings().data_dir
        if self.document is None or data_dir is None:
            return None
        full = data_dir / self.document.path
        return full if full.is_file() else None


def locate(session: Session, finding: Finding) -> FindingLocation:
    """Document and page of a finding.

    Document: the finding's own, else the closest candidate a DOC_MISSING remembers
    (`best_document_id`), else the first document linked to its expense. Page: the one written in
    the evidence, else the first page of the segment it mentions.
    """
    evidence = finding.evidence or {}
    document_id = finding.document_id or evidence.get("best_document_id")
    if document_id is None and finding.expense_id is not None:
        link = session.scalars(
            select(ExpenseDocument)
            .where(ExpenseDocument.expense_id == finding.expense_id)
            .order_by(ExpenseDocument.score.desc().nulls_last())
        ).first()
        document_id = link.document_id if link else None
    document = session.get(Document, document_id) if document_id else None

    page = evidence.get("pagina_inicio")
    if page is None:
        segment_id = evidence.get("segment_id") or evidence.get("best_segment")
        segment = session.get(Segmento, segment_id) if segment_id else None
        page = segment.pagina_inicio if segment else None
    return FindingLocation(document, page)


# --- document status of each expense ----------------------------------------------------------

VERIFIED = "verified"  # a document, confirmed by its content (amount + issuer / date, invoice no.)
BY_NAME = "by_name"  # a document proposed by its file name; the content does not confirm it
BY_CONTENT = "by_content"  # a document found by its content
MANUAL = "manual"  # linked by hand
MISSING = "missing"  # no document and the folder is complete
NOT_IN_SAMPLE = "not_in_sample"  # no document, but the folder is only a sample (informative)
NOT_REQUIRED_INDIRECT = "not_required_indirect"
NOT_REQUIRED_COFINANCED = "not_required_cofinanced"

DOCUMENT_STATUS_LABELS_ES = {
    VERIFIED: "Con justificante · verificado por contenido",
    BY_NAME: "Con justificante · propuesto por el nombre (sin verificar)",
    BY_CONTENT: "Con justificante · encontrado por contenido",
    MANUAL: "Con justificante · vinculado a mano",
    MISSING: "Sin justificante",
    NOT_IN_SAMPLE: "No incluido en la muestra",
    NOT_REQUIRED_INDIRECT: "No requiere justificante (coste indirecto)",
    NOT_REQUIRED_COFINANCED: "No requiere justificante (cofinanciación)",
}


@dataclass
class DocumentStatus:
    status: str
    confidence: Confidence | None = None  # of the best link, when there is one

    @property
    def label(self) -> str:
        return DOCUMENT_STATUS_LABELS_ES[self.status]

    @property
    def confidence_label(self) -> str | None:
        return CONFIDENCE_LABELS_ES[self.confidence] if self.confidence else None


def document_statuses(session: Session, project_id: int) -> dict[int, DocumentStatus]:
    """expense id -> how its justificante stands (see DOCUMENT_STATUS_LABELS_ES)."""
    project = session.get(Project, project_id)
    if project is None:
        raise LookupError(f"Project {project_id} not found")
    config = get_match_config(project)
    complete = config.get("carpeta_completa", True)
    links: dict[int, list[ExpenseDocument]] = {}
    for link in session.scalars(
        select(ExpenseDocument).join(Expense).where(Expense.project_id == project_id)
    ):
        links.setdefault(link.expense_id, []).append(link)

    order = {Confidence.HIGH: 2, Confidence.MEDIUM: 1, Confidence.LOW: 0}
    out: dict[int, DocumentStatus] = {}
    for e in session.scalars(select(Expense).where(Expense.project_id == project_id)):
        mine = links.get(e.id, [])
        if mine:
            best = max(mine, key=lambda x: order.get(x.confidence, -1))
            if any(x.verified for x in mine):
                status = VERIFIED
            elif any(x.method == MatchMethod.CONTENT for x in mine):
                status = BY_CONTENT
            elif any(x.method == MatchMethod.FILENAME for x in mine):
                status = BY_NAME
            else:
                status = MANUAL
            out[e.id] = DocumentStatus(status, best.confidence)
            continue
        kind = expense_kind(e, config)
        if kind == KIND_INDIRECT:
            out[e.id] = DocumentStatus(NOT_REQUIRED_INDIRECT)
        elif kind == KIND_COFINANCED:
            out[e.id] = DocumentStatus(NOT_REQUIRED_COFINANCED)
        else:
            out[e.id] = DocumentStatus(MISSING if complete else NOT_IN_SAMPLE)
    return out
