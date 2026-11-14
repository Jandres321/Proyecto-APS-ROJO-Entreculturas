"""Per-project configuration of the document <-> expense matching.

Nothing donor- or project-specific lives in the matching logic: prefixes, number overrides,
files to ignore and expense types that need no receipt are all declared here. Add an entry to
`MATCH_CONFIG_BY_PROJECT` (keyed by `project.code`) to override the donor default.

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
_COPY = r"(?:copia\s+de\s+)?"

XUNTA_MATCH_CONFIG: MatchConfig = {
    # Names look like "Nº orden. Concepto Nº factura.pdf".
    "ignore_files": [r"^informe", r"^anuncio", r"^20\d\d_", r"^gu[ií]a"],
    "amount_convention": "comma_thousands",
    "default_currency": "UGX",
    "zip_members_use_archive_name": True,
    "no_document_partidas": ["B"],
    "carpeta_completa": True,
    "name_reconciler": {
        # Voucher numbers of JRS Uganda: EB UGA04 602, FP UGA04 108, CV UGA04-113, EBUGA04-060...
        "reference_pattern": (
            r"(?<![A-Z])(?P<prefijo>[A-Z]{2}(?:\s*CAM)?)"
            r"[\s\-_]*UGA\d{2}[\s\-_]*(?P<numero>\d+)"
        ),
        "long_numbers": True,  # any 5+ digit number is an invoice number (46681)
        # Manual order number at the start of the name: "1. ", "10.A ", "49A ", "33-39 "
        "hide_order": (
            r"^(?P<desde>\d+)(?:-(?P<hasta>\d+))?"
            r"(?:\.?\s?(?P<sufijo>[A-Za-z])(?=[\s._]))?(?=[\s.])\.?"
        ),
        "params": {"late_month_concepts": ["nssf", "pension", "seguro medico"]},
        "synonyms": {
            "salario": ["nomina", "payroll", "salary", "wage"],
            "seguro": ["insurance"],
            "combustible": ["fuel"],
            "vehiculo": ["motocicleta", "moto", "vehicle"],
            "refrigerio": ["aperitivo", "snack"],
            "comision": ["fee", "charge"],
        },
    },
    "filename_rules": [
        {
            "name": "range",  # "33-39 Salario bruto ..." -> orders 33..39
            "kind": "range",
            "regex": rf"^\s*{_COPY}(?P<a>\d+)\s*-\s*(?P<b>\d+)(?=\s|\.|$)",
            "max_number": 999,  # "2024-2025 ..." is a year range, not orders
        },
        {
            "name": "number_with_suffix",  # "69.A Combustible", "49A Estancia", "10.A Fondo"
            "kind": "number",
            "regex": rf"^\s*{_COPY}(?P<n>\d+)\.?(?P<suffix>[A-Za-z])(?=[\s._-]|$)",
        },
        {
            "name": "number",  # "12. Fondo pensiones", "5 Material"; not "2024_guia"
            "kind": "number",
            "regex": rf"^\s*{_COPY}(?P<n>\d+)(?=\s|\.|$)",
        },
    ],
}

AECID_MATCH_CONFIG: MatchConfig = {
    # Order numbers carry a prefix: CG = CEPAG, FA = Fe y Alegría, EC = Entreculturas.
    "ignore_files": [r"^informe"],
    "amount_convention": "dot_thousands",
    "default_currency": "PYG",
    "zip_members_use_archive_name": True,
    "no_document_partidas": ["B"],
    "carpeta_completa": False,  # the folder is a sample: 17 PDFs for 1.321 expenses
    "filename_rules": [
        {
            "name": "contract_yvy_pora",  # the contract backs every payment to that supplier
            "kind": "issuer",
            "regex": r"^\s*contrato\s+y[bv]y\s*pora",
            "issuer_query": "Yvy Pora",
            "min_score": 85,
        },
        {
            "name": "orden_cg",  # "ORDEN 14.PDF", "Orden N°113.PDF" -> CG14, CG113(.x)
            "kind": "number",
            "regex": r"^\s*orden\s*(?:n\s*[°º.]?\s*)?(?P<n>\d+)",
            "prefix": "CG",
            "subnumbers": True,
        },
        {
            "name": "number_only",  # "153.pdf" -> FA153(.x); 163_PAGO..., 164_... -> CG
            "kind": "number",
            "regex": r"^\s*(?P<n>\d+)(?=[\s._-]|$)",
            "prefix": "FA",
            "prefix_by_number": {"163": "CG", "164": "CG"},
            "subnumbers": True,
        },
    ],
}

# Explicit config per project (key = project.code); wins over the donor default.
MATCH_CONFIG_BY_PROJECT: dict[str, MatchConfig] = {
    "24-UG-226": XUNTA_MATCH_CONFIG,
    "22-PY-136": AECID_MATCH_CONFIG,
}

MATCH_CONFIG_BY_DONOR: dict[Donor, MatchConfig] = {
    Donor.XUNTA: XUNTA_MATCH_CONFIG,
    Donor.AECID: AECID_MATCH_CONFIG,
}


def get_match_config(project: Project) -> MatchConfig:
    """Config for a project: by project code, falling back to its donor's default."""
    if project.code and project.code in MATCH_CONFIG_BY_PROJECT:
        return MATCH_CONFIG_BY_PROJECT[project.code]
    return MATCH_CONFIG_BY_DONOR[project.donor]


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
      so its receipt belongs to another funder's justification.
    - ``normal``: a receipt is expected.
    Works on ORM ``Expense`` rows and on any object with the same attribute names.
    """
    if getattr(expense, "partida_code", None) in set(config.get("no_document_partidas", ())):
        return KIND_INDIRECT
    extra = getattr(expense, "extra", None) or {}
    donor_amount = getattr(expense, "amount_donor", None)
    if (donor_amount is not None and donor_amount == 0) or extra.get("pct_imputed") == 0:
        return KIND_COFINANCED
    return KIND_NORMAL


def document_required(expense: Any, config: MatchConfig) -> bool:
    return expense_kind(expense, config) == KIND_NORMAL
