"""Normalización de texto y extracción de referencias, meses y fechas."""
from __future__ import annotations

import re
import unicodedata

MESES = {
    "ene": 1, "enero": 1, "jan": 1, "january": 1,
    "feb": 2, "febrero": 2, "february": 2,
    "mar": 3, "marzo": 3, "march": 3,
    "abr": 4, "abril": 4, "apr": 4, "april": 4,
    "may": 5, "mayo": 5,
    "jun": 6, "junio": 6, "june": 6,
    "jul": 7, "julio": 7, "july": 7,
    "ago": 8, "agosto": 8, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "septiembre": 9, "september": 9,
    "oct": 10, "octubre": 10, "october": 10,
    "nov": 11, "noviembre": 11, "november": 11,
    "dic": 12, "diciembre": 12, "dec": 12, "december": 12,
}

STOPWORDS = {
    "de", "del", "la", "las", "el", "los", "y", "e", "en", "para", "por", "con", "a", "al", "un", "una",
    "sobre", "durante", "desde", "the", "of", "for", "and", "to", "in", "on",
    # ruido de nombres de archivo
    "excel", "pdf", "copia", "scan", "escaneado", "version", "def", "final",
}

_FECHA = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2}|\d{4})\b")
_EXTENSION = re.compile(r"\.(pdf|xlsx?|zip|docx?|jpe?g|png)$", re.IGNORECASE)


def sin_acentos(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def quitar_extension(nombre: str) -> str:
    return _EXTENSION.sub("", nombre)


def formato_importe(x: float) -> str:
    """1234.5 -> '1.234,50' (formato español)."""
    return f"{x:,.2f}".replace(",", "#").replace(".", ",").replace("#", ".")


def normalizar(s: str) -> str:
    """Minúsculas, sin acentos y solo letras/números separados por un espacio."""
    s = sin_acentos(str(s)).lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_NUM_LARGO = re.compile(r"(?<!\d)(\d{5,})(?!\d)")


def extraer_referencias(s: str, patron: re.Pattern, numeros_largos: bool = False) -> set[str]:
    """Claves de referencia normalizadas 'PREFIJO#numero' (sin ceros a la izquierda).

    Los números largos sueltos (nº de factura de 5+ cifras) se guardan como '#numero'.
    """
    s = sin_acentos(str(s)).upper()
    refs = set()
    for m in patron.finditer(s):
        prefijo = re.sub(r"[^A-Z0-9]", "", m.group("prefijo"))
        numero = re.sub(r"[^A-Z0-9]", "", m.group("numero"))
        numero = str(int(numero)) if numero.isdigit() else numero
        refs.add(f"{prefijo}#{numero}")
    if numeros_largos:
        refs |= {f"#{int(n)}" for n in _NUM_LARGO.findall(patron.sub(" ", s))}
    return refs


def numeros_de(refs: set[str]) -> set[str]:
    """{'EB#71', 'CV#119'} -> {'71', '119'}"""
    return {r.split("#", 1)[1] for r in refs}


def quitar_referencias(s: str, patron: re.Pattern) -> str:
    return patron.sub(" ", sin_acentos(str(s)).upper())


def raiz(tok: str) -> str:
    """Singular aproximado (es/en): salarios->salario, comisiones->comision, vehiculos->vehiculo."""
    if len(tok) > 5 and tok.endswith("es") and tok[-3] in "lnrdzj":
        return tok[:-2]       # materiales->material, examenes->examen, comisiones->comision
    if len(tok) > 4 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def extraer_meses(s: str) -> set[int]:
    """Meses mencionados por nombre (es/en) o dentro de una fecha dd.mm.aa."""
    meses = set()
    for d, m, _ in _FECHA.findall(s):
        if 1 <= int(m) <= 12 and 1 <= int(d) <= 31:
            meses.add(int(m))
    for tok in normalizar(_FECHA.sub(" ", s)).split():
        if tok in MESES:
            meses.add(MESES[tok])
    return meses


def texto_concepto(s: str, patron_ref: re.Pattern, sinonimos: dict[str, str] | None = None) -> str:
    """Texto para comparar conceptos: sin referencias, meses, fechas, números sueltos ni
    palabras vacías; en singular y con sinónimos del dominio unificados."""
    sinonimos = sinonimos or {}
    s = _FECHA.sub(" ", s)
    s = quitar_referencias(s, patron_ref)
    toks = []
    for t in normalizar(s).split():
        if t in MESES or t in STOPWORDS or t.isdigit() or len(t) < 2:
            continue
        t = raiz(t)
        toks.append(sinonimos.get(t, t))
    return " ".join(toks)


def preparar_sinonimos(cfg: dict | None) -> dict[str, str]:
    """{'salario': ['nomina', ...]} -> {'nomina': 'salario', ...}"""
    out = {}
    for canon, lista in (cfg or {}).items():
        for v in lista:
            out[raiz(normalizar(v))] = raiz(normalizar(canon))
    return out
