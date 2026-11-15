"""Run the document checks on the pilot projects and print the findings by rule.

Usage (from backend/, needs the migrated PostgreSQL and DATA_DIR):
    python scripts/checks_report.py [--only xunta|aecid] [--sample N] [--persist]

For each project: ingest the donor report, register the PDFs, read them (cached OCR), match them
by content, load `rules/<donor>.yaml` into the `rule` table and run the checks of
app/pipeline/checks/. Everything is rolled back at the end unless --persist is given.
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import engine  # noqa: E402
from app.enums import Donor  # noqa: E402
from app.lectura.project import read_project_documents  # noqa: E402
from app.models import Expense, Finding, Project  # noqa: E402
from app.pipeline.checks import run_checks  # noqa: E402
from app.pipeline.ingest import ingest_project  # noqa: E402
from app.pipeline.match import register_documents  # noqa: E402
from app.pipeline.match_content import match_by_segments  # noqa: E402
from app.rules import CHECK_CODES, sync_rules  # noqa: E402

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


def show(key: str, base: Path, sample: int, session: Session) -> None:
    title, donor, code, folder, report = PROJECTS[key]
    project = session.scalar(select(Project).where(Project.code == code))
    if project is None:
        project = Project(name=title, donor=donor, code=code)
        session.add(project)
        session.flush()
    ingest_project(session, project.id, base / folder / report)
    register_documents(session, project.id, base / folder)
    read_project_documents(session, project.id)
    summary = match_by_segments(session, project.id)
    rules = sync_rules(session, donor)
    findings = run_checks(session, project.id)

    expenses = session.query(Expense).filter(Expense.project_id == project.id).count()
    print("=" * 100)
    print(
        f"{title} ({code}): {expenses} gastos · "
        f"{summary.expenses_linked} con documento por contenido"
    )
    print(f"  reglas cargadas desde rules/{donor.value}.yaml: {rules} nuevas o cambiadas")
    print("=" * 100)
    by_rule: Counter[str] = Counter(f.rule_code for f in findings)
    for rule_code in CHECK_CODES:
        print(f"  {rule_code:<26}{by_rule.get(rule_code, 0):>5}")
    rows = list(
        session.scalars(
            select(Finding).where(
                Finding.project_id == project.id, Finding.rule_code.in_(CHECK_CODES)
            )
        )
    )
    dup = [f for f in findings if f.rule_code == "DUPLICATE_INVOICE"]
    kinds = Counter(
        "misma factura en varios PDF"
        if "segmentos" in f.evidence
        else "documento imputado a varios gastos"
        if "suma_gastos" in f.evidence
        else "repetido en el listado"
        for f in dup
    )
    print(f"  duplicados por tipo: {dict(kinds)}")
    severities = dict(Counter(r.severity.value for r in rows))
    print(f"  {'(total)':<26}{len(rows):>5}   severidades: {severities}\n")
    for rule_code in CHECK_CODES:
        shown = [f for f in findings if f.rule_code == rule_code][:sample]
        for f in shown:
            print(f"  [{rule_code} · {f.severity.value}] {f.message}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data-dir", type=Path, default=get_settings().data_dir)
    parser.add_argument("--only", choices=sorted(PROJECTS))
    parser.add_argument("--sample", type=int, default=2, help="messages shown per rule")
    parser.add_argument("--persist", action="store_true")
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
        for key in PROJECTS:
            if args.only and args.only != key:
                continue
            show(key, args.data_dir, args.sample, session)
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
