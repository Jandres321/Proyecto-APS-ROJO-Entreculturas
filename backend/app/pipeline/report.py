"""Analysis report: an Excel workbook that tells, expense by expense, how the review stands.

Every expense has one of three states (from `review_view.project_review`): «Bien» (checked and
nothing pending), «Necesita revisión» (any doubt, with its reasons) or «No aplica» (not reviewed:
co-financed, outside the sample...), and the reviewer's decision on it («Correcto» / «Incidencia
real», with a note).

Sheets (Spanish headers):

- "Resumen": how many expenses and how many euros are in each state, and how the review goes.
- "Análisis por gasto": one row per expense, starting with state, reasons and decision.
- "Requerimientos": what each local partner has to provide or correct (expenses decided as
  «Incidencia real» or with a blocking reason), grouped by partner.
- "Incidencias", "Presupuesto" and "Topes": every finding, the budget and the donor caps.

It is built from the same expense-centred view as the web app, so both always agree.
"""

import datetime as dt
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd
from openpyxl.styles import Alignment, Font
from sqlalchemy.orm import Session

from app.models import Project
from app.pipeline.budget import compute_execution
from app.pipeline.export import budget_tables, findings_table, format_sheet
from app.review_view import project_review

# Verdict codes of `project_review` (anything else is treated as "needs review": when in doubt,
# an expense is never counted as fine).
OK, REVIEW, NOT_APPLICABLE = "ok", "review", "na"
STATE_LABELS_ES = {OK: "Bien", REVIEW: "Necesita revisión", NOT_APPLICABLE: "No aplica"}

# Reviewer decision on an expense.
PENDING, CORRECT, REAL_ISSUE = "pending", "correct", "real_issue"
DECISION_LABELS_ES = {PENDING: "Pendiente", CORRECT: "Correcto", REAL_ISSUE: "Incidencia real"}

BLOCKING_MARK = "[bloqueante]"
NO_PARTNER = "Sin socio"
# Partner = the letters that prefix the order number: "CG12.3" -> CG, "FA236.3" -> FA,
# "EC-5" -> EC. They must be followed by a digit, so "N/A" or "s/n" are not partners.
_PARTNER_RE = re.compile(r"^\s*([A-Za-z]+)\s*[-.]?\s*\d")

EXPENSE_SHEET = "Análisis por gasto"
REQUIREMENTS_SHEET = "Requerimientos"
REQUIREMENT_COLUMNS = [
    "Socio",
    "Nº orden",
    "Proveedor",
    "Importe €",
    "Qué falta o hay que corregir",
]
WRAPPED = (
    "Motivos",
    "Nota del revisor",
    "Detalle de las incidencias",
    "Motivo del vínculo",
    "Documentos vinculados",
    "Archivo y página",
    "Concepto",
    "Qué falta o hay que corregir",
)
DATE_TIME_FORMAT = "dd/mm/yyyy hh:mm"
YES_NO = {True: "sí", False: "no"}


# --- small helpers -------------------------------------------------------------------------------


def _joined(values: list[Any]) -> str | None:
    """Distinct non-empty values, in order, as one cell."""
    seen: list[str] = []
    for value in values:
        if value is None or value == "":
            continue
        text = str(value)
        if text not in seen:
            seen.append(text)
    return " | ".join(seen) or None


def _flag(values: list[bool]) -> str | None:
    """'sí' when any segment has it, 'no' when segments were read and none has it, empty when
    nothing was read (not read is not the same as absent)."""
    return YES_NO[any(values)] if values else None


def _euros(expenses: list[dict[str, Any]]) -> float:
    """Sum of the expenses' amount in euros (an unknown amount counts as 0)."""
    return round(sum(e.get("amount_eur") or 0 for e in expenses), 2)


def state_of(expense: dict[str, Any]) -> str:
    """OK / REVIEW / NOT_APPLICABLE. An unknown verdict code counts as REVIEW."""
    code = (expense.get("verdict") or {}).get("code")
    return code if code in (OK, NOT_APPLICABLE) else REVIEW


def decision_of(expense: dict[str, Any]) -> dict[str, Any]:
    """The reviewer's decision on the expense; a missing one is a pending decision."""
    decision = expense.get("decision") or {}
    status = decision.get("status") or PENDING
    return {
        "status": status if status in DECISION_LABELS_ES else PENDING,
        "note": decision.get("note") or None,
        "decided_at": decision.get("decided_at"),
    }


def reasons_text(reasons: list[dict[str, Any]] | None) -> str | None:
    """Reason labels joined by "; ", blocking ones marked: "[bloqueante] Sin justificante; ..."."""
    texts = [
        f"{BLOCKING_MARK} {r['label']}" if r.get("blocking") else str(r["label"])
        for r in reasons or []
        if r.get("label")
    ]
    return "; ".join(texts) or None


def has_blocking(expense: dict[str, Any]) -> bool:
    return any(r.get("blocking") for r in expense.get("reasons") or [])


def partner_of(order_no: str | None) -> str:
    """Local partner of an expense: the letter prefix of its order number, or "Sin socio"."""
    match = _PARTNER_RE.match(order_no or "")
    return match.group(1).upper() if match else NO_PARTNER


def _decided_at(value: Any) -> dt.datetime | str | None:
    """ISO text -> naive datetime for Excel (which has no time zones). An aware value is shown in
    Spanish local time. Anything unparsable is kept as text."""
    if not value:
        return None
    try:
        moment = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if moment.tzinfo is not None:
        try:
            from zoneinfo import ZoneInfo

            moment = moment.astimezone(ZoneInfo("Europe/Madrid"))
        except Exception:  # noqa: BLE001  (no tz database on this machine: keep UTC)
            moment = moment.astimezone(dt.UTC)
        moment = moment.replace(tzinfo=None)
    return moment


def _where(expense: dict[str, Any]) -> str | None:
    """Files and pages to look at: those of the segments read, else the linked documents."""
    places = []
    for s in expense.get("segments") or []:
        name = str(s.get("archivo") or "").replace("\\", "/").rsplit("/", 1)[-1]
        start, end = s.get("pagina_inicio"), s.get("pagina_fin")
        pages = f"pág. {start}" if start == end or end is None else f"págs. {start}-{end}"
        places.append(f"{name}, {pages}" if name else pages)
    if not places:
        places = [d.get("filename") for d in expense.get("documents") or []]
    return _joined(places)


def _order_sort_key(order_no: str | None) -> tuple:
    """Natural order: CG2 before CG10, FA236.2 before FA236.10."""
    parts = re.split(r"(\d+)", order_no or "")
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p.lower()) for p in parts if p)


def report_filename(project: Any, today: dt.date | None = None) -> str:
    """Name of the downloaded workbook: `<project code>_revision_<YYYY-MM-DD>.xlsx`.

    Falls back to the project key when there is no code; characters that are not valid in a file
    name (AECID codes carry slashes: "2019/PRYC/1234") become "-"."""
    code = str(getattr(project, "code", None) or getattr(project, "key", None) or "proyecto")
    safe = re.sub(r"[^\w.-]+", "-", code).strip("-.") or "proyecto"
    return f"{safe}_revision_{(today or dt.date.today()).isoformat()}.xlsx"


# --- sheets --------------------------------------------------------------------------------------


def expense_table(expenses: list[dict[str, Any]]) -> pd.DataFrame:
    """One row per expense: state, reasons and decision first, then the listing and the reading."""
    rows = []
    for e in expenses:
        segments, findings = e.get("segments") or [], e.get("findings") or []
        decision = decision_of(e)
        detail = "\n".join(
            f"[{f['severity_label']} · {f['status_label']}] {f['label']}: {f['message']}"
            + (f" (nota: {f['reviewer_note']})" if f.get("reviewer_note") else "")
            for f in findings
        )
        amounts = [
            f"{s['importe']:,.2f} {s.get('moneda') or ''}".strip()
            for s in segments
            if s.get("importe") is not None
        ]
        documents = e.get("documents") or []
        rows.append(
            {
                "Estado": (e.get("verdict") or {}).get("label") or STATE_LABELS_ES[state_of(e)],
                "Motivos": reasons_text(e.get("reasons")),
                # Nothing is pending on an expense that is not reviewed: leave it blank.
                "Decisión del revisor": (
                    None
                    if decision["status"] == PENDING and state_of(e) != REVIEW
                    else DECISION_LABELS_ES[decision["status"]]
                ),
                "Nota del revisor": decision["note"],
                "Fecha de la decisión": _decided_at(decision["decided_at"]),
                "Fila del informe": e.get("source_row"),
                "Nº orden": e.get("order_no"),
                "Socio": partner_of(e.get("order_no")),
                "Fecha": e.get("date"),
                "Proveedor": e.get("issuer"),
                "Concepto": e.get("concept"),
                "Partida": e.get("partida_code"),
                "Nº factura (informe)": e.get("invoice_no"),
                "Importe local": e.get("amount_local"),
                "Moneda": e.get("currency"),
                "Importe €": e.get("amount_eur"),
                "Imputado al donante €": e.get("amount_donor"),
                "Estado del justificante": e.get("doc_status_label"),
                "Confianza del vínculo": e.get("confidence_label"),
                "Documentos vinculados": _joined([d.get("filename") for d in documents]),
                "Verificado por contenido": (
                    YES_NO[any(d.get("verified") for d in documents)] if documents else None
                ),
                "Motivo del vínculo": _joined([d.get("reason") for d in documents]),
                "Leído: emisor": _joined([s.get("emisor") for s in segments]),
                "Leído: nº factura": _joined([s.get("numero_factura") for s in segments]),
                "Leído: fecha": _joined([s.get("fecha") for s in segments]),
                "Leído: importe": _joined(amounts),
                "Leído: sello del donante": _flag([s.get("sello_donante") for s in segments]),
                "Leído: prueba de pago": _flag([s.get("prueba_pago") for s in segments]),
                "Leído: legible": _flag([s.get("legible") for s in segments]),
                "Archivo y página": _where(e),
                "Detalle de las incidencias": detail or None,
                "Partida sugerida": e.get("suggested_partida"),
            }
        )
    return pd.DataFrame(rows)


def summary_table(view: dict[str, Any]) -> pd.DataFrame:
    """How many expenses (and euros) are in each state, and how far the review has gone."""
    project, expenses = view["project"], view["expenses"]
    by_state = {s: [e for e in expenses if state_of(e) == s] for s in STATE_LABELS_ES}
    review = by_state[REVIEW]

    def decided(items: list[dict[str, Any]], *statuses: str) -> list[dict[str, Any]]:
        return [e for e in items if decision_of(e)["status"] in statuses]

    pending = decided(review, PENDING)
    reviewed = decided(review, CORRECT, REAL_ISSUE)

    def line(label: str, items: list[dict[str, Any]]) -> tuple[str, Any, Any]:
        return (label, len(items), _euros(items))

    rows: list[tuple[str, Any, Any]] = [
        ("Proyecto", project.get("name"), None),
        ("Código", project.get("code"), None),
        ("Donante", project.get("donor"), None),
        ("Informe generado", dt.datetime.now().strftime("%d/%m/%Y %H:%M"), None),
        ("", None, None),
        line("Gastos del informe", expenses),
        ("", None, None),
        line("Bien", by_state[OK]),
        line("   · marcados «Incidencia real» por el revisor", decided(by_state[OK], REAL_ISSUE)),
        line("Necesita revisión", review),
        line("   · pendientes de decisión", pending),
        line("   · revisados", reviewed),
        line("      · Correcto", decided(review, CORRECT)),
        line("      · Incidencia real", decided(review, REAL_ISSUE)),
        line("No aplica", by_state[NOT_APPLICABLE]),
        ("", None, None),
        line("Gastos con algún motivo bloqueante", [e for e in expenses if has_blocking(e)]),
        ("Documentos registrados", project.get("documents"), None),
    ]
    general_open = [f for f in view.get("general_findings") or [] if f.get("status") == "open"]
    rows.append(
        ("Incidencias generales abiertas (presupuesto, topes, documentos sin gasto)",
         len(general_open), None)
    )  # fmt: skip
    reasons = Counter(r["label"] for e in review for r in e.get("reasons") or [] if r.get("label"))
    if reasons:
        rows += [("", None, None), ("Motivos de «Necesita revisión» (nº de gastos)", None, None)]
        rows += [(f"   · {label}", n, None) for label, n in reasons.most_common()]
    return pd.DataFrame(rows, columns=["Indicador", "Valor", "Importe €"])


def requirements_table(expenses: list[dict[str, Any]]) -> pd.DataFrame:
    """What to ask each local partner: expenses decided as «Incidencia real», or with a blocking
    reason the reviewer has not judged «Correcto». Grouped by partner ("Sin socio" last), then in
    natural order of the order number."""
    rows = []
    for e in expenses:
        decision = decision_of(e)
        if decision["status"] == CORRECT:
            continue  # the reviewer checked it: nothing to ask, even with a blocking reason
        if decision["status"] != REAL_ISSUE and not has_blocking(e):
            continue
        what = [reasons_text(e.get("reasons"))]
        if decision["note"]:
            what.append(f"Nota del revisor: {decision['note']}")
        rows.append(
            {
                "Socio": partner_of(e.get("order_no")),
                "Nº orden": e.get("order_no"),
                "Proveedor": e.get("issuer"),
                "Importe €": e.get("amount_eur"),
                "Qué falta o hay que corregir": ". ".join(t for t in what if t) or None,
            }
        )
    rows.sort(key=lambda r: (r["Socio"] == NO_PARTNER, r["Socio"], _order_sort_key(r["Nº orden"])))
    return pd.DataFrame(rows, columns=REQUIREMENT_COLUMNS)


# --- workbook ------------------------------------------------------------------------------------


def _wrap(ws, frame: pd.DataFrame) -> None:
    for i, name in enumerate(frame.columns, start=1):
        if name in WRAPPED:
            for row in range(2, ws.max_row + 1):
                ws.cell(row, i).alignment = Alignment(wrap_text=True, vertical="top")


def export_report_xlsx(session: Session, project_id: int, out_path: Path) -> Path:
    """Write the analysis report of a project (see the module docstring). The API should name
    the download with `report_filename(project)`."""
    project = session.get(Project, project_id)
    if project is None:
        raise LookupError(f"Project {project_id} not found")
    view = project_review(session, project)
    summary = summary_table(view)
    by_expense = expense_table(view["expenses"])
    requirements = requirements_table(view["expenses"])
    partidas, limits = budget_tables(compute_execution(session, project_id))
    sheets = [
        ("Resumen", summary, ()),
        (EXPENSE_SHEET, by_expense, ()),
        (REQUIREMENTS_SHEET, requirements, ()),
        ("Incidencias", findings_table(session, project_id), ()),
        ("Presupuesto", partidas, ("Desviación %",)),
        ("Topes", limits, ("Porcentaje", "Máximo %")),
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        for name, frame, percent in sheets:
            frame.to_excel(writer, sheet_name=name, index=False)
            format_sheet(writer.sheets[name], frame, percent)

        sheet = writer.sheets["Resumen"]
        sheet.column_dimensions["A"].width = 72
        sheet.column_dimensions["B"].width = 40
        sheet.column_dimensions["C"].width = 16
        sheet.auto_filter.ref = None
        for row in sheet.iter_rows(min_row=2, max_col=3):
            if row[0].value and not str(row[0].value).startswith("   "):
                row[0].font = Font(bold=True)
            row[2].number_format = "#,##0.00"

        sheet = writer.sheets[EXPENSE_SHEET]
        _wrap(sheet, by_expense)
        if "Fecha de la decisión" in by_expense.columns:
            column = by_expense.columns.get_loc("Fecha de la decisión") + 1
            for row in range(2, sheet.max_row + 1):
                sheet.cell(row, column).number_format = DATE_TIME_FORMAT

        sheet = writer.sheets[REQUIREMENTS_SHEET]
        _wrap(sheet, requirements)
        sheet.column_dimensions["E"].width = 90
        for row in range(2, sheet.max_row + 1):
            sheet.cell(row, 4).number_format = "#,##0.00"
    return out_path
