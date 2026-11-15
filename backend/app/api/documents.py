"""Document routes: a page of a registered PDF as an image (with rotation) and the original."""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Response, status
from fastapi.responses import FileResponse

from app.api.deps import SessionDep
from app.models import Document
from app.review_view import document_path

router = APIRouter(prefix="/documents", tags=["documents"])


def _pdf(session: SessionDep, document_id: int) -> Path:
    document = session.get(Document, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Documento no encontrado")
    path = document_path(document)
    if path is None or path.suffix.lower() != ".pdf":
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No se puede abrir el archivo: no está en DATA_DIR, está dentro de un .zip o no es "
            "un PDF.",
        )
    return path


@router.get("/{document_id}/page/{page}.png")
def document_page(
    document_id: int, page: int, session: SessionDep, rotation: int = 0, dpi: int = 110
) -> Response:
    """One page of a registered PDF as PNG (many scans come upside down: `rotation` turns it;
    `dpi` from 72 to 300 zooms in, for handwriting)."""
    import pymupdf

    path = _pdf(session, document_id)
    try:
        with pymupdf.open(path) as pdf:
            pages = pdf.page_count
            selected = pdf[min(max(page, 1), pages) - 1]
            if rotation % 360:
                selected.set_rotation((selected.rotation + rotation) % 360)
            image = selected.get_pixmap(dpi=min(max(dpi, 72), 300)).tobytes("png")
    except Exception as error:  # damaged or protected PDF
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"No se ha podido abrir el PDF: {error}"
        ) from None
    return Response(image, media_type="image/png", headers={"X-Pages": str(pages)})


@router.get("/{document_id}/file")
def document_file(document_id: int, session: SessionDep) -> FileResponse:
    """The original PDF, to open it in a browser tab."""
    path = _pdf(session, document_id)
    return FileResponse(path, media_type="application/pdf", filename=path.name)
