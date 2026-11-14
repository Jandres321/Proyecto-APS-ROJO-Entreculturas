"""Xunta de Galicia adapter.

Report: legacy .xls (read with xlrd). Sheets:
- "Relación de facturas": one row per expense. The header row is the one whose first cell is
  "Partidas" (row 4, not row 1); the partida is repeated on every expense row (column A),
  followed by section/total rows that carry no order number.
- "Comparativa gastos": approved vs executed budget per partida.
- "Tipo de cambio": FX received (the average rate also sits in the "Relación" sheet, row 3).

Conventions:
- ``fx_rate`` is expressed as units of local currency per 1 EUR (UGX per EUR).
- ``amount_donor`` is the amount imputed to Xunta (column i); None when the cell is blank
  (expense charged to another funder, e.g. JRS or Entreculturas).
- ``extra`` keeps what has no dedicated field: invoice_no, pct_imputed, amount_project_eur,
  payment (date + modality) and other_funders.

Limits (see CLAUDE.md; source: Orden de convocatoria, art. 8 and 19): personal <=70 %, indirect
<=5 %, field operation <=5 %, audit <=2.500 EUR, evaluation <=5.000 EUR, three offers above
40.000 EUR (works) / 15.000 EUR (supplies, services), cash payments <1.000 EUR.
"""

import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import xlrd

from app.adapters.base import DonorAdapter
from app.adapters.common import (
    clean_text,
    find_sheet_name,
    is_subtotal_child,
    normalize_order_key,
    to_decimal,
)
from app.enums import Donor, Severity
from app.schemas import (
    BudgetLineIn,
    ExpenseIn,
    Limit,
    LimitBase,
    LimitScope,
    Partida,
)

EXPENSES_SHEET = "Relación de facturas"
BUDGET_SHEET = "Comparativa gastos"
LOCAL_CURRENCY = "UGX"

# "A.III.2. Consumibles", "AV2. Local", "A.V. 3. En sede", "A. III. Equipos...", "A.V.Persoal"
_CODE_RE = re.compile(r"^\s*A\s*\.?\s*(?P<rom>[IVX]+)\.?\s*(?P<num>\d+)?\s*\.?", re.IGNORECASE)
_INDIRECT_RE = re.compile(r"^\s*B\b")

# Column indexes in "Relación de facturas" (header texts are bilingual and noisy, so the
# layout is validated by `_check_expense_header` and then read by position).
C_PARTIDA, C_ORDER, C_DATE, C_INVOICE, C_ISSUER, C_CONCEPT = 0, 1, 2, 3, 4, 5
C_LOCAL, C_EUR, C_PCT, C_PROJECT, C_PAYMENT, C_XUNTA = 6, 7, 8, 9, 10, 11
FIRST_OTHER_FUNDER_COL = 12

# Column indexes in "Comparativa gastos".
B_APPROVED_DONOR, B_APPROVED_TOTAL, B_EXECUTED_DONOR, B_EXECUTED_TOTAL = 1, 5, 6, 10


def _spanish(label: str) -> str:
    """Labels are "galego//castellano": keep the Spanish half."""
    return clean_text(label.split("//")[-1]) or ""


def _strip_code(text: str) -> str:
    m = _CODE_RE.match(text) or _INDIRECT_RE.match(text)
    rest = text[m.end() :] if m else text
    return clean_text(re.sub(r"^\s*\.", "", rest)) or ""


def split_partida(label: str | None) -> tuple[str | None, str]:
    """ "A.III.2. Consumibles" -> ("A.III.2", "Consumibles"); not a partida row -> (None, name).

    Handles the template's inconsistencies (spaces, "AV2.") and the swapped numbering of
    A.V.1/A.V.2 in the "Comparativa" sheet by deciding the subpartida from its name:
    expatriado -> A.V.1, local -> A.V.2, en sede -> A.V.3.
    """
    text = clean_text(label)
    if not text:
        return None, ""
    # Labels are "galego//castellano" and only the first half carries the code.
    name = _strip_code(_spanish(text))
    m = _CODE_RE.match(text)
    if m:
        code = "A." + m["rom"].upper() + (f".{m['num']}" if m["num"] else "")
        if m["num"] and code.startswith("A.V."):
            lowered = name.lower()
            for needle, fixed in (("expatriad", "A.V.1"), ("local", "A.V.2"), ("sede", "A.V.3")):
                if needle in lowered:
                    code = fixed
                    break
        return code, name
    if _INDIRECT_RE.match(text):
        return "B", name
    return None, name


def _excel_date(sheet: xlrd.sheet.Sheet, row: int, col: int, datemode: int):
    if sheet.cell_type(row, col) != xlrd.XL_CELL_DATE:
        return None
    return xlrd.xldate_as_datetime(sheet.cell_value(row, col), datemode).date()


def _num(sheet: xlrd.sheet.Sheet, row: int, col: int) -> Decimal | None:
    """Numeric cell; blanks, text and Excel errors (#DIV/0!) -> None."""
    if col >= sheet.ncols or sheet.cell_type(row, col) != xlrd.XL_CELL_NUMBER:
        return None
    return to_decimal(sheet.cell_value(row, col))


def _text(sheet: xlrd.sheet.Sheet, row: int, col: int) -> str | None:
    if col >= sheet.ncols or sheet.cell_type(row, col) in (xlrd.XL_CELL_ERROR, xlrd.XL_CELL_EMPTY):
        return None
    return clean_text(sheet.cell_value(row, col))


def _find_header_row(sheet: xlrd.sheet.Sheet) -> int:
    for r in range(min(sheet.nrows, 40)):
        if (clean_text(sheet.cell_value(r, 0)) or "").lower() == "partidas" and "orde" in (
            clean_text(sheet.cell_value(r, 1)) or ""
        ).lower():
            return r
    raise ValueError(f"Header row ('Partidas' / 'Nº de orden') not found in '{sheet.name}'")


def _global_fx_rate(sheet: xlrd.sheet.Sheet, header_row: int) -> Decimal | None:
    """Rate cell sits above the header, next to a cell reading 'Tipo de cambio'."""
    for r in range(header_row):
        for c in range(sheet.ncols - 1):
            if (clean_text(sheet.cell_value(r, c)) or "").lower() == "tipo de cambio":
                return _num(sheet, r, c + 1)
    return None


def _funder_name(header: str | None, col: int) -> str:
    m = re.search(r"entidades\s*-\s*([^\n(]+)", header or "", re.IGNORECASE)
    return m[1].strip() if m else f"col{col + 1}"


class XuntaAdapter(DonorAdapter):
    donor = Donor.XUNTA

    # --- expenses ------------------------------------------------------------

    def parse_expenses(self, path: Path) -> list[ExpenseIn]:
        book = xlrd.open_workbook(path)
        sheet = book.sheet_by_name(find_sheet_name(book.sheet_names(), EXPENSES_SHEET))
        header_row = _find_header_row(sheet)
        fx = _global_fx_rate(sheet, header_row)
        funders = {
            c: _funder_name(_text(sheet, header_row, c), c)
            for c in range(FIRST_OTHER_FUNDER_COL, sheet.ncols)
            if _text(sheet, header_row, c)
        }

        expenses: list[ExpenseIn] = []
        for r in range(header_row + 1, sheet.nrows):
            label = _text(sheet, r, C_PARTIDA) or ""
            if label.upper().startswith(("TOTAL XERAL", "TOTAL GENERAL")):
                break
            order_no = _text(sheet, r, C_ORDER)
            date = _excel_date(sheet, r, C_DATE, book.datemode)
            amount_eur = _num(sheet, r, C_EUR)
            # An expense row has an order number ("N/A" counts: it is a filled cell) or, failing
            # that, a date and an amount. Section headers and subtotals have neither.
            if order_no is None and not (date and amount_eur is not None):
                continue

            code, _ = split_partida(label)
            amount_local = _num(sheet, r, C_LOCAL)
            extra: dict[str, Any] = {
                "invoice_no": _text(sheet, r, C_INVOICE),
                "pct_imputed": _num(sheet, r, C_PCT),
                "amount_project_eur": _num(sheet, r, C_PROJECT),
                "payment": _text(sheet, r, C_PAYMENT),
            }
            other = {
                name: _num(sheet, r, c)
                for c, name in funders.items()
                if _num(sheet, r, c) not in (None, Decimal(0))
            }
            if other:
                extra["other_funders"] = other

            expenses.append(
                ExpenseIn(
                    order_no=order_no,
                    order_key=normalize_order_key(order_no),
                    date=date,
                    source=None,
                    issuer=_text(sheet, r, C_ISSUER),
                    concept=_text(sheet, r, C_CONCEPT),
                    partida_code=code,
                    partida_raw=label or None,
                    amount_local=amount_local,
                    currency=LOCAL_CURRENCY if amount_local is not None else "EUR",
                    fx_rate=fx if amount_local is not None else None,
                    amount_eur=amount_eur,
                    amount_donor=_num(sheet, r, C_XUNTA),
                    source_row=r + 1,
                    extra={k: v for k, v in extra.items() if v is not None} or None,
                )
            )
        return expenses

    # --- budget --------------------------------------------------------------

    def parse_budget(self, path: Path) -> list[BudgetLineIn]:
        book = xlrd.open_workbook(path)
        sheet = book.sheet_by_name(find_sheet_name(book.sheet_names(), BUDGET_SHEET))

        header = next(
            (
                r
                for r in range(min(sheet.nrows, 20))
                if (_text(sheet, r, 0) or "").upper() == "PARTIDAS"
            ),
            None,
        )
        if header is None:
            raise ValueError(f"Header row ('PARTIDAS') not found in '{sheet.name}'")
        if "PREVISTO" not in (_text(sheet, header, B_APPROVED_DONOR) or "").upper() or (
            "EXECUTADO" not in (_text(sheet, header, 6) or "").upper()
        ):
            raise ValueError("Unexpected layout in 'Comparativa gastos'")

        lines: list[BudgetLineIn] = []
        for r in range(header + 2, sheet.nrows):  # +2: skip the sub-header (XUNTA / TOTAL...)
            label = _text(sheet, r, 0)
            if not label:
                continue
            values = dict(
                approved_donor=_num(sheet, r, B_APPROVED_DONOR),
                approved_total=_num(sheet, r, B_APPROVED_TOTAL),
                executed_donor=_num(sheet, r, B_EXECUTED_DONOR),
                executed_total=_num(sheet, r, B_EXECUTED_TOTAL),
            )
            if all(v is None for v in values.values()):
                continue  # section header ("A. CUSTOS DIRECTOS...") or footnote

            code, name = split_partida(label)
            if code is not None:
                if code == "B":  # the sheet labels the partida as "B. TOTAL COSTES INDIRECTOS"
                    name = "Costes indirectos"
                lines.append(BudgetLineIn(partida_code=code, partida_name=name or None, **values))
                continue
            upper = label.upper()
            if upper.startswith("%"):
                continue  # derived ratios; recomputed in code when needed
            if re.search(r"(?<![A-Z])DIRECTOS", upper):
                total_code = "TOTAL_DIRECTOS"
            elif upper.startswith("TOTAL"):
                total_code = "TOTAL_GENERAL"
            else:
                continue
            lines.append(
                BudgetLineIn(
                    partida_code=total_code,
                    partida_name=name or None,
                    is_partida=False,
                    **values,
                )
            )

        # Parent rows (A.III over A.III.1/A.III.2, A.V over A.V.x) are subtotals, not partidas.
        codes = {ln.partida_code for ln in lines}
        for ln in lines:
            if ln.is_partida and is_subtotal_child(ln.partida_code, codes):
                ln.is_partida = False
        return lines

    # --- catalog and limits -------------------------------------------------

    def partida_catalog(self) -> list[Partida]:
        return list(XUNTA_PARTIDAS)

    def limits(self) -> list[Limit]:
        pt = LimitBase.PROJECT_TOTAL
        ref = "Orden de convocatoria de acción humanitaria 2024 (DOG 91, 10/05/2024)"
        return [
            Limit(
                code="XUNTA_PERSONAL_MAX_70",
                description="El personal no puede superar el 70 % del presupuesto total del "
                "proyecto (incluido el personal imputado en costes indirectos).",
                partida_codes=["A.V.1", "A.V.2", "A.V.3"],
                base=pt,
                max_pct=Decimal("0.70"),
                reference=f"{ref}, art. 8.2.e",
            ),
            Limit(
                code="XUNTA_INDIRECTOS_MAX_5",
                description="Los costes indirectos no pueden superar el 5 % del presupuesto total.",
                partida_codes=["B"],
                base=pt,
                max_pct=Decimal("0.05"),
                reference=f"{ref}, art. 8.3",
            ),
            Limit(
                code="XUNTA_FUNCIONAMIENTO_MAX_5",
                description="Funcionamiento en terreno: máximo 5 % del presupuesto del proyecto.",
                partida_codes=["A.VIII"],
                base=pt,
                max_pct=Decimal("0.05"),
                reference=f"{ref}, art. 8.2.h",
            ),
            Limit(
                code="XUNTA_AUDITORIA_MAX_2500",
                description="La auditoría contable no puede superar los 2.500 euros.",
                partida_codes=["A.X"],
                max_amount_eur=Decimal("2500"),
                reference=f"{ref}, art. 8.2.j",
            ),
            Limit(
                code="XUNTA_EVALUACION_MAX_5000",
                description="La evaluación externa no puede superar los 5.000 euros.",
                partida_codes=["A.IX"],
                max_amount_eur=Decimal("5000"),
                reference=f"{ref}, art. 8.2.i",
            ),
            Limit(
                code="XUNTA_3_OFERTAS_OBRAS",
                description="Gasto de obra superior a 40.000 euros: deben constar tres ofertas "
                "de proveedores distintos.",
                partida_codes=["A.II"],
                scope=LimitScope.EXPENSE,
                max_amount_eur=Decimal("40000"),
                reference=f"{ref}, art. 8.5 y 19.4.6",
            ),
            Limit(
                code="XUNTA_3_OFERTAS_SUMINISTROS_SERVICIOS",
                description="Suministro de bienes de equipo o servicios de consultoría/asistencia "
                "técnica superiores a 15.000 euros: deben constar tres ofertas.",
                partida_codes=["A.III.1", "A.III.2", "A.VI"],
                scope=LimitScope.EXPENSE,
                max_amount_eur=Decimal("15000"),
                reference=f"{ref}, art. 8.5 y 19.4.6",
            ),
            Limit(
                code="XUNTA_EFECTIVO_MAX_1000",
                description="Pago en efectivo solo excepcionalmente y por importe inferior a "
                "1.000 euros, con justificante del proveedor.",
                scope=LimitScope.EXPENSE,
                max_amount_eur=Decimal("1000"),
                severity=Severity.MEDIUM,
                reference=f"{ref}, art. 19 (pago en efectivo)",
            ),
        ]


_REF = "Orden de convocatoria AH 2024 (DOG 91, 10/05/2024), art. 8.2"

XUNTA_PARTIDAS: list[Partida] = [
    Partida(
        code="A.I",
        name="Alquiler de inmuebles",
        definition="Alquiler de salas, estancias o bodegas para capacitaciones y otras "
        "actividades imprescindibles para el proyecto.",
        includes=["salas o estancias para capacitaciones", "bodegas para almacenaje"],
        excludes=[
            "vivienda del personal expatriado",
            "locales o sedes de la entidad o del socio local (van a Funcionamiento en terreno)",
        ],
        reference=f"{_REF}.a",
    ),
    Partida(
        code="A.II",
        name="Construcción y/o reforma de inmuebles e infraestructuras",
        definition="Proyecto, planos y estudios técnicos, mano de obra, dirección y licencias de "
        "obra, materiales de construcción y su transporte, obras de acceso, agua, electricidad, "
        "saneamiento, pozos, regadíos y obras de contención.",
        includes=["materiales de construcción", "mano de obra", "pozos y sistemas de regadío"],
        excludes=["reformas en vivienda del personal expatriado o en sedes de la entidad/socio"],
        reference=f"{_REF}.b",
    ),
    Partida(
        code="A.III.1",
        name="Equipos y materiales inventariables",
        definition="Elementos de inmovilizado distintos de terrenos y edificios afectos a la "
        "actividad: maquinaria, mobiliario, equipamiento médico, educativo, etc., con envío, "
        "puesta en funcionamiento y tasas aduaneras.",
        includes=["maquinaria", "mobiliario", "equipamiento médico o educativo"],
        reference=f"{_REF}.c",
    ),
    Partida(
        code="A.III.2",
        name="Materiales consumibles",
        definition="Materiales que se consumen en menos de un año: primera necesidad, higiene, "
        "cobijo, menaje, material médico-sanitario, de oficina, informático y de formación, "
        "reprografía, reparaciones, transporte y almacenaje seguros. Incluye alquiler de "
        "maquinaria y herramientas y las transferencias de efectivo y cupones (cash/vouchers).",
        includes=["kits de primera necesidad", "cash transfer y vouchers", "material de formación"],
        reference=f"{_REF}.c",
    ),
    Partida(
        code="A.IV",
        name="Adquisición de medios de transporte",
        definition="Vehículos de dos ruedas y vehículos especializados vinculados a la actividad "
        "(ambulancias, cisternas de agua...).",
        reference=f"{_REF}.d",
    ),
    Partida(
        code="A.V.1",
        name="Personal expatriado",
        definition="Personal de la entidad española sometido a legislación española que presta "
        "servicios en el país de ejecución, con funciones ligadas a la intervención.",
        reference=f"{_REF}.e",
    ),
    Partida(
        code="A.V.2",
        name="Personal local",
        definition="Personal sometido a la legislación laboral del país de ejecución, con "
        "funciones directamente relacionadas con la intervención; incluye 'dinero por trabajo' "
        "(cash for work). Salarios, finiquitos proporcionales y seguros sociales a cargo de la "
        "entidad.",
        includes=["salarios", "seguridad social / pensiones", "cash for work"],
        reference=f"{_REF}.e",
    ),
    Partida(
        code="A.V.3",
        name="Personal en sede",
        definition="Personal de la entidad en Galicia, sometido a legislación española, cuyas "
        "funciones se imputan a la puesta en marcha y seguimiento de la intervención.",
        reference=f"{_REF}.e",
    ),
    Partida(
        code="A.VI",
        name="Servicios técnicos y profesionales",
        definition="Análisis de necesidades y vulnerabilidades, diagnósticos, capacitaciones, "
        "seminarios, informes y sistematizaciones contemplados en la formulación.",
        reference=f"{_REF}.f",
    ),
    Partida(
        code="A.VII",
        name="Viajes, estancias y dietas",
        definition="Movilidad, hospedaje y manutención del personal y de las personas "
        "beneficiarias: combustible, seguros, alquiler, mantenimiento y reparación de "
        "vehículos y seguridad en el traslado de personas.",
        includes=["billetes", "alojamiento", "dietas", "combustible", "alquiler de vehículos"],
        reference=f"{_REF}.g",
    ),
    Partida(
        code="A.VIII",
        name="Funcionamiento en terreno",
        definition="Gastos corrientes en el país de ejecución ligados al proyecto: alquiler de "
        "oficinas, electricidad, agua, comunicaciones, papelería, limpieza, mantenimiento y "
        "seguridad (incluido el personal vinculado). Máximo 5 % del presupuesto.",
        includes=["alquiler de oficina", "electricidad y agua", "comunicaciones", "limpieza"],
        reference=f"{_REF}.h",
    ),
    Partida(
        code="A.IX",
        name="Evaluación externa",
        definition="Evaluación externa no obligatoria por persona o entidad de reconocida "
        "experiencia; puede realizarse hasta 3 meses tras el fin de la ejecución. Máximo "
        "5.000 euros.",
        reference=f"{_REF}.i",
    ),
    Partida(
        code="A.X",
        name="Auditoría contable",
        definition="Auditoría de la cuenta justificativa; obligatoria si la subvención supera "
        "50.000 euros. Máximo 2.500 euros; puede realizarse hasta 3 meses tras el fin de la "
        "ejecución.",
        reference=f"{_REF}.j",
    ),
    Partida(
        code="A.XI",
        name="Gastos financieros",
        definition="Gastos bancarios de la cuenta del proyecto y de transferencias al país de "
        "ejecución, compulsa de documentos, traducciones exigidas, asesoría jurídica o "
        "financiera, gastos registrales y periciales indispensables para el proyecto.",
        includes=["comisiones bancarias", "gastos de transferencia", "traducciones", "notaría"],
        reference=f"{_REF}.k",
    ),
    Partida(
        code="B",
        name="Costes indirectos",
        definition="Gastos propios del funcionamiento regular de la entidad solicitante y de la "
        "contraparte que resultan del planteamiento y ejecución del proyecto, y de su difusión "
        "y seguimiento en Galicia. Máximo 5 % del presupuesto; se acreditan con certificado "
        "desglosado firmado por el representante legal.",
        includes=["gestoría", "servidores y dominios", "material de oficina de la sede"],
        reference="Orden de convocatoria AH 2024 (DOG 91, 10/05/2024), art. 8.3",
    ),
]
