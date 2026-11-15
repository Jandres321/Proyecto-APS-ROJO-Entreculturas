"""project: key, source of the files, period, matching config and LLM options; analysis_job

Projects become the single source of truth: the sample projects, uploaded folders and connected
Google Drive folders are all rows of `project` (no hard-coded list, no JSON registry). Pipeline
runs are persisted in `analysis_job` instead of living in the API process memory.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(name: str, *values: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=32)


def upgrade() -> None:
    op.add_column("project", sa.Column("key", sa.String(80), nullable=True))
    # Existing rows: derive the key from the code (or the id).
    op.execute(
        "UPDATE project SET key = lower(regexp_replace(coalesce(code, 'proyecto-' || id), "
        "'[^A-Za-z0-9]+', '-', 'g'))"
    )
    op.alter_column("project", "key", nullable=False)
    op.create_index(op.f("ix_project_key"), "project", ["key"], unique=True)
    op.add_column(
        "project",
        sa.Column(
            "source_type",
            _enum("sourcetype", "local", "upload", "drive"),
            server_default="local",
            nullable=False,
        ),
    )
    op.add_column("project", sa.Column("source_ref", sa.String(300), nullable=True))
    op.add_column("project", sa.Column("folder", sa.String(500), nullable=True))
    op.add_column("project", sa.Column("report_file", sa.String(500), nullable=True))
    op.add_column("project", sa.Column("start_date", sa.Date(), nullable=True))
    op.add_column("project", sa.Column("end_date", sa.Date(), nullable=True))
    op.add_column(
        "project",
        sa.Column("match_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "project",
        sa.Column(
            "llm_mode", _enum("llmmode", "off", "dudosas", "todas"), server_default="off",
            nullable=False,
        ),
    )
    op.add_column(
        "project", sa.Column("llm_max_pages", sa.Integer(), server_default="20", nullable=False)
    )
    op.add_column(
        "project", sa.Column("is_sample", sa.Boolean(), server_default="false", nullable=False)
    )

    op.create_table(
        "analysis_job",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("state", _enum("jobstate", "running", "done", "error"), nullable=False),
        sa.Column("step", sa.Integer(), server_default="0", nullable=False),
        sa.Column("steps", sa.Integer(), server_default="0", nullable=False),
        sa.Column("fraction", sa.Float(), server_default="0", nullable=False),
        sa.Column("message", sa.Text(), server_default="", nullable=False),
        sa.Column("reread_report", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(op.f("ix_analysis_job_project_id"), "analysis_job", ["project_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_analysis_job_project_id"), table_name="analysis_job")
    op.drop_table("analysis_job")
    for column in (
        "is_sample",
        "llm_max_pages",
        "llm_mode",
        "match_config",
        "end_date",
        "start_date",
        "report_file",
        "folder",
        "source_ref",
        "source_type",
    ):
        op.drop_column("project", column)
    op.drop_index(op.f("ix_project_key"), table_name="project")
    op.drop_column("project", "key")
