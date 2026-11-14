"""Lectura del contenido de un justificante con un modelo de visión (API de Claude).

Es OPCIONAL (extra `.[contenido]`) y está desactivada por defecto: envía la imagen del
documento a un servicio externo, así que solo debe usarse con documentos reales cuando
Entreculturas haya aprobado el proveedor.

- Los PDF se convierten en imágenes (primeras páginas) con PyMuPDF; las imágenes se envían tal cual.
- Una sola llamada por documento, con salida estructurada (JSON con esquema fijo).
- Caché en disco: un JSON por documento, con clave = hash del archivo. Si existe, no se llama.
- La clave de la API se lee de `.env` (ANTHROPIC_API_KEY). Este módulo nunca la lee ni la muestra:
  la recoge directamente el cliente oficial.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from .modelos import Documento

MODELO = "claude-sonnet-5-5"
MAX_PAGINAS = 3
DPI = 130
DIR_CACHE = Path("output") / "cache_lectura"
VERSION_CACHE = 1          # súbela si cambian el esquema o las instrucciones

CAMPOS = ["tipo", "importe_total", "moneda", "fecha", "proveedor", "num_factura", "manuscrito", "legible"]
TIPOS = ["factura", "recibo", "proforma", "nomina", "contrato", "justificante_pago",
         "extracto_bancario", "lista_asistencia", "otro", ""]
IMAGENES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}

ESQUEMA = {
    "type": "object",
    "properties": {
        "tipo": {"type": "string", "enum": TIPOS, "description": "Tipo de documento; vacío si no se sabe."},
        "importe_total": {"anyOf": [{"type": "number"}, {"type": "null"}],
                          "description": "Importe total del documento, en su moneda original; null si no se lee."},
        "moneda": {"type": "string", "description": "Código ISO de la moneda (EUR, UGX, COP...); vacío si no se sabe."},
        "fecha": {"type": "string", "description": "Fecha del documento como AAAA-MM-DD; vacío si no se lee."},
        "proveedor": {"type": "string", "description": "Quien emite el documento o recibe el pago; vacío si no se lee."},
        "num_factura": {"type": "string", "description": "Nº de factura, recibo o comprobante; vacío si no hay."},
        "manuscrito": {"type": "boolean", "description": "Los datos principales están escritos a mano."},
        "legible": {"type": "boolean", "description": "El documento se lee lo bastante para extraer sus datos."},
    },
    "required": CAMPOS,
    "additionalProperties": False,
}

INSTRUCCIONES = (
    "Eres un asistente que ayuda a revisar la justificación económica de proyectos de cooperación. "
    "Recibes las primeras páginas de un justificante de gasto (factura, recibo, nómina, comprobante de "
    "pago...), casi siempre escaneado y a veces escrito a mano. Extrae sus datos para poder cotejarlo "
    "con el listado de gastos.\n"
    "- Copia los datos tal como aparecen en el documento. Si un dato no aparece o no se lee con "
    "claridad, déjalo vacío (null en el importe): no lo deduzcas ni lo inventes. Un dato vacío es útil; "
    "uno inventado hace que el gasto se asocie al documento equivocado.\n"
    "- importe_total es el total a pagar del documento, en su moneda original y sin convertir.\n"
    "- Si el documento reúne varios justificantes, describe el principal (normalmente el primero).\n"
    "- Marca legible=false si la imagen no permite leer los datos principales."
)


class ErrorLectura(RuntimeError):
    """Problema que impide leer contenidos (faltan librerías o la clave); mensaje para el usuario."""


def hash_archivo(ruta: Path) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def se_puede_leer(ruta: str | Path) -> bool:
    """Solo archivos locales en PDF o imagen."""
    if str(ruta).startswith(("http://", "https://")):
        return False
    return Path(ruta).suffix.lower() in {".pdf", *IMAGENES}


def paginas_a_imagenes(ruta: Path, max_paginas: int = MAX_PAGINAS, dpi: int = DPI) -> list[tuple[str, bytes]]:
    """[(tipo MIME, bytes)] de las primeras páginas. Una imagen se devuelve tal cual."""
    ext = ruta.suffix.lower()
    if ext in IMAGENES:
        return [(IMAGENES[ext], ruta.read_bytes())]
    try:
        import pymupdf
    except ImportError as e:
        raise ErrorLectura('Falta PyMuPDF. Instala el extra: pip install -e ".[contenido]"') from e
    with pymupdf.open(ruta) as pdf:
        return [("image/jpeg", pdf[i].get_pixmap(dpi=dpi).tobytes("jpeg"))
                for i in range(min(max_paginas, pdf.page_count))]


def crear_cliente(raiz: Path | None = None):
    """Cliente de la API. La clave sale de `.env` o del entorno; aquí no se lee ni se muestra."""
    try:
        import anthropic
        from dotenv import load_dotenv
    except ImportError as e:
        raise ErrorLectura('Faltan las librerías para leer contenidos. Instala el extra: '
                           'pip install -e ".[contenido]"') from e
    load_dotenv((raiz or Path.cwd()) / ".env")
    try:
        return anthropic.Anthropic()
    except Exception as e:
        raise ErrorLectura("No encuentro la clave de la API. Crea el archivo .env en la raíz del "
                           "repositorio con ANTHROPIC_API_KEY (ver .env.example).") from e


def _errores_api() -> tuple[tuple, tuple]:
    """(errores que impiden seguir, errores de un solo documento). Vacíos si no está la librería."""
    try:
        import anthropic
    except ImportError:
        return (), ()
    return (anthropic.AuthenticationError, anthropic.PermissionDeniedError), (anthropic.APIError,)


def _normalizar(datos: dict) -> dict:
    """Deja exactamente los campos esperados y con su tipo, venga lo que venga."""
    def texto(v) -> str:
        return str(v).strip() if v not in (None, "") else ""
    importe = datos.get("importe_total")
    if isinstance(importe, bool) or not isinstance(importe, (int, float)):
        importe = None
    tipo = texto(datos.get("tipo"))
    return {
        "tipo": tipo if tipo in TIPOS else "otro",
        "importe_total": float(importe) if importe is not None else None,
        "moneda": texto(datos.get("moneda")).upper(),
        "fecha": texto(datos.get("fecha")),
        "proveedor": texto(datos.get("proveedor")),
        "num_factura": texto(datos.get("num_factura")),
        "manuscrito": bool(datos.get("manuscrito")),
        "legible": bool(datos.get("legible")),
    }


class Lector:
    """Lee documentos con caché. El cliente se crea solo si hace falta llamar a la API."""

    def __init__(self, cliente=None, modelo: str = MODELO, max_paginas: int = MAX_PAGINAS,
                 dir_cache: Path | None = None, raiz: Path | None = None):
        self._cliente = cliente
        self.modelo = modelo
        self.max_paginas = max_paginas
        self.raiz = raiz
        self.dir_cache = Path(dir_cache) if dir_cache else (raiz or Path.cwd()) / DIR_CACHE
        self.llamadas = 0

    def _ruta_cache(self, ruta: Path) -> Path:
        clave = f"{hash_archivo(ruta)}|{self.max_paginas}|{self.modelo}|{VERSION_CACHE}"
        return self.dir_cache / f"{hashlib.sha256(clave.encode()).hexdigest()}.json"

    def en_cache(self, ruta: Path) -> bool:
        return self._ruta_cache(Path(ruta)).is_file()

    def leer(self, ruta: Path) -> dict:
        """Datos del documento. Usa la caché si existe; si no, hace UNA llamada a la API."""
        ruta = Path(ruta)
        cache = self._ruta_cache(ruta)
        if cache.is_file():
            return _normalizar(json.loads(cache.read_text(encoding="utf-8"))["datos"])
        datos = self._llamar(paginas_a_imagenes(ruta, self.max_paginas))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"modelo": self.modelo, "max_paginas": self.max_paginas, "datos": datos},
                                    ensure_ascii=False, indent=2), encoding="utf-8")
        return datos

    def _llamar(self, imagenes: list[tuple[str, bytes]]) -> dict:
        if not imagenes:
            raise ValueError("el documento no tiene páginas")
        if self._cliente is None:
            self._cliente = crear_cliente(self.raiz)
        contenido = [{"type": "image", "source": {"type": "base64", "media_type": mime,
                                                  "data": base64.standard_b64encode(datos).decode("ascii")}}
                     for mime, datos in imagenes]
        contenido.append({"type": "text", "text": "Extrae los datos de este justificante."})
        self.llamadas += 1
        r = self._cliente.messages.create(
            model=self.modelo,
            max_tokens=16000,
            system=INSTRUCCIONES,
            messages=[{"role": "user", "content": contenido}],
            # Salida estructurada: la respuesta es un JSON que cumple el esquema
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": ESQUEMA}},
        )
        if r.stop_reason == "refusal":
            raise ValueError("el modelo ha rechazado el documento")
        if r.stop_reason == "max_tokens":
            raise ValueError("la respuesta ha llegado cortada")
        texto = next((b.text for b in r.content if b.type == "text"), "")
        try:
            return _normalizar(json.loads(texto))
        except (json.JSONDecodeError, AttributeError) as e:
            raise ValueError("la respuesta no es un JSON válido") from e


def leer_documento(ruta: str | Path, max_paginas: int = MAX_PAGINAS, cliente=None, modelo: str = MODELO,
                   dir_cache: Path | None = None) -> dict:
    """Lee un documento y devuelve: tipo, importe_total, moneda, fecha, proveedor, num_factura,
    manuscrito y legible. Si ya está en la caché no llama a la API."""
    return Lector(cliente, modelo, max_paginas, dir_cache).leer(Path(ruta))


def leer_documentos(documentos: list[Documento], lector: Lector, max_llamadas: int,
                    max_fallos_seguidos: int = 3) -> dict:
    """Lee los documentos en el orden dado (el de prioridad) y guarda el resultado en cada uno.

    Los que están en la caché no cuentan para el tope de llamadas. Devuelve un resumen.
    """
    fatales, de_documento = _errores_api()
    r = {"leidos": 0, "de_cache": 0, "llamadas": 0, "ilegibles": 0, "errores": 0,
         "sin_leer_por_tope": 0, "no_legibles_por_tipo": 0}
    seguidos = 0        # fallos consecutivos: si la API no responde, no se insiste con el resto
    for d in documentos:
        if not se_puede_leer(d.ubicacion):
            r["no_legibles_por_tipo"] += 1
            continue
        ruta = Path(d.ubicacion)
        cacheado = lector.en_cache(ruta)
        if not cacheado and seguidos >= max_fallos_seguidos:
            d.lectura = "no leído: demasiados errores seguidos"
            r["errores"] += 1
            continue
        if not cacheado and r["llamadas"] >= max_llamadas:
            d.lectura = "no leído: tope de llamadas"
            r["sin_leer_por_tope"] += 1
            continue
        try:
            datos = lector.leer(ruta)
        except ErrorLectura:
            raise
        except fatales as e:
            raise ErrorLectura("La API ha rechazado la clave. Revisa ANTHROPIC_API_KEY en .env.") from e
        except (*de_documento, ValueError, OSError) as e:
            # Fallo de un documento: se anota y se sigue con el resto
            # Los errores de la API traen su propio mensaje (sin datos del documento ni la clave)
            motivo = e if isinstance(e, ValueError) else (getattr(e, "message", "") or type(e).__name__)
            d.lectura = f"no leído: {motivo}"
            r["errores"] += 1
            r["llamadas"] += not cacheado      # el intento también cuenta para el tope
            seguidos += 1
            continue
        seguidos = 0
        d.contenido = datos
        d.lectura = "leido" if datos["legible"] else "ilegible"
        r["leidos"] += 1
        r["ilegibles"] += not datos["legible"]
        r["de_cache" if cacheado else "llamadas"] += 1
    return r

