"""Fuentes de documentos: carpeta local o carpeta de Google Drive, con la misma interfaz.

El cruce solo necesita la lista de documentos (nombre, identificador y enlace), así que
cambiar de fuente no afecta al resto del pipeline.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Protocol

from .modelos import Documento


class FuenteDocumentos(Protocol):
    def listar(self) -> list[Documento]: ...


def _excluido(nombre: str, excluir: list[str]) -> bool:
    return nombre.startswith((".", "~$")) or any(fnmatch.fnmatch(nombre, p) for p in excluir)


class CarpetaLocal:
    def __init__(self, ruta: Path, excluir: list[str] | None = None, recursivo: bool = True):
        self.ruta = Path(ruta)
        self.excluir = excluir or []
        self.recursivo = recursivo

    def listar(self) -> list[Documento]:
        if not self.ruta.is_dir():
            raise FileNotFoundError(f"No existe la carpeta de documentos: {self.ruta}")
        patron = "**/*" if self.recursivo else "*"
        docs = []
        for p in sorted(self.ruta.glob(patron)):
            if not p.is_file() or _excluido(p.name, self.excluir):
                continue
            rel = p.relative_to(self.ruta).as_posix()
            docs.append(Documento(id=rel, nombre=p.name, ubicacion=str(p)))
        return docs


class CarpetaDrive:
    """Carpeta de Google Drive. Solo lee metadatos (nombres y enlaces), no el contenido."""

    def __init__(self, servicio, carpeta: str, excluir: list[str] | None = None, recursivo: bool = True):
        from .drive import id_desde_url
        self.servicio = servicio
        self.carpeta_id = id_desde_url(carpeta)
        self.excluir = excluir or []
        self.recursivo = recursivo

    def listar(self) -> list[Documento]:
        from .drive import listar_carpeta
        docs = []
        for f in listar_carpeta(self.servicio, self.carpeta_id, self.recursivo):
            if _excluido(f["name"], self.excluir):
                continue
            enlace = f.get("webViewLink") or f"https://drive.google.com/file/d/{f['id']}/view"
            docs.append(Documento(id=f["ruta"], nombre=f["name"], ubicacion=enlace))
        return sorted(docs, key=lambda d: d.id)


def crear_fuente(cfg: dict, raiz: Path, servicio_drive=None) -> FuenteDocumentos:
    tipo = cfg.get("tipo", "local")
    if tipo == "local":
        return CarpetaLocal(raiz / cfg["ruta"], cfg.get("excluir"), cfg.get("recursivo", True))
    if tipo == "drive":
        if servicio_drive is None:
            raise ValueError("Para documentos en Drive hace falta un servicio de Drive autenticado.")
        return CarpetaDrive(servicio_drive, cfg["carpeta"], cfg.get("excluir"), cfg.get("recursivo", True))
    raise ValueError(f"Tipo de fuente de documentos desconocido: {tipo!r} (usa 'local' o 'drive')")
