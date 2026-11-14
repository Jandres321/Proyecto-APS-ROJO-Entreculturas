"""Step 7 — Export: the main deliverable, an Excel workbook for the reviewer."""

import json
from pathlib import Path

import pandas as pd
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import FINDING_STATUS_LABELS_ES, SEVERITY_LABELS_ES, FindingStatus, Severity
from app.models import Document, Expense, ExpenseDocument, Finding, Project
from app.pipeline.budget import TRAFFIC_LABELS_ES, BudgetAnalysis, compute_execution
from app.review import document_statuses, list_findings

COLORS = {
    SEVERITY_LABELS_ES[Severity.HIGH]: "F8CBAD",
    SEVERITY_LABELS_ES[Severity.MEDIUM]: "FFEB9C",
    SEVERITY_LABELS_ES[Severity.LOW]: "DDEBF7",
    FINDING_STATUS_LABELS_ES[FindingStatus.ACCEPTED]: "C6EFCE",
    FINDING_STATUS_LABELS_ES[FindingStatus.FALSE_POSITIVE]: "D9D9D9",
    "verde": "C6EFCE",
    "amarillo": "FFEB9C",
    "rojo": "FFC7CE",
    "SUPERADO": "FFC7CE",
}
MAX_COLUMN_WIDTH = 70


def _findings_table(session: Session, project_id: int) -> pd.DataFrame:
    rows = []
    documents = {
        d.id: d for d in session.scalars(select(Document).where(Document.project_id == project_id))
    }
    expenses = {
        e.id: e for e in session.scalars(select(Expense).where(Expense.project_id == project_id))
    }
    for f in list_findings(session, project_id):
        evidence = f.evidence or {}
        expense = expenses.get(f.expense_id) if f.expense_id else None
        document = documents.get(f.document_id or evidence.get("best_document_id"))
        rows.append(
            {
                "Regla": f.rule_code,
                "Gravedad": SEVERITY_LABELS_ES[f.severity],
                "Estado": FINDING_STATUS_LABELS_ES[f.status],
                "Gasto (nº orden)": (expense.order_no if expense else evidence.get("order_no")),
                "Partida": (expense.partida_code if expense else evidence.get("partida")),
                "Documento": document.filename if document else None,
                "Página": evidence.get("pagina_inicio"),
                "Explicación": f.message,
                "Evidencia": json.dumps(evidence, ensure_ascii=False, default=str)
                if evidence
                else None,
                "Nota del revisor": f.reviewer_note,
            }
        )
    return pd.DataFrame(
        rows,
        columns=[
            "Regla",
            "Gravedad",
            "Estado",
            "Gasto (nº orden)",
            "Partida",
            "Documento",
            "Página",
            "Explicación",
            "Evidencia",
            "Nota del revisor",
        ],
    )


def _expenses_table(session: Session, project_id: int) -> pd.DataFrame:
    links: dict[int, list[str]] = {}
    for link, doc in session.execute(
        select(ExpenseDocument, Document)
        .join(Document, Document.id == ExpenseDocument.document_id)
        .where(Document.project_id == project_id)
        .order_by(ExpenseDocument.expense_id, ExpenseDocument.score.desc().nulls_last())
    ):
        score = f" ({link.score:.2f})" if link.score is not None else ""
        links.setdefault(link.expense_id, []).append(f"{doc.filename}{score}")

    open_by_expense: dict[int, int] = {}
    for f in session.scalars(
        select(Finding).where(
            Finding.project_id == project_id,
            Finding.expense_id.is_not(None),
            Finding.status == FindingStatus.OPEN,
        )
    ):
        open_by_expense[f.expense_id] = open_by_expense.get(f.expense_id, 0) + 1

    statuses = document_statuses(session, project_id)
    rows = []
    for e in session.scalars(
        select(Expense).where(Expense.project_id == project_id).order_by(Expense.source_row)
    ):
        suggestion = None
        if e.suggested_partida:
            confidence = f" ({e.suggested_confidence:.0%})" if e.suggested_confidence else ""
            suggestion = f"{e.suggested_partida}{confidence}"
        rows.append(
            {
                "Fila del informe": e.source_row,
                "Nº orden": e.order_no,
                "Fecha": e.date,
                "Emisor": e.issuer,
                "Concepto": e.concept,
                "Partida": e.partida_code,
                "Importe €": e.amount_eur,
                "Imputado al donante €": e.amount_donor,
                "Documentos vinculados": "; ".join(links.get(e.id, [])) or None,
                "Partida sugerida": suggestion,
                "Motivo de la sugerencia": e.suggested_reason,
                "Incidencias abiertas": open_by_expense.get(e.id, 0),
                "Estado del justificante": statuses[e.id].label,
                "Confianza del vínculo": statuses[e.id].confidence_label,
            }
        )
    return pd.DataFrame(rows)


def _budget_tables(analysis: BudgetAnalysis) -> tuple[pd.DataFrame, pd.DataFrame]:
    partidas = pd.DataFrame(
        [
            {
                "Partida": p.code,
                "Nombre": p.name,
                "Aprobado (donante) €": p.approved,
                "Ejecutado (listado) €": p.executed,
                "Ejecutado (informe) €": p.declared,
                "Desviación €": p.deviation,
                "Desviación %": None if p.deviation_pct is None else float(p.deviation_pct),
                "Diferencia con el informe €": p.report_difference,
                "Gastos": p.expenses,
                "Semáforo": TRAFFIC_LABELS_ES[p.traffic],
            }
            for p in analysis.partidas
        ]
    )
    limits = pd.DataFrame(
        [
            {
                "Tope": c.limit.code,
                "Descripción": c.limit.description,
                "Ejecutado €": c.executed,
                "Base aprobada €": c.base,
                "Porcentaje": None if c.pct is None else float(c.pct),
                "Máximo %": None if c.limit.max_pct is None else float(c.limit.max_pct),
                "Máximo €": c.limit.max_amount_eur,
                "Estado": "SUPERADO" if c.exceeded else "ok",
                "Referencia": c.limit.reference,
            }
            for c in analysis.limits
        ]
    )
    return partidas, limits


def _format(ws, df: pd.DataFrame, percent_columns: tuple[str, ...] = ()) -> None:
    ws.freeze_panes = "A2"
    if ws.max_row > 1:
        ws.auto_filter.ref = ws.dimensions
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="305496")
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    for i, name in enumerate(df.columns, start=1):
        longest = max([len(str(name))] + [len(str(v)) for v in df[name].head(300).tolist()])
        ws.column_dimensions[get_column_letter(i)].width = min(
            max(10, longest + 2), MAX_COLUMN_WIDTH
        )
        for row in range(2, ws.max_row + 1):
            cell = ws.cell(row, i)
            if cell.value in COLORS:
                cell.fill = PatternFill("solid", fgColor=COLORS[cell.value])
            if name in percent_columns:
                cell.number_format = "0.00%"
            if name in ("Explicación", "Descripción", "Motivo de la sugerencia"):
                cell.alignment = Alignment(wrap_text=True, vertical="top")


def export_project_xlsx(session: Session, project_id: int, out_path: Path) -> Path:
    """Write an .xlsx with sheets (Spanish headers): "Incidencias" (one row per finding with
    rule, severity, explanation, evidence and status), "Gastos" (expenses + matched documents
    + suggested partida), "Presupuesto" (execution vs approved, traffic light) and "Topes" (donor
    caps)."""
    project = session.get(Project, project_id)
    if project is None:
        raise LookupError(f"Project {project_id} not found")
    partidas, limits = _budget_tables(compute_execution(session, project_id))
    sheets = [
        ("Incidencias", _findings_table(session, project_id), ()),
        ("Gastos", _expenses_table(session, project_id), ()),
        ("Presupuesto", partidas, ("Desviación %",)),
        ("Topes", limits, ("Porcentaje", "Máximo %")),
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        for name, frame, percent in sheets:
            frame.to_excel(writer, sheet_name=name, index=False)
            _format(writer.sheets[name], frame, percent)
    return out_path
