"""segmento: logical documents found inside each PDF

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-14
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIPOS = (
    "factura", "recibo", "nomina", "transferencia",
    "lista_asistencia", "liquidacion", "contrato", "otro",
)


def upgrade() -> None:
    op.create_table(
        "segmento",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("archivo", sa.String(length=500), nullable=False),
        sa.Column("backend", sa.String(length=100), nullable=False),
        sa.Column("pagina_inicio", sa.Integer(), nullable=False),
        sa.Column("pagina_fin", sa.Integer(), nullable=False),
        sa.Column(
            "tipo",
            sa.Enum(*TIPOS, name="tiposegmento", native_enum=False, create_constraint=True,
                    length=32),
            nullable=False,
        ),
        sa.Column("emisor", sa.String(length=300), nullable=True),
        sa.Column("nif", sa.String(length=50), nullable=True),
        sa.Column("numero_factura", sa.String(length=100), nullable=True),
        sa.Column("fecha", sa.Date(), nullable=True),
        sa.Column("importe", sa.Numeric(16, 2), nullable=True),
        sa.Column("moneda", sa.String(length=3), nullable=True),
        sa.Column("numero_orden", sa.String(length=50), nullable=True),
        sa.Column("sello_donante", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("sello_expediente", sa.String(length=100), nullable=True),
        sa.Column("sello_porcentaje", sa.Float(), nullable=True),
        sa.Column("prueba_pago", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("legible", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("confianza_ocr", sa.Float(), nullable=True),
        sa.Column("extra", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"],
                                name=op.f("fk_segmento_document_id_document"),
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_segmento")),
        sa.UniqueConstraint("sha256", "backend", "pagina_inicio",
                            name=op.f("uq_segmento_sha256")),
    )
    op.create_index(op.f("ix_segmento_document_id"), "segmento", ["document_id"])
    op.create_index(op.f("ix_segmento_sha256"), "segmento", ["sha256"])


def downgrade() -> None:
    op.drop_index(op.f("ix_segmento_sha256"), table_name="segmento")
    op.drop_index(op.f("ix_segmento_document_id"), table_name="segmento")
    op.drop_table("segmento")
