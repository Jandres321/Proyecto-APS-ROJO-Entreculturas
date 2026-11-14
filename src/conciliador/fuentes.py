"""Fuentes de documentos. Hoy: carpeta local. Mañana: Google Drive con la misma interfaz."""
from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Protocol

from .modelos import Documento


class FuenteDocumentos(Protocol):
    def listar(self) -> list[Documento]: ...


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
            if not p.is_file() or p.name.startswith((".", "~$")):
                continue
            if any(fnmatch.fnmatch(p.name, pat) for pat in self.excluir):
                continue
            rel = p.relative_to(self.ruta).as_posix()
            docs.append(Documento(id=rel, nombre=p.name, ubicacion=str(p)))
        return docs


def crear_fuente(cfg: dict, raiz: Path) -> FuenteDocumentos:
    tipo = cfg.get("tipo", "local")
    if tipo == "local":
        return CarpetaLocal(raiz / cfg["ruta"], cfg.get("excluir"))
    raise NotImplementedError(f"Fuente de documentos no soportada todavía: {tipo}")
