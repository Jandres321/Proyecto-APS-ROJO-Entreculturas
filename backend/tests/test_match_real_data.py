"""Matching on the real pilot projects (skipped without DATA_DIR / PostgreSQL)."""

from collections import defaultdict

from sqlalchemy import select

from app.enums import Donor, MatchMethod, Severity
from app.models import Document, Expense, ExpenseDocument, Finding, Project
from app.pipeline.ingest import ingest_project
from app.pipeline.match import (
    canonical_key,
    match_by_filename,
    match_report,
    register_documents,
)


def run_pipeline(session, donor, code, docs_dir, report_file):
    project = Project(name=code, donor=donor, code=code)
    session.add(project)
    session.flush()
    ingest_project(session, project.id, report_file)
    registered = register_documents(session, project.id, docs_dir)
    match_by_filename(session, project.id)
    return project, registered, match_report(session, project.id)


def docs_by_expense(session, project) -> dict[int, list[str]]:
    out: dict[int, list[str]] = defaultdict(list)
    rows = session.execute(
        select(ExpenseDocument.expense_id, Document.filename)
        .join(Document, Document.id == ExpenseDocument.document_id)
        .where(Document.project_id == project.id)
    )
    for expense_id, filename in rows:
        out[expense_id].append(filename)
    return out


# --- Uganda / Xunta ---


def test_uganda_at_least_165_expenses_matched_by_name(db_session, xunta_report, xunta_docs):
    project, registered, report = run_pipeline(
        db_session, Donor.XUNTA, "24-UG-226", xunta_docs, xunta_report
    )
    total = len(report.with_document) + len(report.without_document)
    assert total == 203
    assert len(report.with_document) >= 165
    assert len(report.with_document) == 181  # all numbered expenses whose file exists

    # Expenses with order "N/A" stay without document.
    na = [m for m in report.without_document if canonical_key(m.expense.order_no) is None]
    assert len(na) == 22
    assert not any(m.expense.order_key is None for m in report.with_document)

    # 5 files are not evidence (report versions, call, guide, budget form).
    assert len(registered.ignored) == 5
    assert len(registered.registered) == 119 and registered.duplicates == []
    assert all(
        d.is_scanned is not None for d in registered.registered if d.filename.endswith(".pdf")
    )
    assert any(d.is_scanned for d in registered.registered)


def test_uganda_special_cases(db_session, xunta_report, xunta_docs):
    project, _, _ = run_pipeline(db_session, Donor.XUNTA, "24-UG-226", xunta_docs, xunta_report)
    expenses = list(db_session.scalars(select(Expense).where(Expense.project_id == project.id)))
    docs = docs_by_expense(db_session, project)

    def files(key: str) -> list[str]:
        found = {f for e in expenses if canonical_key(e.order_key) == key for f in docs[e.id]}
        return sorted(found)

    # Range "33-39 ...": seven expenses, one shared file.
    range_expenses = [e for e in expenses if e.order_key in {str(n) for n in range(33, 40)}]
    assert len(range_expenses) >= 7
    assert {tuple(docs[e.id]) for e in range_expenses} == {
        ("33-39 Salario bruto Coordinador Regional MHPSS MM.pdf",)
    }
    # Suffixes "69.A" / "49A" / "10.A" / "77.A" resolve to the suffixed expense, not to 69 / 49...
    assert files("69A") == ["69.A Combustible vehículo UBD 708 nov_Kla Vendor STM Nov_.pdf"]
    assert files("69") == ["69. Combustible para vehículos NOV.pdf"]
    assert files("10A")[0].startswith("10.A Fondo Pensiones")
    assert files("77A")[0].startswith("77.A Combustible Dic")
    assert len(files("49A")) == 2  # "49A Estancia..." and "49.A Estancia..."
    assert not any(f.startswith("49A") or f.startswith("49.A") for f in files("49"))
    # Excel + PDF with the same order number: both associated.
    assert sorted(f.rsplit(".", 1)[1] for f in files("95")) == ["pdf", "xlsx"]
    # Payroll orders 40..47: the sealed PDF and the files inside the .zip.
    payroll = files("42")
    # (no personal names in versioned files: match by prefix/suffix)
    assert any(f.startswith("40-47 nóminas") and f.endswith("selladas.pdf") for f in payroll)
    assert len(payroll) > 5


def test_uganda_findings(db_session, xunta_report, xunta_docs):
    project, _, report = run_pipeline(
        db_session, Donor.XUNTA, "24-UG-226", xunta_docs, xunta_report
    )
    missing = list(
        db_session.scalars(
            select(Finding).where(
                Finding.project_id == project.id, Finding.rule_code == "DOC_MISSING"
            )
        )
    )
    # 22 expenses without document, 9 of them indirect costs (certificate, no receipt needed).
    assert len(missing) == 13
    assert all(f.severity == Severity.HIGH and f.expense_id for f in missing)

    orphans = [d.filename for d in report.orphan_documents]
    # Receipt 83 exists as a file but the expense list has no order 83.
    assert len(orphans) == 1
    assert orphans[0].startswith("83. Dietas de oficial de finanzas")


# --- Paraguay / AECID ---

AECID_EXPECTED = {
    "ORDEN 14.PDF": "CG14",
    "ORDEN 52.PDF": "CG52",
    "ORDEN 277.PDF": "CG277",
    "ORDEN 337.PDF": "CG337",
    "Orden N°113.PDF": "CG113",
    "Orden N°176.pdf": "CG176",
    "79.pdf": "FA79",
    "97.pdf": "FA97",
    "132.pdf": "FA132",
    "153.pdf": "FA153",
    "220.pdf": "FA220",
    "222.pdf": "FA222",
    "236.pdf": "FA236",
    "462.pdf": "FA462",
    "163_PAGO YBY PORA 15.000.000.pdf": "CG163",
    "164_ PAGO YBY PORA 24.625.350.pdf": "CG164",
}


def test_paraguay_16_receipts_match_their_order(db_session, aecid_report, aecid_docs):
    project, registered, report = run_pipeline(
        db_session, Donor.AECID, "22-PY-136", aecid_docs, aecid_report
    )
    assert len(registered.registered) == 17  # 16 receipts + the contract
    assert registered.ignored == [registered.ignored[0]]  # the report workbook itself
    assert report.orphan_documents == []

    expenses = list(db_session.scalars(select(Expense).where(Expense.project_id == project.id)))
    by_file: dict[str, set[str]] = defaultdict(set)
    for e in expenses:
        for f in docs_by_expense(db_session, project).get(e.id, []):
            by_file[f].add(e.order_key)

    for filename, key in AECID_EXPECTED.items():
        # The order and all its sub-numbers (CG113 -> CG113.1, .2, .3), and nothing else.
        expected = {
            e.order_key for e in expenses if e.order_key == key or e.order_key.startswith(key + ".")
        }
        assert expected, f"{key} is not in the expense list"
        assert by_file[filename] == expected, filename

    assert len(by_file["Orden N°113.PDF"]) == 3 and len(by_file["236.pdf"]) == 5


def test_paraguay_contract_goes_to_yvy_pora_expenses(db_session, aecid_report, aecid_docs):
    project, _, _ = run_pipeline(db_session, Donor.AECID, "22-PY-136", aecid_docs, aecid_report)
    expenses = list(db_session.scalars(select(Expense).where(Expense.project_id == project.id)))
    contract = "CONTRATO YBY PORA (1).PDF"
    linked = {
        e.order_key for e in expenses if contract in docs_by_expense(db_session, project)[e.id]
    }
    yvy = {e.order_key for e in expenses if e.issuer and "yvy pora" in e.issuer.lower()}
    assert linked == yvy == {"CG163", "CG164", "CG167", "CG168", "CG169"}
    scores = db_session.scalars(
        select(ExpenseDocument.score)
        .join(Document, Document.id == ExpenseDocument.document_id)
        .where(Document.filename == contract, Document.project_id == project.id)
    ).all()
    assert all(s == 1.0 for s in scores)  # exact issuer name -> perfect fuzzy score
    assert {m for (m,) in db_session.execute(select(ExpenseDocument.method).distinct())} <= {
        MatchMethod.FILENAME
    }


def test_paraguay_indirect_and_cofinanced_need_no_receipt(db_session, aecid_report, aecid_docs):
    project, _, report = run_pipeline(
        db_session, Donor.AECID, "22-PY-136", aecid_docs, aecid_report
    )
    exempt = [m for m in report.without_document if not m.document_required]
    # the indirect-cost line plus every expense with nothing imputed to the donor
    indirect = [m for m in exempt if m.expense.partida_code == "B"]
    cofinanced = [m for m in exempt if m.expense.partida_code != "B"]
    assert len(indirect) == 1
    assert cofinanced and all(m.expense.amount_donor == 0 for m in cofinanced)
    missing = db_session.scalars(
        select(Finding).where(Finding.project_id == project.id, Finding.rule_code == "DOC_MISSING")
    ).all()
    assert len(report.without_document) == 1321 - len(report.with_document)
    assert len(missing) == len(report.without_document) - len(exempt)
