"""Sample projects shipped with the app (the real, already justified projects of the dataset).

They are ordinary rows of `project` (source `local`, folder under DATA_DIR) created on startup
when DATA_DIR holds their folder. Their project-specific matching config lives here, as data:
the matching code itself knows nothing about Uganda or Paraguay.
"""

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.enums import Donor, SourceType
from app.models import Project
from app.pipeline.match_config import (
    COPY_PREFIX,
    HIDE_ORDER_NUMBER,
    ORDER_NUMBER_RULES,
    MatchConfig,
)

# 24-UG-226 (Xunta, Uganda). Names look like "Nº orden. Concepto Nº factura.pdf".
UGANDA_MATCH_CONFIG: MatchConfig = {
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
        "hide_order": HIDE_ORDER_NUMBER,
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
    "filename_rules": ORDER_NUMBER_RULES,
}

# 22-PY-136 (AECID, Paraguay). Order numbers carry a prefix: CG = CEPAG, FA = Fe y Alegría,
# EC = Entreculturas.
PARAGUAY_MATCH_CONFIG: MatchConfig = {
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
            "regex": rf"^\s*{COPY_PREFIX}(?P<n>\d+)(?=[\s._-]|$)",
            "prefix": "FA",
            "prefix_by_number": {"163": "CG", "164": "CG"},
            "subnumbers": True,
        },
    ],
}


@dataclass(frozen=True)
class SampleProject:
    key: str
    name: str
    donor: Donor
    code: str
    folder: str  # relative to DATA_DIR
    report_file: str  # relative to `folder`
    currency: str
    match_config: dict[str, Any] = field(default_factory=dict)


SAMPLE_PROJECTS: tuple[SampleProject, ...] = (
    SampleProject(
        key="uganda-adjumani",
        name="Uganda · Adjumani · Xunta 2024",
        donor=Donor.XUNTA,
        code="24-UG-226",
        folder="DocJustificativa/24-UG-226 Adjumani - Xunta 2024",
        report_file="Informe_economico_final_Adjuamni_def 14.10.25 v1.xls",
        currency="UGX",
        match_config=UGANDA_MATCH_CONFIG,
    ),
    SampleProject(
        key="paraguay-aecid",
        name="Paraguay · AECID",
        donor=Donor.AECID,
        code="22-PY-136",
        folder="DocJustificativa/22-PY-136 - AECID",
        report_file="Informe seguimiento y final_31_10.xlsx",
        currency="PYG",
        match_config=PARAGUAY_MATCH_CONFIG,
    ),
)


def ensure_sample_projects(session: Session) -> list[Project]:
    """Create the sample projects whose folder exists under DATA_DIR (idempotent). An existing
    sample keeps its data and reviewer decisions; only its matching config is refreshed."""
    data_dir = get_settings().data_dir
    if data_dir is None or not data_dir.is_dir():
        return []
    created = []
    for sample in SAMPLE_PROJECTS:
        if not (data_dir / sample.folder).is_dir():
            continue
        project = session.scalar(select(Project).where(Project.key == sample.key))
        if project is None:
            # Earlier versions identified the pilots by code: adopt that row if it exists.
            project = session.scalar(
                select(Project).where(Project.code == sample.code, Project.is_sample.is_(False))
            )
        if project is None:
            project = Project(key=sample.key, name=sample.name, donor=sample.donor)
            session.add(project)
            created.append(project)
        project.key = sample.key
        project.name = sample.name
        project.donor = sample.donor
        project.code = sample.code
        project.currency = sample.currency
        project.source_type = SourceType.LOCAL
        project.folder = sample.folder
        project.report_file = sample.report_file
        project.match_config = sample.match_config
        project.is_sample = True
    session.commit()
    return created
