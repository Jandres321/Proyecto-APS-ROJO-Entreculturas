"""Lectura del contenido: siempre con un cliente simulado y datos inventados (no gasta API)."""
import json
from types import SimpleNamespace

import pytest

from conciliador import lectura
from conciliador.lectura import Lector, leer_documento, leer_documentos, resumen_contenido, se_puede_leer
from conciliador.modelos import Documento

DATOS = {"tipo": "factura", "importe_total": 1500.0, "moneda": "UGX", "fecha": "2024-05-10",
         "proveedor": "Papelería Inventada", "num_factura": "F-0042", "manuscrito": False, "legible": True}


class ClienteFalso:
    """Imita `cliente.messages.create` y guarda las peticiones recibidas."""

    def __init__(self, datos=DATOS, stop_reason="end_turn", texto=None):
        self.peticiones = []
        self._texto = json.dumps(datos) if texto is None else texto
        self._stop = stop_reason
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.peticiones.append(kw)
        return SimpleNamespace(stop_reason=self._stop, content=[SimpleNamespace(type="text", text=self._texto)])


def imagen(tmp_path, nombre="recibo.jpg", contenido=b"imagen inventada"):
    ruta = tmp_path / nombre
    ruta.write_bytes(contenido)
    return ruta


def documento(ruta):
    return Documento(id=ruta.name, nombre=ruta.name, ubicacion=str(ruta))


def test_devuelve_los_campos_y_hace_una_sola_llamada(tmp_path):
    cliente = ClienteFalso()
    datos = leer_documento(imagen(tmp_path), cliente=cliente, dir_cache=tmp_path / "cache")
    assert datos == DATOS
    (peticion,) = cliente.peticiones
    assert peticion["model"] == "claude-sonnet-5-5"
    assert peticion["output_config"]["format"]["schema"]["required"] == lectura.CAMPOS
    assert "tool_choice" not in peticion          # el modelo no admite forzar una tool
    tipos = [b["type"] for b in peticion["messages"][0]["content"]]
    assert tipos == ["image", "text"]


def test_la_cache_evita_la_segunda_llamada(tmp_path):
    cliente, ruta, cache = ClienteFalso(), imagen(tmp_path), tmp_path / "cache"
    leer_documento(ruta, cliente=cliente, dir_cache=cache)
    assert leer_documento(ruta, cliente=cliente, dir_cache=cache) == DATOS
    assert len(cliente.peticiones) == 1
    assert len(list(cache.glob("*.json"))) == 1


def test_la_cache_depende_del_contenido_del_archivo(tmp_path):
    cliente, cache = ClienteFalso(), tmp_path / "cache"
    leer_documento(imagen(tmp_path, "a.jpg", b"uno"), cliente=cliente, dir_cache=cache)
    leer_documento(imagen(tmp_path, "copia de a.jpg", b"uno"), cliente=cliente, dir_cache=cache)   # mismo archivo
    leer_documento(imagen(tmp_path, "b.jpg", b"dos"), cliente=cliente, dir_cache=cache)
    assert len(cliente.peticiones) == 2


def test_normaliza_respuestas_incompletas(tmp_path):
    cliente = ClienteFalso({"tipo": "albarán", "importe_total": "mil", "moneda": "eur", "legible": True})
    datos = leer_documento(imagen(tmp_path), cliente=cliente, dir_cache=tmp_path / "cache")
    assert datos == {"tipo": "otro", "importe_total": None, "moneda": "EUR", "fecha": "", "proveedor": "",
                     "num_factura": "", "manuscrito": False, "legible": True}


def test_tope_de_llamadas_y_cache(tmp_path):
    docs = [documento(imagen(tmp_path, f"doc{i}.jpg", f"contenido {i}".encode())) for i in range(3)]
    cliente = ClienteFalso()
    lector = Lector(cliente, dir_cache=tmp_path / "cache")
    r = leer_documentos(docs, lector, max_llamadas=2)
    assert (r["leidos"], r["llamadas"], r["sin_leer_por_tope"]) == (2, 2, 1)
    assert [d.lectura for d in docs] == ["leido", "leido", "no leído: tope de llamadas"]
    assert docs[2].contenido is None
    # Segunda ejecución: los dos ya leídos salen de la caché y el tope alcanza para el tercero
    docs2 = [documento(tmp_path / f"doc{i}.jpg") for i in range(3)]
    r2 = leer_documentos(docs2, lector, max_llamadas=1)
    assert (r2["leidos"], r2["de_cache"], r2["llamadas"], r2["sin_leer_por_tope"]) == (3, 2, 1, 0)
    assert len(cliente.peticiones) == 3


@pytest.mark.parametrize("stop, texto", [("refusal", ""), ("max_tokens", '{"tipo": "fac'), ("end_turn", "no es json")])
def test_una_respuesta_inservible_no_detiene_el_resto(tmp_path, stop, texto):
    docs = [documento(imagen(tmp_path))]
    lector = Lector(ClienteFalso(stop_reason=stop, texto=texto), dir_cache=tmp_path / "cache")
    r = leer_documentos(docs, lector, max_llamadas=5)
    assert r["errores"] == 1 and r["leidos"] == 0
    assert docs[0].lectura.startswith("no leído") and docs[0].contenido is None
    assert not list((tmp_path / "cache").glob("*.json"))      # los fallos no se guardan en la caché


def test_documento_ilegible(tmp_path):
    docs = [documento(imagen(tmp_path))]
    lector = Lector(ClienteFalso({**DATOS, "importe_total": None, "legible": False}), dir_cache=tmp_path / "cache")
    r = leer_documentos(docs, lector, max_llamadas=5)
    assert docs[0].lectura == "ilegible" and r["ilegibles"] == 1
    assert "poco legible" in resumen_contenido(docs[0])


def test_solo_se_leen_pdf_e_imagenes_locales(tmp_path):
    assert se_puede_leer("carpeta/factura.PDF") and se_puede_leer("foto.jpeg")
    assert not se_puede_leer("listado.xlsx")
    assert not se_puede_leer("https://drive.google.com/file/d/abc/view")
    hoja = tmp_path / "listado.xlsx"
    hoja.write_bytes(b"x")
    cliente = ClienteFalso()
    r = leer_documentos([documento(hoja)], Lector(cliente, dir_cache=tmp_path / "cache"), max_llamadas=5)
    assert r["no_legibles_por_tipo"] == 1 and not cliente.peticiones


def test_pdf_se_envia_como_imagenes_de_las_primeras_paginas(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    ruta = tmp_path / "factura.pdf"
    with pymupdf.open() as pdf:
        for i in range(5):
            pdf.new_page().insert_text((72, 72), f"Factura inventada, página {i + 1}")
        pdf.save(ruta)
    cliente = ClienteFalso()
    leer_documento(ruta, max_paginas=2, cliente=cliente, dir_cache=tmp_path / "cache")
    bloques = cliente.peticiones[0]["messages"][0]["content"]
    imagenes = [b for b in bloques if b["type"] == "image"]
    assert len(imagenes) == 2
    assert all(b["source"]["media_type"] == "image/jpeg" for b in imagenes)


def test_resumen_en_una_linea():
    d = Documento(id="x", nombre="x", ubicacion="x", contenido={**DATOS, "manuscrito": True})
    assert resumen_contenido(d) == ("factura · 1.500,00 UGX · 2024-05-10 · Papelería Inventada · "
                                    "nº F-0042 · manuscrito")
    assert resumen_contenido(Documento(id="y", nombre="y", ubicacion="y")) == ""
