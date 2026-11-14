"""Lectura del listado de gastos (Excel .xls/.xlsx) a una lista de Gasto."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd

from .modelos import Gasto


def _a_fecha(v) -> date | None:
    if v is None or (isinstance(v, float) and pd.isna(v)) or v == "":
        return None
    if isinstance(v, (datetime, pd.Timestamp)):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)):  # nº de serie de Excel
        return (pd.Timestamp("1899-12-30") + pd.Timedelta(days=float(v))).date()
    try:
        return pd.to_datetime(str(v), dayfirst=True).date()
    except (ValueError, TypeError):
        return None


def _a_texto(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _a_numero(v) -> float | None:
    try:
        f = float(v)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


def _a_importe(v) -> float | None:
    """Importe que puede venir como número, '€ -', '1.234,5 €' o vacío."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).replace("€", "").replace("\xa0", "").strip()
    if t in ("", "-"):
        return 0.0 if t == "-" else None
    t = t.replace(".", "").replace(",", ".") if "," in t else t
    try:
        return float(t)
    except ValueError:
        return None


def _suma(valores) -> float | None:
    nums = [v for v in valores if v is not None]
    return sum(nums) if nums else None


def leer_listado(cfg: dict, raiz: Path) -> list[Gasto]:
    ruta = raiz / cfg["archivo"]
    df = pd.read_excel(ruta, sheet_name=cfg["hoja"], header=None, dtype=object)
    col = cfg["columnas"]
    obligatorias = cfg.get("obligatorias", ["fecha", "factura"])

    def celda(fila, campo):
        # campo: nombre definido en "columnas" o directamente un nº de columna
        i = campo if isinstance(campo, int) else col.get(campo)
        return None if i is None else fila.iloc[i]

    gastos = []
    for idx, fila in df.iterrows():
        fecha = _a_fecha(celda(fila, "fecha"))
        valores = {"fecha": fecha, "factura": _a_texto(celda(fila, "factura"))}
        if any(not valores.get(c) for c in obligatorias):
            continue
        orden = _a_texto(celda(fila, "orden")) or None
        extra = " ".join(_a_texto(celda(fila, c)) for c in cfg.get("columnas_referencia", []))
        imputacion = cfg.get("imputacion", {})
        imp_donante = _suma(_a_importe(celda(fila, c)) for c in imputacion.get("donante", []))
        imp_otros = _suma(_a_importe(celda(fila, c)) for c in imputacion.get("otros", []))
        gastos.append(Gasto(
            fila=idx + 1,
            partida=_a_texto(celda(fila, "partida")),
            fecha=fecha,
            factura=valores["factura"],
            proveedor=_a_texto(celda(fila, "proveedor")),
            descripcion=_a_texto(celda(fila, "descripcion")),
            importe_eur=_a_numero(celda(fila, "importe_eur")),
            pago=_a_texto(celda(fila, "pago")),
            referencia_extra=extra,
            imputado_donante=imp_donante,
            imputado_otros=imp_otros,
            orden=orden,
        ))
    return gastos
