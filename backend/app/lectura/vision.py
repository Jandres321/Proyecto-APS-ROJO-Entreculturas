"""Optional vision-LLM backend.

It sends page images to an external service, so it is OFF unless `ALLOW_EXTERNAL_LLM=true`
(the offline `fake` provider is always allowed). Results are cached per page under their own
backend name (model + prompt version), so a page is never paid for twice.
"""

import asyncio
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from app.config import Settings, get_settings
from app.enums import TipoSegmento
from app.lectura import pages as pg
from app.llm import LLMClient, get_llm_client

PROMPT_VERSION = "1"

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tipo": {"type": "string", "enum": [t.value for t in TipoSegmento]},
        "emisor": {"type": ["string", "null"]},
        "nif": {"type": ["string", "null"]},
        "numero_factura": {"type": ["string", "null"]},
        "fecha": {"type": ["string", "null"], "description": "ISO 8601 (YYYY-MM-DD)"},
        "importe": {"type": ["number", "null"]},
        "moneda": {"type": ["string", "null"]},
        "numero_orden": {"type": ["string", "null"]},
        "sello_donante": {"type": "boolean"},
        "sello_expediente": {"type": ["string", "null"]},
        "sello_porcentaje": {"type": ["number", "null"]},
        "prueba_pago": {"type": "boolean"},
        "legible": {"type": "boolean"},
    },
    "required": ["tipo", "sello_donante", "prueba_pago", "legible"],
}

PROMPT = (
    "Eres un asistente que lee justificantes de gasto de proyectos de cooperación. "
    "Devuelve SOLO JSON con los campos del esquema para esta página. "
    "tipo: factura, recibo, nomina, transferencia, lista_asistencia, liquidacion, contrato u otro. "
    "importe: el total como número (en Paraguay el punto separa miles y la coma los decimales). "
    "fecha en ISO. numero_orden: el número de orden escrito a mano o sellado. "
    "sello_donante: hay sello del donante (AECID, Xunta...) con expediente y % de imputación. "
    "prueba_pago: sello de pagado, firma del proveedor o justificante bancario. "
    "No inventes: usa null cuando no se lea."
)


class ExternalLlmNotAllowed(PermissionError):
    pass


LlmMode = Literal["off", "dudosas", "todas"]
LLM_MODES = ("off", "dudosas", "todas")
UNLIMITED_PAGES = 1_000_000


@dataclass(frozen=True)
class LlmReadOptions:
    """How much of a run goes through the vision LLM.

    - `off`: never call the LLM.
    - `dudosas`: only pages whose OCR confidence is below `min_ocr_confidence`, or whose segment
      lacks an amount or a date (see `page_doubt`).
    - `todas`: every page (still capped by `max_pages`, most doubtful first).
    `max_pages` is a hard cap on pages SENT per run; pages already in the cache are free and do not
    count against it.
    """

    mode: LlmMode = "dudosas"
    max_pages: int = 50
    min_ocr_confidence: float = 60.0

    def __post_init__(self) -> None:
        if self.mode not in LLM_MODES:
            raise ValueError(f"LlmReadOptions.mode must be one of {LLM_MODES}, got {self.mode!r}")
        if self.max_pages < 0:
            raise ValueError("LlmReadOptions.max_pages cannot be negative")

    @property
    def active(self) -> bool:
        return self.mode != "off"


@dataclass
class LlmReadSummary:
    """What a reading run asked of the LLM (all zeros when it was off)."""

    mode: str = "off"
    model: str | None = None
    pages_total: int = 0  # pages read in the run
    pages_candidate: int = 0  # pages that qualified (and were not cached)
    pages_sent: int = 0  # pages actually sent to the LLM (<= max_pages)
    pages_cached: int = 0  # pages answered from the cache at no cost
    pages_over_limit: int = 0  # candidates left out by max_pages
    pages_failed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)


# Segment types for which an amount / a date is expected (lists and "other" have neither).
_NO_AMOUNT_OR_DATE = {TipoSegmento.LISTA_ASISTENCIA, TipoSegmento.OTRO}


def page_doubt(page: pg.Page, segment: Any, min_ocr_confidence: float) -> float:
    """How doubtful a page is (0 = not doubtful). Higher = send to the LLM first.

    +1 plus how far below `min_ocr_confidence` the OCR is, +1 for a segment without amount and +1
    for one without date (types where those fields do not apply are not penalised).
    """
    doubt = 0.0
    if page.confidence is not None and page.confidence < min_ocr_confidence:
        doubt += 1 + (min_ocr_confidence - page.confidence) / max(min_ocr_confidence, 1.0)
    if segment is not None and segment.tipo not in _NO_AMOUNT_OR_DATE:
        doubt += (segment.importe is None) + (segment.fecha is None)
    return doubt


def get_vision_client(settings: Settings | None = None) -> LLMClient:
    """LLM client for the vision backend; refuses external providers unless allowed."""
    settings = settings or get_settings()
    if settings.llm_provider.strip().lower() != "fake" and not settings.allow_external_llm:
        raise ExternalLlmNotAllowed(
            "El backend LLM con visión envía las páginas a un servicio externo. "
            "Está desactivado: define ALLOW_EXTERNAL_LLM=true solo con aprobación del equipo."
        )
    return get_llm_client(settings)


def backend_name(client: LLMClient) -> str:
    return f"llm-{client.model or client.provider}-p{PROMPT_VERSION}"


async def _enrich_one(
    path: Path, sha256: str, page: pg.Page, client: LLMClient, settings: Settings, backend: str
) -> pg.Page:
    cache = pg._cache_path(sha256, page.n, backend, settings)
    cached = pg._load_cache(cache)
    if cached is not None:
        return cached
    png = await asyncio.to_thread(pg.render, path, page.n, 150, page.rotation)
    # The client bounds its own concurrent calls (LLMClient.semaphore). Do NOT take that
    # semaphore here as well: holding one permit while the client waits for another deadlocks
    # the whole run as soon as there are more pages than permits.
    data = await client.extract([png], SCHEMA, PROMPT)
    enriched = dataclasses.replace(page, backend=backend, llm_fields=data or None)
    pg._save_cache(cache, enriched)
    return enriched


async def enrich_pages(
    path: Path,
    sha256: str,
    page_list: list[pg.Page],
    client: LLMClient,
    settings: Settings | None = None,
) -> list[pg.Page]:
    """Add the vision-LLM fields to already-read pages (bounded by the client's semaphore)."""
    settings = settings or get_settings()
    backend = backend_name(client)
    return list(
        await asyncio.gather(
            *(_enrich_one(path, sha256, p, client, settings, backend) for p in page_list)
        )
    )


def load_cached(
    sha256: str, page: pg.Page, client: LLMClient, settings: Settings
) -> pg.Page | None:
    """The page as the LLM already read it in an earlier run, if cached."""
    return pg._load_cache(pg._cache_path(sha256, page.n, backend_name(client), settings))


async def enrich_selected(
    items: list[tuple[Path, str, pg.Page]],
    client: LLMClient,
    settings: Settings,
    summary: LlmReadSummary,
) -> list[pg.Page]:
    """Send exactly these pages to the LLM (same order back). A failing page keeps its OCR
    reading and is counted in `summary` instead of sinking the run."""
    backend = backend_name(client)

    async def one(path: Path, sha: str, page: pg.Page) -> pg.Page:
        try:
            return await _enrich_one(path, sha, page, client, settings, backend)
        except Exception as exc:
            summary.pages_failed += 1
            message = f"{type(exc).__name__}: {exc}"[:200]
            if message not in summary.errors:
                summary.errors.append(message)
            return page

    return list(await asyncio.gather(*(one(*item) for item in items)))


def fill_usage(summary: LlmReadSummary, client: LLMClient, settings: Settings) -> None:
    """Tokens and estimated cost of the calls the client made (`client.usage`)."""
    summary.input_tokens = sum(u.get("input", 0) for u in client.usage)
    summary.output_tokens = sum(u.get("output", 0) for u in client.usage)
    summary.estimated_cost_usd = round(
        summary.input_tokens * settings.llm_price_input_per_mtok / 1e6
        + summary.output_tokens * settings.llm_price_output_per_mtok / 1e6,
        6,
    )
    summary.model = client.model or client.provider
