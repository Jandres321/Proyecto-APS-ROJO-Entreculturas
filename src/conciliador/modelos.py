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
    orden: str | None = None     # nº de orden manual: solo para validar
    texto: str = ""
    texto_descripcion: str = ""
    refs: set[str] = field(default_factory=set)


@dataclass
class Candidato:
    documento: Documento
    puntuacion: float
    motivos: list[str]


@dataclass
class Resultado:
    gasto: Gasto
    mejor: Candidato | None
    segundo: Candidato | None
    estado: str                  # "asignado" | "sin_justificante"
    confianza: str               # "alta" | "media" | "baja" | ""
    ambiguo: bool = False
