"""expense_document: confidence, verified-by-content flag and reason of the hybrid matching

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "expense_document",
        sa.Column(
            "confidence",
            sa.Enum(
                "high",
                "medium",
                "low",
                name="confidence",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "expense_document",
        sa.Column("verified", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column("expense_document", sa.Column("reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("expense_document", "reason")
    op.drop_column("expense_document", "verified")
    op.drop_constraint(op.f("ck_expense_document_confidence"), "expense_document", type_="check")
    op.drop_column("expense_document", "confidence")
