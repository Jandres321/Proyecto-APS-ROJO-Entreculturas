"""Projects: creation, lookup, settings and deletion. A project is a row of `project`, whatever
the origin of its files (sample folder, browser upload or Google Drive)."""

import datetime as dt
import re
import shutil
import unicodedata
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.enums import Donor, LlmMode, SourceType
from app.models import Expense, Project

# Files of uploaded and Drive projects live under DATA_DIR/_proyectos/<key>/ (the only writable
# part of the data folder; with Docker it is a separate read-write mount).
PROJECTS_DIR = "_proyectos"
SPREADSHEETS = {".xls", ".xlsx"}


class ProjectError(ValueError):
    """A problem the user can fix; message in Spanish."""


def normalized(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", normalized(text)).strip("-")[:60] or "proyecto"


def data_dir() -> Path:
    path = get_settings().data_dir
    if path is None or not path.is_dir():
        raise ProjectError("DATA_DIR no está definido o no existe (revisa el archivo .env).")
    return path


def unique_key(session: Session, name: str) -> str:
    base = slugify(name)
    key, n = base, 2
    while session.scalar(select(Project.id).where(Project.key == key)) is not None:
        key, n = f"{base}-{n}", n + 1
    return key


def get_project(session: Session, key: str) -> Project:
    project = session.scalar(select(Project).where(Project.key == key))
    if project is None:
        raise LookupError(f"Proyecto no encontrado: {key}")
    return project


def list_projects(session: Session) -> list[Project]:
    """Sample projects first, then the rest by creation date."""
    return list(
        session.scalars(select(Project).order_by(Project.is_sample.desc(), Project.created_at))
    )


def expense_count(session: Session, project: Project) -> int:
    return session.scalar(
        select(func.count()).select_from(Expense).where(Expense.project_id == project.id)
    )


def project_folder(project: Project) -> Path:
    """Absolute folder of the project's files."""
    if not project.folder:
        raise ProjectError("El proyecto no tiene carpeta de archivos.")
    return data_dir() / project.folder


def create_project(
    session: Session,
    *,
    name: str,
    donor: str,
    source_type: SourceType,
    code: str | None = None,
    currency: str = "EUR",
    folder: str | None = None,
    source_ref: str | None = None,
    report_file: str | None = None,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    llm_mode: LlmMode = LlmMode.OFF,
    llm_max_pages: int = 20,
) -> Project:
    """Create a project. `folder` is required for `local` (relative to DATA_DIR); uploaded and
    Drive projects get their own folder under DATA_DIR/_proyectos/<key>."""
    name = (name or "").strip()
    if not name:
        raise ProjectError("Pon un nombre al proyecto.")
    try:
        donor_value = Donor(donor)
    except ValueError:
        valid = ", ".join(d.value for d in Donor)
        raise ProjectError(f"Donante no admitido: {donor!r}. Opciones: {valid}.") from None
    if start_date and end_date and end_date < start_date:
        raise ProjectError("La fecha de fin es anterior a la de inicio.")
    key = unique_key(session, name)
    if source_type is SourceType.LOCAL:
        if not folder or not (data_dir() / folder).is_dir():
            raise ProjectError(f"No existe la carpeta {folder!r} dentro de DATA_DIR.")
    else:
        folder = f"{PROJECTS_DIR}/{key}"
        (data_dir() / folder).mkdir(parents=True, exist_ok=True)
    project = Project(
        key=key,
        name=name,
        donor=donor_value,
        code=(code or "").strip() or None,
        currency=(currency or "EUR").upper()[:3],
        source_type=source_type,
        source_ref=source_ref,
        folder=folder,
        report_file=(report_file or "").strip() or None,
        start_date=start_date,
        end_date=end_date,
        llm_mode=llm_mode,
        llm_max_pages=llm_max_pages,
    )
    session.add(project)
    session.commit()
    return project


def update_project(session: Session, project: Project, **changes: object) -> Project:
    """Change the editable settings (name, period, report file, LLM reading)."""
    editable = {"name", "start_date", "end_date", "report_file", "llm_mode", "llm_max_pages"}
    unknown = set(changes) - editable
    if unknown:
        raise ProjectError(f"No se pueden cambiar: {', '.join(sorted(unknown))}.")
    for field, value in changes.items():
        setattr(project, field, value)
    if project.start_date and project.end_date and project.end_date < project.start_date:
        raise ProjectError("La fecha de fin es anterior a la de inicio.")
    if not (project.name or "").strip():
        raise ProjectError("El nombre no puede quedar vacío.")
    session.commit()
    return project


def delete_project(session: Session, project: Project, *, delete_files: bool = False) -> None:
    """Delete a project and everything reviewed in it. Sample projects cannot be deleted. The
    files of uploaded / Drive projects are removed only if `delete_files`."""
    if project.is_sample:
        raise ProjectError("Los proyectos de ejemplo no se pueden borrar.")
    folder = None
    if delete_files and project.source_type is not SourceType.LOCAL and project.folder:
        folder = data_dir() / project.folder
    session.delete(project)
    session.commit()
    if folder is not None and folder.is_dir() and PROJECTS_DIR in folder.parts:
        shutil.rmtree(folder)


def find_report(folder: Path, hint: str = "") -> str:
    """The report spreadsheet inside a project folder, as a path relative to it.

    With a `hint` (file name, with or without extension) that file must exist. If the same name
    appears in the project root and in a subfolder, prefer the root copy. Without a hint: the
    only spreadsheet whose name contains "informe", else the only spreadsheet at all. Anything
    else ambiguous is an error that names the candidates, so the user can say which one it is.
    """
    sheets = sorted(
        p
        for p in folder.rglob("*")
        if p.is_file() and p.suffix.lower() in SPREADSHEETS and not p.name.startswith("~$")
    )
    relative = [p.relative_to(folder).as_posix() for p in sheets]
    if hint:
        wanted = normalized(Path(hint).name)
        matches = [
            r
            for r, p in zip(relative, sheets, strict=True)
            if wanted in (normalized(p.name), normalized(p.stem))
        ]
        if len(matches) == 1:
            return matches[0]
        if matches:
            shallowest = min(match.count("/") for match in matches)
            root_matches = [match for match in matches if match.count("/") == shallowest]
            if len(root_matches) == 1:
                return root_matches[0]
        raise ProjectError(
            f"No encuentro el informe «{hint}» en la carpeta. Hojas de cálculo que hay: "
            + (", ".join(relative) or "ninguna")
            + "."
        )
    named = [r for r in relative if "informe" in normalized(Path(r).name)]
    for candidates in (named, relative):
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise ProjectError(
                "Hay varias hojas de cálculo y no sé cuál es el informe económico: "
                + ", ".join(candidates)
                + ". Indica el nombre del informe en los ajustes del proyecto."
            )
    raise ProjectError("La carpeta no tiene ninguna hoja de cálculo (.xls / .xlsx).")
