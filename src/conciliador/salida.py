"""Excel de resultados."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .modelos import Documento, Resultado
from .validacion import Validacion

COLORES = {
    "alta": "C6EFCE", "media": "FFEB9C", "baja": "F8CBAD", "sin_justificante": "FFC7CE",
    "correcto": "C6EFCE", "correcto_sin_documento": "C6EFCE", "incorrecto": "FFC7CE",
    "no_encontrado": "F8CBAD", "falso_positivo": "FFC7CE",
}


def _tablas(resultados: list[Resultado], documentos: list[Documento]):
    filas = []
    for r in resultados:
        g, m, s = r.gasto, r.mejor, r.segundo
        asignado = r.estado == "asignado"
        filas.append({
            "Fila listado": g.fila,
            "Partida": g.partida,
            "Fecha": g.fecha,
            "Nº factura": g.factura,
            "Proveedor": g.proveedor,
            "Descripción": g.descripcion,
            "Importe €": g.importe_eur,
            "Estado": "Con justificante" if asignado else "Sin justificante",
            "Confianza": r.confianza if asignado else "",
            "Ambiguo": "sí" if r.ambiguo else "",
            "Documento": m.documento.nombre if asignado else "",
            "Puntuación": m.puntuacion if m else None,
            "Motivo": "; ".join(m.motivos) if m else "",
            "Mejor candidato (si no asignado)": m.documento.nombre if (m and not asignado) else "",
            "2º candidato": s.documento.nombre if s else "",
            "Puntuación 2º": s.puntuacion if s else None,
            "_ubicacion": m.documento.ubicacion if asignado else "",
        })
    gastos = pd.DataFrame(filas)

    asignados = gastos[gastos["Estado"] == "Con justificante"]
    por_doc = asignados.groupby("Documento").agg(
        **{"Gastos asignados": ("Fila listado", "count"), "Importe asignado €": ("Importe €", "sum")}
    )
    docs = pd.DataFrame([{"Documento": d.nombre, "Ruta": d.id, "_ubicacion": d.ubicacion} for d in documentos])
    docs = docs.merge(por_doc, left_on="Documento", right_index=True, how="left")
    docs["Gastos asignados"] = docs["Gastos asignados"].fillna(0).astype(int)
    docs["Importe asignado €"] = docs["Importe asignado €"].fillna(0.0)

    # Un documento sin gastos que es copia/otra versión de uno con gastos (mismo concepto,
    # meses y referencias; p. ej. el .xlsx y el .pdf de la misma comisión) no es un huérfano.
    def firma(d: Documento):
        return (d.texto, frozenset(d.meses), frozenset(d.refs))
    con_gastos = set(asignados["Documento"])
    por_firma: dict = {}
    for d in documentos:
        if d.nombre in con_gastos:
            por_firma.setdefault(firma(d), d.nombre)
    obs = []
    for d in documentos:
        original = por_firma.get(firma(d))
        obs.append(f"Copia u otra versión de: {original}" if d.nombre not in con_gastos and original else "")
    docs["Observación"] = obs
    return gastos, docs


def _formatear(ws, df: pd.DataFrame, col_color: str | None = None, col_link: str | None = None,
               link_src: pd.Series | None = None, base: Path | None = None):
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="305496")
        c.alignment = Alignment(wrap_text=True, vertical="center")
    for i, nombre in enumerate(df.columns, start=1):
        largo = max([len(str(nombre))] + [len(str(v)) for v in df[nombre].head(300).tolist()])
        ws.column_dimensions[get_column_letter(i)].width = min(max(10, largo + 2), 60)
    if col_color and col_color in df.columns:
        j = list(df.columns).index(col_color) + 1
        for fila in range(2, ws.max_row + 1):
            v = ws.cell(fila, j).value
            clave = "sin_justificante" if v in ("Sin justificante",) else v
            if clave in COLORES:
                ws.cell(fila, j).fill = PatternFill("solid", fgColor=COLORES[clave])
    if col_link and link_src is not None and col_link in df.columns:
        j = list(df.columns).index(col_link) + 1
        for fila, ubic in enumerate(link_src.tolist(), start=2):
            if ubic:
                c = ws.cell(fila, j)
                # Enlace relativo al Excel: funciona al abrirlo en cualquier equipo con la misma estructura
                c.hyperlink = os.path.relpath(Path(ubic).resolve(), base).replace(os.sep, "/") if base else Path(ubic).resolve().as_uri()
                c.font = Font(color="0563C1", underline="single")


def escribir_excel(ruta: Path, proyecto: str, resultados: list[Resultado], documentos: list[Documento],
                   validacion: Validacion | None = None) -> Path:
    gastos, docs = _tablas(resultados, documentos)
    con = gastos["Estado"] == "Con justificante"
    sin_doc = (docs["Gastos asignados"] == 0) & (docs["Observación"] == "")
    copias = (docs["Gastos asignados"] == 0) & (docs["Observación"] != "")
    resumen = pd.DataFrame([
        ("Proyecto", proyecto),
        ("Generado", datetime.now().strftime("%d/%m/%Y %H:%M")),
        ("Gastos en el listado", len(gastos)),
        ("Gastos con justificante", int(con.sum())),
        ("   · confianza alta", int((gastos["Confianza"] == "alta").sum())),
        ("   · confianza media", int((gastos["Confianza"] == "media").sum())),
        ("   · confianza baja / ambiguos", int((gastos["Confianza"] == "baja").sum())),
        ("Gastos sin justificante", int((~con).sum())),
        ("Importe con justificante €", round(float(gastos.loc[con, "Importe €"].sum()), 2)),
        ("Importe sin justificante €", round(float(gastos.loc[~con, "Importe €"].sum()), 2)),
        ("Documentos en la carpeta", len(docs)),
        ("Documentos sin ningún gasto (revisar)", int(sin_doc.sum())),
        ("Documentos que son copia/otra versión de otro", int(copias.sum())),
    ], columns=["Indicador", "Valor"])
    if validacion:
        m = validacion.metricas
        resumen = pd.concat([resumen, pd.DataFrame([
            ("— Validación contra numeración manual —", ""),
            ("Gastos evaluables (con nº de orden)", m["gastos_evaluables"]),
            ("Correctos", m["correctos"] + m["correctos_sin_documento"]),
            ("Incorrectos (otro documento)", m["incorrectos"]),
            ("No encontrados", m["no_encontrados"]),
            ("Exactitud", f"{m['exactitud']:.1%}"),
            ("Precisión (de los asignados, % correctos)", f"{m['precision']:.1%}"),
            ("Cobertura (de los que tienen documento, % encontrados)", f"{m['cobertura']:.1%}"),
        ] + [
            (f"Acierto con confianza {k}", f"{a}/{a + e} ({a / (a + e):.0%})")
            for k, (a, e) in m["por_confianza"].items() if a + e
        ], columns=["Indicador", "Valor"])])

    visibles = [c for c in gastos.columns if not c.startswith("_")]
    hojas = [
        ("Resumen", resumen, None, None, None),
        ("Gastos", gastos[visibles], "Confianza", "Documento", gastos["_ubicacion"]),
        ("Sin justificante", gastos.loc[~con, visibles], None, None, None),
        ("Documentos", docs[["Documento", "Gastos asignados", "Importe asignado €", "Observación", "Ruta"]], None, "Documento", docs["_ubicacion"]),
        ("Documentos sin gasto", docs.loc[sin_doc, ["Documento", "Ruta"]], None, "Documento", docs.loc[sin_doc, "_ubicacion"]),
    ]
    if validacion:
        val = pd.DataFrame(validacion.por_gasto).merge(
            gastos[["Fila listado", "Descripción", "Importe €", "Puntuación", "Motivo"]],
            left_on="fila", right_on="Fila listado").drop(columns=["Fila listado"])
        hojas.append(("Validación", val, "veredicto", None, None))

    ruta.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(ruta, engine="openpyxl") as xw:
        for nombre, df, color, link, src in hojas:
            df.to_excel(xw, sheet_name=nombre, index=False)
            _formatear(xw.sheets[nombre], df, color, link, src, ruta.parent.resolve())
        xw.sheets["Resumen"].column_dimensions["A"].width = 55
    return ruta
