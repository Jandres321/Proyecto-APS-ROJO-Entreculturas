"""Enumerations shared by ORM models and Pydantic schemas.

Values are stored in English; Spanish labels for the UI/Excel live in *_LABELS_ES.
"""

from enum import StrEnum


class Donor(StrEnum):
    XUNTA = "xunta"
    AECID = "aecid"


class ProjectStatus(StrEnum):
    DRAFT = "draft"
    PROCESSING = "processing"
    READY = "ready"
    ERROR = "error"


class SourceType(StrEnum):
    """Where a project's files come from."""

    LOCAL = "local"  # a folder already under DATA_DIR (the sample projects)
    UPLOAD = "upload"  # files uploaded from the browser, stored under DATA_DIR/_proyectos/<key>
    DRIVE = "drive"  # a Google Drive folder mirrored into DATA_DIR/_proyectos/<key>


class LlmMode(StrEnum):
    """Use of the external vision LLM when reading the PDFs (always behind ALLOW_EXTERNAL_LLM)."""

    OFF = "off"
    DOUBTFUL = "dudosas"  # only pages the OCR reads badly
    ALL = "todas"


class JobState(StrEnum):
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


class MatchMethod(StrEnum):
    FILENAME = "filename"
    CONTENT = "content"
    MANUAL = "manual"


class Confidence(StrEnum):
    """How much a human should trust an automatic expense <-> segment link."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


CONFIDENCE_LABELS_ES = {
    Confidence.HIGH: "alta",
    Confidence.MEDIUM: "media",
    Confidence.LOW: "baja",
}


class RuleKind(StrEnum):
    DETERMINISTIC = "deterministic"
    LLM = "llm"
    DOC_REQUIREMENT = "doc_requirement"


class Severity(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class FindingStatus(StrEnum):
    OPEN = "open"
    ACCEPTED = "accepted"
    FALSE_POSITIVE = "false_positive"


class TipoSegmento(StrEnum):
    """Logical document type of a segment (values are the Spanish slugs used in the DB)."""

    FACTURA = "factura"
    RECIBO = "recibo"
    NOMINA = "nomina"
    TRANSFERENCIA = "transferencia"
    LISTA_ASISTENCIA = "lista_asistencia"
    LIQUIDACION = "liquidacion"
    CONTRATO = "contrato"
    OTRO = "otro"


SEVERITY_LABELS_ES = {
    Severity.HIGH: "alta",
    Severity.MEDIUM: "media",
    Severity.LOW: "baja",
}

FINDING_STATUS_LABELS_ES = {
    FindingStatus.OPEN: "abierta",
    FindingStatus.ACCEPTED: "aceptada",
    FindingStatus.FALSE_POSITIVE: "falso positivo",
}
