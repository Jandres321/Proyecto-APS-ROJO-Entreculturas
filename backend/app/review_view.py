"""Expense-centred view of a reviewed project, as plain JSON-able dicts for the web app.

One entry per expense with what the pipeline really stored about it: the linked documents (and
why), what was read inside them (segments), its findings and a verdict derived from those. Nothing
here is invented or copied from the listing: a field that was not read stays empty.
"""

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import (
    CONFIDENCE_LABELS_ES,
    FINDING_STATUS_LABELS_ES,
    SEVERITY_LABELS_ES,
    FindingStatus,
    Severity,
)
from app.models import (
    Document,
    Expense,
    ExpenseDocument,
    ExpenseSegment,
    Finding,
    Project,
    Rule,
    Segmento,
)
from app.pipeline.budget import TRAFFIC_LABELS_ES, compute_execution
from app.review import (
    BY_CONTENT,
    MANUAL,
    MISSING,
    VERIFIED,
    DocumentStatus,
    document_statuses,
    list_findings,
    locate,
)

RULE_LABELS_ES = {
    "DOC_MISSING": "Gasto sin justificante",
    "NAME_CONTENT_CONFLICT": "El nombre del archivo no coincide con su contenido",
    "SEGMENT_ORPHAN": "Documento que no justifica ningún gasto",
    "MATCH_AMBIGUOUS": "Justificante dudoso (encaja con varios gastos)",
    "ORDER_NUMBER_MISMATCH": "Nº de orden del documento distinto al del gasto",
    "DOC_ORPHAN": "Archivo sin gasto asociado",
    "DOC_DUPLICATE": "Archivo duplicado",
    "AMOUNT_MISMATCH": "El importe no coincide",
    "DATE_MISMATCH": "La fecha no coincide",
    "DONOR_STAMP_MISSING": "Falta el sello del donante",
    "NOT_AN_INVOICE": "No es una factura válida",
    "PAYMENT_EVIDENCE_MISSING": "Falta la prueba de pago",
    "CASH_OVER_LIMIT": "Pago en efectivo por encima del límite",
    "ILLEGIBLE": "Documento ilegible",
    "DUPLICATE_INVOICE": "Factura duplicada",
    "DOC_INCOMPLETE": "Expediente incompleto",
    "LIMIT_EXCEEDED": "Tope del donante superado",
    "BUDGET_DEVIATION": "Desviación respecto al presupuesto",
    "EXECUTED_REPORT_MISMATCH": "Ejecutado del informe no cuadra con el listado",
    "PARTIDA_MISMATCH": "Gasto posiblemente imputado en otra partida",
}

METHOD_LABELS_ES = {
    "filename": "propuesto por el nombre del archivo",
    "content": "encontrado por el contenido",
    "manual": "vinculado a mano",
}

# Verdict of an expense (what the reviewer sees at a glance), worst first.
VERDICT_CRITICAL = "critical"  # an open finding of high severity
VERDICT_REVIEW = "review"  # open findings, none high
VERDICT_CONFIRMED = "confirmed"  # nothing open, but the reviewer accepted a finding as real
VERDICT_NO_DOC = "nodoc"  # no document and nothing left open
VERDICT_NOT_APPLICABLE = "na"  # needs no document / not in the sample
VERDICT_OK = "ok"  # a document and no open or accepted finding


def _plain(value: Any) -> Any:
    """Decimal / date -> JSON-able."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def expense_verdict(status: DocumentStatus, findings: list[Finding]) -> dict[str, Any]:
    """Verdict from the real state of an expense: its document status and its findings."""
    open_ = [f for f in findings if f.status == FindingStatus.OPEN]
    accepted = [f for f in findings if f.status == FindingStatus.ACCEPTED]
    if open_:
        high = any(f.severity == Severity.HIGH for f in open_)
        n = len(open_)
        label = f"Revisar · {n} incidencia{'s' if n != 1 else ''}"
        return {"code": VERDICT_CRITICAL if high else VERDICT_REVIEW, "label": label}
    if accepted:
        n = len(accepted)
        label = f"{n} incidencia{'s' if n != 1 else ''} confirmada{'s' if n != 1 else ''}"
        return {"code": VERDICT_CONFIRMED, "label": label}
    if status.status == MISSING:
        return {"code": VERDICT_NO_DOC, "label": "Sin justificante"}
    if status.status in (VERIFIED, BY_CONTENT, MANUAL) or status.confidence is not None:
        return {"code": VERDICT_OK, "label": "Sin incidencias"}
    return {"code": VERDICT_NOT_APPLICABLE, "label": status.label}


def finding_view(session: Session, finding: Finding) -> dict[str, Any]:
    where = locate(session, finding)
    return {
        "id": finding.id,
        "rule_code": finding.rule_code,
        "label": RULE_LABELS_ES.get(finding.rule_code, finding.rule_code),
        "severity": finding.severity.value,
        "severity_label": SEVERITY_LABELS_ES[finding.severity],
        "status": finding.status.value,
        "status_label": FINDING_STATUS_LABELS_ES[finding.status],
        "message": finding.message,
        "evidence": finding.evidence,
        "reviewer_note": finding.reviewer_note,
        "expense_id": finding.expense_id,
        "document_id": where.document.id if where.document else None,
        "document_name": where.document.filename if where.document else None,
        "document_pages": where.document.pages if where.document else None,
        "document_openable": where.path is not None and where.path.suffix.lower() == ".pdf",
        "page": where.page,
    }


def _segment_view(segment: Segmento, link: ExpenseSegment | None = None) -> dict[str, Any]:
    return {
        "id": segment.id,
        "document_id": segment.document_id,
        "archivo": segment.archivo,
        "pagina_inicio": segment.pagina_inicio,
        "pagina_fin": segment.pagina_fin,
        "tipo": segment.tipo.value,
        "emisor": segment.emisor,
        "nif": segment.nif,
        "numero_factura": segment.numero_factura,
        "fecha": _plain(segment.fecha),
        "importe": _plain(segment.importe),
        "moneda": segment.moneda,
        "numero_orden": segment.numero_orden,
        "sello_donante": segment.sello_donante,
        "sello_expediente": segment.sello_expediente,
        "sello_porcentaje": segment.sello_porcentaje,
        "prueba_pago": segment.prueba_pago,
        "legible": segment.legible,
        "confianza_ocr": segment.confianza_ocr,
        "backend": segment.backend,
        "reason": link.reason if link else None,
        "confidence_label": CONFIDENCE_LABELS_ES[link.confidence] if link else None,
    }


def project_review(session: Session, project: Project) -> dict[str, Any]:
    """Everything the web app shows for a project: its expenses (documents, what was read,
    findings, verdict), the findings that hang from no expense and the totals."""
    statuses = document_statuses(session, project.id)
    expenses = list(
        session.scalars(
            select(Expense)
            .where(Expense.project_id == project.id)
            .order_by(Expense.source_row.nulls_last(), Expense.id)
        )
    )
    documents = {
        d.id: d for d in session.scalars(select(Document).where(Document.project_id == project.id))
    }
    links: dict[int, list[ExpenseDocument]] = {}
    for link in session.scalars(
        select(ExpenseDocument).join(Expense).where(Expense.project_id == project.id)
    ):
        links.setdefault(link.expense_id, []).append(link)
    segments: dict[int, list[tuple[ExpenseSegment, Segmento]]] = {}
    for link, segment in session.execute(
        select(ExpenseSegment, Segmento)
        .join(Segmento, ExpenseSegment.segmento_id == Segmento.id)
        .join(Expense, ExpenseSegment.expense_id == Expense.id)
        .where(Expense.project_id == project.id)
        .order_by(Segmento.archivo, Segmento.pagina_inicio)
    ):
        segments.setdefault(link.expense_id, []).append((link, segment))

    by_expense: dict[int | None, list[Finding]] = {}
    for finding in list_findings(session, project.id):
        by_expense.setdefault(finding.expense_id, []).append(finding)

    data_dir_docs = {doc_id: _openable(document) for doc_id, document in documents.items()}
    order = {"high": 0, "medium": 1, "low": 2, None: 3}
    out_expenses = []
    for e in expenses:
        status = statuses[e.id]
        mine = by_expense.get(e.id, [])
        extra = e.extra or {}
        out_expenses.append(
            {
                "id": e.id,
                "order_no": e.order_no,
                "source_row": e.source_row,
                "date": _plain(e.date),
                "issuer": e.issuer,
                "concept": e.concept,
                "partida_code": e.partida_code,
                "partida_raw": e.partida_raw,
                "amount_local": _plain(e.amount_local),
                "currency": e.currency,
                "amount_eur": _plain(e.amount_eur),
                "amount_donor": _plain(e.amount_donor),
                "invoice_no": extra.get("invoice_no"),
                "payment": extra.get("payment"),
                "suggested_partida": e.suggested_partida,
                "suggested_reason": e.suggested_reason,
                "doc_status": status.status,
                "doc_status_label": status.label,
                "confidence_label": status.confidence_label,
                "verdict": expense_verdict(status, mine),
                "documents": [
                    {
                        "id": link.document_id,
                        "filename": documents[link.document_id].filename,
                        "pages": documents[link.document_id].pages,
                        "method_label": METHOD_LABELS_ES.get(link.method.value, link.method.value),
                        "confidence_label": (
                            CONFIDENCE_LABELS_ES[link.confidence] if link.confidence else None
                        ),
                        "verified": link.verified,
                        "reason": link.reason,
                        "openable": data_dir_docs[link.document_id],
                    }
                    for link in sorted(
                        links.get(e.id, []),
                        key=lambda x: order[x.confidence.value if x.confidence else None],
                    )
                ],
                "segments": [_segment_view(s, link) for link, s in segments.get(e.id, [])],
                "findings": [finding_view(session, f) for f in mine],
            }
        )

    general = [finding_view(session, f) for f in by_expense.get(None, [])]
    counts = {s.value: 0 for s in FindingStatus}
    for group in by_expense.values():
        for f in group:
            counts[f.status.value] += 1
    return {
        "project": {
            "id": project.id,
            "name": project.name,
            "code": project.code,
            "donor": project.donor.value,
            "expenses": len(expenses),
            "documents": len(documents),
            "findings": counts,
        },
        "expenses": out_expenses,
        "general_findings": general,
        "rule_labels": RULE_LABELS_ES,
    }


def _openable(document: Document) -> bool:
    """True when the file is a PDF that exists under DATA_DIR (so a page can be shown)."""
    path = document_path(document)
    return path is not None and path.suffix.lower() == ".pdf"


def document_path(document: Document):
    """Absolute path of a registered document, or None (no DATA_DIR, missing file, inside a .zip
    or a path that escapes DATA_DIR)."""
    from app.config import get_settings

    data_dir = get_settings().data_dir
    if data_dir is None:
        return None
    root = data_dir.resolve()
    full = (root / document.path).resolve()
    if root not in full.parents or not full.is_file():
        return None
    return full


def project_budget(session: Session, project: Project) -> dict[str, Any]:
    """Approved vs executed per partida (traffic light) and the donor caps."""
    analysis = compute_execution(session, project.id)
    return {
        "partidas": [
            {
                "code": p.code,
                "name": p.name,
                "approved": _plain(p.approved),
                "executed": _plain(p.executed),
                "declared": _plain(p.declared),
                "deviation": _plain(p.deviation),
                "deviation_pct": _plain(p.deviation_pct),
                "traffic": TRAFFIC_LABELS_ES[p.traffic],
            }
            for p in analysis.partidas
        ],
        "limits": [
            {
                "code": c.limit.code,
                "description": c.limit.description,
                "executed": _plain(c.executed),
                "pct": _plain(c.pct),
                "max_pct": _plain(c.limit.max_pct),
                "max_amount_eur": _plain(c.limit.max_amount_eur),
                "exceeded": c.exceeded,
            }
            for c in analysis.limits
        ],
    }


def donor_rules(session: Session, project: Project) -> list[dict[str, Any]]:
    """Active rules of the project's donor, as loaded from rules/<donor>.yaml by the pipeline."""
    rules = session.scalars(
        select(Rule).where(Rule.donor == project.donor, Rule.active.is_(True)).order_by(Rule.code)
    )
    return [
        {
            "code": r.code,
            "label": RULE_LABELS_ES.get(r.code, r.code),
            "description": r.description,
            "severity_label": SEVERITY_LABELS_ES[r.severity],
            "reference": r.reference,
        }
        for r in rules
    ]
