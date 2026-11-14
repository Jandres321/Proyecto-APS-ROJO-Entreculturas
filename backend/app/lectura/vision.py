"""Optional vision-LLM backend.

It sends page images to an external service, so it is OFF unless `ALLOW_EXTERNAL_LLM=true`
(the offline `fake` provider is always allowed). Results are cached per page under their own
backend name (model + prompt version), so a page is never paid for twice.
"""

import asyncio
import dataclasses
from pathlib import Path
from typing import Any

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
    async with client.semaphore:
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
