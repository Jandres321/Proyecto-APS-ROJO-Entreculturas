"""Uso:
    python -m conciliador config/uganda_adjumani.yaml [--validar] [--salida output/x.xlsx]
    python -m conciliador config/mi_proyecto_drive.yaml --listar-documentos   # probar la conexión
    python -m conciliador config/uganda_adjumani.yaml --contenido             # leer además los PDF dudosos
"""
from __future__ import annotations

import argparse
import fnmatch
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import yaml

from .cruce import Conciliador
from .fuentes import crear_fuente
from .listado import leer_listado
from .salida import escribir_excel
from .validacion import validar


def _servicio_drive(cfg: dict, raiz: Path):
    """Crea el cliente de Drive solo si la configuración lo necesita."""
    usa_docs = cfg["documentos"].get("tipo") == "drive"
    usa_listado = bool(cfg.get("listado", {}).get("drive"))
    if not (usa_docs or usa_listado):
        return None
    from .drive import crear_servicio
    d = cfg.get("drive", {})
    return crear_servicio(
        credenciales=raiz / d.get("credenciales", ".secrets/credentials.json"),
        token=raiz / d.get("token", ".secrets/token.json"),
        lectura=usa_listado,   # descargar el listado exige permiso de lectura; listar, solo metadatos
    )


def _ruta_listado(cfg_listado: dict, raiz: Path, servicio) -> Path | None:
    if not cfg_listado.get("drive"):
        return None
    from .drive import descargar, id_desde_url
    destino = descargar(servicio, id_desde_url(cfg_listado["drive"]), raiz / "output" / ".drive")
    print(f"Listado descargado de Drive: {destino.name}")
    return destino


def _leer_contenido(conc: Conciliador, resultados: list, cfg: dict, raiz: Path):
    """Lee el contenido de los documentos dudosos y vuelve a decidir sus gastos.
    Devuelve (resultados, resumen de la lectura) o (resultados, None) si no se puede leer."""
    from .lectura import MAX_PAGINAS, MODELO, Lector, leer_documentos, se_puede_leer
    c = cfg.get("contenido") or {}
    if cfg["documentos"].get("tipo", "local") != "local":
        print("Aviso: la lectura del contenido solo funciona con documentos en una carpeta local; "
              "se cruza solo por el nombre.")
        return resultados, None
    modelo, tope = c.get("modelo", MODELO), int(c.get("max_documentos", 50))
    lector = Lector(modelo=modelo, max_paginas=int(c.get("max_paginas", MAX_PAGINAS)), raiz=raiz)
    # Archivos que no deben salir del equipo (p. ej. nóminas): se cruzan por nombre, pero no se leen
    excluidos = [d for d in conc.documentos_a_leer(resultados)
                 if any(fnmatch.fnmatch(d.nombre, p) for p in c.get("excluir", []))]
    for d in excluidos:
        d.lectura = "no leído: excluido de la lectura"
    a_leer = [d for d in conc.documentos_a_leer(resultados)
              if se_puede_leer(d.ubicacion) and d not in excluidos]
    if excluidos:
        print(f"Contenido: {len(excluidos)} documentos excluidos de la lectura (contenido.excluir).")
    en_cache = sum(lector.en_cache(Path(d.ubicacion)) for d in a_leer)
    nuevos = min(len(a_leer) - en_cache, tope)
    print(f"Contenido: {len(a_leer)} documentos por leer, {en_cache} ya leídos antes (caché). "
          f"Se envían {nuevos} a la API de Anthropic ({modelo}).")
    resumen = leer_documentos(a_leer, lector, tope)
    resultados = conc.revisar_con_contenido(resultados)
    resumen["cambiados"] = sum(bool(r.cambio) for r in resultados)
    print(f"Contenido: leídos {resumen['leidos']} (ilegibles: {resumen['ilegibles']}) | con error: "
          f"{resumen['errores']} | sin leer por el tope: {resumen['sin_leer_por_tope']} | "
          f"gastos cuyo resultado cambia: {resumen['cambiados']}")
    return resultados, resumen


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Asocia cada gasto del listado con su justificante.")
    ap.add_argument("config", type=Path, help="YAML del proyecto")
    ap.add_argument("--validar", action="store_true", help="Medir acierto contra la numeración manual")
    ap.add_argument("--salida", type=Path,
                    help="Excel de salida (por defecto output/<config>_<fecha>_<hora>.xlsx)")
    ap.add_argument("--raiz", type=Path, default=Path.cwd(), help="Raíz para las rutas relativas del YAML")
    ap.add_argument("--listar-documentos", action="store_true",
                    help="Solo listar los documentos encontrados (útil para probar la conexión con Drive)")
    ap.add_argument("--contenido", action="store_true",
                    help="Leer el contenido de los documentos dudosos con la API de Claude (envía los "
                         "documentos a un servicio externo; requiere el extra .[contenido] y la clave en .env)")
    a = ap.parse_args(argv)

    cfg = yaml.safe_load(a.config.read_text(encoding="utf-8"))
    raiz = a.raiz.resolve()
    servicio = _servicio_drive(cfg, raiz)
    documentos = crear_fuente(cfg["documentos"], raiz, servicio).listar()

    if a.listar_documentos:
        for d in documentos:
            print(f"  {d.id}")
        print(f"Documentos encontrados: {len(documentos)}")
        return 0

    gastos = leer_listado(cfg["listado"], raiz, _ruta_listado(cfg["listado"], raiz, servicio))
    conc = Conciliador(cfg.get("cruce", {}), cfg["referencias"], cfg.get("prefijo_orden"),
                       cfg.get("sinonimos"), cfg["listado"].get("partidas_indirectos"),
                       cfg.get("contenido"))
    resultados = conc.conciliar(gastos, documentos)
    lectura = None
    if a.contenido or (cfg.get("contenido") or {}).get("activado"):
        resultados, lectura = _leer_contenido(conc, resultados, cfg, raiz)

    val = validar(resultados, documentos, cfg["prefijo_orden"]) if a.validar else None
    # Cada ejecución genera su propio Excel (fecha y hora en el nombre), para poder comparar pruebas
    salida = a.salida or raiz / "output" / f"{a.config.stem}_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    try:
        escribir_excel(salida, cfg.get("proyecto", a.config.stem), resultados, documentos, val, lectura)
    except PermissionError:
        # Solo pasa con --salida: ese Excel está abierto (Windows lo bloquea) y se guarda con otro nombre
        salida = salida.with_name(f"{salida.stem}_{datetime.now():%Y%m%d_%H%M%S}{salida.suffix}")
        print("Aviso: el Excel de salida estaba abierto; se guarda con otro nombre.")
        escribir_excel(salida, cfg.get("proyecto", a.config.stem), resultados, documentos, val, lectura)

    asignados = sum(r.estado == "asignado" for r in resultados)
    clas = Counter(r.clasificacion for r in resultados if r.estado != "asignado")
    print(f"Gastos: {len(gastos)} | con justificante: {asignados} | sin justificante: {len(gastos) - asignados} "
          f"(revisar: {clas['revisar']}, cofinanciación: {clas['cofinanciacion']}, indirectos: {clas['indirecto']})")
    print(f"Documentos: {len(documentos)}")
    if val:
        m = val.metricas
        print(f"Validación -> evaluables: {m['gastos_evaluables']} | correctos: {m['correctos']} | "
              f"incorrectos: {m['incorrectos']} | no encontrados: {m['no_encontrados']} | "
              f"exactitud: {m['exactitud']:.1%} | precisión: {m['precision']:.1%} | cobertura: {m['cobertura']:.1%}")
        print("Acierto por confianza -> " + " | ".join(
            f"{k}: {a_}/{a_ + e}" for k, (a_, e) in m["por_confianza"].items()))
    print(f"Excel: {salida}")
    return 0


def _principal() -> int:
    from .drive import ErrorDrive
    from .lectura import ErrorLectura
    try:
        return main()
    except (ErrorDrive, ErrorLectura, FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(_principal())
