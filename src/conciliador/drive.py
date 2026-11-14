"""Acceso a Google Drive (API v3).

- Para listar documentos basta el permiso de SOLO METADATOS (drive.metadata.readonly):
  se ven nombres, carpetas y enlaces, nunca el contenido de los archivos.
- Solo si el listado de gastos se descarga de Drive hace falta drive.readonly.

Autenticación (se detecta sola según el JSON de credenciales):
- OAuth de escritorio ("installed"): la primera vez se abre el navegador para dar permiso
  con tu cuenta de Google; el token se guarda y las siguientes veces no pregunta.
- Cuenta de servicio ("service_account"): sin navegador; hay que compartir la carpeta de
  Drive con el email de la cuenta de servicio.

Las librerías de Google se importan solo al usarse: sin ellas, el modo local sigue funcionando.
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Iterator

SCOPE_METADATOS = "https://www.googleapis.com/auth/drive.metadata.readonly"
SCOPE_LECTURA = "https://www.googleapis.com/auth/drive.readonly"

MIME_CARPETA = "application/vnd.google-apps.folder"
MIME_ATAJO = "application/vnd.google-apps.shortcut"
MIME_HOJA_GOOGLE = "application/vnd.google-apps.spreadsheet"
MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_CAMPOS = "nextPageToken, files(id, name, mimeType, webViewLink, shortcutDetails)"


class ErrorDrive(RuntimeError):
    pass


def id_desde_url(valor: str) -> str:
    """Acepta un id o una URL de Drive (carpeta, archivo, hoja de cálculo) y devuelve el id."""
    valor = str(valor).strip()
    for patron in (r"/folders/([\w-]+)", r"/d/([\w-]+)", r"[?&]id=([\w-]+)"):
        m = re.search(patron, valor)
        if m:
            return m.group(1)
    if re.fullmatch(r"[\w-]{10,}", valor):
        return valor
    raise ErrorDrive(f"No reconozco el id o la URL de Drive: {valor!r}")


def _importar_google():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as e:  # pragma: no cover - depende del entorno
        raise ErrorDrive(
            "Faltan las librerías de Google. Instálalas con: pip install -e \".[drive]\"") from e
    return Request, service_account, Credentials, InstalledAppFlow, build


def crear_servicio(credenciales: Path, token: Path, lectura: bool = False):
    """Devuelve un cliente de la API de Drive autenticado."""
    Request, service_account, Credentials, InstalledAppFlow, build = _importar_google()
    scopes = [SCOPE_LECTURA if lectura else SCOPE_METADATOS]
    credenciales = Path(credenciales)
    if not credenciales.is_file():
        raise ErrorDrive(
            f"No encuentro el archivo de credenciales de Google: {credenciales}\n"
            "Mira la sección 'Google Drive' del README para crearlo.")
    info = json.loads(credenciales.read_text(encoding="utf-8"))

    if info.get("type") == "service_account":
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    else:
        token = Path(token)
        # Un token por nivel de permiso, para no reutilizar uno con menos permisos
        token = token.with_name(f"{token.stem}_{'lectura' if lectura else 'metadatos'}{token.suffix}")
        creds = None
        if token.is_file():
            creds = Credentials.from_authorized_user_file(str(token), scopes)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        if not creds or not creds.valid:
            flujo = InstalledAppFlow.from_client_secrets_file(str(credenciales), scopes)
            print("Se abrirá el navegador para dar permiso de acceso a Google Drive...")
            creds = flujo.run_local_server(port=0)
            token.parent.mkdir(parents=True, exist_ok=True)
            token.write_text(creds.to_json(), encoding="utf-8")
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def listar_carpeta(servicio, carpeta_id: str, recursivo: bool = True, ruta: str = "",
                   _vistas: set[str] | None = None) -> Iterator[dict]:
    """Recorre una carpeta de Drive (también en unidades compartidas).

    Devuelve dicts con id, name, mimeType, webViewLink y 'ruta' (subcarpetas/nombre).
    Las carpetas se recorren si recursivo=True; los atajos a carpetas también.
    """
    vistas = _vistas if _vistas is not None else set()
    if carpeta_id in vistas:
        return
    vistas.add(carpeta_id)

    token = None
    while True:
        resp = servicio.files().list(
            q=f"'{carpeta_id}' in parents and trashed = false",
            fields=_CAMPOS, pageSize=1000, pageToken=token, orderBy="name",
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        for f in resp.get("files", []):
            mime = f.get("mimeType", "")
            sub = f"{ruta}{f['name']}"
            if mime == MIME_ATAJO:
                destino = f.get("shortcutDetails", {})
                if destino.get("targetMimeType") == MIME_CARPETA:
                    if recursivo:
                        yield from listar_carpeta(servicio, destino["targetId"], True, sub + "/", vistas)
                    continue
                f = {**f, "id": destino.get("targetId", f["id"]), "mimeType": destino.get("targetMimeType", mime)}
                mime = f["mimeType"]
            if mime == MIME_CARPETA:
                if recursivo:
                    yield from listar_carpeta(servicio, f["id"], True, sub + "/", vistas)
                continue
            yield {**f, "ruta": sub}
        token = resp.get("nextPageToken")
        if not token:
            break


def descargar(servicio, archivo_id: str, carpeta_destino: Path) -> Path:
    """Descarga un archivo (las hojas de cálculo de Google se exportan a .xlsx)."""
    from googleapiclient.http import MediaIoBaseDownload

    meta = servicio.files().get(fileId=archivo_id, fields="id, name, mimeType",
                                supportsAllDrives=True).execute()
    nombre, mime = meta["name"], meta["mimeType"]
    if mime == MIME_HOJA_GOOGLE:
        peticion = servicio.files().export_media(fileId=archivo_id, mimeType=MIME_XLSX)
        nombre = nombre if nombre.lower().endswith(".xlsx") else f"{nombre}.xlsx"
    elif mime.startswith("application/vnd.google-apps."):
        raise ErrorDrive(f"«{nombre}» es un documento de Google que no es una hoja de cálculo.")
    else:
        peticion = servicio.files().get_media(fileId=archivo_id, supportsAllDrives=True)

    carpeta_destino = Path(carpeta_destino)
    carpeta_destino.mkdir(parents=True, exist_ok=True)
    destino = carpeta_destino / nombre
    buffer = io.BytesIO()
    descarga = MediaIoBaseDownload(buffer, peticion)
    terminado = False
    while not terminado:
        _, terminado = descarga.next_chunk()
    destino.write_bytes(buffer.getvalue())
    return destino
