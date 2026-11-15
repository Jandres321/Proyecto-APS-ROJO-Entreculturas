"""Process 2b: partida suggestion by keywords (invented data) and the optional LLM layer."""

import asyncio

from app.llm.fake import FakeLLMClient
from app.pipeline.classify import classify_expense, llm_suggestions


def test_keywords_map_to_each_donor_partida():
    assert classify_expense("aecid", "gasolina moto 162 ABFO").partida == "A.I.7"
    assert classify_expense("xunta", "Combustible vehículo UBD 708K").partida == "A.VII"
    assert (
        classify_expense("aecid", "Salario marzo", source="CEPAG", currency="PYG").partida
        == "A.I.6.1"
    )
    assert classify_expense("xunta", "Seguro médico-NSSF Coordinadora").partida == "A.V.2"
    assert classify_expense("aecid", "Comisión bancaria").partida == "A.I.9"


def test_headquarters_staff_and_food_differ_by_donor():
    hq = classify_expense("aecid", "Nómina de enero", source="Entreculturas", currency="EUR")
    assert hq.partida == "A.I.6.3"
    partner = classify_expense("aecid", "Nómina", source="FYA (COFI Entreculturas)", currency="PYG")
    assert partner.partida == "A.I.6.1"
    assert classify_expense("xunta", "Refrigerios diálogo comunitario").partida == "A.VII"
    assert classify_expense("aecid", "Refrigerios taller").partida == "A.I.5"


def test_abstains_without_keywords_and_softens_debatable_categories():
    none = classify_expense("aecid", "Pago varios")
    assert none.partida is None and none.method == "sin_regla"
    cement = classify_expense("aecid", "Arena lavada, cemento, cal")
    assert cement.partida == "A.II.2.1" and cement.confidence < 0.75
    assert "cemento" in cement.reason


def test_llm_layer_parses_items():
    llm = FakeLLMClient(
        responses=[{"items": [{"id": 7, "partida": "A.I.3", "confidence": 0.8, "reason": "x"}]}]
    )
    out = asyncio.run(llm_suggestions(llm, "aecid", [], [{"id": 7, "concepto": "Taller"}]))
    assert out[7].partida == "A.I.3" and out[7].method == "llm"
    assert len(llm.calls) == 1
