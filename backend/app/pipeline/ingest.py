"""Step 1 — Ingest: load the donor report and inventory the supporting documents."""

import hashlib
import logging
import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import pymupdf
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.adapters import get_adapter
from app.models import BudgetLine, Expense, Project
from app.schemas import DocumentIn

logger = logging.getLogger(__name__)

# Files that can be supporting evidence. Anything else in the folder is ignored.
SUPPORT_EXTENSIONS = {
    ".pdf", ".xls", ".xlsx", ".xlsm", ".csv", ".doc", ".docx",
    ".jpg", ".jpeg", ".png", ".tif", ".tiff",
}  # fmt: skip
JUNK_NAMES = {"thumbs.db", ".ds_store", "desktop.ini"}
ZIP_MEMBER_SEPARATOR = "::"

# A PDF is "scanned" when its text layer is (almost) empty: fewer than this many characters
# per page, averaged over the first pages.
SCANNED_CHARS_PER_PAGE = 25
SCANNED_SAMPLE_PAGES = 3


@dataclass(frozen=True)
class IngestResult:
    expenses: int
    budget_lines: int


def ingest_project(
    session: Session,
    project_id: int,
    expenses_file: Path,
    budget_file: Path | None = None,
) -> IngestResult:
    """Load the donor report into the database using the donor's adapter.

    `budget_file` defaults to `expenses_file` (both donors keep expenses and budget in the same
    workbook). Idempotent: previous expenses and budget lines of the project are replaced, which
    also drops what hangs from them (document links, findings on those expenses).
    Commits on success; rolls back and re-raises if parsing or insertion fails.
    """
    project = session.get(Project, project_id)
    if project is None:
        raise LookupError(f"Project {project_id} not found")

    adapter = get_adapter(project.donor)
    # Parse first: a broken file must not wipe the data already loaded.
    expenses = adapter.parse_expenses(expenses_file)
    budget = adapter.parse_budget(budget_file or expenses_file)

    try:
        session.execute(delete(Expense).where(Expense.project_id == project_id))
        session.execute(delete(BudgetLine).where(BudgetLine.project_id == project_id))
        session.add_all(Expense(project_id=project_id, **e.model_dump()) for e in expenses)
        session.add_all(BudgetLine(project_id=project_id, **b.model_dump()) for b in budget)
        session.commit()
    except Exception:
        session.rollback()
        raise
    return IngestResult(expenses=len(expenses), budget_lines=len(budget))


# --- document inventory ------------------------------------------------------


@dataclass
class ScanResult:
    documents: list[DocumentIn] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)  # paths skipped by name/extension rules
    errors: list[str] = field(default_factory=list)  # unreadable files (kept, with no page info)


def file_sha256(path: Path) -> str:
    """Streaming SHA-256 of a file (used for dedup and as LLM cache key)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pdf_info(source: Path | bytes) -> tuple[int | None, bool | None]:
    """(pages, is_scanned) of a PDF given as a path or raw bytes; (None, None) if unreadable."""
    try:
        doc = (
            pymupdf.open(stream=source, filetype="pdf")
            if isinstance(source, bytes)
            else pymupdf.open(source)
        )
    except Exception:  # noqa: BLE001  (PyMuPDF raises several error types on corrupt files)
        return None, None
    with doc:
        pages = doc.page_count
        sample = min(pages, SCANNED_SAMPLE_PAGES)
        chars = sum(len(doc[i].get_text().strip()) for i in range(sample))
        return pages, (chars < SCANNED_CHARS_PER_PAGE * max(sample, 1))


def is_scanned_pdf(path: Path) -> bool:
    """True if the PDF has (almost) no extractable text and must go through the multimodal LLM."""
    _, scanned = pdf_info(path)
    return bool(scanned)


def _wanted(name: str, ignore: Iterable[re.Pattern[str]]) -> bool:
    lowered = name.lower()
    if lowered in JUNK_NAMES or name.startswith("~$"):
        return False
    if PurePosixPath(lowered).suffix not in SUPPORT_EXTENSIONS | {".zip"}:
        return False
    return not any(p.search(name) for p in ignore)


def scan_documents(
    documents_dir: Path,
    *,
    base: Path | None = None,
    include_zips: bool = True,
    ignore: Iterable[str] = (),
) -> ScanResult:
    """Walk `documents_dir` recursively and describe every supporting file.

    Per file: sha256, number of pages and whether it is scanned (PDFs only; spreadsheets and
    images get None). `path` is relative to `base` (default: `documents_dir`). Files inside a
    .zip (one level) are included as "<zip path>::<member path>" when `include_zips`.
    `ignore` are regexes tested against the file name (reports, guides... that are not evidence).
    """
    base = base or documents_dir
    patterns = [re.compile(p, re.IGNORECASE) for p in ignore]
    result = ScanResult()

    def rel(path: Path) -> str:
        return path.relative_to(base).as_posix()

    for path in sorted(p for p in documents_dir.rglob("*") if p.is_file()):
        if not _wanted(path.name, patterns):
            result.ignored.append(rel(path))
            continue
        if path.suffix.lower() == ".zip":
            if include_zips:
                _scan_zip(path, rel(path), patterns, result)
            else:
                result.ignored.append(rel(path))
            continue
        pages, scanned = pdf_info(path) if path.suffix.lower() == ".pdf" else (None, None)
        if path.suffix.lower() == ".pdf" and pages is None:
            result.errors.append(f"{rel(path)}: PDF no legible")
        result.documents.append(
            DocumentIn(
                path=rel(path),
                filename=path.name,
                sha256=file_sha256(path),
                pages=pages,
                is_scanned=scanned,
            )
        )
    return result


def _scan_zip(
    zip_path: Path, zip_rel: str, patterns: list[re.Pattern[str]], result: ScanResult
) -> None:
    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        result.errors.append(f"{zip_rel}: ZIP no legible")
        return
    with archive:
        for member in sorted(archive.namelist()):
            name = PurePosixPath(member).name
            if member.endswith("/") or not _wanted(name, patterns):
                continue
            if PurePosixPath(name).suffix.lower() == ".zip":
                result.ignored.append(f"{zip_rel}{ZIP_MEMBER_SEPARATOR}{member}")
                continue  # nested archives are not expanded
            data = archive.read(member)
            is_pdf = name.lower().endswith(".pdf")
            pages, scanned = pdf_info(data) if is_pdf else (None, None)
            result.documents.append(
                DocumentIn(
                    path=f"{zip_rel}{ZIP_MEMBER_SEPARATOR}{member}",
                    filename=name,
                    sha256=hashlib.sha256(data).hexdigest(),
                    pages=pages,
                    is_scanned=scanned,
                )
            )
