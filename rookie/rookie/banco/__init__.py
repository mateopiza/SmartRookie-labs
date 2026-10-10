"""Grabación de observaciones por tarea (material de depuración de la Fase H y del juez offline).

Cada paso guarda rookie/banco/observaciones/<task_id>/<n>.png y <n>.json. El directorio está
gitignoreado. La grabación es best-effort: nunca interrumpe la ejecución ni propaga errores.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

_log = logging.getLogger("rookie.banco")


def dir_observaciones() -> Path:
    env = os.environ.get("BANCO_OBSERVACIONES")
    if env:
        return Path(env)
    # .../rookie/rookie/banco/__init__.py -> parents[2] = rookie/ (paquete externo)
    return Path(__file__).resolve().parents[2] / "banco" / "observaciones"


class Grabador:
    """Graba (png, acción, metadatos) por paso bajo <base>/<task_id>/<n>.{png,json}."""

    def __init__(self, task_id: str, *, base: str | Path | None = None) -> None:
        self.task_id = task_id
        self.n = 0
        self._activo = True
        base_dir = Path(base) if base is not None else dir_observaciones()
        self.dir = base_dir / task_id
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001 - la grabación no puede tumbar la tarea
            self._activo = False
            _log.warning("grabación desactivada para %s: %s", task_id, type(exc).__name__)

    def paso(
        self,
        *,
        png: bytes | None,
        accion: dict[str, Any],
        meta: dict[str, Any] | None = None,
    ) -> int:
        if not self._activo:
            return self.n
        self.n += 1
        n = self.n
        try:
            if png is not None:
                (self.dir / f"{n}.png").write_bytes(png)
            registro: dict[str, Any] = {"n": n, "task_id": self.task_id, "accion": accion}
            if meta:
                registro.update(meta)
            (self.dir / f"{n}.json").write_text(
                json.dumps(registro, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("no se pudo grabar el paso %d de %s: %s", n, self.task_id, type(exc).__name__)
        return n


__all__ = ["Grabador", "dir_observaciones"]
