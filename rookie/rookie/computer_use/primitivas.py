"""Primitivas deterministas sobre el escritorio VNC (contrato §2).

Toda primitiva llama guardias.comprobar() ANTES de tocar el WebSocket, así que al
cerrar la tarea (guardias.cerrar()) cualquier acción posterior lanza TareaCerrada.

Reglas observadas del escritorio (README "Lo que vimos operando"):
  - tras un clic la pantalla tarda hasta ~1 s en repintar: se captura un frame estable.
  - un clic con <100 ms de hold se ignora: hold mínimo 120 ms (default 150).
  - escribir sobre un campo con texto concatena: reemplazar=True hace Ctrl+A y Delete antes.

La contraseña entra por Escritorio.conectar y vive solo dentro del RFBClient; no se
guarda en Escritorio ni aparece en repr, logs o excepciones.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from .guardias import Guardias
from .rfb import (
    KEYSYMS,
    RFBClient,
    key_event,
    keysym_de_caracter,
    parsear_combo,
    pointer_event,
)

_log = logging.getLogger("rookie.computer_use")

# Temporizaciones (segundos)
_ESPERA_FRAME = 0.150         # entre capturas para detectar frame estable
# Medido en el escritorio real (Fase H): del clic al cambio visible pasan de 0,5 a 4,2 s, y el
# puntero y el repintado llegan con ~0,4 s de diferencia. Una captura "estable" a los 150 ms o a
# 1 s traía a menudo la pantalla VIEJA.
_VENTANA_ESTABLE = 0.6        # tras el último cambio, tanto tiempo sin cambios = frame estable
_MAX_SIN_CAMBIO = 4.5         # si nada cambia en este tiempo, la acción no tuvo efecto visible
_MAX_ESTABLE = 7.0            # tope absoluto de espera
_HOLD_MIN_MS = 120            # clic por debajo de esto se ignora
_HOLD_DEFAULT_MS = 150
_ENTRE_DOBLE = 0.080         # entre los dos clics de un doble clic
_ENTRE_TECLAS = 0.015        # entre teclas al escribir

# Máscaras de botón (button-mask del PointerEvent)
_BTN_IZQ = 1       # bit 0
_BTN_DER = 4       # bit 2
_RUEDA_ARRIBA = 8  # bit 3 (botón 4)
_RUEDA_ABAJO = 16  # bit 4 (botón 5)


@dataclass(frozen=True)
class Observacion:
    id: str
    png: bytes
    hash: str
    cambio: bool
    estable_tras_ms: int


@dataclass
class _Mods:
    """Modificadores que podrían haber quedado presionados (para soltar_entradas)."""

    activos: set[int] = field(default_factory=set)


class Escritorio:
    def __init__(self, rfb: RFBClient, guardias: Guardias) -> None:
        self._rfb = rfb
        self._guardias = guardias
        self._ultima_pos: tuple[int, int] = (0, 0)
        self._ultimo_hash: str | None = None
        self._contador_obs = 0
        self._cerrado = False
        self._mods = _Mods()

    def __repr__(self) -> str:
        return f"Escritorio({self._rfb!r}, cerrado={self._cerrado})"

    # -- conexión ------------------------------------------------------------

    @classmethod
    async def conectar(cls, url: str, password: str, guardias: Guardias) -> Escritorio:
        guardias.comprobar()
        rfb = RFBClient(url, logger=_log)
        await rfb.conectar(password)  # la contraseña no vuelve a salir de aquí
        return cls(rfb, guardias)

    # -- captura -------------------------------------------------------------

    async def capturar(self, *, esperar_cambio: bool = True) -> Observacion:
        """Captura tras una acción: espera a que la pantalla cambie respecto de la observación
        anterior y luego a que se quede quieta `_VENTANA_ESTABLE`; sin cambio en
        `_MAX_SIN_CAMBIO`, devuelve la pantalla tal cual (la acción no tuvo efecto visible).
        `esperar_cambio=False` (sin acción previa, p. ej. al verificar): solo espera quietud."""
        self._guardias.comprobar()
        t0 = time.monotonic()
        referencia = self._ultimo_hash if esperar_cambio else None
        _, h_prev = await self._rfb.capturar_frame()
        png = self._rfb.png_actual()
        h_final = h_prev
        cambio_visto = referencia is None or h_prev != referencia
        ultimo_cambio = time.monotonic()
        estable_tras_ms = int(_MAX_ESTABLE * 1000)

        while (time.monotonic() - t0) < _MAX_ESTABLE:
            await asyncio.sleep(_ESPERA_FRAME)
            self._guardias.comprobar()
            _, h_cur = await self._rfb.capturar_frame()
            ahora = time.monotonic()
            if h_cur != h_prev:
                ultimo_cambio = ahora
                h_prev = h_cur
            if referencia is not None and h_cur != referencia:
                cambio_visto = True
            png = self._rfb.png_actual()
            h_final = h_cur
            quieto = (ahora - ultimo_cambio) >= _VENTANA_ESTABLE
            if quieto and (cambio_visto or (ahora - t0) >= _MAX_SIN_CAMBIO):
                estable_tras_ms = int((ahora - t0) * 1000)
                break

        cambio = self._ultimo_hash is not None and h_final != self._ultimo_hash
        self._ultimo_hash = h_final
        self._contador_obs += 1
        obs = Observacion(
            id=f"obs_{self._contador_obs:04d}",
            png=png,
            hash=h_final,
            cambio=cambio,
            estable_tras_ms=estable_tras_ms,
        )
        _log.info(
            "captura id=%s estable_tras_ms=%d hash=%s cambio=%s",
            obs.id, obs.estable_tras_ms, obs.hash, obs.cambio,
        )
        return obs

    # -- puntero -------------------------------------------------------------

    async def click(
        self,
        x: int,
        y: int,
        *,
        hold_ms: int = _HOLD_DEFAULT_MS,
        doble: bool = False,
        derecho: bool = False,
    ) -> None:
        self._guardias.comprobar()
        hold = max(int(hold_ms), _HOLD_MIN_MS)
        mask = _BTN_DER if derecho else _BTN_IZQ
        self._ultima_pos = (x, y)
        await self._click_una_vez(x, y, mask, hold)
        if doble:
            await asyncio.sleep(_ENTRE_DOBLE)
            await self._click_una_vez(x, y, mask, hold)
        _log.info(
            "click x=%d y=%d hold=%d doble=%s derecho=%s", x, y, hold, doble, derecho
        )

    async def _click_una_vez(self, x: int, y: int, mask: int, hold_ms: int) -> None:
        await self._rfb.enviar_entrada(pointer_event(mask, x, y))
        await asyncio.sleep(hold_ms / 1000.0)
        await self._rfb.enviar_entrada(pointer_event(0, x, y))

    async def scroll(self, x: int, y: int, *, abajo: bool = True, pasos: int = 3) -> None:
        self._guardias.comprobar()
        bit = _RUEDA_ABAJO if abajo else _RUEDA_ARRIBA
        self._ultima_pos = (x, y)
        for _ in range(max(1, pasos)):
            await self._rfb.enviar_entrada(pointer_event(bit, x, y))
            await self._rfb.enviar_entrada(pointer_event(0, x, y))
            await asyncio.sleep(0.02)
        _log.info("scroll x=%d y=%d abajo=%s pasos=%d", x, y, abajo, pasos)

    # -- teclado -------------------------------------------------------------

    async def escribir(self, texto: str, *, reemplazar: bool = True) -> None:
        self._guardias.comprobar()
        if reemplazar:
            # vaciar antes de escribir: seleccionar todo y BORRAR. Sin el delete, un ctrl+a que
            # no llega a seleccionar deja el texto viejo y el nuevo se concatena (visto: una
            # dirección concatenada que luego no se pudo borrar)
            await self.tecla("ctrl+a")
            await asyncio.sleep(0.05)
            await self.tecla("delete")
            await asyncio.sleep(0.05)
        for ch in texto:
            self._guardias.comprobar()
            keysym = keysym_de_caracter(ch)
            await self._rfb.enviar_entrada(key_event(True, keysym))
            await self._rfb.enviar_entrada(key_event(False, keysym))
            await asyncio.sleep(_ENTRE_TECLAS)
        _log.info("escribir len=%d reemplazar=%s", len(texto), reemplazar)

    async def tecla(self, combo: str) -> None:
        self._guardias.comprobar()
        mods, tecla_keysym = parsear_combo(combo)
        # baja modificadores, baja/sube tecla, sube modificadores (orden inverso)
        for m in mods:
            self._mods.activos.add(m)
            await self._rfb.enviar_entrada(key_event(True, m))
        await self._rfb.enviar_entrada(key_event(True, tecla_keysym))
        await self._rfb.enviar_entrada(key_event(False, tecla_keysym))
        for m in reversed(mods):
            await self._rfb.enviar_entrada(key_event(False, m))
            self._mods.activos.discard(m)
        _log.info("tecla combo=%s", combo)

    # -- liberar / cerrar ----------------------------------------------------

    async def soltar_entradas(self) -> None:
        self._guardias.comprobar()
        await self._soltar()

    async def _soltar(self) -> None:
        x, y = self._ultima_pos
        await self._rfb.enviar_entrada(pointer_event(0, x, y))
        for m in (KEYSYMS["shift"], KEYSYMS["control"], KEYSYMS["alt"]):
            await self._rfb.enviar_entrada(key_event(False, m))
        self._mods.activos.clear()

    async def cerrar(self) -> None:
        # Idempotente. Suelta entradas (best-effort), cierra guardias y el WS.
        if self._cerrado:
            return
        self._cerrado = True
        if self._rfb.conectado:
            try:
                await self._soltar()
            except Exception:  # noqa: BLE001,S110 - best-effort al cerrar
                pass
        self._guardias.cerrar()
        await self._rfb.cerrar_ws()
        _log.info("escritorio cerrado")
