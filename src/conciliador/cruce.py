"""Motor de cruce gasto -> documento.

No usa el nº de orden manual: lo elimina del nombre del archivo antes de comparar.
Señales:
  - referencia de factura/comprobante presente en el nombre del archivo
  - similitud de concepto (palabras del nombre del archivo contenidas en el gasto)
  - coherencia del mes (el del texto del gasto o, si no hay, el de su fecha)
"""
from __future__ import annotations

import re

from rapidfuzz import fuzz

from .modelos import Candidato, Documento, Gasto, Resultado
from .texto import (extraer_meses, extraer_referencias, numeros_de, preparar_sinonimos,
                    quitar_extension, texto_concepto)

DEFECTOS = {
    "umbral": 0.60,
    "umbral_alta": 0.80,
    "margen_ambiguo": 0.05,
    "bonus_referencia": 0.35,      # se suma si el nº de factura coincide (prefijo y número)
    "minimo_referencia": 0.62,     # una referencia completa coincidente basta para asignar
    "bonus_numero": 0.20,          # solo coincide el número (p. ej. CV UGA04-119 frente a EB UGA04-119)
    "penalizacion_referencia": 0.15,
    "bonus_mes": 0.08,
    "bonus_mes_anterior": 0.04,
    "penalizacion_mes": 0.25,
    # Gastos que se pagan el mes siguiente al que cubren (cotizaciones): para ellos el mes
    # esperado en el documento es el anterior a la fecha de pago.
    "conceptos_mes_vencido": [],
}


def _contencion(doc: list[str], gasto: list[str]) -> float:
    """Fracción de palabras del nombre del documento que aparecen (aprox.) en el gasto."""
    if not doc:
        return 0.0
    def casa(t: str, g: str) -> bool:
        # igual, casi igual, o abreviatura (UBH -> UBH481E)
        return t == g or fuzz.ratio(t, g) >= 88 or (len(t) >= 3 and g.startswith(t))
    hits = sum(1 for t in doc if any(casa(t, g) for g in gasto))
    return hits / len(doc)


def similitud(texto_gasto: str, texto_doc: str, texto_descripcion: str = "") -> float:
    """Combina:
    - cuánto del nombre del documento está en el gasto completo (¿habla de lo mismo?)
    - cuánto de la descripción del gasto está en el documento (¿es específico o genérico?)
    - similitud global de conjuntos de palabras
    """
    if not texto_gasto or not texto_doc:
        return 0.0
    td = texto_doc.split()
    cont_doc = _contencion(td, texto_gasto.split())
    cont_desc = _contencion((texto_descripcion or texto_gasto).split(), td)
    conj = fuzz.token_set_ratio(texto_gasto, texto_doc) / 100
    return 0.35 * cont_doc + 0.50 * cont_desc + 0.15 * conj


_NOMBRE_MES = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
               "septiembre", "octubre", "noviembre", "diciembre"]


def _meses_legibles(meses: set[int]) -> str:
    return " y ".join(_NOMBRE_MES[m] for m in sorted(meses))


def _ref_legible(ref: str) -> str:
    """'EB#602' -> 'EB-602'; '#46681' -> '46681'"""
    prefijo, numero = ref.split("#", 1)
    return f"{prefijo}-{numero}" if prefijo else numero


def _frase_concepto(sim: float) -> str:
    if sim >= 0.8:
        return f"El concepto del archivo coincide con el del gasto ({sim:.0%})."
    if sim >= 0.5:
        return f"El concepto del archivo se parece al del gasto ({sim:.0%})."
    if sim >= 0.2:
        return f"El concepto del archivo se parece poco al del gasto ({sim:.0%})."
    return f"El concepto del archivo no se parece al del gasto ({sim:.0%})."


class Conciliador:
    def __init__(self, cfg_cruce: dict | None, cfg_referencias: dict, patron_prefijo: str | None,
                 sinonimos: dict | None = None):
        self.p = {**DEFECTOS, **(cfg_cruce or {})}
        self.re_ref = re.compile(cfg_referencias["patron"])
        self.num_largos = bool(cfg_referencias.get("numeros_largos", False))
        self.re_prefijo = re.compile(patron_prefijo) if patron_prefijo else None
        self.sin = preparar_sinonimos(sinonimos)
        self._docs_por_ref: dict[str, int] = {}
        self.mes_vencido = {texto_concepto(c, self.re_ref, self.sin) for c in self.p["conceptos_mes_vencido"]}

    # ---------- preparación ----------
    def ocultar_orden(self, nombre: str) -> str:
        """Quita el nº de orden manual del principio del nombre."""
        if self.re_prefijo is None:
            return nombre
        return self.re_prefijo.sub("", nombre, count=1).strip(" ._-")

    def preparar_documento(self, d: Documento) -> Documento:
        base = quitar_extension(self.ocultar_orden(d.nombre))
        d.nombre_cruce = base
        d.refs = extraer_referencias(base, self.re_ref, self.num_largos)
        d.meses = extraer_meses(base)
        d.texto = texto_concepto(base, self.re_ref, self.sin)
        return d

    def preparar_gasto(self, g: Gasto) -> Gasto:
        g.refs = extraer_referencias(f"{g.factura} {g.referencia_extra}", self.re_ref, self.num_largos)
        g.texto = texto_concepto(f"{g.descripcion} {g.proveedor} {g.factura}", self.re_ref, self.sin)
        g.texto_descripcion = texto_concepto(g.descripcion, self.re_ref, self.sin)
        return g

    def meses_gasto(self, g: Gasto) -> tuple[set[int], set[int]]:
        """(meses exactos, meses anteriores). Si la descripción nombra un mes, manda sobre la fecha."""
        exactos = extraer_meses(f"{g.descripcion} {g.factura}")
        if exactos:
            return exactos, set()
        if not g.fecha:
            return set(), set()
        m = g.fecha.month
        previo = 12 if m == 1 else m - 1
        if self.mes_vencido & set(g.texto.split()):
            return {previo}, {m}
        return {m}, {previo}

    # ---------- puntuación ----------
    def puntuar(self, g: Gasto, d: Documento) -> Candidato:
        """Puntúa el documento para el gasto. Los motivos son frases para el revisor."""
        p, motivos = self.p, []
        sim = similitud(g.texto, d.texto, g.texto_descripcion)

        puntos = 0.85 * sim
        comunes = g.refs & d.refs
        numeros = numeros_de(g.refs) & numeros_de(d.refs)
        if comunes:
            puntos += p["bonus_referencia"]
            refs_txt = ", ".join(_ref_legible(r) for r in sorted(comunes))
            # La referencia basta por sí sola solo si identifica un único documento; si la
            # comparten varios (p. ej. un comprobante de egreso con varias nóminas), decide el concepto.
            if any(self._docs_por_ref.get(r, 0) == 1 for r in comunes):
                puntos = max(puntos, p["minimo_referencia"])
                motivos.append(f"El nº de factura/comprobante {refs_txt} aparece en el nombre del archivo.")
            else:
                motivos.append(f"El nº {refs_txt} aparece en el nombre del archivo, pero también en otros "
                               f"documentos, así que se ha decidido por el concepto.")
        elif numeros:
            puntos += p["bonus_numero"]
            motivos.append(f"El número {', '.join(sorted(numeros))} aparece en el archivo, aunque con otro "
                           f"prefijo (posible errata o distinto tipo de comprobante).")
        elif g.refs and d.refs:
            puntos -= p["penalizacion_referencia"]
            motivos.append("El archivo lleva un nº de factura distinto al del gasto.")
        motivos.append(_frase_concepto(sim))

        if d.meses:
            exactos, anteriores = self.meses_gasto(g)
            mes_doc = _meses_legibles(d.meses)
            if d.meses & exactos:
                puntos += p["bonus_mes"]
                motivos.append(f"El mes del archivo ({mes_doc}) coincide con el del gasto.")
            elif d.meses & anteriores:
                puntos += p["bonus_mes_anterior"]
                motivos.append(f"El archivo es de {mes_doc}, un mes contiguo al del gasto (habitual en "
                               f"cotizaciones que se pagan al mes siguiente).")
            else:
                puntos -= p["penalizacion_mes"]
                motivos.append(f"El mes del archivo ({mes_doc}) no coincide con el del gasto "
                               f"({_meses_legibles(exactos) or 'sin fecha'}).")

        return Candidato(documento=d, puntuacion=round(puntos, 4), motivos=motivos)

    # ---------- cruce ----------
    def conciliar(self, gastos: list[Gasto], documentos: list[Documento]) -> list[Resultado]:
        docs = [self.preparar_documento(d) for d in documentos]
        # Cuántos documentos distintos (sin contar copias del mismo) llevan cada referencia
        firmas_por_ref: dict[str, set] = {}
        for d in docs:
            for r in d.refs:
                firmas_por_ref.setdefault(r, set()).add((d.texto, frozenset(d.meses)))
        self._docs_por_ref = {r: len(f) for r, f in firmas_por_ref.items()}
        resultados = []
        for g in gastos:
            self.preparar_gasto(g)
            cands = sorted((self.puntuar(g, d) for d in docs), key=lambda c: c.puntuacion, reverse=True)
            mejor = cands[0] if cands else None
            # El 2º candidato relevante es un documento distinto, no una copia del mismo
            # (p. ej. el .pdf y el .xlsx de la misma comisión bancaria)
            def firma(doc):
                return (doc.texto, frozenset(doc.meses), frozenset(doc.refs))
            segundo = next((c for c in cands[1:] if firma(c.documento) != firma(mejor.documento)), None) if mejor else None
            if mejor is None or mejor.puntuacion < self.p["umbral"]:
                resultados.append(Resultado(g, mejor, segundo, "sin_justificante", ""))
                continue
            ambiguo = bool(segundo and mejor.puntuacion - segundo.puntuacion < self.p["margen_ambiguo"])
            if ambiguo:
                confianza = "baja"
            elif mejor.puntuacion >= self.p["umbral_alta"]:
                confianza = "alta"
            else:
                confianza = "media"
            resultados.append(Resultado(g, mejor, segundo, "asignado", confianza, ambiguo))
        return resultados
