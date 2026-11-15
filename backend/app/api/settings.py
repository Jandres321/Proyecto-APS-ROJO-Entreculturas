"""What the page needs to know about the installation: donors, data folder, Drive, external LLM."""

from typing import Any

from fastapi import APIRouter

from app import drive
from app.config import get_settings
from app.enums import Donor, LlmMode

router = APIRouter(tags=["settings"])

DONOR_LABELS_ES = {Donor.XUNTA: "Xunta de Galicia", Donor.AECID: "AECID"}
LLM_MODE_LABELS_ES = {
    LlmMode.OFF: "No (solo OCR local)",
    LlmMode.DOUBTFUL: "Solo las páginas que el OCR lee mal",
    LlmMode.ALL: "Todas las páginas",
}


@router.get("/settings")
def app_settings() -> dict[str, Any]:
    settings = get_settings()
    external_llm = settings.llm_provider.strip().lower() != "fake" and settings.allow_external_llm
    return {
        "donors": [{"value": d.value, "label": DONOR_LABELS_ES.get(d, d.value)} for d in Donor],
        "llm_modes": [{"value": m.value, "label": LLM_MODE_LABELS_ES[m]} for m in LlmMode],
        "data_dir_available": settings.data_dir_available,
        "drive": drive.status(settings),
        "external_llm": external_llm,
        "llm_provider": settings.llm_provider if external_llm else None,
        "llm_model": settings.llm_model if external_llm else None,
    }
