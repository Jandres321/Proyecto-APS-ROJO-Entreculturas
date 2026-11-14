"""Print the document <-> expense matching of the pilot projects: the three lists.

Usage (from backend/, or `make match-report` in Docker; needs the migrated PostgreSQL):
    python scripts/match_report.py [--data-dir DIR] [--only xunta|aecid] [--persist] [--max N]
                                   [--solo-nombre]

For each project it loads the donor report, inventories the PDFs, matches them by file name and
prints: (1) expenses with document, (2) expenses without document, (3) orphan documents.
By default the PDFs are read (OCR, cached) and matched BY CONTENT against the expense list
(`app/pipeline/match_content.py`); `--solo-nombre` uses the old file-name matching instead.
By default everything is rolled back at the end (the database is left untouched); with
--persist the projects, documents, links and DOC_* findings are kept.
"""

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import engine  # noqa: E402
from app.enums import Donor  # noqa: E402
from app.lectura.project import read_project_documents  # noqa: E402
from app.models import Finding, Project  # noqa: E402
from app.pipeline.ingest import ingest_project  # noqa: E402
from app.pipeline.match import match_by_filename, match_report, register_documents  # noqa: E402
from app.pipeline.match_content import match_by_segments  # noqa: E402

PROJECTS = {
    "xunta": (
        "Uganda · Adjumani · Xunta 2024",
        Donor.XUNTA,
        "24-UG-226",
        "DocJustificativa/24-UG-226 Adjumani - Xunta 2024",
        "Informe_economico_final_Adjuamni_def 14.10.25 v1.xls",
    ),
    "aecid": (
        "Paraguay · AECID",
        Donor.AECID,
        "22-PY-136",
        "DocJustificativa/22-PY-136 - AECID",
        "Informe seguimiento y final_31_10.xlsx",
    ),
}


def clip(text: str | None, width: int) -> str:
    text = (text or "").replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "…"


def show(
    title: str,
    donor: Donor,
    code: str,
    folder: Path,
    report: Path,
    limit: int,
    session: Session,
    by_name: bool = False,
):
    project = session.scalar(select(Project).where(Project.code == code))
    if project is None:
        project = Project(name=title, donor=donor, code=code)
        session.add(project)
        session.flush()
    ingest_project(session, project.id, report)
    reg = register_documents(session, project.id, folder)
    if by_name:
        links = match_by_filename(session, project.id)
        how = "vínculos por nombre"
    else:
        segments = read_project_documents(session, project.id)
        summary = match_by_segments(session, project.id)
        links = summary.links
        how = (
            f"segmentos leídos: {segments}; vínculos por contenido (confianza "
            f"{summary.by_confidence or '-'})"
        )
    result = match_report(session, project.id, emit_findings=by_name)

    print("=" * 110)
    print(f"{title} ({code})")
    print("=" * 110)
    print(
        f"Documentos registrados: {len(reg.registered)} (+{reg.unchanged} ya existentes) | "
        f"ignorados: {len(reg.ignored)} | duplicados exactos: {len(reg.duplicates)} | "
        f"{how}: {links}"
    )
    for dup in reg.duplicates:
        print(f"  ! duplicado: {dup.filename}  ==  {dup.duplicate_of.filename}")
    for err in reg.errors:
        print(f"  ! {err}")

    required = [m for m in result.without_document if m.document_required]
    print(f"\n(1) GASTOS CON DOCUMENTO: {len(result.with_document)}")
    for m in result.with_document[:limit]:
        names = "; ".join(clip(d.document.filename, 46) for d in m.documents)
        print(f"  {m.expense.order_no or '-':<9}{clip(m.expense.concept, 38):<39}{names}")
    _more(len(result.with_document), limit)

    print(
        f"\n(2) GASTOS SIN DOCUMENTO: {len(result.without_document)} "
        f"({len(required)} con incidencia DOC_MISSING, "
        f"{len(result.without_document) - len(required)} que no requieren justificante)"
    )
    for m in result.without_document[:limit]:
        mark = "" if m.document_required else "  (no requiere justificante)"
        e = m.expense
        amount = f"{e.amount_eur:>10,.2f}" if e.amount_eur is not None else f"{'-':>10}"
        print(
            f"  {e.order_no or '-':<9}{clip(e.issuer, 28):<29}{clip(e.concept, 38):<39}"
            f"{amount} €{mark}"
        )
    _more(len(result.without_document), limit)

    print(f"\n(3) DOCUMENTOS HUÉRFANOS: {len(result.orphan_documents)}")
    for d in result.orphan_documents[:limit]:
        kind = "escaneado" if d.is_scanned else ("con texto" if d.is_scanned is False else "")
        print(f"  {clip(d.filename, 80):<81}{d.pages or '-':>4} pág. {kind}")
    _more(len(result.orphan_documents), limit)

    counts: dict[str, int] = {}
    for rule in session.scalars(select(Finding.rule_code).where(Finding.project_id == project.id)):
        counts[rule] = counts.get(rule, 0) + 1
    print(f"\nIncidencias: {counts}\n")


def _more(total: int, limit: int) -> None:
    if total > limit:
        print(f"  … y {total - limit} más (usa --max para ver más)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data-dir", type=Path, default=get_settings().data_dir)
    parser.add_argument("--only", choices=sorted(PROJECTS))
    parser.add_argument("--solo-nombre", action="store_true", help="match by file name only")
    parser.add_argument("--persist", action="store_true", help="keep the data in the database")
    parser.add_argument("--max", type=int, default=25, help="rows shown per list")
    args = parser.parse_args()
    if args.data_dir is None or not args.data_dir.is_dir():
        print("DATA_DIR no está definido o no existe (usa --data-dir o la variable DATA_DIR).")
        return 1
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        for key, (title, donor, code, folder, report) in PROJECTS.items():
            if args.only and args.only != key:
                continue
            base = args.data_dir / folder
            if not (base / report).is_file():
                print(f"[{key}] informe no encontrado: {base / report}")
                continue
            show(title, donor, code, base, base / report, args.max, session, args.solo_nombre)
    finally:
        session.close()
        if args.persist:
            transaction.commit()
        else:
            transaction.rollback()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
