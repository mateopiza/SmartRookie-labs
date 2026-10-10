"""FastAPI del agente: GET /health, POST /tasks (responde enseguida), GET /tasks/{task_id}.

Todas las rutas exigen `Authorization: Bearer <EQUIPO_TOKEN>` (si falta, vale ESCRITORIO_TOKEN).
Variables desde el entorno; rookie/.env se carga con python-dotenv si existe (sin pisar el entorno).
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from rookie.esquemas.mensajes import EstadoTarea, Resultado, TareaCreada, TareaEntrada
from rookie.motor.tareas import Motor

_ENV = Path(__file__).resolve().parents[2] / ".env"  # rookie/.env
if _ENV.is_file():
    load_dotenv(_ENV, override=False)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("rookie.api")


def token_equipo() -> str:
    return os.environ.get("EQUIPO_TOKEN") or os.environ.get("ESCRITORIO_TOKEN") or ""


def secretos_globales() -> list[str]:
    return [s for s in (token_equipo(), os.environ.get("OPENROUTER_API_KEY", "")) if s]


def exigir_token(request: Request) -> None:
    esperado = token_equipo()
    recibido = request.headers.get("authorization", "")
    if not esperado or not secrets.compare_digest(recibido.encode(), f"Bearer {esperado}".encode()):
        raise HTTPException(status_code=401, detail="no autorizado", headers={"WWW-Authenticate": "Bearer"})


_aviso_relleno = False


async def ejecutar(task_id: str, entrada: TareaEntrada, deadline: float):
    """Ejecutor real con import perezoso; mientras la fase D no exista, uno de relleno."""
    global _aviso_relleno
    try:
        from rookie.roles.ejecutor import ejecutar_tarea
    except ImportError:
        if not _aviso_relleno:
            log.warning("rookie.roles.ejecutor.ejecutar_tarea no disponible: ejecutor de relleno")
            _aviso_relleno = True
        return Resultado(estado="fallida", detalle="ejecutor no disponible")
    return await ejecutar_tarea(task_id, entrada, deadline)


def crear_motor() -> Motor:
    plazo = float(os.environ.get("PLAZO_S") or 285)
    return Motor(ejecutar, plazo_s=plazo, secretos=secretos_globales())


def crear_app(motor: Motor | None = None) -> FastAPI:
    app = FastAPI(title="Rookie", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.motor = motor or crear_motor()

    @app.exception_handler(RequestValidationError)
    async def _validacion(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Sin "input": el cuerpo inválido puede traer la contraseña del escritorio.
        errores = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", "")} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": errores})

    rutas = APIRouter(dependencies=[Depends(exigir_token)])

    @rutas.get("/health")
    async def health() -> dict:
        return {"ok": True}

    @rutas.post("/tasks", response_model=TareaCreada)
    async def crear_tarea(entrada: TareaEntrada, request: Request) -> TareaCreada:
        return TareaCreada(task_id=request.app.state.motor.crear(entrada))

    @rutas.get("/tasks/{task_id}", response_model=EstadoTarea)
    async def estado_tarea(task_id: str, request: Request) -> EstadoTarea:
        estado = request.app.state.motor.estado(task_id)
        if estado is None:
            raise HTTPException(status_code=404, detail="tarea no encontrada")
        return estado

    app.include_router(rutas)
    return app


app = crear_app()
