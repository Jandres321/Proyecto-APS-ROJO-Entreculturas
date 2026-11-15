"""ORM models (SQLAlchemy 2, typed mapping)."""

import datetime as dt
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.enums import (
    Confidence,
    Donor,
    FindingStatus,
    JobState,
    LlmMode,
    MatchMethod,
    ProjectStatus,
    RuleKind,
    Severity,
    SourceType,
    TipoSegmento,
)

Money = Numeric(14, 2)
Rate = Numeric(18, 8)


def enum_type(enum_cls: type[StrEnum]) -> SAEnum:
    """Store enums as VARCHAR + CHECK constraint (easier to migrate than PG native enums)."""
    return SAEnum(
        enum_cls,
        name=enum_cls.__name__.lower(),
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda e: [m.value for m in e],
    )


def _default_project_key(context: Any) -> str:
    """Unique slug when a project is created without an explicit key (the API always sets one)."""
    import re
    import uuid

    params = context.get_current_parameters()
    base = re.sub(r"[^a-z0-9]+", "-", str(params.get("code") or params.get("name") or "").lower())
    return f"{base.strip('-')[:60] or 'proyecto'}-{uuid.uuid4().hex[:6]}"


def created_at_column() -> Mapped[dt.datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Project(Base):
    __tablename__ = "project"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    donor: Mapped[Donor] = mapped_column(enum_type(Donor))
    code: Mapped[str | None] = mapped_column(String(50), index=True)
    currency: Mapped[str] = mapped_column(String(3), default="EUR", server_default="EUR")
    created_at: Mapped[dt.datetime] = created_at_column()
    status: Mapped[ProjectStatus] = mapped_column(
        enum_type(ProjectStatus),
        default=ProjectStatus.DRAFT,
        server_default=ProjectStatus.DRAFT.value,
    )
    # 0-100, updated by the pipeline while processing.
    progress: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # Stable identifier used in URLs (slug), unique.
    key: Mapped[str] = mapped_column(
        String(80), unique=True, index=True, default=_default_project_key
    )
    # Where the files come from and where they are on disk (relative to DATA_DIR).
    source_type: Mapped[SourceType] = mapped_column(
        enum_type(SourceType), default=SourceType.LOCAL, server_default=SourceType.LOCAL.value
    )
    # Drive folder id for `drive`; unused for `local` / `upload`.
    source_ref: Mapped[str | None] = mapped_column(String(300))
    folder: Mapped[str | None] = mapped_column(String(500))
    # Report spreadsheet, relative to `folder`. Empty: located when the analysis starts.
    report_file: Mapped[str | None] = mapped_column(String(500))
    # Execution period of the project (dates outside it are findings).
    start_date: Mapped[dt.date | None] = mapped_column(Date)
    end_date: Mapped[dt.date | None] = mapped_column(Date)
    # Overrides of the donor's default matching config (see pipeline/match_config.py).
    match_config: Mapped[dict[str, Any] | None]
    # Reading the PDFs with the external vision LLM (only if ALLOW_EXTERNAL_LLM is true).
    llm_mode: Mapped[LlmMode] = mapped_column(
        enum_type(LlmMode), default=LlmMode.OFF, server_default=LlmMode.OFF.value
    )
    llm_max_pages: Mapped[int] = mapped_column(Integer, default=20, server_default="20")
    # Sample projects shipped with the app (created by app/samples.py).
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    budget_lines: Mapped[list["BudgetLine"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )
    expenses: Mapped[list["Expense"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )
    documents: Mapped[list["Document"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )
    findings: Mapped[list["Finding"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )


class AnalysisJob(Base):
    """One run of the review pipeline for a project: state and progress, persisted so that it
    survives a restart of the API and can be read by any process."""

    __tablename__ = "analysis_job"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), index=True
    )
    state: Mapped[JobState] = mapped_column(enum_type(JobState))
    step: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    steps: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    fraction: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    message: Mapped[str] = mapped_column(Text, default="", server_default="")
    reread_report: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    started_at: Mapped[dt.datetime] = created_at_column()
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class BudgetLine(Base):
    __tablename__ = "budget_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), index=True
    )
    partida_code: Mapped[str] = mapped_column(String(50))
    partida_name: Mapped[str | None] = mapped_column(String(300))
    approved_donor: Mapped[Decimal | None] = mapped_column(Money)
    approved_total: Mapped[Decimal | None] = mapped_column(Money)
    executed_donor: Mapped[Decimal | None] = mapped_column(Money)
    executed_total: Mapped[Decimal | None] = mapped_column(Money)
    # False for subtotal / aggregate rows (TOTAL A.I, % SOBRE TOTALES, ...): they are kept
    # for reference but expenses are never imputed to them.
    is_partida: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    project: Mapped[Project] = relationship(back_populates="budget_lines")


class Expense(Base):
    __tablename__ = "expense"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), index=True
    )
    # order_no: as written in the report ("33", "FA236.3", "N/A"...).
    # order_key: normalized key used for matching with documents.
    order_no: Mapped[str | None] = mapped_column(String(50))
    order_key: Mapped[str | None] = mapped_column(String(50), index=True)
    date: Mapped[dt.date | None] = mapped_column(Date)
    source: Mapped[str | None] = mapped_column(String(100))
    issuer: Mapped[str | None] = mapped_column(String(300))
    concept: Mapped[str | None] = mapped_column(Text)
    partida_code: Mapped[str | None] = mapped_column(String(50))
    partida_raw: Mapped[str | None] = mapped_column(String(300))
    amount_local: Mapped[Decimal | None] = mapped_column(Money)
    currency: Mapped[str | None] = mapped_column(String(3))
    fx_rate: Mapped[Decimal | None] = mapped_column(Rate)
    amount_eur: Mapped[Decimal | None] = mapped_column(Money)
    amount_donor: Mapped[Decimal | None] = mapped_column(Money)
    # 1-based row in the source spreadsheet, for traceability.
    source_row: Mapped[int | None] = mapped_column(Integer)
    # Donor-specific columns that have no dedicated field (invoice no., payment, other funders...).
    extra: Mapped[dict[str, Any] | None]
    suggested_partida: Mapped[str | None] = mapped_column(String(50))
    suggested_confidence: Mapped[float | None] = mapped_column(Float)
    suggested_reason: Mapped[str | None] = mapped_column(Text)

    project: Mapped[Project] = relationship(back_populates="expenses")
    document_links: Mapped[list["ExpenseDocument"]] = relationship(
        back_populates="expense", cascade="all, delete-orphan", passive_deletes=True
    )


class Document(Base):
    __tablename__ = "document"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), index=True
    )
    # Path relative to DATA_DIR (never absolute host paths).
    path: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    pages: Mapped[int | None] = mapped_column(Integer)
    is_scanned: Mapped[bool | None] = mapped_column(Boolean)

    project: Mapped[Project] = relationship(back_populates="documents")
    expense_links: Mapped[list["ExpenseDocument"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )
    extractions: Mapped[list["Extraction"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )


class ExpenseDocument(Base):
    __tablename__ = "expense_document"

    expense_id: Mapped[int] = mapped_column(
        ForeignKey("expense.id", ondelete="CASCADE"), primary_key=True
    )
    document_id: Mapped[int] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), primary_key=True
    )
    method: Mapped[MatchMethod] = mapped_column(enum_type(MatchMethod))
    score: Mapped[float | None] = mapped_column(Float)
    # Hybrid matching: how much to trust the link, whether the document's CONTENT confirms it
    # (amount + issuer / date, or invoice number) and why it was linked, in Spanish.
    confidence: Mapped[Confidence | None] = mapped_column(enum_type(Confidence))
    verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    reason: Mapped[str | None] = mapped_column(Text)

    expense: Mapped[Expense] = relationship(back_populates="document_links")
    document: Mapped[Document] = relationship(back_populates="expense_links")


class Extraction(Base):
    __tablename__ = "extraction"
    __table_args__ = (UniqueConstraint("document_id", "model", "prompt_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), index=True
    )
    model: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(50))
    data: Mapped[dict[str, Any]]
    created_at: Mapped[dt.datetime] = created_at_column()

    document: Mapped[Document] = relationship(back_populates="extractions")


class Rule(Base):
    __tablename__ = "rule"
    __table_args__ = (UniqueConstraint("donor", "code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # NULL donor = rule applies to every donor.
    donor: Mapped[Donor | None] = mapped_column(enum_type(Donor))
    code: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text)
    kind: Mapped[RuleKind] = mapped_column(enum_type(RuleKind))
    # List of partida codes; NULL/empty = all partidas.
    applies_to_partidas: Mapped[list[Any] | None]
    # Normative reference (guide, article, page).
    reference: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[Severity] = mapped_column(enum_type(Severity))
    params: Mapped[dict[str, Any] | None]
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")


class Finding(Base):
    __tablename__ = "finding"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), index=True
    )
    expense_id: Mapped[int | None] = mapped_column(
        ForeignKey("expense.id", ondelete="CASCADE"), index=True
    )
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), index=True
    )
    # Not a FK: some rules are hard-coded and do not live in the rule table.
    rule_code: Mapped[str] = mapped_column(String(100), index=True)
    severity: Mapped[Severity] = mapped_column(enum_type(Severity))
    # User-facing explanation, in Spanish.
    message: Mapped[str] = mapped_column(Text)
    # e.g. {"page": 2, "field": "total", "expected": "1200.00", "found": "1250.00"}
    evidence: Mapped[dict[str, Any] | None]
    status: Mapped[FindingStatus] = mapped_column(
        enum_type(FindingStatus),
        default=FindingStatus.OPEN,
        server_default=FindingStatus.OPEN.value,
        index=True,
    )
    reviewer_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = created_at_column()

    project: Mapped[Project] = relationship(back_populates="findings")


class Segmento(Base):
    """A logical document found inside a PDF (consecutive pages) with its extracted fields."""

    __tablename__ = "segmento"
    __table_args__ = (UniqueConstraint("sha256", "backend", "pagina_inicio"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # Set when the PDF is registered as a project document; the PDF is always identified by hash.
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("document.id", ondelete="SET NULL"), index=True
    )
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    archivo: Mapped[str] = mapped_column(String(500))
    # Reading backend: "tesseract" or "llm-<model>-p<prompt version>".
    backend: Mapped[str] = mapped_column(String(100))
    # First/last page (1-based) of the segment; `pagina` in the spec is `pagina_inicio`.
    pagina_inicio: Mapped[int] = mapped_column(Integer)
    pagina_fin: Mapped[int] = mapped_column(Integer)
    tipo: Mapped[TipoSegmento] = mapped_column(enum_type(TipoSegmento))
    emisor: Mapped[str | None] = mapped_column(String(300))
    nif: Mapped[str | None] = mapped_column(String(50))
    numero_factura: Mapped[str | None] = mapped_column(String(100))
    fecha: Mapped[dt.date | None] = mapped_column(Date)
    importe: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    moneda: Mapped[str | None] = mapped_column(String(3))
    numero_orden: Mapped[str | None] = mapped_column(String(50))
    sello_donante: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    sello_expediente: Mapped[str | None] = mapped_column(String(100))
    sello_porcentaje: Mapped[float | None] = mapped_column(Float)
    prueba_pago: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    legible: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    confianza_ocr: Mapped[float | None] = mapped_column(Float)
    extra: Mapped[dict[str, Any] | None]
    created_at: Mapped[dt.datetime] = created_at_column()

    document: Mapped[Document | None] = relationship()


class ExpenseSegment(Base):
    """Content-based link between an expense and a segment of a document (N:M)."""

    __tablename__ = "expense_segment"

    expense_id: Mapped[int] = mapped_column(
        ForeignKey("expense.id", ondelete="CASCADE"), primary_key=True
    )
    segmento_id: Mapped[int] = mapped_column(
        ForeignKey("segmento.id", ondelete="CASCADE"), primary_key=True
    )
    score: Mapped[float] = mapped_column(Float)
    confidence: Mapped[Confidence] = mapped_column(enum_type(Confidence))
    # Plain-Spanish explanation of why they were linked, for the reviewer.
    reason: Mapped[str] = mapped_column(Text)
    # Per-signal breakdown: {"importe": 0.45, "factura": 0.0, ...}.
    evidence: Mapped[dict[str, Any] | None]
    created_at: Mapped[dt.datetime] = created_at_column()

    expense: Mapped[Expense] = relationship()
    segmento: Mapped[Segmento] = relationship()
