"""AECID adapter.

Report: .xlsx read with openpyxl (data_only=True: cached formula values). Sheets (NOTE the
trailing spaces in the names; lookup is tolerant to them):
- "X.B Listado de comprobantes ": expenses. The header row is the one whose column A is
  "Nº de Orden" (row 13, not row 1). Order numbers carry a prefix (CG=CEPAG, FA=Fe y Alegría,
  EC=Entreculturas), may have sub-numbers ("FA236.3", sometimes with a decimal comma:
  "CG18,1") and may repeat across rows (e.g. salary + social security of the same payslip).
- "X.A.1 Balance presup ": approved vs executed per partida.

Conventions:
- ``fx_rate`` is expressed as units of local currency per 1 EUR (PYG per EUR). The sheet gives
  the inverse (EUR per PYG); the raw value is kept in ``extra["tc_eur_per_local"]``.
- ``amount_donor`` is the AECID column; None when blank (e.g. "CEPAG CONTRAPARTIDA" rows).
- ``partida_code`` is the prefix of the partida text ("A.I.7", "A.I.6.1", "B"); the text comes
  with leading nbsp/spaces that are stripped.
- Unparseable dates (e.g. "30/9/224") give ``date=None`` and ``extra["date_raw"]``.
- Budget rows that are not partidas (subtotals, "% SOBRE TOTALES"...) are returned with
  ``is_partida=False``; for ``PCT_*`` rows the amount fields hold ratios.

Limits: indirect costs <=12 % of the approved AECID subsidy (guide, section 11.3).
"""

import datetime as dt
import json
import re
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import openpyxl

from app.adapters.base import DonorAdapter
from app.adapters.common import (
    clean_text,
    find_sheet_name,
    is_subtotal_child,
    normalize_order_key,
    to_decimal,
)
from app.enums import Donor
from app.schemas import BudgetLineIn, ExpenseIn, Limit, LimitBase, Partida

EXPENSES_SHEET = "X.B Listado de comprobantes "
BUDGET_SHEET = "X.A.1 Balance presup "
LOCAL_CURRENCY = "PYG"
FX_PRECISION = Decimal("0.00000001")  # matches expense.fx_rate Numeric(18, 8)
CATALOG_PATH = Path(__file__).with_name("aecid_partidas.json")

# Expense header texts (casefolded) -> field.
_EXPENSE_COLUMNS = {
    "nº de orden": "order",
    "fecha": "date",
    "fuente": "source",
    "emisor": "issuer",
    "concepto": "concept",
    "partida": "partida",
    "moneda local": "local",
    "tipo de cambio divisa/m.l.": "tc",
    "euros": "eur",
    "aecid": "aecid",
    "fondos propios ongd": "own",
    "otros": "others",
}

# "A.I.6.1 Personal Local", "A.I 2. Auditorias", "A.II.2.1 Construcción", "B. COSTES INDIRECTOS"
_CODE_RE = re.compile(r"^(?P<l>[AB])(?:\.(?P<rom>[IVX]+))?(?:[.\s]\s*(?P<nums>\d+(?:\.\d+)*))?")

# Budget sheet columns (0-based) and the header texts that validate them.
B_APPROVED_AECID, B_APPROVED_TOTAL, B_EXECUTED_AECID, B_EXECUTED_TOTAL = 1, 4, 17, 19


def split_partida(label: Any) -> tuple[str | None, str]:
    """ "\\xa0\\xa0A.I.6.1 Personal Local" -> ("A.I.6.1", "Personal Local")."""
    text = clean_text(label)
    if not text:
        return None, ""
    m = _CODE_RE.match(text)
    if not m or (m["l"] == "A" and not m["rom"]):
        return None, text
    code = m["l"] + (f".{m['rom']}" if m["rom"] else "") + (f".{m['nums']}" if m["nums"] else "")
    name = clean_text(re.sub(r"^\s*\.", "", text[m.end() :])) or ""
    return code, name


def _parse_date(value: Any) -> tuple[dt.date | None, str | None]:
    """Returns (date, raw_text_if_unparseable)."""
    if isinstance(value, dt.datetime):
        return value.date(), None
    if isinstance(value, dt.date):
        return value, None
    text = clean_text(value)
    if text is None:
        return None, None
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if m:
        try:
            return dt.date(int(m[3]), int(m[2]), int(m[1])), None
        except ValueError:
            pass
    return None, text


def _open(path: Path) -> openpyxl.Workbook:
    return openpyxl.load_workbook(path, data_only=True, read_only=True)


def _drop_empty(data: dict[str, Any]) -> dict[str, Any] | None:
    cleaned = {k: v for k, v in data.items() if v is not None}
    return cleaned or None


class AecidAdapter(DonorAdapter):
    donor = Donor.AECID

    # --- expenses ------------------------------------------------------------

    def parse_expenses(self, path: Path) -> list[ExpenseIn]:
        book = _open(path)
        try:
            sheet = book[find_sheet_name(book.sheetnames, EXPENSES_SHEET)]
            columns: dict[str, int] | None = None
            expenses: list[ExpenseIn] = []
            for row_no, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                if columns is None:  # still looking for the header row
                    if (clean_text(row[0]) or "").casefold() == "nº de orden":
                        columns = {
                            _EXPENSE_COLUMNS[h]: i
                            for i, cell in enumerate(row)
                            if (h := (clean_text(cell) or "").casefold()) in _EXPENSE_COLUMNS
                        }
                        missing = set(_EXPENSE_COLUMNS.values()) - set(columns)
                        if missing:
                            raise ValueError(
                                f"Header row of '{sheet.title}' lacks columns: {sorted(missing)}"
                            )
                    continue
                order_no = clean_text(row[columns["order"]])
                if order_no is None:
                    continue  # blank / footer rows
                cell = {name: row[idx] for name, idx in columns.items()}

                date, date_raw = _parse_date(cell["date"])
                partida_raw = clean_text(cell["partida"])
                code, _ = split_partida(partida_raw)
                local = to_decimal(cell["local"])
                tc = to_decimal(cell["tc"])
                fx = (
                    (Decimal(1) / tc).quantize(FX_PRECISION) if (local is not None and tc) else None
                )

                expenses.append(
                    ExpenseIn(
                        order_no=order_no,
                        order_key=normalize_order_key(order_no),
                        date=date,
                        source=clean_text(cell["source"]),
                        issuer=clean_text(cell["issuer"]),
                        concept=clean_text(cell["concept"]),
                        partida_code=code,
                        partida_raw=partida_raw,
                        amount_local=local,
                        currency=LOCAL_CURRENCY if local is not None else "EUR",
                        fx_rate=fx,
                        amount_eur=to_decimal(cell["eur"]),
                        amount_donor=to_decimal(cell["aecid"]),
                        source_row=row_no,
                        extra=_drop_empty(
                            {
                                "date_raw": date_raw,
                                "own_funds": to_decimal(cell["own"]),
                                "other_funders": to_decimal(cell["others"]),
                                "tc_eur_per_local": tc if local is not None else None,
                            }
                        ),
                    )
                )
            if columns is None:
                raise ValueError(f"Header row ('Nº de Orden') not found in '{sheet.title}'")
            return expenses
        finally:
            book.close()

    # --- budget --------------------------------------------------------------

    def parse_budget(self, path: Path) -> list[BudgetLineIn]:
        book = _open(path)
        try:
            sheet = book[find_sheet_name(book.sheetnames, BUDGET_SHEET)]
            rows = list(sheet.iter_rows(max_col=24, values_only=True))
        finally:
            book.close()

        header = next(
            (i for i, r in enumerate(rows) if (clean_text(r[0]) or "").upper() == "PARTIDAS"), None
        )
        if header is None:
            raise ValueError(f"Header row ('PARTIDAS') not found in '{BUDGET_SHEET.strip()}'")
        titles = rows[header]
        if "APROBADO" not in (clean_text(titles[B_APPROVED_AECID]) or "").upper() or (
            "ACUMULADO" not in (clean_text(titles[B_EXECUTED_AECID]) or "").upper()
        ):
            raise ValueError("Unexpected column layout in 'X.A.1 Balance presup'")

        lines: list[BudgetLineIn] = []
        for row in rows[header + 2 :]:  # +2: skip the sub-header (AECID / TOTAL...)
            label = clean_text(row[0])
            if not label:
                continue
            values = dict(
                approved_donor=to_decimal(row[B_APPROVED_AECID]),
                approved_total=to_decimal(row[B_APPROVED_TOTAL]),
                executed_donor=to_decimal(row[B_EXECUTED_AECID]),
                executed_total=to_decimal(row[B_EXECUTED_TOTAL]),
            )
            if all(v is None for v in values.values()):
                continue  # section header ("A.I. COSTES DIRECTOS CORRIENTES") or note

            line = self._classify_budget_row(label, values)
            if line is not None:
                lines.append(line)
            if label.upper().startswith("TOTAL GENERAL"):
                break

        # Parent rows (A.I.6 TOTAL PERSONAL, A.II.2 TOTAL CONSTRUCCIÓN) are subtotals.
        codes = {ln.partida_code for ln in lines}
        for ln in lines:
            if ln.is_partida and is_subtotal_child(ln.partida_code, codes):
                ln.is_partida = False
        return lines

    @staticmethod
    def _classify_budget_row(label: str, values: dict[str, Decimal | None]) -> BudgetLineIn | None:
        upper = label.upper()

        def non_partida(code: str) -> BudgetLineIn:
            return BudgetLineIn(partida_code=code, partida_name=label, is_partida=False, **values)

        if upper.startswith("% SOBRE TOTAL AECID"):
            return non_partida("PCT_INDIRECTOS")
        if upper.startswith("% SOBRE"):
            return non_partida("PCT_DIRECTOS")
        if upper.startswith("TOTAL GENERAL"):
            return non_partida("TOTAL_GENERAL")
        if upper.startswith("TOTAL COSTES DIRECTOS"):
            return non_partida("TOTAL_DIRECTOS")
        if upper.startswith("INTERESES"):
            return non_partida("INTERESES")
        if upper.startswith("ONGD ESPAÑOLA"):
            return non_partida("B_ONGDE")
        if upper.startswith("ONGD LOCAL"):
            return non_partida("B_ONGDL")
        if upper.startswith("TOTAL B"):
            # The single indirect-cost partida: expenses are imputed to "B".
            return BudgetLineIn(partida_code="B", partida_name="Costes indirectos", **values)
        if upper.startswith("TOTAL A."):
            m = re.match(r"TOTAL\s+(A\.[IVX]+)", upper)
            return non_partida(f"TOTAL_{m[1]}" if m else "TOTAL")
        code, name = split_partida(label)
        if code is None:
            return None
        return BudgetLineIn(partida_code=code, partida_name=name or None, **values)

    # --- catalog and limits -------------------------------------------------

    def partida_catalog(self) -> list[Partida]:
        return load_catalog()

    def limits(self) -> list[Limit]:
        return [
            Limit(
                code="AECID_INDIRECTOS_MAX_12",
                description="Los costes indirectos no pueden superar el 12 % de la subvención "
                "aprobada de la AECID.",
                partida_codes=["B"],
                base=LimitBase.DONOR_GRANT,
                max_pct=Decimal("0.12"),
                reference="Guía AECID (versión nov. 2019), apartado 11.3 (OB11-13.3)",
            )
        ]


@lru_cache
def load_catalog(path: Path = CATALOG_PATH) -> list[Partida]:
    """Partida cards extracted from the AECID guide (built by scripts/build_aecid_catalog.py)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return [Partida.model_validate(item) for item in data]
