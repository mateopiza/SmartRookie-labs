"""API: auth Bearer, /health, POST /tasks inmediato, GET /tasks/{id} (desconocida ⇒ fallida), 422 sin eco del body."""

import asyncio
import time

import httpx
import pytest

from rookie.api import main as api
from rookie.esquemas.mensajes import Resultado
from rookie.motor.tareas import Motor

pytestmark = pytest.mark.asyncio

TOKEN = "token-del-equipo-de-prueba"
CLAVE = "clave-escritorio-xyz"
BODY = {"tarea": "Cuenta las facturas vencidas", "escritorio": {"url": "wss://d/websockify", "password": CLAVE}}
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("EQUIPO_TOKEN", TOKEN)


def cliente(motor: Motor | None = None) -> httpx.AsyncClient:
    app = api.crear_app(motor)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_health_auth():
    async with cliente() as c:
        assert (await c.get("/health")).status_code == 401
        assert (await c.get("/health", headers={"Authorization": "Bearer otro"})).status_code == 401
        assert (await c.get("/health", headers={"Authorization": TOKEN})).status_code == 401
        r = await c.get("/health", headers=AUTH)
        assert r.status_code == 200 and r.json() == {"ok": True}


async def test_todas_las_rutas_exigen_token():
    async with cliente() as c:
        assert (await c.post("/tasks", json=BODY)).status_code == 401
        assert (await c.get("/tasks/abc")).status_code == 401


async def test_fallback_a_escritorio_token(monkeypatch):
    monkeypatch.delenv("EQUIPO_TOKEN")
    monkeypatch.setenv("ESCRITORIO_TOKEN", "tok-escritorio")
    async with cliente() as c:
        assert (await c.get("/health", headers={"Authorization": "Bearer tok-escritorio"})).status_code == 200
        assert (await c.get("/health", headers=AUTH)).status_code == 401


async def test_sin_token_configurado_todo_es_401(monkeypatch):
    monkeypatch.delenv("EQUIPO_TOKEN")
    monkeypatch.delenv("ESCRITORIO_TOKEN", raising=False)
    async with cliente() as c:
        assert (await c.get("/health", headers={"Authorization": "Bearer "})).status_code == 401


async def test_post_tasks_responde_enseguida_aunque_el_ejecutor_tarde():
    async def lento(task_id, entrada, deadline):
        await asyncio.sleep(2)
        return Resultado(estado="terminada", respuesta="45", detalle="se contaron")

    async with cliente(Motor(lento)) as c:
        t0 = time.monotonic()
        r = await c.post("/tasks", json=BODY, headers=AUTH)
        dt = time.monotonic() - t0
        assert r.status_code == 200
        tid = r.json()["task_id"]
        assert set(r.json()) == {"task_id"}
        assert dt < 0.5, f"POST /tasks tardó {dt:.3f}s"
        e = await c.get(f"/tasks/{tid}", headers=AUTH)
        assert e.status_code == 200 and e.json() == {"estado": "en_curso", "resumen": ""}


async def test_get_tarea_terminada_y_404():
    async def rapido(task_id, entrada, deadline):
        return Resultado(estado="terminada", respuesta="45 facturas", detalle="se contaron")

    async with cliente(Motor(rapido)) as c:
        tid = (await c.post("/tasks", json=BODY, headers=AUTH)).json()["task_id"]
        await asyncio.sleep(0.05)
        e = (await c.get(f"/tasks/{tid}", headers=AUTH)).json()
        assert e["estado"] == "terminada" and e["resumen"].startswith("respuesta: 45")
        r = await c.get("/tasks/no-existe", headers=AUTH)
        assert r.status_code == 200 and r.json()["estado"] == "fallida"
        assert "tarea perdida" in r.json()["resumen"]


async def test_ejecutor_de_relleno_mientras_no_existe_el_real():
    async with cliente(Motor(api.ejecutar)) as c:
        tid = (await c.post("/tasks", json=BODY, headers=AUTH)).json()["task_id"]
        await asyncio.sleep(0.05)
        e = (await c.get(f"/tasks/{tid}", headers=AUTH)).json()
    try:
        from rookie.roles.ejecutor import ejecutar_tarea  # noqa: F401
    except ImportError:
        assert e == {"estado": "fallida", "resumen": "No se pudo completar: ejecutor no disponible."}


async def test_body_invalido_no_repite_la_contrasena():
    async with cliente() as c:
        r = await c.post("/tasks", json={"escritorio": BODY["escritorio"]}, headers=AUTH)
        assert r.status_code == 422
        assert CLAVE not in r.text


async def test_get_tasks_lista_las_tareas_en_curso():
    async with cliente() as c:
        assert (await c.get("/tasks")).status_code == 401
        r = await c.get("/tasks", headers=AUTH)
        assert r.status_code == 200 and r.json() == {"en_curso": []}
