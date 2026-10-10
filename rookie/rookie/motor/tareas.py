"""Motor de tareas: estado en memoria, plazo duro, un lock por escritorio, resumen ya filtrado.

- crear() registra la tarea en_curso y lanza el worker con asyncio.create_task; no espera nada.
- Un asyncio.Lock por escritorio.url: dos tareas sobre el mismo escritorio se serializan (la
  segunda espera; no se rechaza).
- El plazo cuenta desde crear(): el evaluador corta a los 5 minutos de POST /tasks, también si la
  tarea pasó tiempo esperando el lock.
- Al terminar (bien, por plazo o por excepción) el estado queda fijo y GET devuelve siempre lo
  mismo. Una excepción se reporta solo con el nombre de su clase: nunca la traza ni el mensaje,
  que podrían llevar secretos.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from rookie.esquemas.mensajes import EstadoTarea, Plan, Resultado, TareaEntrada
from rookie.reporte.plantillas import armar_resumen

log = logging.getLogger("rookie.motor")

# (task_id, entrada, deadline_monotonic) -> Resultado.
# Extensión compatible: también puede devolver (Resultado, Plan) para que el resumen use el plan
# (formato número, orden de los datos, módulo). Ver planteamineto/CAMBIOS-CONTRATO.md.
Ejecutor = Callable[[str, TareaEntrada, float], Awaitable[Resultado | tuple[Resultado, Plan | None]]]

DETALLE_PLAZO = "plazo agotado"


class Motor:
    def __init__(self, ejecutar: Ejecutor, *, plazo_s: float = 285.0, secretos: list[str] | None = None):
        self._ejecutar = ejecutar
        self.plazo_s = float(plazo_s)
        self._secretos = [s for s in (secretos or []) if s]
        self._estados: dict[str, EstadoTarea] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._tareas: dict[str, asyncio.Task] = {}  # referencias fuertes: el GC no las cancela

    # ── API pública (contrato §2) ────────────────────────────────────────────

    def crear(self, entrada: TareaEntrada) -> str:
        task_id = uuid.uuid4().hex[:12]
        while task_id in self._estados:
            task_id = uuid.uuid4().hex[:12]
        self._estados[task_id] = EstadoTarea(estado="en_curso")
        deadline = time.monotonic() + self.plazo_s
        tarea = asyncio.create_task(self._worker(task_id, entrada, deadline), name=f"tarea-{task_id}")
        self._tareas[task_id] = tarea
        tarea.add_done_callback(lambda _t, tid=task_id: self._tareas.pop(tid, None))
        log.info("task_id=%s estado=en_curso", task_id)
        return task_id

    def estado(self, task_id: str) -> EstadoTarea | None:
        return self._estados.get(task_id)

    def en_curso(self) -> list[str]:
        """Ids de las tareas que siguen en_curso (el harness comprueba que no haya ajenas)."""
        return [tid for tid, e in self._estados.items() if e.estado == "en_curso"]

    # ── interno ──────────────────────────────────────────────────────────────

    def _lock(self, url: str) -> asyncio.Lock:
        lock = self._locks.get(url)
        if lock is None:
            lock = self._locks[url] = asyncio.Lock()
        return lock

    def _secretos_de(self, entrada: TareaEntrada) -> list[str]:
        return [
            *self._secretos,
            entrada.escritorio.password.get_secret_value(),
            entrada.escritorio.url,
        ]

    async def _worker(self, task_id: str, entrada: TareaEntrada, deadline: float) -> None:
        inicio = time.monotonic()
        plan: Plan | None = None
        try:
            async with self._lock(entrada.escritorio.url):
                restante = deadline - time.monotonic()
                if restante <= 0:
                    raise TimeoutError
                salida = await asyncio.wait_for(self._ejecutar(task_id, entrada, deadline), timeout=restante)
            if isinstance(salida, tuple):
                resultado, plan = salida
            else:
                resultado = salida
            if not isinstance(resultado, Resultado):
                raise TypeError("el ejecutor no devolvió un Resultado")
        except TimeoutError:
            resultado = Resultado(estado="fallida", detalle=DETALLE_PLAZO)
        except Exception as exc:  # noqa: BLE001 — se reporta solo la clase, sin traza
            if type(exc).__name__ == "PlazoAgotado":  # computer_use.guardias (fase A)
                resultado = Resultado(estado="fallida", detalle=DETALLE_PLAZO)
            else:
                resultado = Resultado(estado="fallida", detalle=f"error interno: {type(exc).__name__}")
            log.warning("task_id=%s excepción del ejecutor: %s", task_id, type(exc).__name__)
        except asyncio.CancelledError:
            # Apagado del proceso: dejar un estado final y propagar la cancelación.
            self._fijar(task_id, Resultado(estado="fallida", detalle="tarea cancelada"), None, entrada, inicio)
            raise
        self._fijar(task_id, resultado, plan, entrada, inicio)

    def _fijar(self, task_id: str, resultado: Resultado, plan: Plan | None, entrada: TareaEntrada, inicio: float):
        actual = self._estados.get(task_id)
        if actual is not None and actual.estado != "en_curso":
            return  # el estado final no cambia nunca
        secretos = self._secretos_de(entrada)
        try:
            resumen = armar_resumen(resultado, plan, secretos)
        except Exception as exc:  # noqa: BLE001
            log.error("task_id=%s fallo al armar el resumen: %s", task_id, type(exc).__name__)
            resultado = Resultado(estado="fallida", detalle=f"error interno: {type(exc).__name__}")
            resumen = armar_resumen(resultado, None, secretos)
        self._estados[task_id] = EstadoTarea(estado=resultado.estado, resumen=resumen)
        log.info("task_id=%s estado=%s duracion_s=%.1f", task_id, resultado.estado, time.monotonic() - inicio)


__all__ = ["DETALLE_PLAZO", "Ejecutor", "Motor"]
