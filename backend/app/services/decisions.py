"""The reviewer's decision on an expense: "Correcto" or "Incidencia real", with a note.

The decision records the fingerprint of what the reviewer saw (see `app/verdict.py`), so it no
longer applies if a later analysis reads or finds something different.
"""

import datetime as dt
from typing import Any

from sqlalchemy.orm import Session

from app.enums import ReviewDecision
from app.models import Expense, Project


class DecisionError(ValueError):
    """A decision that cannot be accepted as sent (the message is shown to the reviewer)."""


def set_expense_decision(
    session: Session, expense_id: int, status: ReviewDecision, note: str | None
) -> dict[str, Any]:
    """Store the decision and return the expense as the review view shows it."""
    from app.review_view import project_review

    expense = session.get(Expense, expense_id)
    if expense is None:
        raise LookupError(f"Expense {expense_id} not found")
    project = session.get(Project, expense.project_id)
    current = project_review(session, project, only_expense=expense_id)["expenses"][0]
    note = (note or "").strip() or None
    if (
        status == ReviewDecision.CORRECT
        and not note
        and any(r["blocking"] for r in current["reasons"])
    ):
        raise DecisionError(
            "Este gasto tiene un motivo bloqueante: explica en la nota por qué es correcto."
        )
    expense.review_status = status
    expense.review_note = note
    expense.reviewed_at = None if status == ReviewDecision.PENDING else dt.datetime.now(dt.UTC)
    expense.review_fingerprint = (
        None if status == ReviewDecision.PENDING else current["fingerprint"]
    )
    session.commit()
    return project_review(session, project, only_expense=expense_id)["expenses"][0]
