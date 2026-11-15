"""expense: the reviewer's decision on the expense as a whole

A decision ("correct" / "real issue", with a note) belongs to the expense, not to one finding:
an expense can need review without having any finding (document only proposed by its file name,
nothing read...). `review_fingerprint` records what the reviewer saw, so a decision goes stale
when the evidence changes.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "expense",
        sa.Column(
            "review_status",
            sa.Enum(
                "pending",
                "correct",
                "real_issue",
                name="reviewdecision",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column("expense", sa.Column("review_note", sa.Text(), nullable=True))
    op.add_column("expense", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("expense", sa.Column("review_fingerprint", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("expense", "review_fingerprint")
    op.drop_column("expense", "reviewed_at")
    op.drop_column("expense", "review_note")
    op.drop_column("expense", "review_status")
