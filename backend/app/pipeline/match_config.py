"""Per-project configuration of the document <-> expense matching.

Nothing donor- or project-specific lives in the matching logic: prefixes, number overrides,
files to ignore and expense types that need no receipt are configuration. Each donor has a
generic default here (`DONOR_DEFAULTS`); a project overrides any key of it with its own
`project.match_config` (stored in the database; the sample projects get theirs from
`app/samples.py`).

A config is a plain dict:

- ``ignore_files``: regexes (searched in the file name) of files that are not evidence
  (the donor report, guides, call text...).
- ``amount_convention`` / ``default_currency``: how amounts are written in this project's
  documents (``dot_thousands`` = "1.250.000", ``comma_thousands`` = "1,250,000.50", ``auto``) and
  the currency to assume when a document does not state one. Used when reading the PDFs.
- ``zip_members_use_archive_name``: files inside a .zip are matched by the .zip's name
  ("40-47 nóminas.zip" covers orders 40..47), because their own names follow another scheme.
- ``no_document_partidas``: partidas whose expenses need no individual receipt for this donor
  (indirect costs are accredited with a certificate / need no justification).
- ``carpeta_completa``: True when the folder holds a receipt for EVERY expense (Uganda); False
  when it is only a sample (Paraguay). With False an expense without document is reported as
  "no incluido en la muestra" (informative), not as a DOC_MISSING finding.
- ``name_reconciler``: settings of the name-based "conciliador" that proposes documents in the
  hybrid matching (app/pipeline/conciliador.py): ``reference_pattern`` (regex with groups
  ``prefijo`` / ``numero``), ``long_numbers``, ``hide_order`` (regex of the manual order number
  at the start of a file name, hidden before comparing), ``synonyms`` and ``params``. Without it
  the matching is content-only.
- ``filename_rules``: ordered rules, the first one that matches the file name (without
  extension) wins. Each rule has a ``regex`` (case-insensitive) and a ``kind``:

  * ``"number"``: group ``n`` is the order number, optional group ``suffix`` a letter suffix
    ("69.A" -> 69A). The key is ``prefix`` (or ``prefix_by_number[n]``) + number + suffix.
    ``subnumbers=True`` also covers sub-numbers of the order (FA236 -> FA236.1, FA236.2...).
  * ``"range"``: groups ``a`` and ``b``: orders a..b ("33-39 ..."), keys use ``prefix``;
    ``max_number`` rejects ranges above it (years).
  * ``"issuer"``: the document covers every expense whose issuer fuzzy-matches
    ``issuer_query`` (rapidfuzz partial ratio >= ``min_score``).
"""

from typing import Any

from app.enums import Donor
from app.models import Project

MatchConfig = dict[str, Any]

# "Copia de 12. Factura.pdf" is the same evidence as "12. Factura.pdf".
COPY_PREFIX = r"(?:copia\s+de\s+)?"

# Order numbers written at the start of a file name, the usual convention in Spanish public
# donors' justifications: "12. Factura", "69.A Combustible", "33-39 Nóminas".
ORDER_NUMBER_RULES: list[dict[str, Any]] = [
    {
        "name": "range",  # "33-39 Salario bruto ..." -> orders 33..39
        "kind": "range",
        "regex": rf"^\s*{COPY_PREFIX}(?P<a>\d+)\s*-\s*(?P<b>\d+)(?=\s|\.|$)",
        "max_number": 999,  # "2024-2025 ..." is a year range, not orders
    },
    {
        "name": "number_with_suffix",  # "69.A Combustible", "49A Estancia", "10.A Fondo"
        "kind": "number",
        "regex": rf"^\s*{COPY_PREFIX}(?P<n>\d+)\.?(?P<suffix>[A-Za-z])(?=[\s._-]|$)",
    },
    {
        "name": "number",  # "12. Fondo pensiones", "5 Material"; not "2024_guia"
        "kind": "number",
        "regex": rf"^\s*{COPY_PREFIX}(?P<n>\d+)(?=\s|\.|$)",
    },
]

# Manual order number at the start of a name ("1. ", "10.A ", "49A ", "33-39 "), hidden by the
# name-based conciliador before comparing (it is the work a person already did).
HIDE_ORDER_NUMBER = (
    r"^(?P<desde>\d+)(?:-(?P<hasta>\d+))?(?:\.?\s?(?P<sufijo>[A-Za-z])(?=[\s._]))?(?=[\s.])\.?"
)

# Words that name the same thing in Spanish / English justifications.
COMMON_SYNONYMS: dict[str, list[str]] = {
    "salario": ["nomina", "payroll", "salary", "wage"],
    "seguro": ["insurance"],
    "combustible": ["fuel", "gasolina", "diesel"],
    "vehiculo": ["motocicleta", "moto", "vehicle"],
    "refrigerio": ["aperitivo", "snack"],
    "comision": ["fee", "charge"],
}

_BASE: MatchConfig = {
    "ignore_files": [r"^informe", r"^anuncio", r"^gu[ií]a", r"^bases"],
    "amount_convention": "auto",
    "default_currency": None,
    "zip_members_use_archive_name": True,
    "no_document_partidas": ["B"],  # indirect costs: accredited with a certificate
    "carpeta_completa": True,
    "filename_rules": ORDER_NUMBER_RULES,
    # Name-based conciliador (proposes documents; the content verifies). Without a
    # `reference_pattern` it compares concept and month; projects add theirs.
    "name_reconciler": {
        "long_numbers": True,
        "hide_order": HIDE_ORDER_NUMBER,
        "synonyms": COMMON_SYNONYMS,
    },
}

# Generic default per donor: what any project of that donor gets unless it overrides a key.
DONOR_DEFAULTS: dict[Donor, MatchConfig] = {
    # Xunta justifies the whole project, also what other funders pay: an expense with nothing
    # imputed to the donor still needs its receipt.
    Donor.XUNTA: {**_BASE, "cofinanced_needs_document": True},
    Donor.AECID: {**_BASE},
}


def get_match_config(project: Project) -> MatchConfig:
    """The donor's default config with the project's overrides applied (top-level keys)."""
    return {**DONOR_DEFAULTS[project.donor], **(project.match_config or {})}


# --- classification of expenses that need no receipt in the donor folder ----------------

KIND_NORMAL = "normal"
KIND_INDIRECT = "indirecto"
KIND_COFINANCED = "cofinanciacion"

KIND_LABELS_ES = {
    KIND_NORMAL: "gasto normal",
    KIND_INDIRECT: "coste indirecto (se acredita con certificado)",
    KIND_COFINANCED: "cofinanciación (imputado a otros financiadores, nada al donante)",
}


def expense_kind(expense: Any, config: MatchConfig) -> str:
    """Why an expense may legitimately have no receipt in the donor's folder.

    - ``indirecto``: its partida is one of the config's ``no_document_partidas``.
    - ``cofinanciacion``: nothing is imputed to the donor (donor amount or imputed % is zero),
      so its receipt belongs to another funder's justification. Not for donors that justify the
      whole project (``cofinanced_needs_document``, Xunta).
    - ``normal``: a receipt is expected.
    Works on ORM ``Expense`` rows and on any object with the same attribute names.
    """
    if getattr(expense, "partida_code", None) in set(config.get("no_document_partidas", ())):
        return KIND_INDIRECT
    if config.get("cofinanced_needs_document"):
        return KIND_NORMAL
    extra = getattr(expense, "extra", None) or {}
    donor_amount = getattr(expense, "amount_donor", None)
    if (donor_amount is not None and donor_amount == 0) or extra.get("pct_imputed") == 0:
        return KIND_COFINANCED
    return KIND_NORMAL


def document_required(expense: Any, config: MatchConfig) -> bool:
    return expense_kind(expense, config) == KIND_NORMAL
