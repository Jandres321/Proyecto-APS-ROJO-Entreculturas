"""Build app/adapters/aecid_partidas.json from the AECID guide (PDF with text layer).

The guide describes each direct-cost type in section 11.4 ("EQUIPOS, MATERIALES Y SUMINISTROS
(R11-VII.4.2.5)" ...: Descripción / Condiciones / Documentación) and indirect costs in 11.3. The
budget of the reports uses finer partidas (A.I.6.1 ...), so one card can feed several partidas.

Usage (from backend/):
    python scripts/build_aecid_catalog.py [path/to/guide.pdf]

Without argument the guide is looked up under DATA_DIR/DocJustificativa/. The output is committed
(it is a derived, public-normative artifact) so the PDF is not re-parsed on every run.
"""

import json
import os
import re
import sys
from pathlib import Path

import pymupdf

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.config import get_settings  # noqa: E402

OUTPUT = BACKEND / "app" / "adapters" / "aecid_partidas.json"

# Guide card (reference tag in the heading) -> partidas of the AECID report budget.
CARD_PARTIDAS: dict[str, list[tuple[str, str]]] = {
    "R11-VII.4.2.1": [("A.I.1", "Evaluación externa")],
    "R11-VII.4.2.2": [("A.I.2", "Auditorías")],
    "R11-VII.4.2.7": [("A.I.3", "Otros servicios técnicos y profesionales")],
    "R11-VII.4.2.3": [
        ("A.I.4", "Arrendamientos (de terrenos, inmuebles)"),
        ("A.II.1", "Adquisición de terrenos y/o inmuebles"),
    ],
    "R11-VII.4.2.5": [
        ("A.I.5", "Materiales y suministros no inventariables"),
        ("A.II.3", "Equipos y materiales inventariables"),
    ],
    "R11-VII.4.2.6": [
        ("A.I.6.1", "Personal local"),
        ("A.I.6.2", "Personal expatriado"),
        ("A.I.6.3", "Personal en sede"),
        ("A.I.6.4", "Personal voluntario"),
    ],
    "R11-VII.4.2.9": [("A.I.7", "Viajes, alojamientos y dietas")],
    "R11-VII.4.2.8": [("A.I.8", "Fondos rotatorios")],
    "R11-VII.4.2.10": [("A.I.9", "Gastos financieros")],
    "R11-VII.4.2.4": [
        ("A.II.2.1", "Construcción y/o reforma de inmuebles e infraestructuras"),
        ("A.II.2.2", "Trabajos y estudios técnicos de construcción"),
    ],
}
# Cards whose automatic summary reads poorly (one guide card feeds several budget partidas, or
# the card mixes bullets and prose). Hand-written from the guide text; `excludes` still come
# from the parsed card unless given here.
OVERRIDES: dict[str, dict[str, object]] = {
    "A.I.4": {
        "definition": (
            "Alquiler de terrenos e inmuebles directamente relacionados con la ejecución: locales "
            "para capacitaciones u otras actividades de la intervención. La subvención no asume "
            "garantías ni indemnizaciones de arrendamiento."
        ),
        "includes": [
            "alquiler de locales para capacitaciones u otras actividades de la intervención",
            "valorización de alquileres aportados (solo si lo permite la convocatoria)",
        ],
    },
    "A.II.1": {
        "definition": (
            "Adquisición de terrenos e inmuebles directamente relacionados con la ejecución y su "
            "inscripción legal en los registros locales (impuestos, tasas, gastos notariales, "
            "licencias legales). Exige cumplir las normas de transferencia y vinculación."
        ),
        "includes": [
            "compra de terrenos e inmuebles",
            "impuestos, tasas y gastos notariales de la inscripción",
        ],
    },
    "A.II.2.1": {
        "definition": (
            "Trabajos de construcción y reforma previstos en la intervención: mano de obra, "
            "materiales y su transporte, desescombrado, informes técnicos obligatorios, "
            "licencias, tasas, seguros obligatorios, proyectos y dirección de obra; obras de "
            "primer establecimiento, de reforma y de gran reparación."
        ),
        "includes": [
            "mano de obra y materiales de construcción",
            "obras de primer establecimiento y de reforma",
            "entregas en metálico a destinatarios para autoconstrucción",
        ],
    },
    "A.II.2.2": {
        "definition": (
            "Trabajos y estudios técnicos ligados a la construcción: proyectos y dirección de "
            "obra, informes técnicos obligatorios, licencias y tasas."
        ),
        "includes": ["proyectos y dirección de obra", "informes técnicos obligatorios"],
    },
    "A.I.5": {
        "definition": (
            "Adquisición y arrendamiento de materiales, suministros y equipos no inventariables "
            "(material de oficina e informático, insumos...), su reparación y mantenimiento, "
            "envío y tasas aduaneras. Incluye entregas en metálico o cupones a destinatarios "
            "cuyo componente principal sea la compra de materiales o insumos."
        ),
    },
    "A.II.3": {
        "definition": (
            "Adquisición de equipos y materiales inventariables (maquinaria, mobiliario, enseres, "
            "elementos de transporte, equipos informáticos), con envío, instalación y puesta en "
            "funcionamiento. Requiere actas de transferencia y afectación de los bienes y, por "
            "encima de 15.000 euros, ofertas."
        ),
    },
    "A.I.7": {
        "definition": (
            "Desplazamientos, viajes, estancias y dietas directamente relacionados con las "
            "actuaciones, realizados por personal vinculado a la intervención, personal en sede "
            "(seguimiento), voluntarios, destinatarios finales o servicios técnicos esporádicos. "
            "Incluye combustible, seguros y mantenimiento de vehículos de la entidad o del socio."
        ),
    },
    "A.I.8": {
        "definition": (
            "Aporte económico para los préstamos del fondo, gastos de gestión y administración "
            "del fondo y gastos financieros ligados a él. Debe estar previsto en la formulación y "
            "no puede superar el 10 % de los gastos directos."
        ),
    },
    "A.I.9": {
        "definition": (
            "Gastos de transferencias bancarias entre cuentas a nombre de la intervención y de "
            "mantenimiento de esas cuentas. Deben ser indispensables y no derivar de malas "
            "prácticas o incumplimientos legales."
        ),
    },
}
# Personal sub-partida -> start of the guide's bullet describing it.
PERSONAL_BULLET = {
    "A.I.6.1": "personal local",
    "A.I.6.2": "personal expatriado",
    "A.I.6.3": "personal en sede",
    "A.I.6.4": "personal voluntario",
}

HEADING_RE = re.compile(
    r"^(?P<title>[A-ZÁÉÍÓÚÜÑ][A-ZÁÉÍÓÚÜÑ ,/()\-]+?)\s*\((?P<ref>R11-VII\.4\.2\.\d+)\)\s*$"
)
NOISE_RE = re.compile(
    r"^(NORMAS DE GESTIÓN, SEGUIMIENTO|GUÍA DE APLICACIÓN \(Resolución|Guía aplicación Normas)"
)
BULLET_RE = re.compile(r"^([✓▪•o\-]|[a-z]\))\s*(.*)$")
EXCLUSION_RE = re.compile(
    r"^(No incluye|No se incluy|No se imput|No son (financiables|subvencionables)|"
    r"NO SON FINANCIABLES|No es financiable|No serán)",
    re.IGNORECASE,
)
MAX_DEFINITION = 700
MAX_ITEM = 240


def find_guide() -> Path:
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    data_dir = get_settings().data_dir or os.environ.get("DATA_DIR")
    if not data_dir:
        sys.exit("Pass the guide PDF path or set DATA_DIR")
    hits = sorted((Path(data_dir) / "DocJustificativa").glob("Gu*AECID*.pdf"))
    if not hits:
        sys.exit(f"AECID guide not found under {data_dir}/DocJustificativa")
    return hits[0]


def page_lines(doc: pymupdf.Document) -> list[str]:
    lines: list[str] = []
    for page in doc:
        for raw in page.get_text().splitlines():
            line = re.sub(r"\s+", " ", raw).strip()
            if line and not NOISE_RE.match(line):
                lines.append(line)
    return lines


def to_items(lines: list[str]) -> list[tuple[bool, str]]:
    """Group wrapped lines into (is_bullet, text) items."""
    items: list[tuple[bool, str]] = []
    for line in lines:
        m = BULLET_RE.match(line)
        if m and m[2]:
            items.append((True, m[2]))
        elif m:  # bullet glyph alone on its line: text follows
            items.append((True, ""))
        elif items and (not items[-1][1] or not re.search(r"[.:]$", items[-1][1])):
            is_bullet, text = items[-1]
            items[-1] = (is_bullet, f"{text} {line}".strip())
        else:
            items.append((False, line))
    return items


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("; "))
    return (cut[: end + 1] if end > limit // 2 else cut.rsplit(" ", 1)[0]) + "…"


def split_card(lines: list[str]) -> tuple[list[str], list[str]]:
    """Lines of a card -> (Descripción lines, Condiciones lines)."""
    desc: list[str] = []
    cond: list[str] = []
    target: list[str] | None = None
    for line in lines:
        if line == "Descripción":
            target = desc
        elif line == "Condiciones":
            target = cond
        elif line.startswith("Documentación acreditativa requerida"):
            if target is cond or target is desc:
                target = None
        elif target is not None:
            target.append(line)
    return desc, cond


def exclusions(items: list[tuple[bool, str]]) -> list[str]:
    out: list[str] = []
    capture = False
    for is_bullet, text in items:
        if not is_bullet and re.match(r"^No se incluyen:?$", text, re.IGNORECASE):
            capture = True
        elif EXCLUSION_RE.match(text):
            capture = text.endswith(":")
            if not capture:
                # Wrapped lines of the next paragraph can be glued on when this one lacks a final
                # period: keep only the exclusion sentence itself.
                out.append(clip(re.split(r"\s(?=En caso |SÓLO EN |Para )", text)[0], MAX_ITEM))
        elif capture and is_bullet:
            out.append(clip(text, MAX_ITEM))
        elif not is_bullet:
            capture = False
    return out


def build_card(ref: str, lines: list[str]) -> dict[str, object]:
    desc_lines, cond_lines = split_card(lines)
    desc = to_items(desc_lines)
    cond = to_items(cond_lines)
    bullets = [t for b, t in desc if b and t]
    prose = [t for b, t in desc if not b and t and not t.endswith(":")]
    definition_src = prose[:2] or bullets[:2]
    return {
        "definition": clip(" ".join(definition_src), MAX_DEFINITION),
        "includes": [clip(t, MAX_ITEM) for t in bullets if not EXCLUSION_RE.match(t)][:12],
        "excludes": exclusions(desc + cond),
        "bullets_full": bullets,
    }


def indirect_card(lines: list[str]) -> dict[str, object]:
    """Section 11.3: from 'Costes indirectos: concepto...' (body) to 'Cómo se justifican'."""
    start = [i for i, ln in enumerate(lines) if ln.startswith("Costes indirectos: concepto")][-1]
    end = next(i for i in range(start, len(lines)) if lines[i].startswith("Cómo se justifican"))
    text = " ".join(ln for ln in lines[start + 1 : end] if ln not in {"➢", "❖"})
    text = re.sub(r"\s+", " ", text)
    text = text.replace("Qué son ", "", 1).replace("(OB11-13.3):", "(OB11-13.3):")
    return {"definition": clip(text, MAX_DEFINITION), "includes": [], "excludes": []}


def main() -> None:
    guide = find_guide()
    doc = pymupdf.open(guide)
    lines = page_lines(doc)

    # Sections of section 11.4 keyed by their R11 reference.
    starts = [(i, m["ref"]) for i, ln in enumerate(lines) if (m := HEADING_RE.match(ln))]
    # End of section 11.4 = chapter 12 heading, searched after the first card (the table of
    # contents at the start of the PDF has the same text).
    first = starts[0][0]
    stop = next(
        i
        for i in range(first, len(lines))
        if lines[i].startswith("12.") and "REINTEGRO" in lines[i].upper()
    )
    cards: dict[str, dict[str, object]] = {}
    for n, (i, ref) in enumerate(starts):
        j = starts[n + 1][0] if n + 1 < len(starts) else stop
        if ref in CARD_PARTIDAS and ref not in cards:
            cards[ref] = build_card(ref, lines[i + 1 : min(j, stop)])

    catalog: list[dict[str, object]] = []
    for ref, partidas in CARD_PARTIDAS.items():
        card = cards[ref]
        for code, name in partidas:
            definition = str(card["definition"])
            if code in PERSONAL_BULLET:
                needle = PERSONAL_BULLET[code]
                sub = next(
                    (b for b in card["bullets_full"] if b.lower().startswith(needle)),
                    None,  # type: ignore[attr-defined]
                )
                if sub:
                    definition = clip(sub, MAX_DEFINITION)
            entry: dict[str, object] = {
                "code": code,
                "name": name,
                "definition": definition,
                "includes": [] if code in PERSONAL_BULLET else card["includes"],
                "excludes": card["excludes"],
                "reference": f"Guía AECID (versión nov. 2019), apartado 11.4, {ref}",
            }
            entry.update(OVERRIDES.get(code, {}))
            catalog.append(entry)
    b = indirect_card(lines)
    catalog.append(
        {
            "code": "B",
            "name": "Costes indirectos",
            **b,
            "reference": "Guía AECID (versión nov. 2019), apartado 11.3 (OB11-13.3)",
        }
    )
    catalog.sort(
        key=lambda c: [int(p) if p.isdigit() else p for p in re.split(r"[.]", str(c["code"]))]
    )
    OUTPUT.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(catalog)} partidas written to {OUTPUT}")


if __name__ == "__main__":
    main()
