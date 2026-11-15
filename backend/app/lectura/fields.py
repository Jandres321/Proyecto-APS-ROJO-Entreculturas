"""Field extraction (regex heuristics) from the text of a segment.

Numeric parsing is always done here in code. Amount conventions: Paraguay writes `1.250.000`
(dot = thousands separator, comma = decimals), Uganda/UK style `1,250,000.50`.
"""

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.enums import TipoSegmento
from app.lectura.classify import normalize

# "dot_thousands" (Paraguay), "comma_thousands" (Uganda, UK, US) or "auto".
CONVENTIONS = ("dot_thousands", "comma_thousands", "auto")

_MONTHS = {
    **{
        m: i
        for i, m in enumerate(
            [
                "enero",
                "febrero",
                "marzo",
                "abril",
                "mayo",
                "junio",
                "julio",
                "agosto",
                "septiembre",
                "octubre",
                "noviembre",
                "diciembre",
            ],
            1,
        )
    },
    **{
        m: i
        for i, m in enumerate(
            [
                "january",
                "february",
                "march",
                "april",
                "may",
                "june",
                "july",
                "august",
                "september",
                "october",
                "november",
                "december",
            ],
            1,
        )
    },
    **{
        m: i
        for i, m in enumerate(
            [
                "janvier",
                "fevrier",
                "mars",
                "avril",
                "mai",
                "juin",
                "juillet",
                "aout",
                "septembre",
                "octobre",
                "novembre",
                "decembre",
            ],
            1,
        )
    },
    "setiembre": 9,
    "sept": 9,
    "sep": 9,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "oct": 10,
    "nov": 11,
    "dec": 12,
    "dic": 12,
    "ene": 1,
    "abr": 4,
    "ago": 8,
}
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_YEAR_MIN, _YEAR_MAX = 2015, 2030


# ---------------------------------------------------------------- amounts
_AMOUNT_RE = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:[.,\s]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)(?![\d])"
)
_TOTAL_LABELS = re.compile(
    r"total a pagar|grand total|total amount|amount due|importe total|total general|"
    r"monto total|total pagado|total paid|net pay|neto a (?:pagar|cobrar)|\btotal\b|"
    r"\bimporte\b|\bmonto\b|\bamount\b",
)
_STRONG_TOTAL = re.compile(
    r"total a pagar|grand total|total amount|amount due|importe total|monto total|total general"
)
_ID_LINE = re.compile(r"\bruc\b|\bnif\b|\btin\b|timbrado|\btel|\bcel\b|expdte|expediente|\bfax\b")


def parse_amount(raw: str, convention: str = "auto") -> Decimal | None:
    """'1.250.000' (dot_thousands) or '1,250.50' (comma_thousands) -> Decimal."""
    s = re.sub(r"(?<=\d)[·'’`](?=\d)", ".", raw.strip())
    s = re.sub(r"(?<=\d)\s(?=\d{3}\b)", "", s)
    if not re.fullmatch(r"[\d.,]+", s):
        return None
    if convention == "dot_thousands":
        s = s.replace(".", "").replace(",", ".")
    elif convention == "comma_thousands":
        s = s.replace(",", "")
    else:
        if "." in s and "," in s:
            decimal = "." if s.rfind(".") > s.rfind(",") else ","
            thousands = "," if decimal == "." else "."
            s = s.replace(thousands, "").replace(decimal, ".")
        elif "," in s or "." in s:
            sep = "," if "," in s else "."
            parts = s.split(sep)
            if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3):
                s = "".join(parts)  # thousands separator
            else:
                s = s.replace(sep, ".")
    try:
        value = Decimal(s)
    except InvalidOperation:
        return None
    return value if 0 < value < Decimal("1e12") else None


_PERCENT_NOISE = re.compile(r"\(?\s*\d{1,3}(?:[.,]\d+)?\s*%\s*\)?")
_DATE_NOISE_RE = re.compile(r"\d{1,2}\s*[/\-.]\s*\d{1,2}\s*[/\-.]\s*\d{2,4}")
MIN_PLAUSIBLE_TOTAL = Decimal(100)


def _strip_noise(line: str) -> str:
    """Remove percentages ('IVA 10%') and dates so they are not read as amounts."""
    return _DATE_NOISE_RE.sub(" ", _PERCENT_NOISE.sub(" ", line))


def _amounts_in(line: str, convention: str) -> list[Decimal]:
    out = []
    for m in _AMOUNT_RE.finditer(_strip_noise(line)):
        value = parse_amount(m.group(1), convention)
        if value is not None:
            out.append(value)
    return out


def find_total(texts: list[str], convention: str = "auto") -> tuple[Decimal | None, int | None]:
    """Total amount of a segment: (amount, 0-based index of the text where it was found).

    Prefers amounts on lines labelled 'total...'; later pages win (totals are at the end).
    Falls back to the largest separator-formatted amount when nothing is labelled.
    """
    labelled: list[tuple[int, int, Decimal, int]] = []  # (strength, page, amount, ...)
    fallback: list[tuple[Decimal, int]] = []
    for idx, text in enumerate(texts):
        lines = text.splitlines()
        for i, line in enumerate(lines):
            norm = normalize(line)
            if _ID_LINE.search(norm):
                continue
            window = [line] + ([lines[i + 1]] if i + 1 < len(lines) else [])
            if _TOTAL_LABELS.search(norm):
                strength = 2 if _STRONG_TOTAL.search(norm) else 1
                if "subtotal" in norm and "total" not in norm.replace("subtotal", ""):
                    continue
                for value in (v for w in window for v in _amounts_in(w, convention)):
                    labelled.append((strength, idx, value, i))
            for m in _AMOUNT_RE.finditer(_strip_noise(line)):
                if re.search(r"[.,·]", m.group(1)):  # spaces alone are often phone numbers
                    value = parse_amount(m.group(1), convention)
                    if value is not None:
                        fallback.append((value, idx))
    # Amounts below 100 are almost always percentages, units or OCR noise; they only count when
    # the line explicitly says "total a pagar" / "grand total" and nothing bigger was found.
    big = [c for c in labelled if c[2] >= MIN_PLAUSIBLE_TOTAL]
    unlabelled = [f for f in fallback if f[0] >= MIN_PLAUSIBLE_TOTAL]
    candidates = big or ([] if unlabelled else [c for c in labelled if c[0] == 2])
    if candidates:
        top = max(s for s, *_ in candidates)
        top_candidates = [c for c in candidates if c[0] == top]
        last_page = max(c[1] for c in top_candidates)
        best = max((c for c in top_candidates if c[1] == last_page), key=lambda c: c[2])
        return best[2], best[1]
    if unlabelled:
        value, idx = max(unlabelled, key=lambda f: f[0])
        return value, idx
    return None, None


_AMOUNT_WORDS = re.compile(
    r"total|amount|importe|monto|ugx|\bush\b|\bgs\b|paid|debit|credit|\bnet\b|price|cost|"
    r"payment|\bsum\b|pagado|precio|valor"
)
MAX_FIGURES = 40


def find_all_amounts(texts: list[str], convention: str = "auto") -> list[Decimal]:
    """Every plausible money figure of a segment, in reading order, without duplicates.

    OCR often picks the wrong figure as the 'total'; keeping them all lets the matching find an
    expense whose amount is printed anywhere in the document. A figure needs thousands/decimal
    separators, or (without them) at least 5 digits on a line that talks about money.
    """
    found: list[Decimal] = []
    for text in texts:
        for line in text.splitlines():
            norm = normalize(line)
            if _ID_LINE.search(norm):
                continue
            moneyish = bool(_AMOUNT_WORDS.search(norm))
            for m in _AMOUNT_RE.finditer(_strip_noise(line)):
                raw = m.group(1)
                if not re.search(r"[.,·]", raw) and not (moneyish and len(raw) >= 5):
                    continue
                value = parse_amount(raw, convention)
                if value is None or value < MIN_PLAUSIBLE_TOTAL or value in found:
                    continue
                found.append(value)
                if len(found) >= MAX_FIGURES:
                    return found
    return found


# ---------------------------------------------------------------- currency
_CURRENCIES = [
    ("PYG", r"\bgs\b|\bgs\.|guarani|₲|\bpyg\b"),
    ("UGX", r"\bugx\b|\bush\b|ugandan shilling|uganda shilling|\bshs?\b\.?\s*\d"),
    ("USD", r"us\s?\$|\busd\b|dolares|dollars?"),
    ("EUR", r"€|\beur\b|\beuros?\b"),
    ("XOF", r"\bfcfa\b|\bxof\b|\bxaf\b"),
]


def find_currency(text: str, default: str | None = None) -> str | None:
    norm = normalize(text)
    found = {code: len(re.findall(rx, norm)) for code, rx in _CURRENCIES}
    found = {c: n for c, n in found.items() if n}
    if not found:
        return default
    top = max(found, key=lambda c: found[c])
    if default and top != default and found[top] < 2 + found.get(default, 0):
        return default  # a stray "euros" in a stamp must not override the project currency
    return top


# ---------------------------------------------------------------- dates
_DATE_NUM = re.compile(r"(?<!\d)(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{4}|\d{2})(?!\d)")
_DATE_ISO = re.compile(r"(?<!\d)(\d{4})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{1,2})(?!\d)")
_DATE_TXT_DMY = re.compile(
    rf"(?<!\d)(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:de|of)?\s*({_MONTH_RE})\w*\.?,?\s*(?:de|of)?\s*(\d{{4}})",
)
_DATE_TXT_MDY = re.compile(rf"({_MONTH_RE})\w*\.?\s*(\d{{1,2}})(?:st|nd|rd|th)?,?\s*(\d{{4}})")
_DATE_LABEL = re.compile(r"fecha|date|emision|emitid|issued|dated|fait le|le \d")
_DATE_NOISE = re.compile(r"vigencia|vencimiento|validez|validity|expir|due date|nacimiento|birth")


def _make_date(y: int, m: int, d: int) -> dt.date | None:
    if y < 100:
        y += 2000
    try:
        date = dt.date(y, m, d)
    except ValueError:
        return None
    return date if _YEAR_MIN <= y <= _YEAR_MAX else None


def _dates_in(line: str) -> list[dt.date]:
    out: list[dt.date | None] = []
    norm = normalize(line)
    out += [_make_date(int(m[3]), int(m[2]), int(m[1])) for m in _DATE_NUM.finditer(norm)]
    out += [_make_date(int(m[1]), int(m[2]), int(m[3])) for m in _DATE_ISO.finditer(norm)]
    out += [_make_date(int(m[3]), _MONTHS[m[2]], int(m[1])) for m in _DATE_TXT_DMY.finditer(norm)]
    out += [_make_date(int(m[3]), _MONTHS[m[1]], int(m[2])) for m in _DATE_TXT_MDY.finditer(norm)]
    return [d for d in out if d]


def find_date(text: str) -> dt.date | None:
    """Issue date (day-first). Prefers lines labelled 'fecha'/'date'; skips validity dates."""
    plain: list[dt.date] = []
    for line in text.splitlines():
        if _DATE_NOISE.search(normalize(line)):
            continue
        dates = _dates_in(line)
        if not dates:
            continue
        if _DATE_LABEL.search(normalize(line)):
            return dates[0]
        plain += dates
    return plain[0] if plain else None


# ---------------------------------------------------------------- identifiers
_TAX_ID = re.compile(
    r"(?:\br\.?\s?u\.?\s?c\.?|\bnif\b|\bcif\b|\bnit\b|\btin\b|\bvat\s*(?:reg\.?\s*)?(?:no\.?|number)?|"
    r"tax\s*id)\s*(?:n[ºo°.]*\s*)?[:#\-.]?\s*"
    r"(\d[\d.\-]{3,14}\d|[A-Z]\d{7}[A-Z0-9]|\d{8}[A-Z])",
    re.IGNORECASE,
)
_SPANISH_NIF = re.compile(r"\b([A-Z]\d{7}[A-Z0-9]|\d{8}[A-Z])\b")


def find_tax_id(text: str) -> str | None:
    for m in _TAX_ID.finditer(text):
        raw = m.group(1).strip("-.")
        if len(re.findall(r"\d", raw)) >= 5:
            return raw[:20]
    m = _SPANISH_NIF.search(text)
    return m.group(1) if m else None


_INVOICE_PY = re.compile(r"(?<!\d)(\d{3})[-\s](\d{3})[-\s](\d{5,7})(?!\d)")
_INVOICE_LABEL = re.compile(
    r"(?:factura|invoice|facture|receipt|recibo|bill|inv|fact)\s*(?:n[ºo°.]*|no\.?|num(?:ero)?|#)?"
    r"\s*[:.\-]?\s*((?:[A-Z]{1,6}[\s\-]?){0,3}\d[\dA-Z\-/]{2,})",
    re.IGNORECASE,
)


def find_invoice_number(text: str) -> str | None:
    m = _INVOICE_PY.search(text)
    if m:
        return f"{m[1]}-{m[2]}-{m[3]}"
    for m in _INVOICE_LABEL.finditer(text):
        cand = re.sub(r"\s+", " ", m.group(1)).strip(" -")
        if len(re.findall(r"\d", cand)) < 3 or _DATE_NUM.fullmatch(cand):
            continue
        return cand[:40]
    return None


_ORDER_RE = re.compile(
    r"(?:n[ºo°]\.?\s*(?:de\s*)?orden|\borden\s*(?:de\s*pago\s*)?(?:n[ºo°]\.?|num\w*|#)?|"
    r"order\s*(?:no\.?|n[ºo°]|#)|voucher\s*(?:no\.?|#))\s*[:\-]?\s*"
    r"((?:CG|FA|EC)?\s*\d{1,4}(?:\.\d{1,2})?[A-Z]?)(?!\d)",
    re.IGNORECASE,
)


def find_order_number(text: str) -> str | None:
    for m in _ORDER_RE.finditer(text):
        value = re.sub(r"\s+", "", m.group(1)).upper()
        if re.search(r"\d", value):
            return value
    return None


# ---------------------------------------------------------------- donor stamp
_DONOR_NAME = re.compile(
    r"aecid|agencia espanola|xunta|galicia|cooperacion galega|gobierno de la rioja|\bechod?\b|"
    r"union europea|european union|financiado por|funded by"
)
# Spanish stamps ("Nº Expdte", "porcentaje de imputación") and English ones ("File number",
# "Awarded to", "Code") used by the local partners.
_STAMP_HINT = re.compile(
    r"imputaci|expdte|expte|expediente|\bexp\.|cofinanci|file number|file no|awarded to|"
    r"adjudicado a|porcentaje|percentage"
)
_EXPEDIENT_PATTERNS = (
    re.compile(
        r"(?:expdte|expte|exped\w*|\bexp|file\s*(?:number|no))\.?\s*[:\-.]?\s*"
        r"([0-9A-Z][A-Z0-9/\-.]{4,})",
        re.IGNORECASE,
    ),
    # the project code of the stamp ("Code: 24-UG-226") is the fallback identifier
    re.compile(r"\b(?:code|codigo|código)\s*[:\-]\s*([0-9A-Z][A-Z0-9/\-.]{4,})", re.IGNORECASE),
)
_PERCENT_NEAR = re.compile(
    r"(?:imputaci[oó]n|percentage|porcentaje)[^\n]{0,70}?(\d{1,3}(?:[.,]\d+)?)\s*%?",
    re.IGNORECASE,
)
STAMP_WINDOW = 300


@dataclass
class Stamp:
    present: bool
    expedient: str | None = None
    percent: float | None = None


def find_stamp(text: str) -> Stamp:
    """Donor stamp: a donor name with an imputation/expedient hint close to it."""
    norm = normalize(text)
    present = any(
        _STAMP_HINT.search(norm[max(0, m.start() - STAMP_WINDOW) : m.end() + STAMP_WINDOW])
        for m in _DONOR_NAME.finditer(norm)
    )
    if not present:
        return Stamp(False)
    expedient = None
    for pattern in _EXPEDIENT_PATTERNS:
        for m in pattern.finditer(text):
            if re.search(r"\d", m.group(1)):  # 'experiencia' must not count as an expedient
                expedient = m.group(1).strip(".-/")
                break
        if expedient:
            break
    percent = None
    m = _PERCENT_NEAR.search(text)
    if m:
        try:
            value = float(m.group(1).replace(",", "."))
        except ValueError:
            value = 0.0
        if 0 < value <= 100:
            percent = value
    return Stamp(True, expedient, percent)


# ---------------------------------------------------------------- payment proof
# Señales fuertes: por sí solas indican que el pago se hizo.
_PAYMENT = re.compile(
    r"pagado|cancelado|\bpaid\b|recibi conforme|received with thanks|cash received|"
    r"payment received|abonado|comprobante de pago|se cancela|recibimos de|"
    r"(?:justificante|comprobante|resguardo|recibo) de (?:la )?transferencia|"
    r"\bn(?:o|º|°|um|umero)?\.? ?de operacion|referencia bancaria|"
    r"transaction (?:reference|id)|\bmt ?103\b"
)
# Señales débiles: solo cuentan si no están en una cláusula de condiciones de pago
# («Forma de pago: transferencia a 30 días» no prueba que se haya pagado).
_PAYMENT_WEAK = re.compile(r"transferencia|cheque|efectivo")
_PAYMENT_TERMS = re.compile(
    r"(?:forma|condiciones|condicion|modo|metodo|medio)s? de pago|payment (?:terms|method)|"
    r"terms of payment|\ba \d+ dias\b|\b\d+ days\b|\bnet \d+\b|vencimiento|"
    r"datos bancarios|bank details|para (?:la )?transferencia|a pagar|due date"
)
_CLAUSE = re.compile(r"[\n;.]+")


def has_payment_proof(text: str, tipo: TipoSegmento) -> bool:
    if tipo in (TipoSegmento.TRANSFERENCIA, TipoSegmento.RECIBO):
        return True
    if _PAYMENT.search(normalize(text)):
        return True
    for clause in _CLAUSE.split(text or ""):
        norm = normalize(clause)
        if _PAYMENT_WEAK.search(norm) and not _PAYMENT_TERMS.search(norm):
            return True
    return False


# ------------------------------------------------- social-security contribution
_SOCIAL_SECURITY = re.compile(
    r"seguridad social|cotizaci|\bnssf\b|\bips\b|social security|pension fund|fondo de pensi|"
    r"aportes? (?:obrero|patronal)|seguro medico"
)


def has_social_security(text: str) -> bool:
    """The text talks about a social-security contribution (NSSF, IPS, cotización...)."""
    return bool(_SOCIAL_SECURITY.search(normalize(text)))


# ---------------------------------------------------------------- issuer
_COMPANY = re.compile(
    r"\b(?:s\.?a\.?|s\.?r\.?l\.?|s\.?l\.?|e\.?i\.?r\.?l\.?|ltd\.?|limited|sarl|inc\.?|"
    r"hotel|ferreteria|farmacia|supermercado|libreria|distribuidora|cooperativa|"
    r"construcciones|servicios|enterprises?|company|traders?|stationers?)\b",
    re.IGNORECASE,
)
_ISSUER_LABEL = re.compile(
    r"(?:proveedor|emisor|raz[oó]n social|vendor|supplier|received from|recibi de|"
    r"don\s+do[ñn]a|do[ñn]a|nombre y apellidos?)\s*[:\-]\s*(.+)",
    re.IGNORECASE,
)
_NOT_ISSUER = re.compile(
    r"factura|recibo|receipt|invoice|original|duplicado|fecha|timbrado|\bruc\b|\bnif\b|"
    r"vigencia|\bdate\b|\btel\b|www\.|@|\bpage\b|sello|expediente|cliente|customer|normas|"
    r"gestion|seguimiento|justificaci|contrato|comprobante|anexo|formulario|planilla|lista|"
    r"asistencia|liquidaci|cheque|boleta|nota de|guia|aplicaci",
    re.IGNORECASE,
)


def _clean_line(line: str) -> str:
    return re.sub(r"[^\w&.,:'\- ]", " ", line).strip()


def find_issuer(text: str) -> str | None:
    lines = [ln for ln in (_clean_line(x) for x in text.splitlines()[:25]) if ln]
    for line in lines:
        m = _ISSUER_LABEL.search(line)
        if m and len(m.group(1).strip()) >= 3:
            return re.sub(r"\s+", " ", m.group(1)).strip()[:120]
    for line in lines:
        if _COMPANY.search(line) and not _NOT_ISSUER.search(line) and 4 <= len(line) <= 80:
            return re.sub(r"\s+", " ", line)
    for line in lines:
        letters = sum(c.isalpha() for c in line)
        words = line.split()
        long_words = [w for w in words if sum(c.isalpha() for c in w) >= 4]
        if (
            letters >= 5
            and letters / len(line) > 0.7
            and long_words
            and len(long_words) * 2 >= len(words)  # mostly real words, not OCR noise
            and not _NOT_ISSUER.search(line)
        ):
            return re.sub(r"\s+", " ", line)[:80]
    return None


# ---------------------------------------------------------------- legibility
MIN_CONFIDENCE = 45.0
MIN_WORDS = 20


def is_legible(n_words: int, mean_confidence: float | None) -> bool:
    """Native text is always legible; OCR needs enough words read with enough confidence."""
    if mean_confidence is None:
        return n_words >= MIN_WORDS // 2
    return mean_confidence >= MIN_CONFIDENCE and n_words >= MIN_WORDS
