"""expense_segment: content-based links between expenses and document segments

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-14
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "expense_segment",
        sa.Column("expense_id", sa.Integer(), nullable=False),
        sa.Column("segmento_id", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column(
            "confidence",
            sa.Enum("high", "medium", "low", name="confidence", native_enum=False,
                    create_constraint=True, length=32),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.ForeignKeyConstraint(["expense_id"], ["expense.id"],
                                name=op.f("fk_expense_segment_expense_id_expense"),
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["segmento_id"], ["segmento.id"],
                                name=op.f("fk_expense_segment_segmento_id_segmento"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("expense_id", "segmento_id", name=op.f("pk_expense_segment")),
    )


def downgrade() -> None:
    op.drop_table("expense_segment")
