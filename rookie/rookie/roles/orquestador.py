"""Orquestador: instrucción -> Plan (contrato §2).

UNA sola llamada al rol "orquestador" con la captura inicial, para leer la fecha "Hoy" y el
menú de la app antes de clasificar la tarea y resolver las fechas a absolutas.
"""

from __future__ import annotations

import logging
import re

from rookie.esquemas.mensajes import Plan
from rookie.modelos.cliente import ClienteModelos, ModeloNoDisponible
from rookie.roles.prompts import ORQUESTADOR_SYSTEM

log = logging.getLogger("rookie.roles.orquestador")


class PlanNoValido(Exception):
    """El orquestador no devolvió un plan válido tras reintentar con el error."""

    def __init__(self, motivo: str):
        super().__init__(motivo)
        self.motivo = motivo


def _resumir(exc: Exception, limite: int = 160) -> str:
    """Motivo corto y sin datos de entrada: quita `[type=…, input_value=…]` y los enlaces de
    pydantic (el valor de entrada puede traer texto del modelo)."""
    texto = re.sub(r"\[type=[^\]]*\]", "", str(exc))
    texto = re.sub(r"For further information visit \S+", "", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto[:limite] or type(exc).__name__


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
    mensajes = [{"role": "user", "content": contenido}]
    try:
        plan = await cliente.estructurado(
            "orquestador", system=ORQUESTADOR_SYSTEM, mensajes=mensajes, esquema=Plan,
            imagenes=[captura_inicial], task_id=task_id,
        )
    except ModeloNoDisponible as exc:
        # en evaluación terminó dos veces en ~22 s sin explicación: se registra y se reintenta
        # UNA vez pasando el error al modelo
        motivo = _resumir(exc)
        log.warning("task_id=%s plan no válido (intento 1): %s", task_id, motivo)
        mensajes = [{
            "role": "user",
            "content": contenido + (
                f"\n\nTu plan anterior no fue válido: {motivo}\nCorrígelo y devuelve SOLO el PLAN "
                "en JSON con todos los campos del esquema."
            ),
        }]
        try:
            plan = await cliente.estructurado(
                "orquestador", system=ORQUESTADOR_SYSTEM, mensajes=mensajes, esquema=Plan,
                imagenes=[captura_inicial], task_id=task_id,
            )
        except ModeloNoDisponible as exc2:
            motivo = _resumir(exc2)
            log.warning("task_id=%s plan no válido (intento 2): %s", task_id, motivo)
            raise PlanNoValido(motivo) from exc2
    log.info(
        "plan tipo=%s modulo=%s cambios=%d textos_permitidos=%d fecha_app=%s filtros=%s ident=%s",
        plan.tipo, plan.modulo, len(plan.cambios_autorizados),
        len(plan.textos_permitidos), plan.fecha_app, plan.filtros, list(plan.identificacion),
    )
    return plan


__all__ = ["PlanNoValido", "planear"]
