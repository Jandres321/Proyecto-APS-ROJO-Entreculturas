"""Motor de cruce gasto -> documento.

No usa el nº de orden manual: lo elimina del nombre del archivo antes de comparar.
Señales:
  - referencia de factura/comprobante presente en el nombre del archivo
  - similitud de concepto (palabras del nombre del archivo contenidas en el gasto)
  - coherencia del mes (el del texto del gasto o, si no hay, el de su fecha)
  - (opcional) datos leídos del contenido del documento: importe, fecha, proveedor y nº de factura.
    Solo cuentan en los documentos que se han leído (ver lectura.py); sin lectura, el cruce
    es exactamente el de los nombres de archivo.
"""
from __future__ import annotations

import re
from datetime import date

from rapidfuzz import fuzz

from .modelos import Candidato, Documento, Gasto, Resultado
from .texto import (extraer_meses, extraer_referencias, normalizar, numeros_de, preparar_sinonimos,
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

# Señales del contenido leído (sección `contenido:` del YAML). Sin calibrar con datos reales.
DEFECTOS_CONTENIDO = {
    "bonus_importe": 0.30,             # el importe del gasto coincide con el total del documento
    "tolerancia_importe": 0.01,        # diferencia relativa admitida (1 %)
    "bonus_factura": 0.35,             # el nº de factura del gasto es el que se lee en el documento
    "penalizacion_factura": 0.15,      # en el documento se lee otro nº de factura
    "bonus_fecha": 0.10,               # la fecha del documento está a pocos días de la del gasto
    "dias_fecha": 3,
    "bonus_proveedor": 0.10,           # el proveedor del documento se parece al del gasto
    "similitud_proveedor": 80,         # parecido mínimo (0-100)
    "candidatos": 3,                   # documentos candidatos que se leen por cada gasto dudoso
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


def _a_fecha(texto: str) -> date | None:
    try:
        return date.fromisoformat((texto or "")[:10])
    except ValueError:
        return None


def _clave_factura(texto: str) -> str:
    """Nº de factura comparable: solo letras y números, en mayúsculas. Vacío si no lleva cifras."""
    clave = re.sub(r"[^A-Z0-9]", "", (texto or "").upper())
    return clave if len(clave) >= 3 and any(c.isdigit() for c in clave) else ""


def _misma_factura(a: str, b: str) -> bool:
    corta, larga = sorted((a, b), key=len)
    return a == b or (len(corta) >= 4 and corta in larga)


def _importe_legible(x: float) -> str:
    return f"{x:,.2f}".replace(",", "#").replace(".", ",").replace("#", ".")


class Conciliador:
    def __init__(self, cfg_cruce: dict | None, cfg_referencias: dict, patron_prefijo: str | None,
                 sinonimos: dict | None = None, partidas_indirectos: str | None = None,
                 cfg_contenido: dict | None = None):
        self.p = {**DEFECTOS, **(cfg_cruce or {})}
        self.pc = {**DEFECTOS_CONTENIDO,
                   **{k: v for k, v in (cfg_contenido or {}).items() if k in DEFECTOS_CONTENIDO}}
        self._docs: list[Documento] = []
        self.re_ref = re.compile(cfg_referencias["patron"])
        self.num_largos = bool(cfg_referencias.get("numeros_largos", False))
        self.re_prefijo = re.compile(patron_prefijo) if patron_prefijo else None
        self.sin = preparar_sinonimos(sinonimos)
        self._docs_por_ref: dict[str, int] = {}
        self.re_indirectos = re.compile(partidas_indirectos, re.IGNORECASE) if partidas_indirectos else None
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

        extra, coincidencias, contradice = self._senales_contenido(g, d, motivos)
        return Candidato(documento=d, puntuacion=round(puntos + extra, 4), motivos=motivos,
                         puntos_contenido=extra, coincidencias=coincidencias, contradice=contradice)

    def _senales_contenido(self, g: Gasto, d: Documento, motivos: list[str]) -> tuple[float, int, bool]:
        """Puntos por lo leído DENTRO del documento. Devuelve (puntos, nº de coincidencias, contradice).

        Un importe distinto no resta: un mismo documento puede cubrir varios gastos.
        """
        c, pc = d.contenido, self.pc
        if not c:
            return 0.0, 0, False
        if not c.get("legible"):
            motivos.append("Se ha abierto el documento, pero no se lee lo bastante para comprobar sus datos.")
            return 0.0, 0, False
        puntos, coinciden, comparables = 0.0, 0, 0

        # Importe: en moneda local si el listado la tiene; en euros si el documento está en euros
        total, moneda = c.get("importe_total"), c.get("moneda") or ""
        del_gasto = [x for x in (g.importe_local, g.importe_eur if moneda in ("", "EUR") else None) if x]
        if total and del_gasto:
            comparables += 1
            leido = f"{_importe_legible(total)} {moneda}".strip()
            if any(abs(total - x) <= pc["tolerancia_importe"] * abs(x) for x in del_gasto):
                puntos += pc["bonus_importe"]
                coinciden += 1
                motivos.append(f"Dentro del documento, el importe total ({leido}) coincide con el del gasto.")
            else:
                motivos.append(f"Dentro del documento, el importe total ({leido}) no es el del gasto "
                               f"(puede cubrir varios gastos).")

        # Nº de factura
        fac_gasto, fac_doc = _clave_factura(g.factura), _clave_factura(c.get("num_factura"))
        otra_factura = False
        if fac_gasto and fac_doc:
            if _misma_factura(fac_gasto, fac_doc):
                puntos += pc["bonus_factura"]
                coinciden += 1
                motivos.append(f"Dentro del documento se lee el nº de factura del gasto ({c['num_factura']}).")
            else:
                puntos -= pc["penalizacion_factura"]
                otra_factura = True
                motivos.append(f"Dentro del documento se lee otro nº de factura ({c['num_factura']}).")

        # Fecha
        fecha_doc = _a_fecha(c.get("fecha"))
        if fecha_doc and g.fecha:
            comparables += 1
            dias = abs((fecha_doc - g.fecha).days)
            if dias <= pc["dias_fecha"]:
                puntos += pc["bonus_fecha"]
                coinciden += 1
                motivos.append(f"La fecha del documento ({fecha_doc:%d/%m/%Y}) coincide con la del gasto"
                               + ("." if dias == 0 else f" (±{dias} días)."))
            else:
                motivos.append(f"La fecha del documento ({fecha_doc:%d/%m/%Y}) no coincide con la del gasto.")

        # Proveedor
        prov_gasto, prov_doc = normalizar(g.proveedor), normalizar(c.get("proveedor") or "")
        if prov_gasto and prov_doc:
            comparables += 1
            if fuzz.token_set_ratio(prov_gasto, prov_doc) >= pc["similitud_proveedor"]:
                puntos += pc["bonus_proveedor"]
                coinciden += 1
                motivos.append(f"El proveedor del documento ({c['proveedor']}) es el del gasto.")
            else:
                motivos.append(f"El proveedor del documento ({c['proveedor']}) no parece el del gasto.")

        if c.get("manuscrito"):
            motivos.append("El documento está escrito a mano: la lectura es menos fiable.")
        # Contradice: lleva otro nº de factura, o había al menos dos datos comparables y no coincide ninguno
        contradice = otra_factura or (comparables >= 2 and coinciden == 0)
        return round(puntos, 4), coinciden, contradice

    def clasificar_sin_justificante(self, g: Gasto) -> str:
        """Por qué un gasto puede no tener documento en la carpeta del donante."""
        # Imputado entero a otros financiadores: cofinanciación, no se presenta al donante
        if (g.imputado_otros or 0) > 0 and not (g.imputado_donante or 0):
            return "cofinanciacion"
        # Costes indirectos: se suelen justificar con un certificado de la entidad
        if self.re_indirectos and self.re_indirectos.search(g.partida):
            return "indirecto"
        return "revisar"

    # ---------- cruce ----------
    @staticmethod
    def _firma(doc: Documento):
        return (doc.texto, frozenset(doc.meses), frozenset(doc.refs))

    def _candidatos(self, g: Gasto) -> list[Candidato]:
        return sorted((self.puntuar(g, d) for d in self._docs), key=lambda c: c.puntuacion, reverse=True)

    def _decidir(self, g: Gasto) -> Resultado:
        cands = self._candidatos(g)
        mejor = cands[0] if cands else None
        # El 2º candidato relevante es un documento distinto, no una copia del mismo
        # (p. ej. el .pdf y el .xlsx de la misma comisión bancaria)
        segundo = next((c for c in cands[1:] if self._firma(c.documento) != self._firma(mejor.documento)),
                       None) if mejor else None
        if mejor is None or mejor.puntuacion < self.p["umbral"]:
            return Resultado(g, mejor, segundo, "sin_justificante", "",
                             clasificacion=self.clasificar_sin_justificante(g))
        ambiguo = bool(segundo and mejor.puntuacion - segundo.puntuacion < self.p["margen_ambiguo"])
        aviso = ""
        # Solo con contenido leído: ¿apuntan el nombre y el contenido a documentos distintos?
        por_nombre = max(cands, key=lambda c: c.puntuacion - c.puntos_contenido)
        nombre_apunta_a_otro = (self._firma(por_nombre.documento) != self._firma(mejor.documento)
                                and por_nombre.puntuacion - por_nombre.puntos_contenido >= self.p["umbral"])
        if mejor.puntos_contenido > 0 and nombre_apunta_a_otro:
            ambiguo = True
            aviso = (f"Ojo: por el nombre del archivo el justificante sería «{por_nombre.documento.nombre}», "
                     f"pero lo que se lee dentro de «{mejor.documento.nombre}» encaja mejor con el gasto. "
                     f"Revisar los dos (puede haber un archivo mal nombrado).")
        elif mejor.contradice:
            ambiguo = True
            aviso = ("Ojo: el nombre del archivo encaja con el gasto, pero lo que se lee dentro del "
                     "documento no. Revisar si el archivo está mal nombrado.")
        if ambiguo:
            confianza = "baja"
        elif mejor.puntuacion >= self.p["umbral_alta"]:
            confianza = "alta"
        else:
            confianza = "media"
        return Resultado(g, mejor, segundo, "asignado", confianza, ambiguo, aviso=aviso)

    def conciliar(self, gastos: list[Gasto], documentos: list[Documento]) -> list[Resultado]:
        docs = self._docs = [self.preparar_documento(d) for d in documentos]
        # Cuántos documentos distintos (sin contar copias del mismo) llevan cada referencia
        firmas_por_ref: dict[str, set] = {}
        for d in docs:
            for r in d.refs:
                firmas_por_ref.setdefault(r, set()).add((d.texto, frozenset(d.meses)))
        self._docs_por_ref = {r: len(f) for r, f in firmas_por_ref.items()}
        return [self._decidir(self.preparar_gasto(g)) for g in gastos]

    # ---------- contenido (opcional) ----------
    @staticmethod
    def _dudoso(r: Resultado) -> bool:
        """Gastos que merece la pena comprobar leyendo documentos."""
        return r.confianza == "baja" if r.estado == "asignado" else r.clasificacion == "revisar"

    def documentos_a_leer(self, resultados: list[Resultado]) -> list[Documento]:
        """Documentos cuyo contenido conviene leer, por orden de prioridad y sin repetir:
        1) candidatos de los gastos con confianza baja, 2) candidatos de los gastos sin
        justificante (a revisar), 3) documentos que no se han asignado a ningún gasto."""
        elegidos: dict[str, Documento] = {}
        n = int(self.pc["candidatos"])
        for grupo in ("asignado", "sin_justificante"):
            for r in resultados:
                if r.estado == grupo and self._dudoso(r):
                    for c in self._candidatos(r.gasto)[:n]:
                        elegidos.setdefault(c.documento.id, c.documento)
        asignados = {r.mejor.documento.id for r in resultados if r.estado == "asignado"}
        for d in self._docs:
            if d.id not in asignados:
                elegidos.setdefault(d.id, d)
        return list(elegidos.values())

    def revisar_con_contenido(self, resultados: list[Resultado]) -> list[Resultado]:
        """Vuelve a decidir los gastos dudosos, ahora con lo leído en los documentos.
        El resto de gastos se queda como estaba."""
        def describir(r: Resultado) -> str:
            if r.estado != "asignado":
                return "sin justificante"
            return f"«{r.mejor.documento.nombre}» con confianza {r.confianza}"
        salida = []
        for r in resultados:
            if not self._dudoso(r):
                salida.append(r)
                continue
            nuevo = self._decidir(r.gasto)
            if describir(nuevo) != describir(r):
                nuevo.cambio = f"Solo por el nombre del archivo el resultado era: {describir(r)}."
            salida.append(nuevo)
        return salida
