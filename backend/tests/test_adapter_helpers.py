"""Adapter logic that needs no real data."""

import datetime as dt
from decimal import Decimal

import pytest

from app.adapters import AecidAdapter, XuntaAdapter
from app.adapters import aecid as aecid_mod
from app.adapters import xunta as xunta_mod
from app.adapters.common import clean_text, find_sheet_name, normalize_order_key, to_decimal
from app.schemas import LimitScope


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("33", "33"),
        (" FA 236.3 ", "FA236.3"),
        ("CG18,1", "CG18.1"),
        ("fa236", "FA236"),
        ("10.A", "10.A"),
        ("49a", "49A"),
        ("33-39", "33-39"),
        ("N/A", None),
        ("n/a", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_order_key(raw, expected):
    assert normalize_order_key(raw) == expected


def test_clean_text_and_decimal():
    assert clean_text("\xa0\xa0  A.I.7 Viajes \n x ") == "A.I.7 Viajes x"
    assert clean_text(1.0) == "1"
    assert clean_text(20241875.0) == "20241875"
    assert clean_text("   ") is None
    assert to_decimal(77.9) == Decimal("77.9")
    assert to_decimal("1,5") == Decimal("1.5")
    assert to_decimal("#DIV/0!") is None
    assert to_decimal(None) is None
    assert to_decimal(True) is None


def test_find_sheet_name_is_tolerant():
    names = ["Hoja2", "X.B Listado de comprobantes ", "comparativa gastos"]
    assert find_sheet_name(names, "X.B Listado de comprobantes") == names[1]
    assert find_sheet_name(names, "Comparativa gastos") == names[2]
    with pytest.raises(KeyError):
        find_sheet_name(names, "nope")


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("A.I. Aluguer de inmobles//Alquiler de inmuebles", ("A.I", "Alquiler de inmuebles")),
        ("A.III.2. Consumibles", ("A.III.2", "Consumibles")),
        ("AV2. Local", ("A.V.2", "Local")),
        ("A.V. 3. En sede", ("A.V.3", "En sede")),
        ("A. III. Equipos e materiais//Equiposy materiales", ("A.III", "Equiposy materiales")),
        # The Comparativa sheet numbers A.V.1 = Local / A.V.2 = Expatriado; the name wins.
        ("A.V.1 Local", ("A.V.2", "Local")),
        ("A.V.2 Expatriado", ("A.V.1", "Expatriado")),
        ("B. CUSTOS INDIRECTOS// COSTES INDIRECTOS", ("B", "COSTES INDIRECTOS")),
        ("TOTAL XERAL//TOTAL GENERAL", (None, "TOTAL GENERAL")),
        (None, (None, "")),
    ],
)
def test_xunta_split_partida(label, expected):
    assert xunta_mod.split_partida(label) == expected


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        (
            "\xa0\xa0\xa0\xa0    A.I.6.3 Personal en Sede  (Excepto...)",
            ("A.I.6.3", "Personal en Sede (Excepto...)"),
        ),
        ("\xa0\xa0A.I 2. Auditorias", ("A.I.2", "Auditorias")),
        ("A.I.7. Viajes,alojamientos y dietas", ("A.I.7", "Viajes,alojamientos y dietas")),
        ("  A.II.2.1 Construcción y/o reforma", ("A.II.2.1", "Construcción y/o reforma")),
        ("B. COSTES INDIRECTOS", ("B", "COSTES INDIRECTOS")),
        ("A.I.6. TOTAL PERSONAL", ("A.I.6", "TOTAL PERSONAL")),
        ("TOTAL GENERAL", (None, "TOTAL GENERAL")),
        ("ONGD española", (None, "ONGD española")),
    ],
)
def test_aecid_split_partida(label, expected):
    assert aecid_mod.split_partida(label) == expected


def test_aecid_date_parsing():
    assert aecid_mod._parse_date(dt.datetime(2024, 3, 1)) == (dt.date(2024, 3, 1), None)
    assert aecid_mod._parse_date("30/9/2024") == (dt.date(2024, 9, 30), None)
    # Typo present in the real report: kept as raw text instead of guessing the year.
    assert aecid_mod._parse_date("30/9/224") == (None, "30/9/224")
    assert aecid_mod._parse_date(None) == (None, None)


def test_aecid_catalog_covers_report_partidas():
    catalog = {p.code: p for p in AecidAdapter().partida_catalog()}
    expected = {
        *("A.I.1", "A.I.2", "A.I.3", "A.I.4", "A.I.5"),
        *("A.I.6.1", "A.I.6.2", "A.I.6.3", "A.I.6.4"),
        *("A.I.7", "A.I.8", "A.I.9", "A.II.1", "A.II.2.1", "A.II.2.2", "A.II.3", "B"),
    }
    assert set(catalog) == expected
    for partida in catalog.values():
        assert partida.name
        assert len(partida.definition) > 40, partida.code
        assert partida.reference.startswith("Guía AECID")
    assert any("sedes administrativas" in x for x in catalog["A.I.4"].excludes)


def test_xunta_catalog_and_limits():
    adapter = XuntaAdapter()
    codes = {p.code for p in adapter.partida_catalog()}
    assert {"A.I", "A.II", "A.III.1", "A.III.2", "A.V.1", "A.V.2", "A.V.3", "A.X", "B"} <= codes
    limits = {limit.code: limit for limit in adapter.limits()}
    assert limits["XUNTA_PERSONAL_MAX_70"].max_pct == Decimal("0.70")
    assert limits["XUNTA_INDIRECTOS_MAX_5"].max_pct == Decimal("0.05")
    assert limits["XUNTA_FUNCIONAMIENTO_MAX_5"].partida_codes == ["A.VIII"]
    assert limits["XUNTA_AUDITORIA_MAX_2500"].max_amount_eur == Decimal("2500")
    assert limits["XUNTA_EVALUACION_MAX_5000"].max_amount_eur == Decimal("5000")
    assert limits["XUNTA_3_OFERTAS_OBRAS"].max_amount_eur == Decimal("40000")
    assert limits["XUNTA_3_OFERTAS_SUMINISTROS_SERVICIOS"].max_amount_eur == Decimal("15000")
    assert limits["XUNTA_EFECTIVO_MAX_1000"].scope == LimitScope.EXPENSE
    assert all(limit.reference for limit in limits.values())


def test_aecid_limits():
    (limit,) = AecidAdapter().limits()
    assert limit.partida_codes == ["B"]
    assert limit.max_pct == Decimal("0.12")
