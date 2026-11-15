"""Tigray ground truth: the external reviewer's incidences and Entreculturas' answers.

The workbook `Resumen incidencias_ Respuestas EC.xlsx` lists, per expense, up to four incidences
raised by the donor's reviewer, each with the answer of Entreculturas (EC). Many of them are false
positives of the reviewer. This module turns it into a flat table (one row per incidence) with:

- `categoria`: our rule code(s) the incidence corresponds to (`|`-separated), `SIN_REGLA` when no
  rule of ours covers it;
- `veredicto`: `real` (EC accepts / fixes it), `falso_positivo` (EC shows it was fine) or
  `indeterminado` (the answer does not say; review by hand).

The classification is keyword based and conservative: anything unclear stays `indeterminado` and
is left out of the metrics. The output holds real data: it is git-ignored, never commit it.
"""

import csv
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

REAL = "real"
FALSE_POSITIVE = "falso_positivo"
UNDECIDED = "indeterminado"
NO_RULE = "SIN_REGLA"

# Fixed leading columns of the workbook (their headers are blank in the file).
COL_PARTIDA, COL_ORDER, COL_DATE, COL_INVOICE = 0, 1, 2, 3

# (regex on the normalised incidence text, our codes), first match wins.
CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    (r"sello|diligencia", ("DONOR_STAMP_MISSING",)),
    (r"proforma|no hay factura|nota expedida|no consta fecha ni num", ("NOT_AN_INVOICE",)),
    (r"efectivo|justificante del proveedor", ("PAYMENT_EVIDENCE_MISSING",)),
    (r"intelegible|ininteligible|ilegible|idioma", ("ILLEGIBLE",)),
    (r"no corresponde|no correponde", ("NAME_CONTENT_CONFLICT",)),
    (r"errata en fecha", ("DATE_MISMATCH",)),
    (r"fecha e importe|importe y fecha", ("DATE_MISMATCH", "AMOUNT_MISMATCH")),
    (r"descripcion factura|concepto de factura", (NO_RULE,)),
    (r"fecha", ("DATE_MISMATCH",)),
    (r"importe imputado", (NO_RULE,)),
    (r"no localizamos|importe", ("AMOUNT_MISMATCH",)),
    (r"contrato|nomina|trabajador|salario|paga extra", ("DOC_INCOMPLETE",)),
    (r"no se aporta documentacion|no se adjunta documentacion", ("DOC_MISSING",)),
]

# EC answers, in order of strength: "the reviewer was right", then "it was fine", then the weak
# "reviewer was right" markers (EC sends something / fixes something).
STRONG_REAL_ANSWERS = [
    r"^incidencia (aceptada|subsanada)",
    r"^se cambiar",
    r"^pendiente de",
    r"faltaria",
    r"\bdifiere\b",
]
FALSE_POSITIVE_ANSWERS = [
    r"^coincid",
    r"\bexisten? (el|la|los|las|un|una|formularios)\b",
    r"\blegible\b",
    r"\bsellos? completos?\b",
    r"\bno aplica\b",
    r"\baparece (la|el|en)\b",
]
WEAK_REAL_ANSWERS = [
    r"\b(subsan|correg|rectific|cambi)",
    r"\b(aport|adjunt|enviad|remit)",
]


def normalize(text: str) -> str:
    """Lower-case, no accents, collapsed spaces (the workbook mixes encodings and typos)."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", plain.lower()).strip()


def categorize(incidence: str) -> str:
    """Our rule code(s) for a reviewer incidence text, `|`-separated, or `SIN_REGLA`."""
    text = normalize(incidence)
    for pattern, codes in CATEGORY_RULES:
        if re.search(pattern, text):
            return "|".join(codes)
    return NO_RULE


def verdict(answer: str | None) -> str:
    """Was the reviewer right, according to what EC answered?"""
    if not answer or not str(answer).strip():
        return UNDECIDED
    text = normalize(str(answer))
    if any(re.search(p, text) for p in STRONG_REAL_ANSWERS):
        return REAL
    if any(re.search(p, text) for p in FALSE_POSITIVE_ANSWERS):
        return FALSE_POSITIVE
    if any(re.search(p, text) for p in WEAK_REAL_ANSWERS):
        return REAL
    return UNDECIDED


@dataclass
class TigrayRow:
    partida: str
    orden: str
    factura: str
    fecha: str
    n_incidencia: int
    incidencia: str
    comentario_ec: str
    categoria: str
    veredicto: str


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if hasattr(value, "date") and callable(value.date):
        return value.date().isoformat()
    return str(value).strip()


def order_key(value: Any) -> str:
    """Order numbers compare as text without a trailing `.0` ("12.0" -> "12")."""
    return re.sub(r"\.0+$", "", _text(value)).upper()


def read_workbook(path: Path) -> list[TigrayRow]:
    """One row per incidence of the sheet that holds both the incidences and EC's comments."""
    import openpyxl

    workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        for sheet in workbook.worksheets:
            rows = [list(r) for r in sheet.iter_rows(values_only=True)]
            parsed = _parse_sheet(rows)
            if parsed is not None:
                return parsed
    finally:
        workbook.close()
    raise ValueError(
        f"{path.name}: no sheet with 'Incidencia N' and 'Comentarios Incidencia N' columns"
    )


def _parse_sheet(rows: list[list[Any]]) -> list[TigrayRow] | None:
    for index, row in enumerate(rows):
        cells = [normalize(_text(v)) for v in row]
        pairs: dict[int, tuple[int, int]] = {}
        for col, text in enumerate(cells):
            match = re.fullmatch(r"incidencia (\d+)", text)
            if match and f"comentarios incidencia {match.group(1)}" in cells:
                number = int(match.group(1))
                pairs[number] = (col, cells.index(f"comentarios incidencia {number}"))
        if not pairs:
            continue
        out: list[TigrayRow] = []
        for data in rows[index + 1 :]:
            for number, (inc_col, com_col) in sorted(pairs.items()):
                incidence = _text(data[inc_col]) if inc_col < len(data) else ""
                if not incidence:
                    continue
                answer = _text(data[com_col]) if com_col < len(data) else ""
                out.append(
                    TigrayRow(
                        partida=_text(data[COL_PARTIDA]),
                        orden=order_key(data[COL_ORDER]),
                        factura=_text(data[COL_INVOICE]),
                        fecha=_text(data[COL_DATE]),
                        n_incidencia=number,
                        incidencia=incidence,
                        comentario_ec=answer,
                        categoria=categorize(incidence),
                        veredicto=verdict(answer),
                    )
                )
        return out
    return None


CSV_FIELDS = [f.name for f in fields(TigrayRow)]


def write_csv(rows: Iterable[TigrayRow], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
            count += 1
    return count


def read_csv(path: Path) -> list[TigrayRow]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [
            TigrayRow(**{**row, "n_incidencia": int(row["n_incidencia"])})
            for row in csv.DictReader(handle)
        ]


# --- evaluation --------------------------------------------------------------------------------


@dataclass
class CategoryResult:
    categoria: str
    reales: int = 0  # incidences EC accepted
    detectadas: int = 0  # ... for which we raised one of the category's codes on that expense
    falsos_positivos: int = 0  # incidences EC refuted (reviewer false positives)
    evitados: int = 0  # ... for which we did NOT raise the category's code
    indeterminadas: int = 0

    @property
    def recall(self) -> float | None:
        return self.detectadas / self.reales if self.reales else None

    @property
    def evitados_pct(self) -> float | None:
        return self.evitados / self.falsos_positivos if self.falsos_positivos else None


def evaluate(
    truth: Iterable[TigrayRow], findings: dict[str, set[str]]
) -> dict[str, CategoryResult]:
    """Per category: real incidences we detect and reviewer false positives we avoid.

    `findings` maps an order number (see `order_key`) to the rule codes we raised on it. A row of a
    multi-code category (`DATE_MISMATCH|AMOUNT_MISMATCH`) counts as hit when we raised any of
    them. `SIN_REGLA` rows are listed (nothing of ours could detect them) but never count as hit.
    """
    results: dict[str, CategoryResult] = defaultdict(lambda: CategoryResult(""))
    for row in truth:
        result = results[row.categoria]
        result.categoria = row.categoria
        ours = findings.get(row.orden, set())
        hit = bool(ours & set(row.categoria.split("|")))
        if row.veredicto == REAL:
            result.reales += 1
            result.detectadas += hit
        elif row.veredicto == FALSE_POSITIVE:
            result.falsos_positivos += 1
            result.evitados += not hit
        else:
            result.indeterminadas += 1
    return dict(sorted(results.items()))
