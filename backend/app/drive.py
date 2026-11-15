"""Google Drive as the source of a project's files (API v3).

The pipeline reads PDFs from disk (OCR, pages, hashes), so a Drive folder is mirrored into a
local folder under DATA_DIR and analysed from there. Only new or changed files are downloaded.
That needs the read-only scope (`drive.readonly`): the files' content is fetched, never changed.

Authentication (detected from the credentials JSON):
- service account: no browser; share the Drive folder with the account's e-mail. Best for Docker.
- OAuth desktop app: needs a browser once. Run `python -m app.drive` on a machine with a browser
  (not inside Docker) to store the token; afterwards the server refreshes it by itself.

The Google libraries are imported lazily (extra `drive`): without them the rest of the app works.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from app.config import Settings, get_settings

SCOPE = "https://www.googleapis.com/auth/drive.readonly"
MIME_FOLDER = "application/vnd.google-apps.folder"
MIME_SHORTCUT = "application/vnd.google-apps.shortcut"
MIME_GOOGLE_SHEET = "application/vnd.google-apps.spreadsheet"
MIME_GOOGLE_PREFIX = "application/vnd.google-apps."
MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_FIELDS = "nextPageToken, files(id, name, mimeType, md5Checksum, modifiedTime, shortcutDetails)"

Progress = Callable[[int, int], None]  # files handled so far, total


class DriveError(RuntimeError):
    """A problem the user can fix (credentials, access, unknown folder); message in Spanish."""


def folder_id(value: str) -> str:
    """Accepts a Drive folder URL or a bare id and returns the id."""
    value = str(value or "").strip()
    for pattern in (r"/folders/([\w-]+)", r"[?&]id=([\w-]+)"):
        found = re.search(pattern, value)
        if found:
            return found.group(1)
    if re.fullmatch(r"[\w-]{10,}", value):
        return value
    raise DriveError(f"No reconozco la carpeta de Drive: {value!r}. Pega la URL de la carpeta.")


# --- authentication ----------------------------------------------------------------------------


def _google() -> tuple[Any, ...]:
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as error:  # pragma: no cover - depends on the environment
        raise DriveError(
            'Faltan las librerías de Google. Instálalas con: pip install -e ".[drive]"'
        ) from error
    return Request, service_account, Credentials, InstalledAppFlow, build


def status(settings: Settings | None = None) -> dict[str, Any]:
    """Whether Drive can be used, with a message for the page (never raises)."""
    settings = settings or get_settings()
    try:
        _google()
    except DriveError as error:
        return {"available": False, "message": str(error)}
    if not settings.google_credentials.is_file():
        return {
            "available": False,
            "message": "Falta el archivo de credenciales de Google "
            f"({settings.google_credentials.name}). Ver README, «Google Drive».",
        }
    return {"available": True, "message": "Credenciales de Google encontradas."}


def build_service(settings: Settings | None = None, *, interactive: bool = False) -> Any:
    """Authenticated Drive client. `interactive` allows opening a browser for the OAuth consent
    (only from the command line: a server cannot do it)."""
    settings = settings or get_settings()
    request_cls, service_account, credentials_cls, flow_cls, build = _google()
    path = settings.google_credentials
    if not path.is_file():
        raise DriveError(
            f"No encuentro las credenciales de Google: {path}. Ver README, «Google Drive»."
        )
    info = json.loads(path.read_text(encoding="utf-8"))
    if info.get("type") == "service_account":
        credentials = service_account.Credentials.from_service_account_info(info, scopes=[SCOPE])
    else:
        token = settings.google_token
        credentials = None
        if token.is_file():
            credentials = credentials_cls.from_authorized_user_file(str(token), [SCOPE])
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(request_cls())
        if not credentials or not credentials.valid:
            if not interactive:
                raise DriveError(
                    "Falta autorizar el acceso a Google Drive. Ejecuta una vez, fuera de Docker y "
                    "en un equipo con navegador: «python -m app.drive» (desde backend/). Con una "
                    "cuenta de servicio no hace falta."
                )
            credentials = flow_cls.from_client_secrets_file(str(path), [SCOPE]).run_local_server(
                port=0
            )
            token.parent.mkdir(parents=True, exist_ok=True)
            token.write_text(credentials.to_json(), encoding="utf-8")
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def folder_name(service: Any, folder: str) -> str:
    """Name of the folder; raises DriveError when it does not exist or is not shared with us."""
    try:
        meta = (
            service.files()
            .get(fileId=folder, fields="id, name, mimeType", supportsAllDrives=True)
            .execute()
        )
    except Exception as error:
        raise DriveError(
            "No se puede abrir la carpeta de Drive: no existe o esta cuenta no tiene acceso. Con "
            "una cuenta de servicio, comparte la carpeta con su correo."
        ) from error
    if meta.get("mimeType") != MIME_FOLDER:
        raise DriveError("El enlace de Drive no es de una carpeta.")
    return meta["name"]


# --- listing -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime: str
    path: str  # relative posix path inside the mirrored folder (sanitised, unique)
    md5: str | None
    modified: str | None

    @property
    def version(self) -> str:
        """Changes when the file's content does (md5 for binaries, modification time otherwise)."""
        return self.md5 or self.modified or ""


def _safe(name: str) -> str:
    """File name valid on every OS (Drive allows characters Windows rejects)."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(".")
    return cleaned or "sin_nombre"


def list_folder(service: Any, folder: str) -> list[DriveFile]:
    """Every file under a Drive folder (recursive, shared drives and shortcuts included)."""
    out: list[DriveFile] = []
    used: set[str] = set()
    seen_folders: set[str] = set()

    def unique(path: str) -> str:
        candidate, n = path, 1
        while candidate.lower() in used:
            n += 1
            pure = PurePosixPath(path)
            candidate = str(pure.with_name(f"{pure.stem} ({n}){pure.suffix}"))
        used.add(candidate.lower())
        return candidate

    def walk(current: str, prefix: str) -> None:
        if current in seen_folders:  # a shortcut can point back to an ancestor
            return
        seen_folders.add(current)
        token = None
        while True:
            response = (
                service.files()
                .list(
                    q=f"'{current}' in parents and trashed = false",
                    fields=_FIELDS,
                    pageSize=1000,
                    pageToken=token,
                    orderBy="name",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            for item in response.get("files", []):
                mime, file_id = item.get("mimeType", ""), item["id"]
                if mime == MIME_SHORTCUT:
                    target = item.get("shortcutDetails", {})
                    mime = target.get("targetMimeType", mime)
                    file_id = target.get("targetId", file_id)
                name = _safe(item["name"])
                if mime == MIME_FOLDER:
                    walk(file_id, f"{prefix}{name}/")
                    continue
                if mime == MIME_GOOGLE_SHEET and not name.lower().endswith(".xlsx"):
                    name += ".xlsx"  # exported as Excel
                out.append(
                    DriveFile(
                        id=file_id,
                        name=name,
                        mime=mime,
                        path=unique(f"{prefix}{name}"),
                        md5=item.get("md5Checksum"),
                        modified=item.get("modifiedTime"),
                    )
                )
            token = response.get("nextPageToken")
            if not token:
                break

    walk(folder, "")
    return out


# --- mirroring ---------------------------------------------------------------------------------


@dataclass
class SyncResult:
    total: int = 0
    downloaded: int = 0
    unchanged: int = 0
    removed: int = 0
    skipped: list[str] = field(default_factory=list)  # Google documents that cannot be exported


def _download(service: Any, item: DriveFile) -> bytes:
    files = service.files()
    if item.mime == MIME_GOOGLE_SHEET:
        return files.export_media(fileId=item.id, mimeType=MIME_XLSX).execute()
    return files.get_media(fileId=item.id, supportsAllDrives=True).execute()


def sync_folder(
    service: Any,
    folder: str,
    target: Path,
    manifest_path: Path,
    progress: Progress | None = None,
) -> SyncResult:
    """Mirror a Drive folder into `target`: download what is new or changed, keep what is the
    same and delete local files that are no longer in Drive. `manifest_path` remembers what was
    downloaded (kept outside `target` so it is never taken for a document)."""
    items = list_folder(service, folder)
    manifest: dict[str, dict[str, str]] = {}
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    result = SyncResult(total=len(items))
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    current: dict[str, dict[str, str]] = {}

    for done, item in enumerate(items, start=1):
        if item.mime.startswith(MIME_GOOGLE_PREFIX) and item.mime != MIME_GOOGLE_SHEET:
            result.skipped.append(item.path)  # Google Docs, Slides...: not evidence files
        else:
            destination = (root / item.path).resolve()
            if root not in destination.parents:
                raise DriveError(f"Nombre de archivo no válido en Drive: {item.name!r}")
            known = manifest.get(item.id)
            same = (
                known is not None
                and known.get("version") == item.version
                and known.get("path") == item.path
                and destination.is_file()
            )
            if same:
                result.unchanged += 1
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(_download(service, item))
                result.downloaded += 1
            current[item.id] = {"path": item.path, "version": item.version}
        if progress:
            progress(done, len(items))

    kept = {entry["path"] for entry in current.values()}
    for entry in manifest.values():  # gone from Drive (or renamed): remove the local copy
        stale = (root / entry["path"]).resolve()
        if entry["path"] not in kept and root in stale.parents and stale.is_file():
            stale.unlink()
            result.removed += 1
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(current, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


if __name__ == "__main__":  # one-off OAuth consent, from a machine with a browser
    try:
        build_service(interactive=True)
        print("Acceso a Google Drive autorizado. El token queda guardado.")
    except DriveError as error:
        raise SystemExit(f"Error: {error}") from None
