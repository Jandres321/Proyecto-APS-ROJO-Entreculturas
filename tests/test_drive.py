"""Tests de la fuente Google Drive con un servicio simulado (sin red ni credenciales)."""
import re

import pytest

from conciliador.drive import MIME_ATAJO, MIME_CARPETA, ErrorDrive, id_desde_url, listar_carpeta
from conciliador.fuentes import CarpetaDrive, crear_fuente

PDF = "application/pdf"


class _Peticion:
    def __init__(self, resultado):
        self._r = resultado

    def execute(self):
        return self._r


class _Files:
    """Imita servicio.files().list(...): árbol {carpeta_id: [archivos]} con páginas de tamaño 2."""

    def __init__(self, arbol, tam_pagina=2):
        self.arbol, self.tam = arbol, tam_pagina
        self.llamadas = []

    def list(self, q, pageToken=None, **kw):
        assert kw.get("supportsAllDrives") and kw.get("includeItemsFromAllDrives")
        padre = re.search(r"'([^']+)' in parents", q).group(1)
        self.llamadas.append((padre, pageToken))
        hijos = self.arbol.get(padre, [])
        inicio = int(pageToken or 0)
        pagina = hijos[inicio:inicio + self.tam]
        siguiente = str(inicio + self.tam) if inicio + self.tam < len(hijos) else None
        return _Peticion({"files": pagina, **({"nextPageToken": siguiente} if siguiente else {})})


class ServicioFalso:
    def __init__(self, arbol):
        self._files = _Files(arbol)

    def files(self):
        return self._files


def archivo(i, nombre, mime=PDF, **extra):
    return {"id": i, "name": nombre, "mimeType": mime, "webViewLink": f"https://drive.google.com/file/d/{i}/view", **extra}


ARBOL = {
    "RAIZ": [
        archivo("a1", "1. Alquiler sala FP UGA04 108.pdf"),
        archivo("a2", "2. Material taller EB UGA04 600.pdf"),
        archivo("a3", "Informe_economico_final.xls", "application/vnd.ms-excel"),
        archivo("f1", "Personal", MIME_CARPETA),
        archivo("s1", "Atajo a viajes", MIME_ATAJO,
                shortcutDetails={"targetId": "f2", "targetMimeType": MIME_CARPETA}),
        archivo("s2", "Atajo a factura.pdf", MIME_ATAJO,
                shortcutDetails={"targetId": "x9", "targetMimeType": PDF}),
    ],
    "f1": [archivo("b1", "13. Salarios brutos Junio.pdf"), archivo("b2", "~$temporal.pdf")],
    "f2": [archivo("c1", "50. Seguro vehículo.pdf"),
           archivo("ciclo", "Vuelta a la raíz", MIME_CARPETA)],   # carpeta que reaparece
    "ciclo": [],
}
ARBOL["ciclo"] = [archivo("raiz-otra-vez", "raiz", MIME_ATAJO,
                          shortcutDetails={"targetId": "RAIZ", "targetMimeType": MIME_CARPETA})]


def test_id_desde_url():
    assert id_desde_url("https://drive.google.com/drive/folders/1AbC_def-123456?usp=sharing") == "1AbC_def-123456"
    assert id_desde_url("https://drive.google.com/drive/u/0/folders/1AbC_def-123456") == "1AbC_def-123456"
    assert id_desde_url("https://docs.google.com/spreadsheets/d/1XyZ_987654321/edit#gid=0") == "1XyZ_987654321"
    assert id_desde_url("https://drive.google.com/open?id=1AbC_def-123456") == "1AbC_def-123456"
    assert id_desde_url("1AbC_def-123456") == "1AbC_def-123456"
    with pytest.raises(ErrorDrive):
        id_desde_url("no es un id")


def test_listado_recursivo_paginado_con_atajos_y_ciclos():
    servicio = ServicioFalso(ARBOL)
    rutas = sorted(f["ruta"] for f in listar_carpeta(servicio, "RAIZ"))
    assert rutas == sorted([
        "1. Alquiler sala FP UGA04 108.pdf",
        "2. Material taller EB UGA04 600.pdf",
        "Informe_economico_final.xls",
        "Personal/13. Salarios brutos Junio.pdf",
        "Personal/~$temporal.pdf",
        "Atajo a viajes/50. Seguro vehículo.pdf",
        "Atajo a factura.pdf",
    ])
    # La raíz tiene 6 elementos -> 3 páginas de 2
    assert [t for p, t in servicio.files().llamadas if p == "RAIZ"] == [None, "2", "4"]


def test_no_recursivo():
    rutas = [f["ruta"] for f in listar_carpeta(ServicioFalso(ARBOL), "RAIZ", recursivo=False)]
    assert "Personal/13. Salarios brutos Junio.pdf" not in rutas
    assert "1. Alquiler sala FP UGA04 108.pdf" in rutas


def test_carpeta_drive_excluye_y_da_enlaces():
    fuente = crear_fuente({"tipo": "drive", "carpeta": "https://drive.google.com/drive/folders/RAIZ0000000",
                           "excluir": ["Informe_economico*"]}, raiz=None,
                          servicio_drive=ServicioFalso({"RAIZ0000000": ARBOL["RAIZ"], **ARBOL}))
    docs = fuente.listar()
    nombres = {d.nombre for d in docs}
    assert "Informe_economico_final.xls" not in nombres       # excluido por patrón
    assert "~$temporal.pdf" not in nombres                     # temporales de Office fuera
    alquiler = next(d for d in docs if d.nombre.startswith("1. Alquiler"))
    assert alquiler.ubicacion == "https://drive.google.com/file/d/a1/view"
    assert next(d for d in docs if "Salarios" in d.nombre).id == "Personal/13. Salarios brutos Junio.pdf"


def test_drive_sin_servicio_da_error_claro():
    with pytest.raises(ValueError, match="servicio de Drive"):
        crear_fuente({"tipo": "drive", "carpeta": "1AbC_def-123456"}, raiz=None)


def test_cruce_identico_con_drive():
    """El cruce no depende de la fuente: mismos nombres -> mismo resultado."""
    from datetime import date
    from conciliador.cruce import Conciliador
    from conciliador.modelos import Gasto

    refs = {"patron": r'(?<![A-Z])(?P<prefijo>[A-Z]{2})[\s\-_]*UGA\d{2}[\s\-_]*(?P<numero>\d+)'}
    docs = CarpetaDrive(ServicioFalso({"RAIZ0000000": ARBOL["RAIZ"], **ARBOL}), "RAIZ0000000").listar()
    g = Gasto(fila=1, partida="", fecha=date(2024, 10, 24), factura="FP UGA04 108", proveedor="",
              descripcion="Alquiler sala formación", importe_eur=10.0)
    (r,) = Conciliador({}, refs, r"^\d+\.").conciliar([g], docs)
    assert r.estado == "asignado" and r.mejor.documento.nombre.startswith("1. Alquiler")
    assert r.mejor.documento.ubicacion.startswith("https://")
