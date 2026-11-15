"""File-name parsing of the matching step (pure functions, no data, no database)."""

import pytest

from app.enums import Donor
from app.models import Project
from app.pipeline.match import canonical_key, match_name, order_keys_from_filename, parse_filename
from app.pipeline.match_config import DONOR_DEFAULTS, get_match_config
from app.samples import PARAGUAY_MATCH_CONFIG, UGANDA_MATCH_CONFIG


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10.A", "10A"),
        ("10A", "10A"),
        ("69a", "69A"),
        ("FA236.3", "FA236.3"),  # a numeric sub-number keeps its dot
        ("CG18,1", "CG18.1"),
        (" FA 153 ", "FA153"),
        ("N/A", None),
        ("n/a", None),
        (None, None),
    ],
)
def test_canonical_key(raw, expected):
    assert canonical_key(raw) == expected


@pytest.mark.parametrize(
    ("filename", "keys"),
    [
        ("1. Alquiler sala formación prevención sustancias FP UGA04 108.pdf", ["1"]),
        ("12. Fondo pensiones Junio. EBUGA04-062.pdf", ["12"]),
        ("5 Material diálogos.pdf", ["5"]),
        ("50. 50_ Seguro vehículo UBH481E EB UGA04 419.pdf", ["50"]),
        ("51. Combustible Septiembre..pdf", ["51"]),
        ("33-39 Salario bruto Coordinador Regional MHPSS MM.pdf", [str(n) for n in range(33, 40)]),
        ("40 - 42 nóminas.pdf", ["40", "41", "42"]),
        ("69.A Combustible vehículo UBD 708 nov_Kla Vendor STM Nov_.pdf", ["69A"]),
        ("49A Estancia Coordinador Regional MHPSS_.pdf", ["49A"]),
        ("49.A Estancia Coordinador Regional MHPSS EB UGA04 372.pdf", ["49A"]),
        ("10.A Fondo Pensiones 5_ Conductor DB JUN_ EB UGA04 0052_.pdf", ["10A"]),
        ("77.A Combustible Dic_vehículo UBD 708K.pdf", ["77A"]),
        ("Copia de 12. Fondo pensiones.pdf", ["12"]),
        ("copia de 33-35 Salario.pdf", ["33", "34", "35"]),
        ("95. Comisión bancaria Septiembre 24 Excel.xlsx", ["95"]),
        # Not evidence of an order:
        ("2024_guia_ah-_galego-castellano.pdf", []),
        ("AnuncioG0759-260424-0004_es.pdf", []),
        ("Informe_economico_final_Adjuamni_def 14.10.25 v1.xls", []),
        ("2024-2025 resumen.pdf", []),
    ],
)
def test_xunta_filenames(filename, keys):
    assert order_keys_from_filename(filename, UGANDA_MATCH_CONFIG) == keys


def test_xunta_5_with_dot_and_word_is_not_a_suffix():
    # "5.Alquiler" is order 5, not "5A"+"lquiler".
    assert order_keys_from_filename("5.Alquiler sala.pdf", UGANDA_MATCH_CONFIG) == ["5"]


@pytest.mark.parametrize(
    ("filename", "keys", "subnumbers"),
    [
        ("ORDEN 14.PDF", ["CG14"], True),
        ("Orden N°113.PDF", ["CG113"], True),
        ("Orden Nº 176.pdf", ["CG176"], True),
        ("orden n.5.pdf", ["CG5"], True),
        ("153.pdf", ["FA153"], True),
        ("462.pdf", ["FA462"], True),
        ("163_PAGO YBY PORA 15.000.000.pdf", ["CG163"], True),  # overrides in config
        ("164_ PAGO YBY PORA 24.625.350.pdf", ["CG164"], True),
        ("Informe seguimiento y final_31_10.xlsx", [], False),
    ],
)
def test_aecid_filenames(filename, keys, subnumbers):
    ref = parse_filename(filename, PARAGUAY_MATCH_CONFIG)
    assert (list(ref.keys) if ref else []) == keys
    if ref:
        assert ref.subnumbers is subnumbers


def test_aecid_contract_matches_by_issuer_not_by_number():
    ref = parse_filename("CONTRATO YBY PORA (1).PDF", PARAGUAY_MATCH_CONFIG)
    assert ref is not None and not ref.keys
    assert ref.issuer_query == "Yvy Pora" and ref.min_score == 85


def test_prefix_rules_live_in_config_not_in_logic():
    """Same name, other project config -> other prefix: nothing is hardcoded in the parser."""
    custom = {
        "filename_rules": [
            {
                "name": "n",
                "kind": "number",
                "regex": r"^(?P<n>\d+)",
                "prefix": "ZZ",
                "prefix_by_number": {"7": "QQ"},
            }
        ]
    }
    assert order_keys_from_filename("5.pdf", custom) == ["ZZ5"]
    assert order_keys_from_filename("7.pdf", custom) == ["QQ7"]


def test_match_name_uses_archive_name_for_zip_members():
    path = "Proj/40-47 nóminas.zip::nóminas/7-EJEMPLO NOMINA JULIO.pdf"
    assert match_name(path, UGANDA_MATCH_CONFIG) == "40-47 nóminas.zip"
    assert match_name("Proj/1. Factura.pdf", UGANDA_MATCH_CONFIG) == "1. Factura.pdf"


def test_config_is_the_donor_default_with_the_project_overrides():
    plain = Project(name="x", donor=Donor.AECID, code="OTHER")
    assert get_match_config(plain) == DONOR_DEFAULTS[Donor.AECID]
    sample = Project(name="x", donor=Donor.AECID, match_config=PARAGUAY_MATCH_CONFIG)
    assert get_match_config(sample)["filename_rules"] == PARAGUAY_MATCH_CONFIG["filename_rules"]
    partial = Project(name="x", donor=Donor.XUNTA, match_config={"carpeta_completa": False})
    config = get_match_config(partial)
    assert config["carpeta_completa"] is False
    assert config["filename_rules"] == DONOR_DEFAULTS[Donor.XUNTA]["filename_rules"]
