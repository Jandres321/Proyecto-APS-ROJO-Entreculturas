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
