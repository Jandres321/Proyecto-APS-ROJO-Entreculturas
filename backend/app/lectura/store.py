"""Persist segments in the `segmento` table (idempotent per PDF hash + backend)."""

from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.lectura.pipeline import PdfResult
from app.models import Document, Segmento


def save_segments(session: Session, result: PdfResult, base_dir: Path | None = None) -> int:
    """Replace the stored segments of this PDF/backend with the freshly read ones.

    Returns the number of rows written. The PDF is linked to a `document` row when one with
    the same sha256 exists. `archivo` is relative to `base_dir` when possible (never absolute).
    """
    if result.error or not result.sha256:
        return 0
    session.execute(
        delete(Segmento).where(Segmento.sha256 == result.sha256, Segmento.backend == result.backend)
    )
    document_id = session.scalars(
        select(Document.id).where(Document.sha256 == result.sha256).limit(1)
    ).first()
    try:
        archivo = str(result.path.relative_to(base_dir)) if base_dir else result.path.name
    except ValueError:
        archivo = result.path.name
    for seg in result.segments:
        session.add(
            Segmento(
                document_id=document_id,
                sha256=result.sha256,
                archivo=archivo.replace("\\", "/")[:500],
                backend=result.backend,
                pagina_inicio=seg.pagina_inicio,
                pagina_fin=seg.pagina_fin,
                tipo=seg.tipo,
                emisor=seg.emisor[:300] if seg.emisor else None,
                nif=seg.nif,
                numero_factura=seg.numero_factura,
                fecha=seg.fecha,
                importe=seg.importe,
                moneda=seg.moneda,
                numero_orden=seg.numero_orden[:50] if seg.numero_orden else None,
                sello_donante=seg.sello_donante,
                sello_expediente=seg.sello_expediente[:100] if seg.sello_expediente else None,
                sello_porcentaje=seg.sello_porcentaje,
                prueba_pago=seg.prueba_pago,
                legible=seg.legible,
                confianza_ocr=seg.confianza_ocr,
                extra=seg.extra or None,
            )
        )
    return len(result.segments)
