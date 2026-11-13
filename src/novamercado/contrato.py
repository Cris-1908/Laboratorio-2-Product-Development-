"""Modelos Pydantic del contrato (Anexo del laboratorio).

Hay tres grupos:
  1. *Crudos*: lo que llega por la red. Aceptan casi cualquier cosa, porque la
     decisión de reparar, advertir o rechazar la toma la lógica de limpieza, no FastAPI.
  2. *Limpios*: lo que el resto del código usa después de limpiar. Estrictos.
  3. *Respuestas*: lo que devolvemos. Los campos del Anexo no se renombran ni se omiten.
"""
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ------------------------------------------------------------ 1. crudos
class SolicitudCruda(BaseModel):
    """Cualquier objeto JSON. extra='allow' deja pasar campos con otro nombre (E07)."""

    model_config = ConfigDict(extra="allow")


# ------------------------------------------------------------ 2. limpios
class FamiliaPromocion(BaseModel):
    familia_id: str
    en_promocion: int = Field(ge=0)


class SolicitudPrediccion(BaseModel):
    tienda_id: int
    fecha: date
    familias: list[FamiliaPromocion]


class VentaFamilia(BaseModel):
    familia_id: str
    ventas: int = Field(ge=0)


class SolicitudObservacion(BaseModel):
    tienda_id: int
    fecha: date
    ventas: list[VentaFamilia]


# ------------------------------------------------------------ 3. respuestas
class ObservacionCalidad(BaseModel):
    campo: str
    accion: Literal["reparar", "advertir"]
    detalle: str


class Calidad(BaseModel):
    estado: Literal["ok", "corregido", "advertencia"]
    observaciones: list[ObservacionCalidad] = []


class PrediccionFamilia(BaseModel):
    familia_id: str
    venta_esperada: float
    cantidad_sugerida: int = Field(ge=0)


class RespuestaPrediccion(BaseModel):
    tienda_id: int
    fecha: date
    version_modelo: str
    predicciones: list[PrediccionFamilia]
    calidad: Calidad


class RespuestaObservacion(BaseModel):
    registros_guardados: int
    calidad: Calidad


class RespuestaReentrenar(BaseModel):
    id: str


class EstadoReentrenamiento(BaseModel):
    estado: Literal["en_curso", "exitoso", "fallido"]
    # solo cuando la ejecución terminó
    promovido: bool | None = None
    version: str | None = None
    motivo: str | None = None


class ModeloVigente(BaseModel):
    nombre: str
    alias: str
    version: str


class Salud(BaseModel):
    estado: Literal["ok", "degradado"]
    motivo: str | None = None


def calcular_estado(observaciones: list[ObservacionCalidad]) -> Calidad:
    """ok sin observaciones; advertencia si alguna es 'advertir'; si no, corregido."""
    if not observaciones:
        return Calidad(estado="ok", observaciones=[])
    if any(o.accion == "advertir" for o in observaciones):
        return Calidad(estado="advertencia", observaciones=observaciones)
    return Calidad(estado="corregido", observaciones=observaciones)


# tipo auxiliar para los valores que llegan sin validar
Crudo = Any
