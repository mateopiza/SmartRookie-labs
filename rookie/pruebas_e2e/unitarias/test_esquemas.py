"""Contrato §2: modelos pydantic y que la contraseña nunca se muestre."""

import pytest
from pydantic import ValidationError

from rookie.esquemas.mensajes import (
    Accion,
    EstadoTarea,
    Plan,
    Pregunta,
    Resultado,
    TareaEntrada,
)

CLAVE = "clave-super-secreta-123"


def _entrada() -> TareaEntrada:
    return TareaEntrada.model_validate(
        {"tarea": "Registra al cliente X", "escritorio": {"url": "wss://d/websockify", "password": CLAVE}}
    )


def test_password_no_aparece_en_repr_str_ni_json():
    e = _entrada()
    assert CLAVE not in repr(e)
    assert CLAVE not in str(e)
    assert CLAVE not in repr(e.escritorio)
    assert CLAVE not in e.model_dump_json()
    assert e.escritorio.password.get_secret_value() == CLAVE


def test_estado_tarea_solo_estados_validos():
    assert EstadoTarea(estado="en_curso").resumen == ""
    with pytest.raises(ValidationError):
        EstadoTarea(estado="pendiente")


def test_plan_defaults_y_tipos():
    p = Plan(tipo="consultar", modulo="facturas", pregunta=Pregunta(datos=["conteo"], formato="numero"))
    assert p.identificacion == {} and p.cambios_autorizados == [] and p.textos_permitidos == []
    assert p.fecha_app is None
    with pytest.raises(ValidationError):
        Plan(tipo="borrar", modulo="x")


def test_defaults_mutables_no_se_comparten():
    a, b = Plan(tipo="crear", modulo="x"), Plan(tipo="crear", modulo="y")
    a.textos_permitidos.append("hola")
    assert b.textos_permitidos == []


def test_accion_terminar_con_resultado_anidado():
    a = Accion.model_validate(
        {"tipo": "terminar", "resultado": {"estado": "terminada", "respuesta": "45"}, "razon": "listo"}
    )
    assert isinstance(a.resultado, Resultado) and a.resultado.respuesta == "45"
    with pytest.raises(ValidationError):
        Accion(tipo="click", compuerta="borrar_todo")


def test_resultado_rechaza_estado_en_curso():
    with pytest.raises(ValidationError):
        Resultado(estado="en_curso")
