"""Inventory, filename matching and report on synthetic data (needs only the database, which
is rolled back). Content matching is tested in test_match_content.py."""

import zipfile
from decimal import Decimal
from pathlib import Path

import pymupdf
import pytest
from sqlalchemy import select

from app.enums import Donor, FindingStatus, MatchMethod, Severity
from app.models import Document, Expense, ExpenseDocument, Finding, Project
from app.pipeline.match import (
    match_by_filename,
    match_report,
    register_documents,
)


def make_pdf(path: Path, text: str | None) -> bytes:
    """One-page PDF; text=None gives an empty page (what a scan looks like to the text layer)."""
    doc = pymupdf.open()
    page = doc.new_page()
    if text:
        page.insert_text((72, 72), text * 20)  # enough characters to count as a text layer
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()
    return path.read_bytes()


def new_project(session, donor=Donor.XUNTA, code="T-1") -> Project:
    project = Project(name="test", donor=donor, code=code)
    session.add(project)
    session.flush()
    return project


def add_expense(session, project, order_no, row, **fields) -> Expense:
    from app.adapters.common import normalize_order_key

    expense = Expense(
        project_id=project.id,
        order_no=order_no,
        order_key=normalize_order_key(order_no),
        source_row=row,
        partida_code=fields.pop("partida_code", "A.VII"),
        concept=fields.pop("concept", f"Gasto {order_no}"),
        **fields,
    )
    session.add(expense)
    session.flush()
    return expense


def links_of(session, expense) -> list[ExpenseDocument]:
    return list(
        session.scalars(select(ExpenseDocument).where(ExpenseDocument.expense_id == expense.id))
    )


@pytest.fixture
def folder(tmp_path) -> Path:
    root = tmp_path / "docs"
    one = make_pdf(root / "1. Factura A.pdf", "factura A ")
    (root / "2. Factura B.pdf").write_bytes(one)  # exact duplicate of order 1's file
    make_pdf(root / "3. Escaneo.pdf", None)
    make_pdf(root / "nested" / "95. Pago.pdf", "pago ")
    (root / "nested" / "95. Pago Excel.xlsx").write_bytes(b"PK fake workbook")
    with zipfile.ZipFile(root / "40-42 nóminas.zip", "w") as z:
        make_pdf(root / "tmp_a.pdf", "nomina a ")
        make_pdf(root / "tmp_b.pdf", "nomina b ")
        z.write(root / "tmp_a.pdf", "inner/7-NOMINA JULIO.pdf")
        z.write(root / "tmp_b.pdf", "inner/8-NOMINA AGOSTO.pdf")
    (root / "tmp_a.pdf").unlink()
    (root / "tmp_b.pdf").unlink()
    (root / "Informe_final.xlsx").write_bytes(b"report")  # ignored by name
    (root / "~$lock.xlsx").write_bytes(b"x")  # junk
    (root / "notes.txt").write_text("not evidence")  # extension not supported
    return root


@pytest.fixture
def project_with_expenses(db_session):
    project = new_project(db_session)
    for row, order in enumerate(["1", "2", "3", "40", "41", "42", "95", "N/A", "n/a"], start=1):
        add_expense(db_session, project, order, row, amount_eur=Decimal(row * 100))
    add_expense(db_session, project, "N/A", 20, partida_code="B", concept="Gestoría")
    return project


def test_register_documents(db_session, project_with_expenses, folder):
    result = register_documents(db_session, project_with_expenses.id, folder)

    names = sorted(d.filename for d in result.registered)
    assert names == [
        "1. Factura A.pdf",
        "3. Escaneo.pdf",
        "7-NOMINA JULIO.pdf",
        "8-NOMINA AGOSTO.pdf",
        "95. Pago Excel.xlsx",
        "95. Pago.pdf",
    ]
    assert len(result.ignored) == 3  # report, junk and unsupported extension
    assert [d.filename for d in result.duplicates] == ["2. Factura B.pdf"]
    assert result.duplicates[0].duplicate_of.filename == "1. Factura A.pdf"

    by_name = {d.filename: d for d in result.registered}
    assert len(by_name["1. Factura A.pdf"].sha256) == 64
    assert (by_name["1. Factura A.pdf"].pages, by_name["1. Factura A.pdf"].is_scanned) == (1, False)
    assert (by_name["3. Escaneo.pdf"].pages, by_name["3. Escaneo.pdf"].is_scanned) == (1, True)
    assert by_name["95. Pago Excel.xlsx"].pages is None  # not a PDF
    assert "::inner/7-NOMINA JULIO.pdf" in by_name["7-NOMINA JULIO.pdf"].path

    warning = db_session.scalars(select(Finding).where(Finding.rule_code == "DOC_DUPLICATE")).one()
    assert warning.severity == Severity.LOW
    assert warning.evidence["duplicate_filename"] == "2. Factura B.pdf"


def test_register_documents_is_idempotent(db_session, project_with_expenses, folder):
    first = register_documents(db_session, project_with_expenses.id, folder)
    second = register_documents(db_session, project_with_expenses.id, folder)
    assert len(first.registered) == 6 and second.registered == []
    assert second.unchanged == 6
    assert len(second.duplicates) == 1  # the warning is raised again, not duplicated
    count = db_session.scalars(select(Finding).where(Finding.rule_code == "DOC_DUPLICATE")).all()
    assert len(count) == 1
    docs = db_session.scalars(
        select(Document).where(Document.project_id == project_with_expenses.id)
    )
    assert len(docs.all()) == 6


def test_match_by_filename(db_session, project_with_expenses, folder):
    register_documents(db_session, project_with_expenses.id, folder)
    created = match_by_filename(db_session, project_with_expenses.id)

    expenses = {
        e.order_no + str(e.source_row): e
        for e in db_session.scalars(
            select(Expense).where(Expense.project_id == project_with_expenses.id)
        )
    }

    def docs_of(order_row: str) -> list[str]:
        links = links_of(db_session, expenses[order_row])
        return sorted(db_session.get(Document, lk.document_id).filename for lk in links)

    assert docs_of("11") == ["1. Factura A.pdf"]
    # Order 2 points at a file skipped as exact duplicate: it is linked to the kept document.
    assert docs_of("22") == ["1. Factura A.pdf"]
    assert docs_of("33") == ["3. Escaneo.pdf"]
    # Range 40-42 -> the three expenses share the zip members.
    for key in ("404", "415", "426"):
        assert docs_of(key) == ["7-NOMINA JULIO.pdf", "8-NOMINA AGOSTO.pdf"]
    # Excel + PDF of the same order are both associated.
    assert docs_of("957") == ["95. Pago Excel.xlsx", "95. Pago.pdf"]
    # "N/A" never matches anything.
    assert docs_of("N/A8") == [] and docs_of("n/a9") == []
    assert created == sum(len(links_of(db_session, e)) for e in expenses.values())
    assert all(
        lk.method == MatchMethod.FILENAME and lk.score == 1.0
        for lk in db_session.scalars(select(ExpenseDocument))
    )

    # Re-running rebuilds the links instead of duplicating them.
    assert match_by_filename(db_session, project_with_expenses.id) == created


def test_match_report_and_findings(db_session, project_with_expenses, folder):
    pid = project_with_expenses.id
    register_documents(db_session, pid, folder)
    match_by_filename(db_session, pid)
    report = match_report(db_session, pid)

    assert len(report.with_document) == 7
    assert len(report.without_document) == 3
    assert report.orphan_documents == []
    required = [m for m in report.without_document if m.document_required]
    assert sorted(m.expense.order_no for m in required) == ["N/A", "n/a"]
    exempt = [m for m in report.without_document if not m.document_required]
    assert [m.expense.partida_code for m in exempt] == ["B"]  # indirect costs: no receipt needed

    missing = list(db_session.scalars(select(Finding).where(Finding.rule_code == "DOC_MISSING")))
    assert len(missing) == 2
    assert all(f.severity == Severity.HIGH and f.status == FindingStatus.OPEN for f in missing)
    assert "No se ha encontrado ningún justificante" in missing[0].message
    assert missing[0].evidence["source_row"] in (8, 9)

    # The reviewer discards one; a new run keeps that decision and does not recreate it.
    missing[0].status = FindingStatus.FALSE_POSITIVE
    missing[0].reviewer_note = "Gasto de otro financiador"
    db_session.flush()
    match_report(db_session, pid)
    after = list(db_session.scalars(select(Finding).where(Finding.rule_code == "DOC_MISSING")))
    assert {f.status for f in after} == {FindingStatus.OPEN, FindingStatus.FALSE_POSITIVE}
    assert len(after) == 2


def test_orphan_documents_raise_doc_orphan(db_session, tmp_path):
    project = new_project(db_session)
    add_expense(db_session, project, "1", 1)
    make_pdf(tmp_path / "1. Ok.pdf", "ok ")
    make_pdf(tmp_path / "99. Sin gasto.pdf", "sin gasto ")
    make_pdf(tmp_path / "Foto recibo.pdf", "foto ")  # no order number in the name
    register_documents(db_session, project.id, tmp_path)
    match_by_filename(db_session, project.id)
    report = match_report(db_session, project.id)

    assert sorted(d.filename for d in report.orphan_documents) == [
        "99. Sin gasto.pdf",
        "Foto recibo.pdf",
    ]
    findings = list(db_session.scalars(select(Finding).where(Finding.rule_code == "DOC_ORPHAN")))
    assert len(findings) == 2 and all(f.severity == Severity.MEDIUM for f in findings)
    assert all(f.document_id and f.expense_id is None for f in findings)
