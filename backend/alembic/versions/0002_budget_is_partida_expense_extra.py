"""budget_line.is_partida and expense.extra

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-14
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "budget_line",
        sa.Column("is_partida", sa.Boolean(), server_default="true", nullable=False),
    )
    op.add_column(
        "expense",
        sa.Column("extra", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("expense", "extra")
    op.drop_column("budget_line", "is_partida")
