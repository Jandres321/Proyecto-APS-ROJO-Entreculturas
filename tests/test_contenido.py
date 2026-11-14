"""Señales del contenido en el cruce, Excel y CLI. Datos inventados; la API está simulada."""
import json
from datetime import date
from types import SimpleNamespace

import pandas as pd
import yaml

from conciliador import lectura
from conciliador.__main__ import main
from conciliador.cruce import Conciliador
from conciliador.modelos import Documento, Gasto
from conciliador.salida import escribir_excel

REFS = {"patron": r'(?<![A-Z])(?P<prefijo>[A-Z]{2})[\s\-_]*UGA\d{2}[\s\-_]*(?P<numero>\d+)'}


def gasto(desc, factura="F-0042", fecha=date(2024, 5, 10), proveedor="Papelería Inventada", local=1500.0):
    return Gasto(fila=1, partida="A.1", fecha=fecha, factura=factura, proveedor=proveedor, descripcion=desc,
                 importe_eur=0.4, importe_local=local)


def doc(nombre, **contenido):
    d = Documento(id=nombre, nombre=nombre, ubicacion=nombre)
    if contenido:
        d.contenido = {"tipo": "factura", "importe_total": None, "moneda": "UGX", "fecha": "", "proveedor": "",
                       "num_factura": "", "manuscrito": False, "legible": True, **contenido}
        d.lectura = "leido" if d.contenido["legible"] else "ilegible"
    return d


def conciliador(**contenido):
    return Conciliador({}, REFS, None, cfg_contenido=contenido)


def puntuar(g, d, **cfg):
    c = conciliador(**cfg)
    c.preparar_gasto(g)
    return c.puntuar(g, c.preparar_documento(d))


def test_sin_lectura_la_puntuacion_no_cambia():
    g = gasto("Material de oficina")
    sin = puntuar(g, doc("Material oficina.pdf"))
    assert sin.puntos_contenido == 0 and not sin.contradice
    assert not any("documento" in m.lower() and "dentro" in m.lower() for m in sin.motivos)


def test_cada_dato_del_contenido_suma_y_se_explica():
    g = gasto("Material de oficina")
    base = puntuar(g, doc("Material oficina.pdf")).puntuacion
    casos = [
        ({"importe_total": 1500.0}, 0.30, "importe total (1.500,00 UGX) coincide"),
        ({"importe_total": 1510.0}, 0.30, "coincide"),                    # dentro del 1 %
        ({"num_factura": "F 0042"}, 0.35, "nº de factura del gasto"),
        ({"fecha": "2024-05-12"}, 0.10, "(±2 días)"),
        ({"proveedor": "PAPELERIA INVENTADA S.L."}, 0.10, "es el del gasto"),
    ]
    for contenido, puntos, frase in casos:
        c = puntuar(g, doc("Material oficina.pdf", **contenido))
        assert round(c.puntuacion - base, 4) == puntos, contenido
        assert c.coincidencias == 1 and any(frase in m for m in c.motivos), c.motivos


def test_los_pesos_se_configuran_en_el_yaml():
    g = gasto("Material de oficina")
    base = puntuar(g, doc("Material oficina.pdf")).puntuacion
    c = puntuar(g, doc("Material oficina.pdf", importe_total=1500.0), bonus_importe=0.5)
    assert round(c.puntuacion - base, 4) == 0.5


def test_un_importe_distinto_no_resta_pero_otra_factura_si():
    g = gasto("Material de oficina")
    base = puntuar(g, doc("Material oficina.pdf")).puntuacion
    varios = puntuar(g, doc("Material oficina.pdf", importe_total=9000.0))
    assert varios.puntuacion == base and not varios.contradice
    otra = puntuar(g, doc("Material oficina.pdf", num_factura="F-0099"))
    assert round(base - otra.puntuacion, 4) == 0.15 and otra.contradice


def test_importe_en_euros_solo_si_el_documento_esta_en_euros():
    g = gasto("Material de oficina", local=None)
    g.importe_eur = 250.0
    assert puntuar(g, doc("Material oficina.pdf", importe_total=250.0, moneda="EUR")).coincidencias == 1
    assert puntuar(g, doc("Material oficina.pdf", importe_total=250.0, moneda="UGX")).coincidencias == 0


def test_documento_ilegible_o_manuscrito_se_menciona_sin_inventar():
    g = gasto("Material de oficina")
    base = puntuar(g, doc("Material oficina.pdf")).puntuacion
    ilegible = puntuar(g, doc("Material oficina.pdf", importe_total=1500.0, legible=False))
    assert ilegible.puntuacion == base and any("no se lee" in m for m in ilegible.motivos)
    mano = puntuar(g, doc("Material oficina.pdf", importe_total=1500.0, manuscrito=True))
    assert any("a mano" in m for m in mano.motivos)


def test_archivo_mal_nombrado_se_encuentra_por_el_contenido():
    """Un gasto sin justificante por nombre pasa a tenerlo cuando el contenido encaja."""
    c = conciliador()
    escaneo = doc("Escaneo 0007.pdf")
    (r,) = c.conciliar([gasto("Material de oficina")], [escaneo, doc("Combustible vehículo.pdf")])
    assert r.estado == "sin_justificante"
    assert {d.nombre for d in c.documentos_a_leer([r])} == {"Escaneo 0007.pdf", "Combustible vehículo.pdf"}
    escaneo.contenido = doc("x", importe_total=1500.0, num_factura="F-0042", fecha="2024-05-10",
                            proveedor="Papelería Inventada").contenido
    (r2,) = c.revisar_con_contenido([r])
    assert r2.estado == "asignado" and r2.mejor.documento is escaneo
    assert "sin justificante" in r2.cambio


def test_el_contenido_resuelve_un_empate_entre_documentos_parecidos():
    c = conciliador()
    a, b = doc("Material oficina tienda.pdf"), doc("Material oficina almacén.pdf")
    (r,) = c.conciliar([gasto("Material de oficina")], [a, b])
    assert r.confianza == "baja" and r.ambiguo
    por_nombre = r.mejor.documento
    por_nombre.contenido = doc("x", importe_total=1500.0, num_factura="F-0042").contenido
    (r2,) = c.revisar_con_contenido([r])
    assert r2.mejor.documento is por_nombre and r2.confianza == "alta" and not r2.aviso


def test_conflicto_entre_nombre_y_contenido_se_marca_para_revisar():
    c = conciliador()
    a, b = doc("Material oficina tienda.pdf"), doc("Material oficina almacén.pdf")
    (r,) = c.conciliar([gasto("Material de oficina")], [a, b])
    por_nombre, otro = r.mejor.documento, r.segundo.documento
    otro.contenido = doc("x", importe_total=1500.0, num_factura="F-0042").contenido
    (r2,) = c.revisar_con_contenido([r])
    assert r2.mejor.documento is otro
    assert r2.confianza == "baja" and r2.ambiguo
    assert por_nombre.nombre in r2.aviso and "mal nombrado" in r2.aviso


def test_contenido_que_no_encaja_con_el_documento_elegido_por_nombre():
    c = conciliador()
    a, b = doc("Material oficina tienda.pdf"), doc("Material oficina almacén.pdf")
    (r,) = c.conciliar([gasto("Material de oficina")], [a, b])
    r.mejor.documento.contenido = doc("x", fecha="2023-01-01", proveedor="Ferretería Imaginaria").contenido
    r.segundo.documento.contenido = doc("x", fecha="2023-02-02", proveedor="Taller Ficticio").contenido
    (r2,) = c.revisar_con_contenido([r])
    assert r2.estado == "asignado" and r2.confianza == "baja"
    assert "lo que se lee dentro" in r2.aviso


def test_solo_se_leen_documentos_de_gastos_dudosos_y_documentos_sin_gasto():
    c = conciliador()
    seguro = gasto("Alquiler sala formación", factura="FP UGA04 108", proveedor="")
    dudoso = gasto("Material de oficina")
    docs = [doc("Alquiler sala formación FP UGA04 108.pdf"), doc("Material oficina tienda.pdf"),
            doc("Material oficina almacén.pdf"), doc("Escaneo 0007.pdf")]
    res = c.conciliar([seguro, dudoso], docs)
    assert res[0].confianza == "alta" and res[1].confianza == "baja"
    c.pc["candidatos"] = 2
    nombres = [d.nombre for d in c.documentos_a_leer(res)]
    assert set(nombres[:2]) == {"Material oficina tienda.pdf", "Material oficina almacén.pdf"}   # candidatos
    assert nombres[2:] == ["Escaneo 0007.pdf"]                                                    # sin gasto
    # El gasto seguro no se vuelve a decidir aunque después se lea su documento
    docs[0].contenido = doc("x", num_factura="OTRA-999").contenido
    assert c.revisar_con_contenido(res)[0] is res[0]


def test_excel_con_columnas_de_contenido(tmp_path):
    c = conciliador()
    escaneo = doc("Escaneo 0007.pdf")
    res = c.conciliar([gasto("Material de oficina")], [escaneo])
    escaneo.contenido = doc("x", importe_total=1500.0, num_factura="F-0042").contenido
    escaneo.lectura = "leido"
    res = c.revisar_con_contenido(res)
    resumen = {"leidos": 1, "ilegibles": 0, "errores": 0, "sin_leer_por_tope": 0, "cambiados": 1}
    ruta = escribir_excel(tmp_path / "salida.xlsx", "Prueba", res, [escaneo], None, resumen)
    gastos = pd.read_excel(ruta, sheet_name="Gastos")
    assert gastos.loc[0, "Leído por contenido"] == "sí"
    assert "1.500,00 UGX" in gastos.loc[0, "Datos del contenido"]
    assert "Solo por el nombre del archivo" in gastos.loc[0, "Motivo"]
    assert "Datos del contenido" in pd.read_excel(ruta, sheet_name="Documentos").columns
    # Sin lectura, el Excel es el de siempre
    ruta = escribir_excel(tmp_path / "sin.xlsx", "Prueba", res, [escaneo])
    assert "Leído por contenido" not in pd.read_excel(ruta, sheet_name="Gastos").columns


def _proyecto(tmp_path, monkeypatch):
    """Proyecto inventado en disco y cliente simulado en lugar del real."""
    carpeta = tmp_path / "docs"
    carpeta.mkdir()
    (carpeta / "Escaneo 0007.jpg").write_bytes(b"imagen inventada")
    (carpeta / "Combustible.jpg").write_bytes(b"otra imagen inventada")
    pd.DataFrame([["A.1", "1", date(2024, 5, 10), "F-0042", "Papelería Inventada", "Material de oficina",
                   1500.0, 0.4]]).to_excel(tmp_path / "listado.xlsx", sheet_name="Gastos", header=False, index=False)
    cfg = {
        "proyecto": "Prueba",
        "documentos": {"tipo": "local", "ruta": "docs"},
        "listado": {"archivo": "listado.xlsx", "hoja": "Gastos",
                    "columnas": {"partida": 0, "orden": 1, "fecha": 2, "factura": 3, "proveedor": 4,
                                 "descripcion": 5, "importe_local": 6, "importe_eur": 7}},
        "referencias": REFS,
        "prefijo_orden": r"^(?P<desde>\d+)\.",
    }
    (tmp_path / "prueba.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    llamadas = []

    def create(**kw):
        llamadas.append(kw)
        datos = {"tipo": "factura", "importe_total": 1500.0, "moneda": "UGX", "fecha": "2024-05-10",
                 "proveedor": "Papelería Inventada", "num_factura": "F-0042", "manuscrito": False, "legible": True}
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(datos))])

    monkeypatch.setattr(lectura, "crear_cliente", lambda raiz=None: SimpleNamespace(
        messages=SimpleNamespace(create=create)))
    return llamadas


def test_cli_sin_la_opcion_no_llama_a_la_api(tmp_path, monkeypatch):
    llamadas = _proyecto(tmp_path, monkeypatch)
    assert main([str(tmp_path / "prueba.yaml"), "--raiz", str(tmp_path)]) == 0
    assert not llamadas and not (tmp_path / "output" / "cache_lectura").exists()


def test_cli_con_contenido_lee_guarda_cache_y_respeta_el_tope(tmp_path, monkeypatch, capsys):
    llamadas = _proyecto(tmp_path, monkeypatch)
    args = [str(tmp_path / "prueba.yaml"), "--raiz", str(tmp_path), "--contenido"]
    assert main(args) == 0
    assert len(llamadas) == 2
    assert len(list((tmp_path / "output" / "cache_lectura").glob("*.json"))) == 2
    assert "Se envían 2 a la API" in capsys.readouterr().out
    gastos = pd.read_excel(tmp_path / "output" / "prueba.xlsx", sheet_name="Gastos")
    assert gastos.loc[0, "Estado"] == "Con justificante"
    assert main(args) == 0                       # segunda vez: todo sale de la caché
    assert len(llamadas) == 2
    assert "Se envían 0 a la API" in capsys.readouterr().out
