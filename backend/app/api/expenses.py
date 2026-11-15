"""Expense routes: the reviewer's decision on an expense as a whole."""

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.api.deps import SessionDep
from app.enums import ReviewDecision
from app.services.decisions import DecisionError, set_expense_decision

router = APIRouter(prefix="/expenses", tags=["expenses"])


class DecisionIn(BaseModel):
    status: ReviewDecision
    note: str | None = None


@router.patch("/{expense_id}/decision")
def decide(expense_id: int, payload: DecisionIn, session: SessionDep) -> dict[str, Any]:
    """Mark the expense as correct or as a real issue (or back to pending)."""
    try:
        return set_expense_decision(session, expense_id, payload.status, payload.note)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Gasto no encontrado") from None
    except DecisionError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from None
