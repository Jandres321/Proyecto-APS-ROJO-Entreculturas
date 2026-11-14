"""Estructuras de datos comunes."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class Documento:
    """Un archivo justificante, venga de una carpeta local o de Drive."""
    id: str                      # identificador único (ruta relativa o id de Drive)
    nombre: str                  # nombre original del archivo
    ubicacion: str               # ruta local o enlace
    nombre_cruce: str = ""       # nombre sin el nº de orden manual (lo que ve el algoritmo)
    texto: str = ""              # texto normalizado para comparar
    refs: set[str] = field(default_factory=set)
    meses: set[int] = field(default_factory=set)
    # Lectura del contenido (opcional, ver lectura.py)
    contenido: dict | None = None   # datos extraídos del documento; None = no leído
    lectura: str = ""               # "" (no se intentó) | "leido" | "ilegible" | "no leído: <motivo>"

    @property
    def firma(self) -> tuple:
        """Dos archivos con la misma firma son copias o versiones del mismo documento
        (p. ej. el .pdf y el .xlsx de la misma comisión bancaria).

        Si el nombre no dice nada (solo el nº de orden: AC-07.pdf), no se puede afirmar que dos
        archivos sean copias: cada uno es distinto."""
        if not (self.texto or self.meses or self.refs):
            return ("nombre sin información", self.id)
        return (self.texto, frozenset(self.meses), frozenset(self.refs))


@dataclass
class Gasto:
    """Una línea del listado de gastos."""
    fila: int                    # fila en el Excel original (1 = primera)
    partida: str
    fecha: date | None
    factura: str
    proveedor: str
    descripcion: str
    importe_eur: float | None
    pago: str = ""
    importe_local: float | None = None      # importe en moneda local (para comparar con el documento)
    fecha_pago: date | None = None          # fecha de pago, si el listado la tiene (los recibos llevan esta)
    referencia_extra: str = ""   # otras columnas con referencias (p. ej. código contable)
    imputado_donante: float | None = None   # importe imputado al donante (None = no informado)
    imputado_otros: float | None = None     # importe imputado a otros financiadores
    orden: str | None = None     # nº de orden manual: solo para validar
    texto: str = ""
    texto_descripcion: str = ""
    refs: set[str] = field(default_factory=set)


@dataclass
class Candidato:
    documento: Documento
    puntuacion: float
    motivos: list[str]
    puntos_contenido: float = 0.0   # parte de la puntuación que viene del contenido leído
    coincidencias: int = 0          # nº de datos del contenido que coinciden con el gasto
    contradice: bool = False        # el contenido leído no encaja con el gasto


@dataclass
class Resultado:
    gasto: Gasto
    mejor: Candidato | None
    segundo: Candidato | None
    estado: str                  # "asignado" | "sin_justificante"
    confianza: str               # "alta" | "media" | "baja" | ""
    ambiguo: bool = False
    # Para gastos sin justificante: "revisar" | "cofinanciacion" | "indirecto"
    clasificacion: str = ""
    aviso: str = ""              # advertencia extra para el revisor (conflicto nombre/contenido)
    cambio: str = ""             # qué decía el cruce por nombre, si el contenido lo ha cambiado
