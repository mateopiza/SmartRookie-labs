"""Orquestador: instrucción -> Plan (contrato §2).

UNA sola llamada al rol "orquestador" con la captura inicial, para leer la fecha "Hoy" y el
menú de la app antes de clasificar la tarea y resolver las fechas a absolutas.
"""

from __future__ import annotations

import logging

from rookie.esquemas.mensajes import Plan
from rookie.modelos.cliente import ClienteModelos
from rookie.roles.prompts import ORQUESTADOR_SYSTEM

log = logging.getLogger("rookie.roles.orquestador")


async def planear(
    instruccion: str,
    cliente: ClienteModelos,
    captura_inicial: bytes,
    *,
    task_id: str | None = None,
) -> Plan:
    """Convierte la instrucción en un Plan, leyendo la captura inicial (fecha 'Hoy', menú)."""
    contenido = (
        f"INSTRUCCIÓN DEL USUARIO (literal):\n{instruccion}\n\n"
        "Te muestro la captura INICIAL de la aplicación (1280x800). Lee la fecha 'Hoy' que "
        "aparece en pantalla y el menú de módulos. Devuelve el PLAN en JSON según tus reglas, "
        "con las fechas ya resueltas a absolutas desde esa fecha 'Hoy'."
    )
    plan = await cliente.estructurado(
        "orquestador",
        system=ORQUESTADOR_SYSTEM,
        mensajes=[{"role": "user", "content": contenido}],
        esquema=Plan,
        imagenes=[captura_inicial],
        task_id=task_id,
    )
    log.info(
        "plan tipo=%s modulo=%s cambios=%d textos_permitidos=%d fecha_app=%s",
        plan.tipo, plan.modulo, len(plan.cambios_autorizados),
        len(plan.textos_permitidos), plan.fecha_app,
    )
    return plan


__all__ = ["planear"]
