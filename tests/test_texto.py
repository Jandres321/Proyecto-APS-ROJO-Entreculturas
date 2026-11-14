import re

from conciliador.texto import (extraer_meses, extraer_referencias, normalizar, preparar_sinonimos,
                               raiz, texto_concepto)

PATRON = re.compile(r'(?<![A-Z])(?P<prefijo>[A-Z]{2}(?:\s*CAM)?)[\s\-_]*UGA\d{2}[\s\-_]*(?P<numero>\d+)')


def test_normalizar_quita_acentos_y_simbolos():
    assert normalizar("Comisión bancaria_Octubre-24") == "comision bancaria octubre 24"


def test_referencias_formatos_variados():
    assert extraer_referencias("EB UGA04 602", PATRON) == {"EB#602"}
    assert extraer_referencias("EBUGA04-060", PATRON) == {"EB#60"}
    assert extraer_referencias("Salarios agosto_EB UGA04071_2", PATRON) == {"EB#71"}
    assert extraer_referencias("EB-UGA04-0100", PATRON) == {"EB#100"}
    assert extraer_referencias("CV UGA04-113", PATRON) == {"CV#113"}


def test_numeros_largos_opcionales():
    assert extraer_referencias("Auditoría GAP 46681_", PATRON) == set()
    assert extraer_referencias("Auditoría GAP 46681_", PATRON, numeros_largos=True) == {"#46681"}


def test_meses_por_nombre_y_fecha():
    assert extraer_meses("Seguro médico julio NSSF") == {7}
    assert extraer_meses("Refrigerios 25.09.2024") == {9}
    assert extraer_meses("Factura agua Oct&Nov") == {10, 11}
    assert extraer_meses("Materiales de construcción") == set()


def test_raiz_singulariza():
    assert raiz("salarios") == "salario"
    assert raiz("comisiones") == "comision"
    assert raiz("vehiculos") == "vehiculo"


def test_texto_concepto_unifica_sinonimos_y_quita_ruido():
    sin = preparar_sinonimos({"salario": ["nomina"]})
    assert texto_concepto("Nóminas de junio EB UGA04 059", PATRON, sin) == "salario"
