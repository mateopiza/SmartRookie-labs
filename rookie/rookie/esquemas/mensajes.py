"""Modelos pydantic del contrato §2 (PLAN-FASES.md). Cambiarlos exige anotarlo en CAMBIOS-CONTRATO.md.

La contraseña del escritorio es SecretStr: no aparece en repr, str ni model_dump en modo json.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, SecretStr


class EscritorioEntrada(BaseModel):
    url: str
    password: SecretStr


class TareaEntrada(BaseModel):
    """Body de POST /tasks."""

    tarea: str
    escritorio: EscritorioEntrada


class TareaCreada(BaseModel):
    task_id: str


class EstadoTarea(BaseModel):
    estado: Literal["en_curso", "terminada", "fallida"]
    resumen: str = ""


class Cambio(BaseModel):
    entidad: str
    campo: str
    valor: str


class Pregunta(BaseModel):
    datos: list[str]
    formato: Literal["numero", "texto"]


class Plan(BaseModel):
    tipo: Literal["crear", "actualizar", "consultar", "exportar", "imposible"]
    modulo: str  # en palabras del negocio ("clientes", "pacientes"…)
    identificacion: dict[str, str] = {}  # {"nombre": "Ana Torres"}
    cambios_autorizados: list[Cambio] = []
    pregunta: Pregunta | None = None
    filtros: dict[str, str] = {}  # fechas ya absolutas
    textos_permitidos: list[str] = []  # ÚNICOS textos que el ejecutor puede escribir
    fecha_app: str | None = None  # la fecha "Hoy" leída de la pantalla


class Resultado(BaseModel):
    estado: Literal["terminada", "fallida"]
    respuesta: str | None = None  # solo consultas
    datos_clave: list[str] = []  # identificadores leídos de pantalla que explican una fallida
    escrituras: list[Cambio] = []
    detalle: str = ""


class Accion(BaseModel):
    tipo: Literal["click", "doble_click", "escribir", "tecla", "scroll", "esperar", "terminar"]
    x: int | None = None
    y: int | None = None
    texto: str | None = None
    combo: str | None = None
    elemento: str = ""  # texto visible del objetivo ("botón Guardar")
    razon: str = ""
    compuerta: Literal["identidad", "guardar", "crear", "exportar", "eliminar"] | None = None
    coincidencias: list[dict[str, str]] | None = None  # obligatorio cuando compuerta == "identidad"
    resultado: Resultado | None = None  # obligatorio cuando tipo == "terminar"


class Veredicto(BaseModel):
    aprobado: bool
    razon: str
    datos_clave: list[str] = []


__all__ = [
    "Accion",
    "Cambio",
    "EscritorioEntrada",
    "EstadoTarea",
    "Plan",
    "Pregunta",
    "Resultado",
    "TareaCreada",
    "TareaEntrada",
    "Veredicto",
]
