"""The "conciliador": proposes the document of each expense using ONLY file names.

Port of the standalone name-based reconciler (Proyecto APS ROJO), which is the first half of the
hybrid matching (`app/pipeline/match_hybrid.py`): the names propose, the content verifies.

The manual order number written in the file name ("12. Fondo pensiones.pdf") is HIDDEN before
comparing: it is the work a person already did, and it is what the matching is measured against.
Signals, per (expense, document):

- concept similarity: words of the file name vs. the expense text (concept + issuer + invoice no.);
- invoice / voucher reference written in the file name (pattern per project);
- month: the one named by the file against the one of the expense (or of its payment date).

Decision per expense: the best document is assigned when its score reaches ``threshold``.
Confidence is HIGH from ``threshold_high`` with no rival, MEDIUM below it, and LOW when another
DIFFERENT document (not a copy of the same one) is within ``ambiguous_margin``.
"""

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from rapidfuzz import fuzz

from app.enums import Confidence
from app.pipeline.match_content import ExpenseView

MONTHS = {
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
}  # fmt: skip
MONTH_NAMES_ES = [
    "", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
    "septiembre", "octubre", "noviembre", "diciembre",
]  # fmt: skip

STOPWORDS = {
    "de", "del", "la", "las", "el", "los", "y", "e", "en", "para", "por", "con", "a", "al", "un",
    "una", "sobre", "durante", "desde", "the", "of", "for", "and", "to", "in", "on",
    # noise of file names
    "excel", "pdf", "copia", "scan", "escaneado", "version", "def", "final",
}  # fmt: skip

DEFAULT_PARAMS: dict[str, Any] = {
    "threshold": 0.60,  # below: no document is proposed
    "threshold_high": 0.80,  # at or above (and no rival): HIGH confidence
    "ambiguous_margin": 0.05,  # another different document this close: LOW confidence
    "reference_bonus": 0.35,  # invoice reference (prefix and number) found in the file name
    "reference_minimum": 0.62,  # a reference that identifies ONE document is enough to assign
    "number_bonus": 0.20,  # only the number matches (CV UGA04-119 vs EB UGA04-119)
    "reference_penalty": 0.15,  # both carry a reference and they differ
    "month_bonus": 0.08,
    "previous_month_bonus": 0.04,
    "month_penalty": 0.25,
    # Expenses paid the month after the one they cover (social security): the file carries the
    # month BEFORE the payment date.
    "late_month_concepts": [],
}

_DATE = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2}|\d{4})\b")
_EXTENSION = re.compile(r"\.(pdf|xlsx?|zip|docx?|jpe?g|png)$", re.IGNORECASE)
_LONG_NUMBER = re.compile(r"(?<!\d)(\d{5,})(?!\d)")


# --- text helpers --------------------------------------------------------------------------


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def normalize(text: str) -> str:
    """Lower case, no accents, only letters / digits separated by one space."""
    text = re.sub(r"[^a-z0-9]+", " ", strip_accents(str(text)).lower())
    return re.sub(r"\s+", " ", text).strip()


def root(token: str) -> str:
    """Approximate singular (es / en): salarios -> salario, comisiones -> comision."""
    if len(token) > 5 and token.endswith("es") and token[-3] in "lnrdzj":
        return token[:-2]
    if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def extract_references(text: str, pattern: re.Pattern[str], long_numbers: bool = False) -> set[str]:
    """Normalized reference keys 'PREFIX#number' (no leading zeros). Loose numbers of 5+ digits
    (invoice numbers) are kept as '#number' when `long_numbers`."""
    text = strip_accents(str(text)).upper()
    refs = set()
    for m in pattern.finditer(text):
        prefix = re.sub(r"[^A-Z0-9]", "", m.group("prefijo"))
        number = re.sub(r"[^A-Z0-9]", "", m.group("numero"))
        number = str(int(number)) if number.isdigit() else number
        refs.add(f"{prefix}#{number}")
    if long_numbers:
        refs |= {f"#{int(n)}" for n in _LONG_NUMBER.findall(pattern.sub(" ", text))}
    return refs


def reference_numbers(refs: set[str]) -> set[str]:
    return {r.split("#", 1)[1] for r in refs}


def extract_months(text: str) -> set[int]:
    """Months named in the text (es / en) or inside a dd.mm.yy date."""
    months = {int(m) for d, m, _ in _DATE.findall(text) if 1 <= int(m) <= 12 and 1 <= int(d) <= 31}
    for token in normalize(_DATE.sub(" ", text)).split():
        if token in MONTHS:
            months.add(MONTHS[token])
    return months


def concept_text(text: str, pattern: re.Pattern[str], synonyms: dict[str, str]) -> str:
    """Text to compare concepts: no references, months, dates, loose numbers or stop words;
    singular, with the domain's synonyms unified."""
    text = pattern.sub(" ", strip_accents(_DATE.sub(" ", text)).upper())
    tokens = []
    for token in normalize(text).split():
        if token in MONTHS or token in STOPWORDS or token.isdigit() or len(token) < 2:
            continue
        token = root(token)
        tokens.append(synonyms.get(token, token))
    return " ".join(tokens)


def prepare_synonyms(config: dict[str, list[str]] | None) -> dict[str, str]:
    """{'salario': ['nomina', ...]} -> {'nomina': 'salario', ...}"""
    return {
        root(normalize(word)): root(normalize(canonical))
        for canonical, words in (config or {}).items()
        for word in words
    }


def _containment(doc: list[str], expense: list[str]) -> float:
    """Fraction of the document's words that appear (approximately) in the expense."""
    if not doc:
        return 0.0

    def matches(t: str, g: str) -> bool:
        # equal, almost equal, or an abbreviation (UBH -> UBH481E)
        return t == g or fuzz.ratio(t, g) >= 88 or (len(t) >= 3 and g.startswith(t))

    return sum(1 for t in doc if any(matches(t, g) for g in expense)) / len(doc)


def name_similarity(expense_text: str, doc_text: str, description_text: str = "") -> float:
    """0.35 * (document words in the expense) + 0.50 * (description words in the document, which
    rewards specific names over generic ones) + 0.15 * set similarity."""
    if not expense_text or not doc_text:
        return 0.0
    doc_tokens = doc_text.split()
    return (
        0.35 * _containment(doc_tokens, expense_text.split())
        + 0.50 * _containment((description_text or expense_text).split(), doc_tokens)
        + 0.15 * fuzz.token_set_ratio(expense_text, doc_text) / 100
    )


# --- prepared objects --------------------------------------------------------------------------


@dataclass
class NameDoc:
    doc_key: str  # sha256 of the file
    name: str  # as stored (file name)
    text: str = ""
    refs: set[str] = field(default_factory=set)
    months: set[int] = field(default_factory=set)

    @property
    def signature(self) -> tuple:
        """Copies / other versions of the same document share it (e.g. the .pdf and the .xlsx)."""
        return (self.text, frozenset(self.months), frozenset(self.refs))


@dataclass
class NameCandidate:
    doc: NameDoc
    score: float
    reasons: list[str]


@dataclass
class NameProposal:
    expense_id: int
    best: NameCandidate | None
    second: NameCandidate | None  # best candidate that is a DIFFERENT document
    assigned: bool  # the best one reached the threshold
    confidence: Confidence | None  # None when not assigned
    ambiguous: bool = False
    # When ambiguous: the best candidate plus every other DIFFERENT document within the margin
    # (the content may be able to choose among them).
    tied: list[NameCandidate] = field(default_factory=list)

    @property
    def doc_key(self) -> str | None:
        return self.best.doc.doc_key if self.assigned and self.best else None


@dataclass
class _Prepared:
    refs: set[str]
    text: str
    description_text: str
    exact_months: set[int]
    previous_months: set[int]


def _frase_concept(similarity: float) -> str:
    if similarity >= 0.8:
        return f"El concepto del archivo coincide con el del gasto ({similarity:.0%})."
    if similarity >= 0.5:
        return f"El concepto del archivo se parece al del gasto ({similarity:.0%})."
    if similarity >= 0.2:
        return f"El concepto del archivo se parece poco al del gasto ({similarity:.0%})."
    return f"El concepto del archivo no se parece al del gasto ({similarity:.0%})."


def _readable_reference(ref: str) -> str:
    prefix, number = ref.split("#", 1)
    return f"{prefix}-{number}" if prefix else number


def _readable_months(months: set[int]) -> str:
    return " y ".join(MONTH_NAMES_ES[m] for m in sorted(months))


class NameReconciler:
    """Configured from the project's ``name_reconciler`` section (see match_config.py):
    ``reference_pattern`` (regex with groups ``prefijo`` and ``numero``), ``long_numbers``,
    ``hide_order`` (regex of the manual order number at the start of the name), ``synonyms`` and
    ``params`` (overrides of DEFAULT_PARAMS)."""

    def __init__(self, config: dict[str, Any]):
        self.params = {**DEFAULT_PARAMS, **config.get("params", {})}
        self.reference_pattern = re.compile(config["reference_pattern"])
        self.long_numbers = bool(config.get("long_numbers", False))
        self.hide_order = re.compile(config["hide_order"]) if config.get("hide_order") else None
        self.synonyms = prepare_synonyms(config.get("synonyms"))
        self.late_months = {
            concept_text(c, self.reference_pattern, self.synonyms)
            for c in self.params["late_month_concepts"]
        }
        self._docs_per_ref: dict[str, int] = {}

    # -- preparation --

    def prepare_doc(self, doc_key: str, name: str) -> NameDoc:
        """`name` without the manual order number and without the extension."""
        base = PurePosixPath(name).name
        if self.hide_order is not None:
            base = self.hide_order.sub("", base, count=1).strip(" ._-")
        base = _EXTENSION.sub("", base)
        return NameDoc(
            doc_key=doc_key,
            name=name,
            text=concept_text(base, self.reference_pattern, self.synonyms),
            refs=extract_references(base, self.reference_pattern, self.long_numbers),
            months=extract_months(base),
        )

    def _prepare_expense(self, e: ExpenseView) -> _Prepared:
        invoice = e.invoice_no or ""
        concept = e.concept or ""
        text = concept_text(
            f"{concept} {e.issuer or ''} {invoice}", self.reference_pattern, self.synonyms
        )
        exact = extract_months(f"{concept} {invoice}")
        previous: set[int] = set()
        if not exact and e.date is not None:
            month = e.date.month
            before = 12 if month == 1 else month - 1
            if self.late_months & set(text.split()):
                exact, previous = {before}, {month}
            else:
                exact, previous = {month}, {before}
        return _Prepared(
            refs=extract_references(invoice, self.reference_pattern, self.long_numbers),
            text=text,
            description_text=concept_text(concept, self.reference_pattern, self.synonyms),
            exact_months=exact,
            previous_months=previous,
        )

    # -- scoring --

    def score(self, e: _Prepared, d: NameDoc) -> NameCandidate:
        p, reasons = self.params, []
        similarity = name_similarity(e.text, d.text, e.description_text)
        points = 0.85 * similarity
        common = e.refs & d.refs
        numbers = reference_numbers(e.refs) & reference_numbers(d.refs)
        if common:
            points += p["reference_bonus"]
            shown = ", ".join(_readable_reference(r) for r in sorted(common))
            # A reference is enough only when it identifies ONE document; if several share it
            # (a voucher covering several payrolls) the concept decides.
            if any(self._docs_per_ref.get(r, 0) == 1 for r in common):
                points = max(points, p["reference_minimum"])
                reasons.append(
                    f"El nº de factura/comprobante {shown} aparece en el nombre del archivo."
                )
            else:
                reasons.append(
                    f"El nº {shown} aparece en el nombre del archivo, pero también en otros "
                    "documentos, así que se ha decidido por el concepto."
                )
        elif numbers:
            points += p["number_bonus"]
            reasons.append(
                f"El número {', '.join(sorted(numbers))} aparece en el archivo, aunque con otro "
                "prefijo (posible errata o distinto tipo de comprobante)."
            )
        elif e.refs and d.refs:
            points -= p["reference_penalty"]
            reasons.append("El archivo lleva un nº de factura distinto al del gasto.")
        reasons.append(_frase_concept(similarity))

        if d.months:
            shown = _readable_months(d.months)
            if d.months & e.exact_months:
                points += p["month_bonus"]
                reasons.append(f"El mes del archivo ({shown}) coincide con el del gasto.")
            elif d.months & e.previous_months:
                points += p["previous_month_bonus"]
                reasons.append(
                    f"El archivo es de {shown}, un mes contiguo al del gasto (habitual en "
                    "cotizaciones que se pagan al mes siguiente)."
                )
            else:
                points -= p["month_penalty"]
                reasons.append(
                    f"El mes del archivo ({shown}) no coincide con el del gasto "
                    f"({_readable_months(e.exact_months) or 'sin fecha'})."
                )
        return NameCandidate(d, round(points, 4), reasons)

    # -- decision --

    def propose(
        self, expenses: Sequence[ExpenseView], names: dict[str, list[str]]
    ) -> dict[int, NameProposal]:
        """One proposal per expense. `names`: doc_key -> every file name of that document."""
        docs = [self.prepare_doc(key, name) for key, paths in names.items() for name in paths]
        signatures_per_ref: dict[str, set[tuple]] = {}
        for d in docs:
            for ref in d.refs:
                signatures_per_ref.setdefault(ref, set()).add((d.text, frozenset(d.months)))
        self._docs_per_ref = {ref: len(sigs) for ref, sigs in signatures_per_ref.items()}

        out: dict[int, NameProposal] = {}
        for e in expenses:
            prepared = self._prepare_expense(e)
            ranked = sorted((self.score(prepared, d) for d in docs), key=lambda c: -c.score)
            best = ranked[0] if ranked else None
            second = (
                next((c for c in ranked[1:] if c.doc.signature != best.doc.signature), None)
                if best
                else None
            )
            if best is None or best.score < self.params["threshold"]:
                out[e.id] = NameProposal(e.id, best, second, False, None)
                continue
            ambiguous = bool(second and best.score - second.score < self.params["ambiguous_margin"])
            if ambiguous:
                confidence = Confidence.LOW
            elif best.score >= self.params["threshold_high"]:
                confidence = Confidence.HIGH
            else:
                confidence = Confidence.MEDIUM
            tied: list[NameCandidate] = []
            if ambiguous:
                margin = self.params["ambiguous_margin"]
                seen: set[tuple] = set()
                for c in ranked:
                    if c.score < best.score - margin:
                        break
                    if c.doc.signature not in seen:
                        seen.add(c.doc.signature)
                        tied.append(c)
            out[e.id] = NameProposal(e.id, best, second, True, confidence, ambiguous, tied)
        return out
