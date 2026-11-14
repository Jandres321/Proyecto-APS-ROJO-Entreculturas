"""Document-type classification of a page by weighted keywords (es / en / fr)."""

import re
import unicodedata

from app.enums import TipoSegmento

# Order = tie-break priority.
_RULES: dict[TipoSegmento, list[tuple[str, float]]] = {
    TipoSegmento.FACTURA: [
        (r"\bfactura\b|\bfacture\b|\binvoice\b", 3),
        (r"\bproforma\b|\bpro forma\b|\bfactura electronica\b|\btax invoice\b", 1),
        (r"\btimbrado\b|\bsubtotal\b|\bi\.?v\.?a\.?\b|\bvat\b|\btva\b", 1),
        (r"\bruc\b|\bnif\b|\bcif\b|\btin\b|\bnit\b", 1),
        (r"\btotal a pagar\b|\bamount due\b|\bgrand total\b|\btotal ttc\b", 1),
    ],
    TipoSegmento.RECIBO: [
        (r"\brecibo\b|\breceipt\b|\brecu\b|\bcash receipt\b|\bacuse de recibo\b", 3),
        (r"\brecibi\b|\breceived from\b|\breceived with thanks\b|\bpayment received\b", 2),
        (r"\bpor concepto de\b|\bthe sum of\b|\bla suma de\b", 1),
    ],
    TipoSegmento.NOMINA: [
        (
            r"\bnomina\b|\bpayroll\b|\bpayslip\b|\bpay slip\b|\bbulletin de paie\b|"
            r"\bplanilla de (?:sueldos|salarios)\b",
            3,
        ),
        (r"\bsalario\b|\bsueldo\b|\bsalary\b|\bnet pay\b|\bgross pay\b|\bnssf\b|\bpaye\b", 1.5),
        (r"\bseguridad social\b|\bips\b|\bdeducciones\b|\bdeductions\b|\bhaberes\b", 1),
    ],
    TipoSegmento.TRANSFERENCIA: [
        (r"\btransferencia\b|\bbank transfer\b|\bswift\b|\bvirement\b|\bwire transfer\b", 3),
        (r"\biban\b|\bbeneficiari[oa]\b|\bordenante\b|\bremitter\b|\bsender\b", 1),
        (r"\bcomprobante de (?:pago|transferencia|deposito)\b|\bboleta de deposito\b", 2),
        (r"\bextracto\b|\bstatement\b|\bdebito\b|\bdebit\b|\bnro\.? de operacion\b", 1),
    ],
    TipoSegmento.LISTA_ASISTENCIA: [
        (r"\blista de asistencia\b|\battendance\b|\bregistro de asistencia\b", 3),
        (r"\bparticipantes\b|\bparticipants\b|\bparticipant name\b|\bplanilla de asistencia\b", 2),
        (r"\bfirma\b|\bsignature\b|\bnombre y apellidos?\b|\bnames?\b|\bc\.?i\.? n", 0.5),
    ],
    TipoSegmento.LIQUIDACION: [
        (r"\bliquidacion\b|\bsettlement\b|\bexpense (?:report|claim)\b|\brendicion\b", 3),
        (r"\bviaticos?\b|\bper diem\b|\bgastos de viaje\b|\btravel claim\b|\bdieta\b", 2),
    ],
    TipoSegmento.CONTRATO: [
        (r"\bcontrato\b|\bcontract\b|\bcontrat\b|\bagreement\b", 3),
        (r"\bclausula\b|\bcl[aá]usula\b|\blas partes\b|\bcontratante\b|\bthe parties\b", 1.5),
        (r"\bobjeto del contrato\b|\bduracion\b|\bhereby\b|\bwitnesseth\b", 1),
    ],
}

MIN_SCORE = 3.0
_HEAD_CHARS = 700  # a keyword in the title area weighs double


def normalize(text: str) -> str:
    """Lowercase and strip accents (keeps the character count, so offsets stay aligned)."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def scores(text: str) -> dict[TipoSegmento, float]:
    norm = normalize(text)
    head = norm[:_HEAD_CHARS]
    result: dict[TipoSegmento, float] = {}
    for tipo, rules in _RULES.items():
        total = 0.0
        for pattern, weight in rules:
            hits = len(re.findall(pattern, norm))
            if hits:
                total += weight * min(hits, 3) ** 0.5
                if re.search(pattern, head):
                    total += weight
        result[tipo] = total
    return result


def classify_scored(text: str) -> tuple[TipoSegmento, float]:
    """(type, score) of a page; OTRO when nothing reaches the minimum score."""
    ranked = scores(text)
    best = max(ranked, key=lambda t: ranked[t])  # dict order breaks ties
    if ranked[best] >= MIN_SCORE:
        return best, ranked[best]
    return TipoSegmento.OTRO, ranked[best]


def classify_page(text: str) -> TipoSegmento:
    return classify_scored(text)[0]


def is_proforma(text: str) -> bool:
    return bool(
        re.search(r"\bpro\s?forma\b|\bpresupuesto\b|\bquotation\b|\bcotizacion\b", normalize(text))
    )
