"""Motor: plazo, excepciones sin traza, lock por escritorio, estado final fijo."""

import asyncio
import time

import pytest

from rookie.esquemas.mensajes import Plan, Pregunta, Resultado, TareaEntrada
from rookie.motor.tareas import Motor

pytestmark = pytest.mark.asyncio

CLAVE = "clave-del-escritorio-99"


def entrada(url: str = "wss://uno/websockify", tarea: str = "x") -> TareaEntrada:
    return TareaEntrada.model_validate({"tarea": tarea, "escritorio": {"url": url, "password": CLAVE}})


async def esperar_final(motor: Motor, task_id: str, limite_s: float = 5.0):
    fin = time.monotonic() + limite_s
    while time.monotonic() < fin:
        e = motor.estado(task_id)
        if e.estado != "en_curso":
            return e
        await asyncio.sleep(0.01)
    raise AssertionError("la tarea no terminó")


async def test_plazo_agotado():
    cancelada = asyncio.Event()

    async def lento(task_id, ent, deadline):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelada.set()
            raise

    motor = Motor(lento, plazo_s=0.2)
    t0 = time.monotonic()
    tid = motor.crear(entrada())
    assert motor.estado(tid).estado == "en_curso"
    e = await esperar_final(motor, tid)
    assert e.estado == "fallida"
    assert "plazo agotado" in e.resumen
    assert time.monotonic() - t0 < 1.0
    assert cancelada.is_set(), "el ejecutor debe ser cancelado al agotar el plazo"


async def test_excepcion_sin_traza_ni_mensaje():
    async def roto(task_id, ent, deadline):
        raise ValueError(f"fallo con {CLAVE} en línea 42")

    motor = Motor(roto)
    e = await esperar_final(motor, motor.crear(entrada()))
    assert e.estado == "fallida"
    assert e.resumen == "No se pudo completar: error interno: ValueError."
    assert "Traceback" not in e.resumen and CLAVE not in e.resumen


async def test_plazo_agotado_de_guardias_se_reporta_como_plazo():
    class PlazoAgotado(Exception):
        pass

    async def corta(task_id, ent, deadline):
        raise PlazoAgotado()

    motor = Motor(corta)
    e = await esperar_final(motor, motor.crear(entrada()))
    assert e.resumen == "No se pudo completar: plazo agotado."


async def test_mismo_escritorio_se_serializa_y_distinto_corre_en_paralelo():
    activos: dict[str, int] = {}
    max_activos: dict[str, int] = {}

    async def ejecutor(task_id, ent, deadline):
        url = ent.escritorio.url
        activos[url] = activos.get(url, 0) + 1
        max_activos[url] = max(max_activos.get(url, 0), activos[url])
        await asyncio.sleep(0.15)
        activos[url] -= 1
        return Resultado(estado="terminada", detalle="ok")

    motor = Motor(ejecutor)
    t0 = time.monotonic()
    a = motor.crear(entrada("wss://uno/websockify"))
    b = motor.crear(entrada("wss://uno/websockify"))
    c = motor.crear(entrada("wss://dos/websockify"))
    assert motor.estado(b).estado == "en_curso"  # espera, no se rechaza
    for tid in (a, b, c):
        assert (await esperar_final(motor, tid)).estado == "terminada"
    assert max_activos["wss://uno/websockify"] == 1
    assert time.monotonic() - t0 >= 0.3
    assert time.monotonic() - t0 < 0.45 + 0.3


async def test_el_plazo_cuenta_desde_crear_aunque_espere_el_lock():
    async def ejecutor(task_id, ent, deadline):
        await asyncio.sleep(0.15)
        return Resultado(estado="terminada")

    motor = Motor(ejecutor, plazo_s=0.2)
    a = motor.crear(entrada())
    b = motor.crear(entrada())
    assert (await esperar_final(motor, a)).estado == "terminada"
    eb = await esperar_final(motor, b)
    assert eb.estado == "fallida" and "plazo agotado" in eb.resumen


async def test_deadline_que_recibe_el_ejecutor():
    recibido = {}

    async def ejecutor(task_id, ent, deadline):
        recibido["restante"] = deadline - time.monotonic()
        recibido["task_id"] = task_id
        return Resultado(estado="terminada")

    motor = Motor(ejecutor, plazo_s=285)
    tid = motor.crear(entrada())
    await esperar_final(motor, tid)
    assert 284 < recibido["restante"] <= 285
    assert recibido["task_id"] == tid


async def test_estado_final_estable_y_resumen_filtrado():
    async def ejecutor(task_id, ent, deadline):
        return Resultado(estado="fallida", detalle=f"la clave {CLAVE} y el token tok_equipo_777 no sirven",
                         datos_clave=["wss://uno/websockify"])

    motor = Motor(ejecutor, secretos=["tok_equipo_777"])
    tid = motor.crear(entrada())
    e1 = await esperar_final(motor, tid)
    await asyncio.sleep(0.05)
    e2 = motor.estado(tid)
    assert e1 == e2
    assert CLAVE not in e1.resumen and "tok_equipo_777" not in e1.resumen and "wss://uno" not in e1.resumen
    assert "[oculto]" in e1.resumen


async def test_ejecutor_puede_devolver_plan_para_el_resumen():
    plan = Plan(tipo="consultar", modulo="facturas", pregunta=Pregunta(datos=["conteo"], formato="numero"))

    async def ejecutor(task_id, ent, deadline):
        return Resultado(estado="terminada", respuesta="Total: 45", detalle="se contaron en facturas"), plan

    motor = Motor(ejecutor)
    e = await esperar_final(motor, motor.crear(entrada()))
    assert e.estado == "terminada"
    assert e.resumen.splitlines()[0] == "respuesta: 45 (Total: 45)"


async def test_task_ids_unicos_y_cortos():
    async def ejecutor(task_id, ent, deadline):
        return Resultado(estado="terminada")

    motor = Motor(ejecutor)
    ids = {motor.crear(entrada(f"wss://{i}/w")) for i in range(50)}
    assert len(ids) == 50 and all(len(i) <= 16 for i in ids)
    assert motor.estado("no-existe") is None
    await asyncio.sleep(0.05)
