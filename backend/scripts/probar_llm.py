"""Una sola llamada de prueba al LLM con un gasto INVENTADO (no envía datos reales).

Uso (desde backend/):  python scripts/probar_llm.py
Requiere LLM_PROVIDER=anthropic y LLM_API_KEY en el .env. Coste: ~1 llamada pequeña.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/ -> "app" importable

from app.llm.factory import get_llm_client
from app.pipeline.classify import llm_suggestions

CATALOGO = [
    {"code": "A.I.3", "name": "Otros servicios técnicos"},
    {"code": "A.I.5", "name": "Materiales y suministros"},
    {"code": "A.I.7", "name": "Viajes, alojamientos y dietas"},
]
GASTO = {
    "id": 1,
    "concepto": "Pago a facilitadora de taller de huertas (ejemplo)",
    "proveedor": "Ejemplo",
}

if __name__ == "__main__":
    llm = get_llm_client()
    out = asyncio.run(llm_suggestions(llm, "aecid", CATALOGO, [GASTO]))
    for s in out.values():
        print(
            f"{llm.provider}/{llm.model}: partida {s.partida} "
            f"(confianza {s.confidence}) - {s.reason}"
        )
    print("tokens:", getattr(llm, "usage", None))
