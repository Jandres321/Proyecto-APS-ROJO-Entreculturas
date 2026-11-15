"""Where a project's files come from, and getting them onto disk before the analysis.

- `local`: a folder already under DATA_DIR (nothing to do).
- `upload`: files sent from the browser; `store_upload` saves them (a .zip is unpacked).
- `drive`: a Google Drive folder; `prepare_files` mirrors it (only new or changed files).

After this step every project is analysed the same way: from its folder under DATA_DIR.
"""

import shutil
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from app import drive
from app.enums import SourceType
from app.models import Project
from app.services.projects import PROJECTS_DIR, ProjectError, data_dir, find_report

Progress = Callable[[int, int], None]
DOCUMENTS_SUBFOLDER = "justificantes"
ALLOWED_DOCUMENTS = {".pdf", ".zip", ".xls", ".xlsx"}
MAX_UNPACKED_BYTES = 2 * 1024**3  # refuse zip bombs: 2 GB unpacked at most


def _safe_name(name: str) -> str:
    """File name without directories (a browser may send 'carpeta/archivo.pdf')."""
    clean = PurePosixPath(str(name).replace("\\", "/")).name.strip()
    if not clean or clean in {".", ".."}:
        raise ProjectError(f"Nombre de archivo no válido: {name!r}")
    return clean


def _unpack(archive: Path, target: Path) -> int:
    """Extract a .zip into `target` without letting any member escape it. Returns the number of
    files extracted. The archive is removed afterwards."""
    root = target.resolve()
    count = 0
    with zipfile.ZipFile(archive) as zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        if sum(m.file_size for m in members) > MAX_UNPACKED_BYTES:
            raise ProjectError(
                f"El zip «{archive.name}» es demasiado grande una vez descomprimido."
            )
        for member in members:
            relative = PurePosixPath(member.filename.replace("\\", "/"))
            if relative.name.startswith(".") or "__MACOSX" in relative.parts:
                continue
            destination = (root / relative).resolve()
            if root not in destination.parents:
                raise ProjectError(f"El zip «{archive.name}» contiene rutas no válidas.")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as source, destination.open("wb") as out:
                shutil.copyfileobj(source, out)
            count += 1
    archive.unlink()
    return count


def store_upload(
    project: Project,
    report: tuple[str, BinaryIO] | None,
    documents: list[tuple[str, BinaryIO]],
) -> dict[str, int]:
    """Save the files of an uploaded project into its folder: the report at the top and the
    documents under `justificantes/` (zips unpacked there). Uploading again adds or replaces
    files with the same name."""
    if project.source_type is not SourceType.UPLOAD:
        raise ProjectError("Solo se pueden subir archivos a un proyecto de tipo «subida».")
    folder = data_dir() / project.folder
    docs_folder = folder / DOCUMENTS_SUBFOLDER
    docs_folder.mkdir(parents=True, exist_ok=True)
    saved = {"documentos": 0, "informe": 0}
    if report is not None:
        name = _safe_name(report[0])
        if Path(name).suffix.lower() not in {".xls", ".xlsx"}:
            raise ProjectError("El informe económico tiene que ser un Excel (.xls o .xlsx).")
        with (folder / name).open("wb") as out:
            shutil.copyfileobj(report[1], out)
        project.report_file = name
        saved["informe"] = 1
    for raw_name, stream in documents:
        name = _safe_name(raw_name)
        if Path(name).suffix.lower() not in ALLOWED_DOCUMENTS:
            raise ProjectError(f"Tipo de archivo no admitido: {name} (PDF, Excel o .zip).")
        path = docs_folder / name
        with path.open("wb") as out:
            shutil.copyfileobj(stream, out)
        saved["documentos"] += _unpack(path, docs_folder) if path.suffix.lower() == ".zip" else 1
    return saved


def _drive_manifest(project: Project) -> Path:
    # Outside the project folder, so it is never taken for a document.
    return data_dir() / PROJECTS_DIR / f"{project.key}.manifest.json"


def prepare_files(project: Project, progress: Progress | None = None) -> str:
    """Make sure the project's files are on disk and its report is known. Returns a sentence on
    what was done (empty for local / uploaded projects). Raises ProjectError / DriveError."""
    note = ""
    folder = data_dir() / (project.folder or "")
    if project.source_type is SourceType.DRIVE:
        result = drive.sync_folder(
            drive.build_service(), project.source_ref, folder, _drive_manifest(project), progress
        )
        note = (
            f"Drive: {result.total} archivos ({result.downloaded} descargados, "
            f"{result.unchanged} sin cambios"
            + (f", {result.removed} eliminados" if result.removed else "")
            + (f", {len(result.skipped)} documentos de Google omitidos" if result.skipped else "")
            + ")."
        )
    if not folder.is_dir():
        raise ProjectError(f"No existe la carpeta del proyecto: {project.folder}")
    if project.source_type is not SourceType.LOCAL or not project.report_file:
        project.report_file = find_report(folder, project.report_file or "")
    return note
