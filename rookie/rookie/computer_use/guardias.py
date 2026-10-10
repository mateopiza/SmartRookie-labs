"""Guardias por código: se llaman antes de cada primitiva que toca el WebSocket.

Dos condiciones de parada:
  - plazo vencido (restante_s <= 0) -> PlazoAgotado
  - tarea cerrada (tras cerrar())  -> TareaCerrada  (prioritaria)

Esto es lo que garantiza "detenerse al reportar": en cuanto el orquestador o el
propio Escritorio llaman guardias.cerrar(), cualquier primitiva posterior lanza
TareaCerrada y no vuelve a tocar el escritorio.

Ninguna excepción lleva la contraseña: solo el task_id.
"""

from __future__ import annotations

import time


class PlazoAgotado(Exception):
    """Se agotó el presupuesto de tiempo de la tarea."""


class TareaCerrada(Exception):
    """La tarea ya se cerró; ninguna primitiva debe volver a operar el escritorio."""


class Guardias:
    def __init__(self, *, task_id: str, deadline: float) -> None:
        # deadline es un instante absoluto de time.monotonic().
        self._task_id = task_id
        self._deadline = float(deadline)
        self._cerrada = False

    def comprobar(self) -> None:
        # El cierre tiene prioridad sobre el plazo: tras cerrar() siempre TareaCerrada.
        if self._cerrada:
            raise TareaCerrada(self._task_id)
        if self.restante_s <= 0:
            raise PlazoAgotado(self._task_id)

    def cerrar(self) -> None:
        # Idempotente: a partir de aquí comprobar() lanza siempre TareaCerrada.
        self._cerrada = True

    @property
    def restante_s(self) -> float:
        return self._deadline - time.monotonic()

    @property
    def cerrada(self) -> bool:
        return self._cerrada

    def __repr__(self) -> str:
        return f"Guardias(task_id={self._task_id!r}, restante_s={self.restante_s:.1f}, cerrada={self._cerrada})"
