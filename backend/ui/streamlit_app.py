"""Review app: pick a project, run the full pipeline, review findings with the PDF beside them
(on the page of the problem), accept / mark false positive, and download the Excel.

Run it from backend/:  streamlit run ui/streamlit_app.py   (or `make ui`, inside Docker).
Everything runs locally: no document leaves the machine.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `app` importable

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import db  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.enums import (  # noqa: E402
    FINDING_STATUS_LABELS_ES,
    SEVERITY_LABELS_ES,
    FindingStatus,
    Severity,
)
from app.models import Expense, Finding, Project  # noqa: E402
from app.pipeline.budget import TRAFFIC_LABELS_ES, compute_execution  # noqa: E402
from app.pipeline.export import export_project_xlsx  # noqa: E402
from app.pipeline.run import PILOTS, STEPS, PilotProject, run_pipeline  # noqa: E402
from app.review import FindingLocation, list_findings, locate, set_decision  # noqa: E402
from app.review_view import RULE_LABELS_ES  # noqa: E402

SEVERITY_ICONS = {
    Severity.HIGH: "🔴 " + SEVERITY_LABELS_ES[Severity.HIGH],
    Severity.MEDIUM: "🟠 " + SEVERITY_LABELS_ES[Severity.MEDIUM],
    Severity.LOW: "🟡 " + SEVERITY_LABELS_ES[Severity.LOW],
}
STATUS_ICONS = {
    FindingStatus.OPEN: "⏳ " + FINDING_STATUS_LABELS_ES[FindingStatus.OPEN],
    FindingStatus.ACCEPTED: "✅ " + FINDING_STATUS_LABELS_ES[FindingStatus.ACCEPTED],
    FindingStatus.FALSE_POSITIVE: "🚫 " + FINDING_STATUS_LABELS_ES[FindingStatus.FALSE_POSITIVE],
}
TRAFFIC_COLORS = {"verde": "#C6EFCE", "amarillo": "#FFEB9C", "rojo": "#FFC7CE"}
SELECTED = "selected_finding"
EXCEL = "excel_bytes"


@st.cache_data(show_spinner=False, max_entries=64)
def render_page(path: str, page: int, rotation: int, mtime: float) -> tuple[bytes, int]:
    """PNG of one PDF page and the PDF's page count. `mtime` invalidates the cache on change."""
    import pymupdf

    with pymupdf.open(path) as pdf:
        n = pdf.page_count
        selected = pdf[min(max(page, 1), n) - 1]
        if rotation:  # many scans come upside down
            selected.set_rotation((selected.rotation + rotation) % 360)
        return selected.get_pixmap(dpi=110).tobytes("png"), n


def decide(finding_id: int, status: FindingStatus) -> None:
    """Button callback: store the decision (and the note typed beside it)."""
    note = st.session_state.get(f"note_{finding_id}")
    with db.SessionLocal() as session:
        set_decision(session, finding_id, status, note)
    st.session_state.pop(EXCEL, None)  # the prepared Excel no longer has the latest decisions


# --- sidebar: project, run, Excel ---


def launch(session: Session, pilot: PilotProject, reread: bool) -> None:
    bar = st.progress(0.0, text="Preparando…")
    order = {key: i for i, (key, _) in enumerate(STEPS)}

    def on_progress(step: str, fraction: float, message: str) -> None:
        bar.progress(
            min((order[step] + fraction) / len(STEPS), 1.0),
            text=f"Paso {order[step] + 1} de {len(STEPS)} · {message}",
        )

    try:
        summary = run_pipeline(session, pilot, reread_report=reread, on_progress=on_progress)
    except FileNotFoundError as error:
        session.rollback()
        bar.empty()
        st.error(f"Faltan datos: {error}. Comprueba DATA_DIR en el archivo .env.")
        return
    except Exception as error:  # surface it: a silent failure would look like "no findings"
        session.rollback()
        bar.empty()
        st.error(f"El análisis ha fallado: {error}")
        st.exception(error)
        return
    bar.empty()
    st.session_state.pop(EXCEL, None)
    st.session_state.pop(SELECTED, None)
    m = summary.matching
    st.success(
        f"Análisis terminado: {summary.expenses} gastos y {summary.documents} documentos. "
        f"{m.expenses_linked} gastos con justificante ({m.verified} verificados por contenido, "
        f"{m.from_name} propuestos por el nombre, {m.from_content} encontrados por contenido)"
        + (f"; {m.not_in_sample} no incluidos en la muestra" if m.not_in_sample else "")
        + "."
        + (" Se ha releído el informe." if summary.report_reread else "")
    )


def sidebar(session: Session) -> tuple[PilotProject, Project | None]:
    with st.sidebar:
        st.title("Revisor de justificación")
        key = st.selectbox("Proyecto", list(PILOTS), format_func=lambda k: PILOTS[k].title)
        pilot = PILOTS[key]
        project = session.scalar(select(Project).where(Project.code == pilot.code))
        loaded = project is not None and session.scalar(
            select(Expense.id).where(Expense.project_id == project.id).limit(1)
        )
        reread = st.checkbox(
            "Releer el informe",
            disabled=not loaded,
            help="Vuelve a leer el Excel del informe. Descarta las decisiones tomadas sobre "
            "incidencias de gastos concretos (las de presupuesto se conservan).",
        )
        if st.button("Lanzar análisis", type="primary", use_container_width=True):
            launch(session, pilot, reread)
            project = session.scalar(select(Project).where(Project.code == pilot.code))
        st.caption("Todo se procesa en local. El análisis conserva tus decisiones.")
    return pilot, project


def excel_button(session: Session, project: Project, pilot: PilotProject) -> None:
    with st.sidebar:
        st.divider()
        if st.button("Preparar Excel", use_container_width=True):
            with tempfile.TemporaryDirectory() as tmp:
                out = export_project_xlsx(session, project.id, Path(tmp) / f"{pilot.code}.xlsx")
                st.session_state[EXCEL] = out.read_bytes()
        if EXCEL in st.session_state:
            st.download_button(
                "⬇ Descargar Excel",
                st.session_state[EXCEL],
                f"{pilot.code}_revision.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
            st.caption("Incluye las decisiones tomadas hasta ahora.")


# --- findings tab ---


def findings_table(findings: list[Finding], expenses: dict[int, Expense]) -> pd.DataFrame:
    rows = []
    for f in findings:
        expense = expenses.get(f.expense_id) if f.expense_id else None
        evidence = f.evidence or {}
        rows.append(
            {
                "Estado": STATUS_ICONS[f.status],
                "Gravedad": SEVERITY_ICONS[f.severity],
                "Incidencia": RULE_LABELS_ES.get(f.rule_code, f.rule_code),
                "Regla": f.rule_code,
                "Gasto": expense.order_no if expense else evidence.get("order_no"),
                "Partida": expense.partida_code if expense else evidence.get("partida"),
                "Importe €": float(expense.amount_donor or 0) if expense else None,
                "Explicación": f.message,
            }
        )
    return pd.DataFrame(rows)


def pdf_panel(finding: Finding, where: FindingLocation) -> None:
    if where.document is None:
        st.info(
            "Esta incidencia no apunta a un documento (por ejemplo, una incidencia de "
            "presupuesto, o un gasto sin ningún candidato)."
        )
        return
    st.markdown(f"**{where.document.filename}**")
    path = where.path
    if path is None:
        st.warning(
            "No se puede abrir el archivo: no está en DATA_DIR o está dentro de un .zip. "
            f"Ruta registrada: {where.document.path}"
        )
        return
    if path.suffix.lower() != ".pdf":
        st.warning(f"El documento es un archivo {path.suffix} y no se puede previsualizar aquí.")
        return
    try:
        mtime = path.stat().st_mtime
        _, pages = render_page(str(path), 1, 0, mtime)
        left, right = st.columns(2)
        page = left.number_input(
            f"Página (de {pages})",
            min_value=1,
            max_value=pages,
            value=min(where.page or 1, pages),
            key=f"page_{finding.id}",
        )
        rotation = right.selectbox("Girar", [0, 90, 180, 270], key=f"rot_{finding.id}")
        image, _ = render_page(str(path), int(page), int(rotation), mtime)
        st.image(image, use_container_width=True)
    except Exception as error:  # damaged or protected PDF
        st.error(f"No se ha podido abrir el PDF: {error}")
        return
    st.caption(
        f"Página {where.page} del problema."
        if where.page
        else "Esta incidencia no indica página: se muestra la primera."
    )


def detail(session: Session, finding: Finding, visible: bool) -> None:
    where = locate(session, finding)
    left, right = st.columns(2)
    with left:
        st.subheader(RULE_LABELS_ES.get(finding.rule_code, finding.rule_code))
        st.caption(
            f"{finding.rule_code} · {SEVERITY_ICONS[finding.severity]} · "
            f"{STATUS_ICONS[finding.status]}"
        )
        if not visible:
            st.caption("Ya no aparece en la tabla porque no cumple los filtros actuales.")
        st.write(finding.message)
        if finding.evidence:
            with st.expander("Evidencia"):
                st.json(finding.evidence)
        st.text_input(
            "Nota (opcional)", value=finding.reviewer_note or "", key=f"note_{finding.id}"
        )
        accept, discard, reopen = st.columns(3)
        accept.button(
            "✅ Aceptar",
            on_click=decide,
            args=(finding.id, FindingStatus.ACCEPTED),
            use_container_width=True,
            help="La incidencia es real: hay que corregirla o comunicarla.",
        )
        discard.button(
            "🚫 Falso positivo",
            on_click=decide,
            args=(finding.id, FindingStatus.FALSE_POSITIVE),
            use_container_width=True,
            help="La herramienta se ha equivocado.",
        )
        reopen.button(
            "↺ Reabrir",
            on_click=decide,
            args=(finding.id, FindingStatus.OPEN),
            use_container_width=True,
        )
    with right:
        pdf_panel(finding, where)


def matches_text(finding: Finding, expense: Expense | None, needle: str) -> bool:
    """Search box: explanation, rule, order number and partida."""
    if not needle:
        return True
    parts = [finding.message, finding.rule_code, (finding.evidence or {}).get("order_no")]
    if expense:
        parts += [expense.order_no, expense.partida_code, expense.concept, expense.issuer]
    return needle in " ".join(str(p) for p in parts if p).lower()


def findings_tab(session: Session, findings: list[Finding], expenses: dict[int, Expense]) -> None:
    c1, c2, c3, c4 = st.columns([3, 2, 2, 3])
    rules = c1.multiselect(
        "Incidencia",
        sorted({f.rule_code for f in findings}),
        format_func=lambda c: RULE_LABELS_ES.get(c, c),
    )
    severities = c2.multiselect("Gravedad", list(Severity), format_func=lambda s: SEVERITY_ICONS[s])
    statuses = c3.multiselect(
        "Estado",
        list(FindingStatus),
        default=[FindingStatus.OPEN],
        format_func=lambda s: STATUS_ICONS[s],
    )
    text = c4.text_input("Buscar", placeholder="nº de orden, partida, palabra de la explicación…")

    needle = text.strip().lower()
    visible = [
        f
        for f in findings
        if (not rules or f.rule_code in rules)
        and (not severities or f.severity in severities)
        and (not statuses or f.status in statuses)
        and matches_text(f, expenses.get(f.expense_id), needle)
    ]
    st.caption(f"{len(visible)} de {len(findings)} incidencias")
    if not visible:
        st.info("Ninguna incidencia cumple los filtros.")
    else:
        event = st.dataframe(
            findings_table(visible, expenses),
            hide_index=True,
            use_container_width=True,
            height=320,
            on_select="rerun",
            selection_mode="single-row",
            key="findings_table",
            column_config={
                "Importe €": st.column_config.NumberColumn(format="%.2f"),
                "Explicación": st.column_config.TextColumn(width="large"),
            },
        )
        if event.selection.rows:
            st.session_state[SELECTED] = visible[event.selection.rows[0]].id

    selected = next((f for f in findings if f.id == st.session_state.get(SELECTED)), None)
    if selected is None:
        st.info("Selecciona una fila para ver la incidencia y su documento.")
        return
    st.divider()
    detail(session, selected, visible=selected in visible)


# --- budget tab ---


def budget_tab(session: Session, project: Project) -> None:
    analysis = compute_execution(session, project.id)
    partidas = pd.DataFrame(
        [
            {
                "Partida": p.code,
                "Nombre": p.name,
                "Aprobado €": float(p.approved),
                "Ejecutado (listado) €": float(p.executed),
                "Ejecutado (informe) €": None if p.declared is None else float(p.declared),
                "Desviación €": float(p.deviation),
                "Desviación %": None if p.deviation_pct is None else float(p.deviation_pct),
                "Semáforo": TRAFFIC_LABELS_ES[p.traffic],
            }
            for p in analysis.partidas
        ]
    )
    st.subheader("Aprobado frente a ejecutado")
    st.caption("Lo ejecutado se recalcula sumando el listado de gastos, no se toma del informe.")
    if partidas.empty:
        st.info("Este proyecto no tiene presupuesto cargado.")
    else:
        styled = partidas.style.map(
            lambda v: f"background-color: {TRAFFIC_COLORS.get(v, '')}", subset=["Semáforo"]
        ).format(
            {
                "Aprobado €": "{:,.2f}",
                "Ejecutado (listado) €": "{:,.2f}",
                "Ejecutado (informe) €": "{:,.2f}",
                "Desviación €": "{:+,.2f}",
                "Desviación %": "{:+.1%}",
            },
            na_rep="—",
        )
        st.dataframe(styled, hide_index=True, use_container_width=True)

    st.subheader("Topes del donante")
    limits = pd.DataFrame(
        [
            {
                "Tope": c.limit.code,
                "Descripción": c.limit.description,
                "Ejecutado €": float(c.executed),
                "Porcentaje": None if c.pct is None else float(c.pct),
                "Máximo %": None if c.limit.max_pct is None else float(c.limit.max_pct),
                "Máximo €": None
                if c.limit.max_amount_eur is None
                else float(c.limit.max_amount_eur),
                "Estado": "SUPERADO" if c.exceeded else "ok",
            }
            for c in analysis.limits
        ]
    )
    if not limits.empty:
        st.dataframe(
            limits.style.map(
                lambda v: "background-color: #FFC7CE" if v == "SUPERADO" else "", subset=["Estado"]
            ).format(
                {
                    "Ejecutado €": "{:,.2f}",
                    "Porcentaje": "{:.2%}",
                    "Máximo %": "{:.2%}",
                    "Máximo €": "{:,.2f}",
                },
                na_rep="—",
            ),
            hide_index=True,
            use_container_width=True,
        )


# --- page ---


def main() -> None:
    st.set_page_config(page_title="Revisor de justificación", layout="wide")
    if get_settings().data_dir is None:
        st.warning(
            "DATA_DIR no está definido: se puede revisar lo ya cargado, pero no lanzar análisis."
        )
    try:
        with db.SessionLocal() as session:
            pilot, project = sidebar(session)
            findings = list_findings(session, project.id) if project else []
            if project is None or not findings:
                st.info("Elige un proyecto y pulsa **Lanzar análisis**.")
                return
            expenses = {
                e.id: e
                for e in session.scalars(select(Expense).where(Expense.project_id == project.id))
            }
            st.header(pilot.title)
            counts = {s: sum(f.status == s for f in findings) for s in FindingStatus}
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Gastos", len(expenses))
            m2.metric("Incidencias abiertas", counts[FindingStatus.OPEN])
            m3.metric("Aceptadas", counts[FindingStatus.ACCEPTED])
            m4.metric("Falsos positivos", counts[FindingStatus.FALSE_POSITIVE])
            excel_button(session, project, pilot)

            incidents, budget = st.tabs(["Incidencias", "Presupuesto y topes"])
            with incidents:
                findings_tab(session, findings, expenses)
            with budget:
                budget_tab(session, project)
    except OperationalError:
        st.error(
            "No hay conexión con PostgreSQL. Arráncalo con `docker compose up -d db` y aplica las "
            "migraciones con `alembic upgrade head`."
        )


main()
