"""Step 5 — Classify (Process 2b): suggest the correct partida for each expense.

Two layers:

1. Keyword rules (always, no network): the concept + issuer of the listing are normalised
   (accents, case) and matched against keyword groups ("combustible", "salario", "IPS",
   "comisión"...). Each group is a *category* (viajes, personal local, materiales...) that every
   donor maps to its own partida code. When no rule fires, or several categories tie, the layer
   abstains: no suggestion, no finding.
2. LLM (optional, OFF by default): only for the expenses layer 1 left without a suggestion, only
   when ALLOW_EXTERNAL_LLM=true and only up to `max_llm_expenses` (the API budget is small).
   Only the listing's concept / issuer text is sent, never the PDFs.

A finding PARTIDA_MISMATCH is created when a clear suggestion differs from the declared partida.
Indirect costs (partida B) are never contradicted: they are justified with a certificate and
their concepts (gestoría, servidores, material de oficina) overlap with direct partidas.
"""

import json
import re
import unicodedata
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import Donor, Severity
from app.llm.base import LLMClient
from app.models import Expense, Project
from app.schemas import FindingIn
from app.services.findings import sync_findings

PARTIDA_MISMATCH = "PARTIDA_MISMATCH"
MIN_CONFIDENCE_FINDING = 0.75

# --- categories and keywords (normalised: lowercase, no accents) --------------------------------

# Order matters only to break ties: more specific categories first.
KEYWORDS: dict[str, list[str]] = {
    "auditoria": [r"\bauditori"],
    "evaluacion": [r"\bevaluacion (final|externa|intermedia)", r"\bevaluador"],
    "financieros": [
        r"\bcomision(es)? bancari",
        r"\bcomision\b",
        r"\bgastos? bancari",
        r"\bbank (fee|charge)",
    ],
    "personal": [
        r"\bsalari",
        r"\bnomina",
        r"\bpayroll",
        r"\bsueldo",
        r"\bhaberes\b",
        r"\baguinaldo",
        r"\baporte patronal",
        r"\bips\b",
        r"\bnssf\b",
        r"\bfondo (de )?pensiones",
        r"\bpension",
        r"\bseguro medico",
        r"\bseguridad social",
        r"\bliquidacion haberes",
        r"\bvacaciones",
    ],
    "viajes": [
        r"\bcombustible",
        r"\bgasolina",
        r"\bgasoil",
        r"\bdiesel",
        r"\bfuel\b",
        r"\bnafta",
        r"\bpasaje",
        r"\bbillete",
        r"\bvuelo",
        r"\balojamiento",
        r"\bhotel",
        r"\bestancia",
        r"\bdieta",
        r"\bviatico",
        r"\bperdiem",
        r"\bper diem",
        r"\btransporte",
        r"\bliquidacion (individual|colectiva)",
        r"\bliqui \d",
        r"\bdesplazamiento",
        r"\bpeaje",
    ],
    "alquiler": [r"\balquiler", r"\barrendamiento", r"\brent\b"],
    "construccion": [
        r"\bconstruccion",
        r"\breforma",
        r"\bmateriales de construccion",
        r"\bcemento",
        r"\bladrillo",
    ],
    "equipos": [
        r"\bordenador",
        r"\bportatil",
        r"\blaptop",
        r"\bimpresora\b",
        r"\bproyector",
        r"\bmotocicleta nueva",
        r"\benvasadora",
        r"\bmaquina",
        r"\bequipo(s)? (informatico|de)",
        r"\bmobiliario",
        r"\btanque",
        r"\bmotobomba",
        r"\bdesgranadora",
        r"\binventariable",
        r"\bsistema de riego",
        r"\bmateriales de riego",
    ],
    "servicios": [
        r"\bfacilitador",
        r"\bformador",
        r"\btallerista",
        r"\bconsultor",
        r"\bservicio profesional",
        r"\banalisis de suelo",
        r"\bdiagnostico de fincas",
        r"\bhonorarios",
        r"\bcapacitador",
    ],
    "funcionamiento": [
        r"\belectricidad",
        r"\bfactura (de )?agua",
        r"\bagua oficina",
        r"\btelefon",
        r"\binternet",
        r"\brecoleccion de residuos",
        r"\blimpieza oficina",
        r"\breparacion oficina",
    ],
    "materiales": [
        r"\binsumo",
        r"\bsemilla",
        r"\bfertilizante",
        r"\bmaterial(es)?\b",
        r"\bkit\b",
        r"\bimpresion",
        r"\bpapeleria",
        r"\bmaterial de oficina",
        r"\bproductos? para comercializacion",
        r"\bnfi\b",
        r"\bbioinsumo",
        r"\bcompostaje",
        r"\bplantin",
    ],
    # Food for activities: Xunta puts it with travel/subsistence (dietas), AECID with supplies.
    "comidas": [r"\brefrigerio", r"\baperitivo", r"\bcomida", r"\balimentacion", r"\bcatering"],
}
# Categories whose keywords are often legitimately imputed elsewhere (building materials bought
# as supplies, renting chairs for an event): suggest, but only as a low-severity "check".
CONFIDENCE_CAP = {"construccion": 0.65, "alquiler": 0.7, "comidas": 0.7}

# Category -> partida code, per donor. Categories a donor does not have are absent.
CATEGORY_TO_PARTIDA: dict[str, dict[str, str]] = {
    Donor.AECID.value: {
        "evaluacion": "A.I.1",
        "auditoria": "A.I.2",
        "servicios": "A.I.3",
        "alquiler": "A.I.4",
        "materiales": "A.I.5",
        "comidas": "A.I.5",
        "funcionamiento": "A.I.5",
        "personal": "A.I.6.1",  # A.I.6.3 (sede) decided below
        "viajes": "A.I.7",
        "financieros": "A.I.9",
        "construccion": "A.II.2.1",
        "equipos": "A.II.3",
    },
    Donor.XUNTA.value: {
        "alquiler": "A.I",
        "construccion": "A.II",
        "equipos": "A.III.1",
        "materiales": "A.III.2",
        "comidas": "A.VII",
        "personal": "A.V.2",  # A.V.3 (sede) decided below
        "servicios": "A.VI",
        "viajes": "A.VII",
        "funcionamiento": "A.VIII",
        "evaluacion": "A.IX",
        "auditoria": "A.X",
        "financieros": "A.XI",
    },
}
HQ_STAFF = {Donor.AECID.value: "A.I.6.3", Donor.XUNTA.value: "A.V.3"}
INDIRECT_CODES = {"B"}

LABELS_ES = {
    "auditoria": "auditoría",
    "evaluacion": "evaluación externa",
    "financieros": "gastos financieros",
    "personal": "personal",
    "viajes": "viajes, transporte y dietas",
    "alquiler": "alquiler",
    "construccion": "construcción o reforma",
    "equipos": "equipos inventariables",
    "servicios": "servicios técnicos",
    "funcionamiento": "funcionamiento (suministros de oficina)",
    "materiales": "materiales y suministros",
    "comidas": "comidas y refrigerios de actividades",
}

_COMPILED = {cat: [re.compile(p) for p in pats] for cat, pats in KEYWORDS.items()}


def normalize(text: str | None) -> str:
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class Suggestion:
    partida: str | None
    confidence: float
    reason: str
    method: str  # "reglas" | "llm" | "sin_regla"
    category: str | None = None


def _base_code(code: str | None) -> str:
    return (code or "").strip().upper()


def classify_expense(
    donor: str,
    concept: str | None,
    issuer: str | None = None,
    source: str | None = None,
    currency: str | None = None,
    declared: str | None = None,
) -> Suggestion:
    """Keyword layer for one expense."""
    mapping = CATEGORY_TO_PARTIDA.get(donor, {})
    text = normalize(f"{concept or ''} | {issuer or ''}")
    hits: dict[str, list[str]] = {}
    for cat, patterns in _COMPILED.items():
        if cat not in mapping:
            continue
        found = [p.search(text).group(0) for p in patterns if p.search(text)]
        if found:
            hits[cat] = found
    if not hits:
        return Suggestion(
            None, 0.0, "Ninguna palabra clave reconocida en el concepto.", "sin_regla"
        )

    # Concept words weigh more than issuer words; the most specific category wins ties.
    concept_text = normalize(concept)

    def weight(cat: str) -> tuple[int, int]:
        in_concept = sum(1 for p in _COMPILED[cat] if p.search(concept_text))
        return (in_concept, len(hits[cat]))

    ranked = sorted(hits, key=weight, reverse=True)
    best = ranked[0]
    tie = len(ranked) > 1 and weight(ranked[1]) == weight(best)
    code = mapping[best]
    if best == "personal" and (
        normalize(source) == "entreculturas" or (currency or "").upper() == "EUR"
    ):
        code = HQ_STAFF.get(donor, code)
    confidence = 0.6 if tie else (0.9 if weight(best)[0] else 0.7)
    confidence = min(confidence, CONFIDENCE_CAP.get(best, 1.0))
    words = ", ".join(sorted(set(hits[best])))
    reason = f"El concepto indica {LABELS_ES[best]} («{words}»)."
    if tie:
        reason += f" También encaja en {LABELS_ES[ranked[1]]}: revisar."
    return Suggestion(code, confidence, reason, "reglas", best)


# --- optional LLM layer -------------------------------------------------------------------------

LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "partida": {"type": ["string", "null"]},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "partida", "confidence", "reason"],
            },
        }
    },
    "required": ["items"],
}


def llm_prompt(donor: str, catalog: list[dict], items: list[dict]) -> str:
    return (
        "Eres técnico de justificación económica de una ONG. Para cada gasto, indica la partida "
        f"presupuestaria del donante ({donor}) que le corresponde según este catálogo. Si no "
        "está claro, devuelve partida null y confianza baja. Responde solo con JSON.\n\n"
        f"CATÁLOGO:\n{json.dumps(catalog, ensure_ascii=False)}\n\n"
        f"GASTOS:\n{json.dumps(items, ensure_ascii=False)}"
    )


async def llm_suggestions(
    llm: LLMClient, donor: str, catalog: list[dict], items: list[dict]
) -> dict[int, Suggestion]:
    out = await llm.complete_json(llm_prompt(donor, catalog, items), LLM_SCHEMA)
    result: dict[int, Suggestion] = {}
    for it in out.get("items", []):
        if it.get("partida"):
            result[int(it["id"])] = Suggestion(
                it["partida"], float(it.get("confidence", 0.5)), it.get("reason", ""), "llm"
            )
    return result


# --- database -----------------------------------------------------------------------------------


def _catalog(donor: str) -> list[dict]:
    from app.adapters import AecidAdapter, XuntaAdapter

    adapter = AecidAdapter() if donor == Donor.AECID.value else XuntaAdapter()
    return [
        {"code": p.code, "name": p.name, "definition": p.definition[:200]}
        for p in adapter.partida_catalog()
    ]


async def suggest_partidas(
    session: Session,
    project_id: int,
    llm: LLMClient | None = None,
    *,
    allow_llm: bool = False,
    max_llm_expenses: int = 1,
) -> dict[str, int]:
    """Fill expense.suggested_partida / suggested_confidence / suggested_reason.

    The LLM layer runs only if `llm` is given AND `allow_llm` is true, on at most
    `max_llm_expenses` expenses that the keyword layer left without a suggestion.
    """
    project = session.get(Project, project_id)
    if project is None:
        raise LookupError(f"Project {project_id} not found")
    donor = project.donor.value if hasattr(project.donor, "value") else str(project.donor)
    expenses = list(session.scalars(select(Expense).where(Expense.project_id == project_id)))
    stats = {"reglas": 0, "llm": 0, "sin_regla": 0}
    pending: list[Expense] = []
    for e in expenses:
        s = classify_expense(donor, e.concept, e.issuer, e.source, e.currency, e.partida_code)
        e.suggested_partida, e.suggested_confidence = s.partida, s.confidence or None
        e.suggested_reason = s.reason if s.partida else None
        stats[s.method] += 1
        if s.partida is None:
            pending.append(e)
    if llm is not None and allow_llm and pending and max_llm_expenses > 0:
        batch = pending[:max_llm_expenses]
        items = [{"id": e.id, "concepto": e.concept, "proveedor": e.issuer} for e in batch]
        for eid, s in (await llm_suggestions(llm, donor, _catalog(donor), items)).items():
            e = next(x for x in batch if x.id == eid)
            e.suggested_partida, e.suggested_confidence = s.partida, s.confidence
            e.suggested_reason = f"(IA) {s.reason}"
            stats["llm"] += 1
            stats["sin_regla"] -= 1
    session.commit()
    return stats


def flag_doubtful_imputations(session: Session, project_id: int) -> list[FindingIn]:
    """PARTIDA_MISMATCH where a clear suggestion differs from the declared partida."""

    findings: list[FindingIn] = []
    for e in session.scalars(select(Expense).where(Expense.project_id == project_id)):
        declared, suggested = _base_code(e.partida_code), _base_code(e.suggested_partida)
        if not suggested or not declared or declared in INDIRECT_CODES or suggested == declared:
            continue
        conf = e.suggested_confidence or 0
        if conf < 0.5:
            continue
        severity = Severity.MEDIUM if conf >= MIN_CONFIDENCE_FINDING else Severity.LOW
        findings.append(
            FindingIn(
                expense_id=e.id,
                rule_code=PARTIDA_MISMATCH,
                severity=severity,
                message=(
                    f"El gasto nº {e.order_no or '?'} («{(e.concept or '').strip()[:80]}») está "
                    f"imputado en la partida {declared}, pero parece de la partida {suggested}. "
                    f"{e.suggested_reason or ''} Comprueba la imputación."
                ),
                evidence={
                    "order_no": e.order_no,
                    "partida_declarada": declared,
                    "partida_sugerida": suggested,
                    "confianza": conf,
                },
            )
        )
    sync_findings(session, project_id, {PARTIDA_MISMATCH}, findings)
    session.commit()
    return findings
