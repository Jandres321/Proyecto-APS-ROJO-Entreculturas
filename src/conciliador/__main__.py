"""Uso: python -m conciliador config/uganda_adjumani.yaml [--validar] [--salida output/x.xlsx]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from .cruce import Conciliador
from .fuentes import crear_fuente
from .listado import leer_listado
from .salida import escribir_excel
from .validacion import validar


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Asocia cada gasto del listado con su justificante.")
    ap.add_argument("config", type=Path, help="YAML del proyecto")
    ap.add_argument("--validar", action="store_true", help="Medir acierto contra la numeración manual")
    ap.add_argument("--salida", type=Path, help="Excel de salida (por defecto output/<config>.xlsx)")
    ap.add_argument("--raiz", type=Path, default=Path.cwd(), help="Raíz para las rutas relativas del YAML")
    a = ap.parse_args(argv)

    cfg = yaml.safe_load(a.config.read_text(encoding="utf-8"))
    raiz = a.raiz.resolve()

    gastos = leer_listado(cfg["listado"], raiz)
    documentos = crear_fuente(cfg["documentos"], raiz).listar()
    conc = Conciliador(cfg.get("cruce", {}), cfg["referencias"], cfg.get("prefijo_orden"), cfg.get("sinonimos"),
                       cfg["listado"].get("partidas_indirectos"))
    resultados = conc.conciliar(gastos, documentos)

    val = validar(resultados, documentos, cfg["prefijo_orden"]) if a.validar else None
    salida = a.salida or raiz / "output" / f"{a.config.stem}.xlsx"
    try:
        escribir_excel(salida, cfg.get("proyecto", a.config.stem), resultados, documentos, val)
    except PermissionError:
        # El Excel anterior está abierto (Windows lo bloquea): se guarda con otro nombre
        from datetime import datetime
        salida = salida.with_name(f"{salida.stem}_{datetime.now():%Y%m%d_%H%M%S}{salida.suffix}")
        print("Aviso: el Excel de salida estaba abierto; se guarda con otro nombre.")
        escribir_excel(salida, cfg.get("proyecto", a.config.stem), resultados, documentos, val)

    asignados = sum(r.estado == "asignado" for r in resultados)
    from collections import Counter
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
            f"{k}: {a}/{a + e}" for k, (a, e) in m["por_confianza"].items()))
    print(f"Excel: {salida}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
