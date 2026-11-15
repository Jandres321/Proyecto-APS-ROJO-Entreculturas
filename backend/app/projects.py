"""Projects the review app can open: the built-in pilots (files already under DATA_DIR) plus the
Google Drive folders the user has connected.

A connected folder is mirrored into DATA_DIR/_drive/<code>/ and analysed like any other project;
the list of connections lives in DATA_DIR/_drive/proyectos.json (inside the confidential data
folder, never in the repository).
"""

import json
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path

from app.config import get_settings
from app.enums import Donor
from app.pipeline.run import PILOTS, PilotProject

DRIVE_DIR = "_drive"
REGISTRY = "proyectos.json"
SPREADSHEETS = {".xls", ".xlsx"}


class ProjectError(ValueError):
    """A problem the user can fix; message in Spanish."""


@dataclass(frozen=True)
class DriveProject:
    key: str
    title: str
    donor: str  # Donor value: decides which report layout (adapter) and rules apply
    code: str
    drive_folder: str  # Drive folder id
    report: str = ""  # name of the report spreadsheet; empty = find it

    @property
    def folder(self) -> str:
        """Local mirror, relative to DATA_DIR."""
        return f"{DRIVE_DIR}/{self.code}"

    def as_pilot(self, report: str = "") -> PilotProject:
        return PilotProject(
            self.key, self.title, Donor(self.donor), self.code, self.folder, report or self.report
        )


def _drive_root() -> Path:
    data_dir = get_settings().data_dir
    if data_dir is None or not data_dir.is_dir():
        raise ProjectError("DATA_DIR no está definido o no existe: no se puede conectar Drive.")
    return data_dir / DRIVE_DIR


def drive_projects() -> dict[str, DriveProject]:
    """Connected Drive folders (empty when there is no DATA_DIR or nothing was connected)."""
    try:
        registry = _drive_root() / REGISTRY
    except ProjectError:
        return {}
    if not registry.is_file():
        return {}
    items = json.loads(registry.read_text(encoding="utf-8"))
    return {item["key"]: DriveProject(**item) for item in items}


def _save(projects: dict[str, DriveProject]) -> None:
    root = _drive_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / REGISTRY).write_text(
        json.dumps([asdict(p) for p in projects.values()], ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def connect_drive_folder(
    drive_folder: str, title: str, donor: str, report: str = ""
) -> DriveProject:
    """Remember a Drive folder as a project (one project per folder: connecting it again updates
    its title, donor and report)."""
    title = (title or "").strip()
    if not title:
        raise ProjectError("Pon un nombre al proyecto.")
    try:
        donor = Donor(donor).value
    except ValueError:
        valid = ", ".join(d.value for d in Donor)
        raise ProjectError(f"Donante no admitido: {donor!r}. Opciones: {valid}.") from None
    code = f"DRIVE-{drive_folder[:10]}"
    project = DriveProject(
        key=code.lower(),
        title=title,
        donor=donor,
        code=code,
        drive_folder=drive_folder,
        report=(report or "").strip(),
    )
    projects = drive_projects()
    projects[project.key] = project
    _save(projects)
    return project


def disconnect_drive_folder(key: str) -> None:
    """Forget a connected folder. The files already downloaded and the reviewed data stay."""
    projects = drive_projects()
    if key not in projects:
        raise ProjectError("Esa conexión de Drive no existe.")
    del projects[key]
    _save(projects)


def all_projects() -> dict[str, PilotProject]:
    """Every project of the app as a PilotProject (built-in pilots first)."""
    return {**PILOTS, **{key: p.as_pilot() for key, p in drive_projects().items()}}


def manifest_path(project: DriveProject) -> Path:
    return _drive_root() / f"{project.code}.manifest.json"


def local_folder(project: DriveProject) -> Path:
    return _drive_root() / project.code


def _normalized(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def find_report(folder: Path, hint: str = "") -> str:
    """The report spreadsheet inside a mirrored folder, as a path relative to it.

    With a `hint` (file name, with or without extension) that file must exist. Without it: the
    only spreadsheet whose name contains "informe", else the only spreadsheet at all. Anything
    ambiguous is an error that names the candidates, so the user can say which one it is.
    """
    sheets = sorted(
        p
        for p in folder.rglob("*")
        if p.is_file() and p.suffix.lower() in SPREADSHEETS and not p.name.startswith("~$")
    )
    relative = [p.relative_to(folder).as_posix() for p in sheets]
    if hint:
        wanted = _normalized(hint)
        matches = [
            r
            for r, p in zip(relative, sheets, strict=True)
            if wanted in (_normalized(p.name), _normalized(p.stem))
        ]
        if len(matches) == 1:
            return matches[0]
        raise ProjectError(
            f"No encuentro el informe «{hint}» en la carpeta. Hojas de cálculo que hay: "
            + (", ".join(relative) or "ninguna")
            + "."
        )
    named = [r for r in relative if "informe" in _normalized(Path(r).name)]
    for candidates in (named, relative):
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise ProjectError(
                "Hay varias hojas de cálculo y no sé cuál es el informe económico: "
                + ", ".join(candidates)
                + ". Vuelve a conectar la carpeta indicando el nombre del informe."
            )
    raise ProjectError("La carpeta de Drive no tiene ninguna hoja de cálculo (.xls / .xlsx).")
