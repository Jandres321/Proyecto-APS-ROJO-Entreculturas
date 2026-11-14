"""Validación contra la numeración manual (nº de orden en el listado y en el nombre del archivo).

La numeración es el trabajo ya hecho a mano por el equipo: aquí se usa solo como respuesta
correcta para medir el acierto del cruce automático.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from .modelos import Documento, Resultado


def clave_orden(valor: str | None) -> str | None:
    """'10.A' -> '10A', '69A' -> '69A', '7' -> '7', 'N/A' -> None."""
    if not valor:
        return None
    v = re.sub(r"[\s.]", "", str(valor)).upper()
    m = re.fullmatch(r"(\d+)([A-Z]?)", v)
    return f"{int(m.group(1))}{m.group(2)}" if m else None


def ordenes_documento(nombre: str, patron_prefijo: re.Pattern) -> set[str]:
    m = patron_prefijo.match(nombre)
    if not m:
        return set()
    desde = int(m.group("desde"))
    hasta = int(m.group("hasta") or desde)
    sufijo = (m.group("sufijo") or "").upper()
    if hasta < desde or hasta - desde > 50:
        return set()
    return {f"{n}{sufijo}" for n in range(desde, hasta + 1)}


@dataclass
class Validacion:
    por_gasto: list[dict]
    metricas: dict


def validar(resultados: list[Resultado], documentos: list[Documento], patron_prefijo: str) -> Validacion:
    pat = re.compile(patron_prefijo)
    docs_por_orden: dict[str, set[str]] = {}
    for d in documentos:
        for o in ordenes_documento(d.nombre, pat):
            docs_por_orden.setdefault(o, set()).add(d.id)

    filas, cuenta, por_conf = [], Counter(), {}
    for r in resultados:
        clave = clave_orden(r.gasto.orden)
        esperados = docs_por_orden.get(clave, set()) if clave else set()
        asignado = r.mejor.documento.id if r.estado == "asignado" else None
        if not clave:
            veredicto = "sin_referencia_manual"
        elif not esperados:
            veredicto = "correcto" if asignado is None else "falso_positivo"
            if asignado is None:
                veredicto = "correcto_sin_documento"
        elif asignado is None:
            veredicto = "no_encontrado"
        elif asignado in esperados:
            veredicto = "correcto"
        else:
            veredicto = "incorrecto"
        cuenta[veredicto] += 1
        if r.estado == "asignado" and veredicto in ("correcto", "incorrecto", "falso_positivo"):
            c = por_conf.setdefault(r.confianza, [0, 0])
            c[0 if veredicto == "correcto" else 1] += 1
        filas.append({
            "fila": r.gasto.fila,
            "orden_manual": r.gasto.orden or "",
            "documento_esperado": " | ".join(sorted(esperados)),
            "documento_asignado": asignado or "",
            "veredicto": veredicto,
        })

    evaluables = sum(v for k, v in cuenta.items() if k != "sin_referencia_manual")
    aciertos = cuenta["correcto"] + cuenta["correcto_sin_documento"]
    asignados_eval = cuenta["correcto"] + cuenta["incorrecto"] + cuenta["falso_positivo"]
    con_doc = cuenta["correcto"] + cuenta["incorrecto"] + cuenta["no_encontrado"]
    metricas = {
        "gastos_totales": len(resultados),
        "gastos_evaluables": evaluables,
        "sin_referencia_manual": cuenta["sin_referencia_manual"],
        "correctos": cuenta["correcto"],
        "incorrectos": cuenta["incorrecto"],
        "no_encontrados": cuenta["no_encontrado"],
        "falsos_positivos": cuenta["falso_positivo"],
        "correctos_sin_documento": cuenta["correcto_sin_documento"],
        "exactitud": aciertos / evaluables if evaluables else 0.0,
        "precision": cuenta["correcto"] / asignados_eval if asignados_eval else 0.0,
        "cobertura": cuenta["correcto"] / con_doc if con_doc else 0.0,
        # {"alta": (aciertos, errores), ...}
        "por_confianza": {k: tuple(v) for k, v in sorted(por_conf.items())},
    }
    return Validacion(filas, metricas)
