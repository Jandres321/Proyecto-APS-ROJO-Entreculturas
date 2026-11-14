"""Step 2 — Match: link each expense with its supporting document(s).

Relation is N:M: one document can back several expenses (a payroll file for orders 33..39) and
one expense can have several documents (invoice + payment proof, Excel + PDF).

1. `register_documents`: inventory of the folder into `document` (exact duplicates skipped).
2. `match_by_filename`: file name -> order numbers, with per-project rules (match_config.py).
   Kept for the file-name signal of the content matching and for the name-only comparison.
3. Content matching lives in `app/pipeline/match_content.py` (`match_by_segments`): it scores every
   expense against every document segment read by `app/lectura/` and replaces the name links.
4. `match_report`: three lists (with document, without document, orphan documents) and the
   DOC_MISSING / DOC_ORPHAN findings.
"""

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path, PurePosixPath

from rapidfuzz import fuzz
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.enums import FindingStatus, MatchMethod, Severity
from app.models import Document, Expense, ExpenseDocument, Finding, Project
from app.pipeline.ingest import ZIP_MEMBER_SEPARATOR, scan_documents
from app.pipeline.match_config import MatchConfig, document_required, get_match_config
from app.schemas import (
    DocumentMatchOut,
    DocumentOut,
    ExpenseMatchOut,
    ExpenseOut,
    FindingIn,
    MatchReportOut,
)

logger = logging.getLogger(__name__)

RULE_DOC_MISSING = "DOC_MISSING"
RULE_DOC_ORPHAN = "DOC_ORPHAN"
RULE_DOC_DUPLICATE = "DOC_DUPLICATE"

ISSUER_MIN_SCORE = 85  # rapidfuzz partial ratio for issuer-based file-name rules

# --- keys and file names -------------------------------------------------------


def canonical_key(key: str | None) -> str | None:
    """Comparison key of an order number: "10.A" and "10A" are the same order, "FA236.3" keeps
    its sub-number. Returns None for blank / "N/A"."""
    if key is None:
        return None
    k = re.sub(r"\s+", "", key).upper().replace(",", ".")
    if k in {"", "N/A", "NA", "-"}:
        return None
    return re.sub(r"(?<=\d)\.(?=[A-Z]$)", "", k)


@dataclass(frozen=True)
class NameRef:
    """What a file name says about the expenses it backs."""

    rule: str
    keys: tuple[str, ...] = ()
    subnumbers: bool = False
    issuer_query: str | None = None
    min_score: int = ISSUER_MIN_SCORE


def parse_filename(filename: str, config: MatchConfig) -> NameRef | None:
    """Apply the project's filename rules; None when no rule recognizes the name."""
    stem = PurePosixPath(filename).stem
    for rule in config["filename_rules"]:
        m = re.match(rule["regex"], stem, re.IGNORECASE)
        if not m:
            continue
        prefix = rule.get("prefix", "")
        kind = rule["kind"]
        if kind == "range":
            a, b = int(m["a"]), int(m["b"])
            if not 0 <= b - a < 500 or b > rule.get("max_number", 10**6):
                continue  # e.g. "2024-2025 ...": years are not order numbers
            keys = tuple(f"{prefix}{n}" for n in range(a, b + 1))
            return NameRef(rule["name"], keys)
        if kind == "number":
            number = str(int(m["n"]))
            number_prefix = rule.get("prefix_by_number", {}).get(number, prefix)
            suffix = (m.groupdict().get("suffix") or "").upper()
            key = canonical_key(f"{number_prefix}{number}{suffix}")
            return NameRef(rule["name"], (key,), subnumbers=rule.get("subnumbers", False))
        if kind == "issuer":
            return NameRef(
                rule["name"],
                issuer_query=rule["issuer_query"],
                min_score=rule.get("min_score", ISSUER_MIN_SCORE),
            )
        raise ValueError(f"Unknown filename rule kind: {kind}")
    return None


def order_keys_from_filename(filename: str, config: MatchConfig) -> list[str]:
    """Order keys named by a file name (empty if unrecognized or an issuer-based rule)."""
    ref = parse_filename(filename, config)
    return list(ref.keys) if ref else []


def match_name(path: str, config: MatchConfig) -> str:
    """Name used to match a stored document: files inside a .zip use the archive's name."""
    if ZIP_MEMBER_SEPARATOR in path and config.get("zip_members_use_archive_name", True):
        path = path.split(ZIP_MEMBER_SEPARATOR)[0]
    return PurePosixPath(path.split(ZIP_MEMBER_SEPARATOR)[-1]).name


# --- helpers -------------------------------------------------------------------


def _project(session: Session, project_id: int) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise LookupError(f"Project {project_id} not found")
    return project


def _finding_key(
    rule_code: str, expense_id: int | None, document_id: int | None, evidence: dict | None
) -> tuple:
    """Identity of a finding across runs. Findings tied to neither an expense nor a document
    (budget ones) carry `evidence["subject"]` (partida or limit code) so that a decision on one
    does not hide the others of the same rule."""
    subject = evidence.get("subject") if evidence else None
    return (rule_code, expense_id, document_id, subject)


def _sync_findings(
    session: Session, project_id: int, rule_codes: set[str], new: list[FindingIn]
) -> None:
    """Replace the OPEN findings of `rule_codes` with `new`, keeping reviewer decisions.

    A finding for which the reviewer already
    decided (accepted / false positive) on the same expense+document (+ subject) is not created
    again.
    """
    session.execute(
        delete(Finding).where(
            Finding.project_id == project_id,
            Finding.rule_code.in_(rule_codes),
            Finding.status == FindingStatus.OPEN,
        )
    )
    reviewed = {
        _finding_key(f.rule_code, f.expense_id, f.document_id, f.evidence)
        for f in session.scalars(
            select(Finding).where(
                Finding.project_id == project_id, Finding.rule_code.in_(rule_codes)
            )
        )
    }
    for finding in new:
        key = _finding_key(
            finding.rule_code, finding.expense_id, finding.document_id, finding.evidence
        )
        if key in reviewed:
            continue
        session.add(Finding(project_id=project_id, **finding.model_dump()))


def _relative_base(folder: Path) -> Path:
    """Paths are stored relative to DATA_DIR when the folder lives under it."""
    data_dir = get_settings().data_dir
    if data_dir is not None and folder.resolve().is_relative_to(data_dir.resolve()):
        return data_dir.resolve()
    return folder.resolve()


# --- 1. inventory ----------------------------------------------------------------


@dataclass
class DuplicateWarning:
    path: str
    filename: str
    duplicate_of: Document


@dataclass
class RegisterResult:
    registered: list[Document] = field(default_factory=list)
    duplicates: list[DuplicateWarning] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    unchanged: int = 0  # already registered (same path and hash)


def register_documents(
    session: Session, project_id: int, folder: Path, *, include_zips: bool = True
) -> RegisterResult:
    """Inventory `folder` (recursive, .zip members included) into the `document` table.

    Computes sha256, pages and whether the PDF has a text layer. A file whose hash is already
    registered for the project is NOT registered again; it raises a warning (log + a low
    severity DOC_DUPLICATE finding that remembers the skipped name, so its order number can
    still be honoured by the filename matching). Re-running is idempotent.
    """
    project = _project(session, project_id)
    config = get_match_config(project)
    folder = Path(folder)
    if not folder.is_dir():
        raise NotADirectoryError(folder)

    scan = scan_documents(
        folder,
        base=_relative_base(folder),
        include_zips=include_zips,
        ignore=config.get("ignore_files", ()),
    )
    known: dict[str, Document] = {
        d.sha256: d
        for d in session.scalars(select(Document).where(Document.project_id == project_id))
    }
    result = RegisterResult(ignored=scan.ignored, errors=scan.errors)
    for item in scan.documents:
        kept = known.get(item.sha256)
        if kept is None:
            row = Document(project_id=project_id, **item.model_dump())
            session.add(row)
            known[item.sha256] = row
            result.registered.append(row)
        elif kept.path == item.path:
            result.unchanged += 1
        else:
            logger.warning("Duplicate document %s (same content as %s)", item.path, kept.path)
            result.duplicates.append(DuplicateWarning(item.path, item.filename, kept))
    session.flush()

    findings = [
        FindingIn(
            document_id=dup.duplicate_of.id,
            rule_code=RULE_DOC_DUPLICATE,
            severity=Severity.LOW,
            message=(
                f"«{dup.filename}» tiene exactamente el mismo contenido que "
                f"«{dup.duplicate_of.filename}» y no se ha registrado de nuevo."
            ),
            evidence={
                "duplicate_path": dup.path,
                "duplicate_filename": dup.filename,
                "kept_path": dup.duplicate_of.path,
                "sha256": dup.duplicate_of.sha256,
            },
        )
        for dup in result.duplicates
    ]
    _sync_findings(session, project_id, {RULE_DOC_DUPLICATE}, findings)
    session.commit()
    return result


# --- 2. matching by file name ---------------------------------------------------------


def match_by_filename(session: Session, project_id: int) -> int:
    """Create expense_document links (method=filename) from the document names.

    Previous filename links of the project are rebuilt (manual and content links are kept), so
    changing a rule and re-running is safe. Returns the number of links created.
    """
    project = _project(session, project_id)
    config = get_match_config(project)

    expenses = list(session.scalars(select(Expense).where(Expense.project_id == project_id)))
    documents = list(session.scalars(select(Document).where(Document.project_id == project_id)))

    by_key: dict[str, list[Expense]] = defaultdict(list)
    for expense in expenses:
        if key := canonical_key(expense.order_key):
            by_key[key].append(expense)

    # Names of exact duplicates that were skipped at registration also count for the kept file.
    aliases: dict[int, list[str]] = defaultdict(list)
    for f in session.scalars(
        select(Finding).where(
            Finding.project_id == project_id, Finding.rule_code == RULE_DOC_DUPLICATE
        )
    ):
        if f.document_id and f.evidence and f.evidence.get("duplicate_path"):
            aliases[f.document_id].append(f.evidence["duplicate_path"])

    session.execute(
        delete(ExpenseDocument).where(
            ExpenseDocument.method == MatchMethod.FILENAME,
            ExpenseDocument.expense_id.in_([e.id for e in expenses] or [0]),
        )
    )

    pairs: dict[tuple[int, int], float] = {}
    for doc in documents:
        for path in [doc.path, *aliases.get(doc.id, [])]:
            ref = parse_filename(match_name(path, config), config)
            if ref is None:
                continue
            for expense, score in _expenses_for(ref, expenses, by_key):
                pairs[(expense.id, doc.id)] = max(score, pairs.get((expense.id, doc.id), 0.0))

    session.add_all(
        ExpenseDocument(expense_id=e_id, document_id=d_id, method=MatchMethod.FILENAME, score=score)
        for (e_id, d_id), score in pairs.items()
    )
    session.commit()
    return len(pairs)


def _expenses_for(
    ref: NameRef, expenses: list[Expense], by_key: dict[str, list[Expense]]
) -> list[tuple[Expense, float]]:
    if ref.issuer_query:
        query = ref.issuer_query.casefold()
        hits = []
        for e in expenses:
            if e.issuer and (s := fuzz.partial_ratio(query, e.issuer.casefold())) >= ref.min_score:
                hits.append((e, round(s / 100, 3)))
        return hits

    found: dict[int, Expense] = {}
    for key in ref.keys:
        for e in by_key.get(key, []):
            found[e.id] = e
        if ref.subnumbers:
            for other, items in by_key.items():
                if other.startswith(key + "."):
                    found.update({e.id: e for e in items})
    return [(e, 1.0) for e in found.values()]


# --- 4. report and findings ----------------------------------------------------------------


def _money(value: Decimal | None) -> str:
    return "importe desconocido" if value is None else f"{value:,.2f} €"


def match_report(
    session: Session, project_id: int, *, emit_findings: bool = True
) -> MatchReportOut:
    """Expenses with / without document and orphan documents; persists the findings.

    - expense without document -> DOC_MISSING (high), unless it needs no receipt in the donor's
      folder: indirect costs (`no_document_partidas`) or co-financing (nothing imputed to the
      donor), see `match_config.expense_kind`;
    - document linked to no expense -> DOC_ORPHAN (medium).
    Open findings of both rules are replaced on every call; reviewer decisions are kept.
    With `emit_findings=False` only the lists are built (the content matching writes its own
    DOC_MISSING / SEGMENT_ORPHAN findings with richer explanations).
    """
    project = _project(session, project_id)
    config = get_match_config(project)

    expenses = list(
        session.scalars(
            select(Expense).where(Expense.project_id == project_id).order_by(Expense.source_row)
        )
    )
    documents = {
        d.id: d for d in session.scalars(select(Document).where(Document.project_id == project_id))
    }
    links: dict[int, list[ExpenseDocument]] = defaultdict(list)
    linked_docs: set[int] = set()
    for link in session.scalars(
        select(ExpenseDocument).where(
            ExpenseDocument.expense_id.in_([e.id for e in expenses] or [0])
        )
    ):
        links[link.expense_id].append(link)
        linked_docs.add(link.document_id)

    report = MatchReportOut()
    findings: list[FindingIn] = []
    for expense in expenses:
        item = ExpenseMatchOut(
            expense=ExpenseOut.model_validate(expense),
            documents=[
                DocumentMatchOut(
                    document=DocumentOut.model_validate(documents[link.document_id]),
                    method=link.method,
                    score=link.score,
                )
                for link in sorted(links.get(expense.id, []), key=lambda lk: lk.document_id)
            ],
            document_required=document_required(expense, config),
        )
        if item.documents:
            report.with_document.append(item)
            continue
        report.without_document.append(item)
        if item.document_required:
            label = f"nº {expense.order_no}" if expense.order_no else "sin nº de orden"
            findings.append(
                FindingIn(
                    expense_id=expense.id,
                    rule_code=RULE_DOC_MISSING,
                    severity=Severity.HIGH,
                    message=(
                        f"No se ha encontrado ningún justificante para el gasto {label} "
                        f"({(expense.concept or 'sin concepto')[:80]}; "
                        f"{_money(expense.amount_eur)})."
                    ),
                    evidence={
                        "order_no": expense.order_no,
                        "partida": expense.partida_code,
                        "issuer": expense.issuer,
                        "amount_eur": float(expense.amount_eur)
                        if expense.amount_eur is not None
                        else None,
                        "source_row": expense.source_row,
                    },
                )
            )

    for doc_id, doc in documents.items():
        if doc_id in linked_docs:
            continue
        report.orphan_documents.append(DocumentOut.model_validate(doc))
        findings.append(
            FindingIn(
                document_id=doc.id,
                rule_code=RULE_DOC_ORPHAN,
                severity=Severity.MEDIUM,
                message=(
                    f"El documento «{doc.filename}» no se ha podido asociar a ningún "
                    "gasto del listado."
                ),
                evidence={
                    "path": doc.path,
                    "sha256": doc.sha256,
                    "pages": doc.pages,
                    "is_scanned": doc.is_scanned,
                },
            )
        )

    if emit_findings:
        _sync_findings(session, project_id, {RULE_DOC_MISSING, RULE_DOC_ORPHAN}, findings)
        session.commit()
    return report
