"""Analysis report: an Excel workbook that tells, expense by expense, what the review found.

Sheets (Spanish headers): "Resumen" (totals), "Análisis por gasto" (one row per expense: verdict,
document, what was read inside it and its findings), "Incidencias", "Presupuesto" and "Topes".
It is built from the same expense-centred view as the web app, so both always agree, and it
carries the reviewer's decisions taken so far.
"""

import datetime as dt
from pathlib import Path
from typing import Any

import pandas as pd
from openpyxl.styles import Alignment, Font
from sqlalchemy.orm import Session

from app.models import Project
from app.pipeline.budget import compute_execution
from app.pipeline.export import budget_tables, findings_table, format_sheet
from app.review_view import project_review

VERDICT_TRAFFIC = {
    "critical": "rojo",
    "confirmed": "rojo",
    "review": "amarillo",
    "ok": "verde",
    "nodoc": "gris",
    "na": "gris",
}
VERDICT_SUMMARY_ES = {
    "critical": "Gastos que hay que revisar (alguna incidencia grave abierta)",
    "review": "Gastos que hay que revisar (incidencias abiertas, ninguna grave)",
    "confirmed": "Gastos con incidencias confirmadas por el revisor",
    "nodoc": "Gastos sin justificante",
    "ok": "Gastos con justificante y sin incidencias",
    "na": "Gastos que no requieren justificante o no están en la muestra",
}
WRAPPED = ("Detalle de las incidencias", "Motivo del vínculo", "Documentos vinculados", "Concepto")
YES_NO = {True: "sí", False: "no"}


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


def _expense_rows(expenses: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for e in expenses:
        segments, findings = e["segments"], e["findings"]
        by_status = {s: [f for f in findings if f["status"] == s] for s in ("open", "accepted")}
        false_positives = [f for f in findings if f["status"] == "false_positive"]
        detail = "\n".join(
            f"[{f['severity_label']} · {f['status_label']}] {f['label']}: {f['message']}"
            + (f" (nota: {f['reviewer_note']})" if f["reviewer_note"] else "")
            for f in findings
        )
        amounts = [
            f"{s['importe']:,.2f} {s['moneda'] or ''}".strip()
            for s in segments
            if s["importe"] is not None
        ]
        rows.append(
            {
                "Semáforo": VERDICT_TRAFFIC[e["verdict"]["code"]],
                "Veredicto": e["verdict"]["label"],
                "Fila del informe": e["source_row"],
                "Nº orden": e["order_no"],
                "Fecha": e["date"],
                "Proveedor": e["issuer"],
                "Concepto": e["concept"],
                "Partida": e["partida_code"],
                "Nº factura (informe)": e["invoice_no"],
                "Importe local": e["amount_local"],
                "Moneda": e["currency"],
                "Importe €": e["amount_eur"],
                "Imputado al donante €": e["amount_donor"],
                "Estado del justificante": e["doc_status_label"],
                "Confianza del vínculo": e["confidence_label"],
                "Documentos vinculados": _joined([d["filename"] for d in e["documents"]]),
                "Verificado por contenido": (
                    YES_NO[any(d["verified"] for d in e["documents"])] if e["documents"] else None
                ),
                "Motivo del vínculo": _joined([d["reason"] for d in e["documents"]]),
                "Leído: emisor": _joined([s["emisor"] for s in segments]),
                "Leído: nº factura": _joined([s["numero_factura"] for s in segments]),
                "Leído: fecha": _joined([s["fecha"] for s in segments]),
                "Leído: importe": _joined(amounts),
                "Leído: sello del donante": _flag([s["sello_donante"] for s in segments]),
                "Leído: prueba de pago": _flag([s["prueba_pago"] for s in segments]),
                "Leído: legible": _flag([s["legible"] for s in segments]),
                "Incidencias abiertas": len(by_status["open"]),
                "Incidencias aceptadas": len(by_status["accepted"]),
                "Falsos positivos": len(false_positives),
                "Detalle de las incidencias": detail or None,
                "Partida sugerida": e["suggested_partida"],
            }
        )
    return pd.DataFrame(rows)


def _summary(view: dict[str, Any]) -> pd.DataFrame:
    project, expenses = view["project"], view["expenses"]

    def total(items: list[dict[str, Any]]) -> float:
        return round(sum(e["amount_eur"] or 0 for e in items), 2)

    def with_verdict(*codes: str) -> list[dict[str, Any]]:
        return [e for e in expenses if e["verdict"]["code"] in codes]

    rows: list[tuple[str, Any]] = [
        ("Proyecto", project["name"]),
        ("Código", project["code"]),
        ("Donante", project["donor"]),
        ("Informe generado", dt.datetime.now().strftime("%d/%m/%Y %H:%M")),
        ("", ""),
        ("Gastos del informe", len(expenses)),
        ("Documentos registrados", project["documents"]),
        ("Importe total €", total(expenses)),
        ("", ""),
    ]
    for code, label in VERDICT_SUMMARY_ES.items():
        group = with_verdict(code)
        rows.append((label, len(group)))
        rows.append((f"   · importe € de: {label[0].lower()}{label[1:]}", total(group)))
    verified = [e for e in expenses if any(d["verified"] for d in e["documents"])]
    rows += [
        ("", ""),
        ("Gastos con justificante vinculado", sum(bool(e["documents"]) for e in expenses)),
        ("   · verificados por el contenido del documento", len(verified)),
        ("", ""),
        ("Incidencias abiertas", project["findings"]["open"]),
        ("Incidencias aceptadas (reales)", project["findings"]["accepted"]),
        ("Incidencias marcadas como falso positivo", project["findings"]["false_positive"]),
        ("", ""),
        ("Incidencias abiertas por tipo", ""),
    ]
    open_by_rule: dict[str, int] = {}
    everything = [f for e in expenses for f in e["findings"]] + view["general_findings"]
    for f in everything:
        if f["status"] == "open":
            open_by_rule[f["label"]] = open_by_rule.get(f["label"], 0) + 1
    rows += [(f"   · {label}", n) for label, n in sorted(open_by_rule.items(), key=lambda x: -x[1])]
    return pd.DataFrame(rows, columns=["Indicador", "Valor"])


def export_report_xlsx(session: Session, project_id: int, out_path: Path) -> Path:
    """Write the analysis report of a project (see the module docstring)."""
    project = session.get(Project, project_id)
    if project is None:
        raise LookupError(f"Project {project_id} not found")
    view = project_review(session, project)
    by_expense = _expense_rows(view["expenses"])
    partidas, limits = budget_tables(compute_execution(session, project_id))
    sheets = [
        ("Resumen", _summary(view), ()),
        ("Análisis por gasto", by_expense, ()),
        ("Incidencias", findings_table(session, project_id), ()),
        ("Presupuesto", partidas, ("Desviación %",)),
        ("Topes", limits, ("Porcentaje", "Máximo %")),
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        for name, frame, percent in sheets:
            frame.to_excel(writer, sheet_name=name, index=False)
            format_sheet(writer.sheets[name], frame, percent)
        summary = writer.sheets["Resumen"]
        summary.column_dimensions["A"].width = 72
        summary.column_dimensions["B"].width = 40
        summary.auto_filter.ref = None
        for row in summary.iter_rows(min_row=2, max_col=1):
            if row[0].value and not str(row[0].value).startswith("   "):
                row[0].font = Font(bold=True)
        analysis = writer.sheets["Análisis por gasto"]
        for i, name in enumerate(by_expense.columns, start=1):
            if name in WRAPPED:
                for row in range(2, analysis.max_row + 1):
                    analysis.cell(row, i).alignment = Alignment(wrap_text=True, vertical="top")
    return out_path
