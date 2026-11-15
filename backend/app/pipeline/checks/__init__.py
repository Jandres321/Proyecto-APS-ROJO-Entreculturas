"""Step 4 — Checks: per expense and its document segments, driven by `rules/<donor>.yaml`.

Numeric comparisons are done here in code; the thresholds, tolerances, normative references and
document requirements come from the donor's rules (app/rules.py). Segments come from
`app/lectura/` and are linked to expenses by `app/pipeline/match_content.py`.

Two views of an expense's documents are used:

- ``linked``: the segments linked to the expense by content (amount / date / stamp checks are
  made on these: they ARE its justificante);
- ``bundle``: every segment of the PDFs those links belong to (payment proof, contract,
  attendance list... usually sit next to the invoice in the same PDF).

Checks (rule codes in app/rules.py): AMOUNT_MISMATCH, DATE_MISMATCH, DONOR_STAMP_MISSING,
NOT_AN_INVOICE, PAYMENT_EVIDENCE_MISSING, CASH_OVER_LIMIT, ILLEGIBLE (`check_documents`),
DATE_OUT_OF_PERIOD (when the project has a period), DOC_INCOMPLETE (`check_file_completeness`)
and DUPLICATE_INVOICE (`check_duplicates`).
An expense without any linked segment gets none of the document checks: DOC_MISSING (content
matching) already reports it.
"""

from sqlalchemy.orm import Session

from app.pipeline.checks.completeness import check_file_completeness
from app.pipeline.checks.context import (
    FIELD_LABELS_ES,
    FULL_PERCENT,
    INVOICE_LIKE,
    TIPO_LABELS_ES,
    Case,
    CheckContext,
    ExpenseFacts,
    Seg,
    build_context,
    expected_percent,
    payment_date,
    payment_method,
    project_period,
    soften,
    unreliable_reading,
)
from app.pipeline.checks.documents import check_documents
from app.pipeline.checks.duplicates import check_duplicates
from app.rules import CHECK_CODES
from app.schemas import FindingIn
from app.services.findings import sync_findings

__all__ = [
    "FIELD_LABELS_ES",
    "FULL_PERCENT",
    "INVOICE_LIKE",
    "TIPO_LABELS_ES",
    "Case",
    "CheckContext",
    "ExpenseFacts",
    "Seg",
    "build_context",
    "check_documents",
    "check_duplicates",
    "check_file_completeness",
    "expected_percent",
    "payment_date",
    "payment_method",
    "project_period",
    "run_checks",
    "soften",
    "unreliable_reading",
]


def run_checks(session: Session, project_id: int) -> list[FindingIn]:
    """Run every check and persist the findings, replacing the open ones of a previous run and
    keeping the reviewer's decisions (accepted / false positive)."""
    ctx = build_context(session, project_id)
    findings = check_documents(ctx) + check_file_completeness(ctx) + check_duplicates(ctx)
    sync_findings(session, project_id, set(CHECK_CODES), findings)
    session.commit()
    return findings
