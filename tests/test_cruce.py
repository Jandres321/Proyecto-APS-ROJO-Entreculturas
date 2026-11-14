from datetime import date

from conciliador.cruce import Conciliador
from conciliador.modelos import Documento, Gasto
from conciliador.validacion import clave_orden, ordenes_documento, validar

REFS = {"patron": r'(?<![A-Z])(?P<prefijo>[A-Z]{2})[\s\-_]*UGA\d{2}[\s\-_]*(?P<numero>\d+)'}
PREFIJO = r'^(?P<desde>\d+)(?:-(?P<hasta>\d+))?(?:\.?\s?(?P<sufijo>[A-Za-z])(?=[\s._]))?(?=[\s.])\.?'


def gasto(fila, desc, factura, fecha, orden=None):
    return Gasto(fila=fila, partida="", fecha=fecha, factura=factura, proveedor="", descripcion=desc,
                 importe_eur=1.0, orden=orden)


def doc(nombre):
    return Documento(id=nombre, nombre=nombre, ubicacion=nombre)


def conciliador():
    return Conciliador({}, REFS, PREFIJO, {"salario": ["nomina"]})


def test_oculta_el_numero_de_orden_manual():
    c = conciliador()
    assert c.ocultar_orden("10.A Fondo pensiones.pdf") == "Fondo pensiones.pdf"
    assert c.ocultar_orden("33-39 Salario bruto.pdf") == "Salario bruto.pdf"
    assert c.ocultar_orden("1. Alquiler sala.pdf") == "Alquiler sala.pdf"


def test_la_referencia_manda_sobre_el_orden():
    """Aunque los números de orden estén cambiados, el cruce sigue la referencia de factura."""
    docs = [doc("1. Material taller EB UGA04 600.pdf"), doc("2. Alquiler sala FP UGA04 108.pdf")]
    g = gasto(1, "Alquiler sala formación", "FP UGA04 108", date(2024, 10, 24), orden="1")
    (r,) = conciliador().conciliar([g], docs)
    assert r.estado == "asignado"
    assert r.mejor.documento.nombre.startswith("2.")


def test_el_mes_desempata_documentos_mensuales():
    docs = [doc("Salarios brutos Jul.pdf"), doc("Salarios brutos sept.pdf")]
    g = gasto(1, "Salario bruto conductor", "X", date(2024, 9, 27))
    (r,) = conciliador().conciliar([g], docs)
    assert r.mejor.documento.nombre == "Salarios brutos sept.pdf"


def test_sin_documento_adecuado_queda_sin_justificante():
    docs = [doc("Combustible vehículo.pdf")]
    g = gasto(1, "Gestoría", "B/165/2004", date(2024, 6, 21))
    (r,) = conciliador().conciliar([g], docs)
    assert r.estado == "sin_justificante"


def test_validacion_rangos_y_sufijos():
    import re
    pat = re.compile(PREFIJO)
    assert ordenes_documento("33-39 Salario.pdf", pat) == {str(n) for n in range(33, 40)}
    assert ordenes_documento("10.A Fondo.pdf", pat) == {"10A"}
    assert ordenes_documento("49A Estancia.pdf", pat) == {"49A"}
    assert ordenes_documento("2024_formulario.xlsx", pat) == set()
    assert clave_orden("10.A") == "10A" and clave_orden("N/A") is None


def test_validar_cuenta_aciertos():
    docs = [doc("1. Alquiler sala FP UGA04 108.pdf")]
    g = gasto(1, "Alquiler sala", "FP UGA04 108", date(2024, 10, 24), orden="1")
    res = conciliador().conciliar([g], docs)
    v = validar(res, docs, PREFIJO)
    assert v.metricas["correctos"] == 1 and v.metricas["exactitud"] == 1.0
