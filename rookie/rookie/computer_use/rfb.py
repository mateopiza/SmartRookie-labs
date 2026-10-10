"""Cliente RFB 3.8 sobre WebSocket (wss://.../websockify).

Reconstruido a partir de las sondas validadas contra el escritorio real
(ver planteamineto/INFORME-estado.md §2-§3):

  - ProtocolVersion "RFB 003.008"
  - SecurityTypes [2] (VNC auth, DES)
  - ServerInit 1280x800, pixelFormat 32 bpp depth 24 little-endian truecolor
    shifts (16,8,0) => BGRX en memoria
  - Encoding Raw (0), negociado con SetEncodings ANTES del primer
    FramebufferUpdateRequest (bug #2 del informe)
  - PointerEvent = 6 bytes, SIN padding (bug #1 del informe)
  - KeyEvent = 8 bytes, con 2 bytes de padding

Una conexión persistente por tarea; buffer propio (take) por encima de los frames
WS; reconexión con backoff (0.5 / 1 / 2 s) si el WS se cae a mitad de tarea.

La contraseña entra por parametro, vive solo en un atributo privado con mangling
y nunca aparece en repr, logs ni excepciones.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import struct
from urllib.parse import urlsplit

import websockets
from PIL import Image

try:  # pycryptodome
    from Crypto.Cipher import DES
except ImportError:  # pragma: no cover - pycryptodome esta en pyproject
    DES = None  # type: ignore

_log = logging.getLogger("rookie.computer_use")

# ─────────────────────────────── Keysyms X11 ────────────────────────────────

KEYSYMS: dict[str, int] = {
    "enter": 0xFF0D,
    "return": 0xFF0D,
    "escape": 0xFF1B,
    "esc": 0xFF1B,
    "tab": 0xFF09,
    "backspace": 0xFF08,
    "delete": 0xFFFF,
    "left": 0xFF51,
    "up": 0xFF52,
    "right": 0xFF53,
    "down": 0xFF54,
    "home": 0xFF50,
    "end": 0xFF57,
    "space": 0x0020,
    "shift": 0xFFE1,
    "control": 0xFFE3,
    "ctrl": 0xFFE3,
    "alt": 0xFFE9,
}

MODIFICADORES: dict[str, int] = {
    "shift": 0xFFE1,
    "control": 0xFFE3,
    "ctrl": 0xFFE3,
    "alt": 0xFFE9,
}


def keysym_de_caracter(ch: str) -> int:
    """Keysym de un carácter: latin-1 => su codepoint; fuera de latin-1 => 0x01000000|cp."""
    cp = ord(ch)
    if cp <= 0xFF:
        return cp
    return 0x01000000 | cp


def parsear_combo(combo: str) -> tuple[list[int], int]:
    """Parsea "ctrl+a", "shift+tab", "enter"... -> (keysyms_modificadores, keysym_tecla)."""
    partes = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not partes:
        raise ValueError("combo vacío")
    *mods, tecla = partes
    mods_keysyms: list[int] = []
    for m in mods:
        if m not in MODIFICADORES:
            raise ValueError(f"modificador desconocido: {m!r}")
        mods_keysyms.append(MODIFICADORES[m])
    if tecla in KEYSYMS:
        tecla_keysym = KEYSYMS[tecla]
    elif len(tecla) == 1:
        tecla_keysym = keysym_de_caracter(tecla)
    else:
        raise ValueError(f"tecla desconocida: {tecla!r}")
    return mods_keysyms, tecla_keysym


# ───────────────────────── Constructores de mensajes ────────────────────────
# Tamaños exactos fijados por tests (planteamineto §3).

def pointer_event(mask: int, x: int, y: int) -> bytes:
    """PointerEvent = 6 bytes [5][mask][x:2][y:2] SIN padding."""
    return struct.pack(">BBHH", 5, mask & 0xFF, x, y)


def key_event(down: bool, keysym: int) -> bytes:
    """KeyEvent = 8 bytes [4][down][pad:2][keysym:4]."""
    return struct.pack(">BBHI", 4, 1 if down else 0, 0, keysym)


def set_encodings(encodings: list[int]) -> bytes:
    """SetEncodings = [2][pad:1][n:2][encoding:4]*n. Para Raw: [0] => 8 bytes."""
    msg = struct.pack(">BBH", 2, 0, len(encodings))
    for e in encodings:
        msg += struct.pack(">i", e)
    return msg


def set_pixel_format() -> bytes:
    """SetPixelFormat = [0][pad:3][pixel-format:16] = 20 bytes. BGRX: shifts (16,8,0)."""
    pf = struct.pack(
        ">BBBBHHHBBB3x",
        32,   # bits-per-pixel
        24,   # depth
        0,    # big-endian-flag (0 = little endian)
        1,    # true-colour-flag
        255,  # red-max
        255,  # green-max
        255,  # blue-max
        16,   # red-shift
        8,    # green-shift
        0,    # blue-shift
    )
    return struct.pack(">BBBB", 0, 0, 0, 0) + pf


def framebuffer_update_request(incremental: bool, x: int, y: int, w: int, h: int) -> bytes:
    """FramebufferUpdateRequest = [3][incremental][x:2][y:2][w:2][h:2] = 10 bytes."""
    return struct.pack(">BBHHHH", 3, 1 if incremental else 0, x, y, w, h)


# ──────────────────────────────── VNC auth ──────────────────────────────────

def _reverse_bits(b: int) -> int:
    r = 0
    for _ in range(8):
        r = (r << 1) | (b & 1)
        b >>= 1
    return r


def vnc_des_key(password: str) -> bytes:
    """Clave DES de la auth VNC: password a 8 bytes (trunca/rellena) con bits invertidos por byte."""
    pw = password.encode("latin-1", "ignore")[:8]
    pw = pw + b"\x00" * (8 - len(pw))
    return bytes(_reverse_bits(b) for b in pw)


def vnc_cifrar_reto(password: str, challenge: bytes) -> bytes:
    """Cifra el reto de 16 bytes con DES-ECB usando la clave VNC (dos bloques de 8)."""
    if DES is None:  # pragma: no cover
        raise RuntimeError("pycryptodome no disponible")
    cipher = DES.new(vnc_des_key(password), DES.MODE_ECB)
    return cipher.encrypt(challenge)


# ────────────────────────────── Excepciones ─────────────────────────────────

class RFBError(Exception):
    """Fallo del protocolo RFB. Nunca contiene la contraseña."""


def _host(url: str) -> str:
    try:
        return urlsplit(url).hostname or "?"
    except Exception:  # noqa: BLE001
        return "?"


# ──────────────────────────────── Cliente ───────────────────────────────────

_BACKOFF = (0.5, 1.0, 2.0)
_RECV_TIMEOUT = 20.0


class RFBClient:
    """Una conexión RFB persistente por tarea, sobre un WebSocket websockify."""

    def __init__(self, url: str, *, logger: logging.Logger | None = None) -> None:
        self._url = url
        self._log = logger or _log
        self._ws = None
        self._buf = bytearray()
        self._fb: bytearray | None = None
        self.width = 0
        self.height = 0
        self.name = ""
        self.__password: str | None = None  # mangling: _RFBClient__password

    def __repr__(self) -> str:  # nunca expone la contraseña
        return f"RFBClient(host={_host(self._url)!r}, {self.width}x{self.height})"

    # -- ciclo de vida -------------------------------------------------------

    async def conectar(self, password: str) -> None:
        self.__password = password
        await self._abrir()
        self._log.info("RFB conectado host=%s %dx%d", _host(self._url), self.width, self.height)

    async def _abrir(self) -> None:
        self._ws = await websockets.connect(
            self._url, subprotocols=["binary"], max_size=None, open_timeout=15
        )
        self._buf.clear()
        await self._handshake(self.__password or "")

    async def cerrar_ws(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001,S110 - cierre best-effort
                pass
            self._ws = None

    @property
    def conectado(self) -> bool:
        return self._ws is not None

    # -- buffer / IO ---------------------------------------------------------

    async def _take(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = await asyncio.wait_for(self._ws.recv(), timeout=_RECV_TIMEOUT)
            if isinstance(chunk, str):
                chunk = chunk.encode("latin-1")
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    async def _send(self, data: bytes) -> None:
        await self._ws.send(data)

    async def _reconectar(self) -> None:
        ultimo: Exception | None = None
        for intento, espera in enumerate(_BACKOFF, 1):
            await asyncio.sleep(espera)
            try:
                await self.cerrar_ws()
                await self._abrir()
                self._log.info("reconexión RFB ok (intento %d/3)", intento)
                return
            except Exception as e:  # noqa: BLE001
                ultimo = e
                self._log.warning(
                    "reconexión RFB falló (intento %d/3): %s", intento, type(e).__name__
                )
        raise RFBError("reconexión agotada tras 3 intentos") from ultimo

    # -- handshake -----------------------------------------------------------

    async def _handshake(self, password: str) -> None:
        version = await self._take(12)
        if not version.startswith(b"RFB "):
            raise RFBError("ProtocolVersion inesperada")
        await self._send(b"RFB 003.008\n")

        n = (await self._take(1))[0]
        if n == 0:
            reason_len = struct.unpack(">I", await self._take(4))[0]
            await self._take(reason_len)
            raise RFBError("el servidor rechazó la conexión en SecurityTypes")
        tipos = await self._take(n)
        if 2 not in tipos:
            raise RFBError("VNC auth (2) no ofrecido por el servidor")
        await self._send(bytes([2]))

        challenge = await self._take(16)
        await self._send(vnc_cifrar_reto(password, challenge))

        resultado = struct.unpack(">I", await self._take(4))[0]
        if resultado != 0:
            # En 3.8 puede venir una razón; no la propagamos para no filtrar nada.
            raise RFBError("autenticación VNC rechazada")

        # ClientInit: shared-flag = 1 (no expulsar a otros, aunque esperamos ser únicos).
        await self._send(bytes([1]))

        # ServerInit: width(2) height(2) pixel-format(16) name-length(4) name
        cab = await self._take(24)
        self.width, self.height = struct.unpack(">HH", cab[:4])
        name_len = struct.unpack(">I", cab[20:24])[0]
        self.name = (await self._take(name_len)).decode("latin-1", "replace")
        self._fb = bytearray(self.width * self.height * 4)

        # Orden crítico: SetEncodings Raw ANTES del primer FramebufferUpdateRequest.
        await self._send(set_pixel_format())
        await self._send(set_encodings([0]))  # 0 = Raw

    # -- framebuffer ---------------------------------------------------------

    async def capturar_frame(self) -> tuple[bytes, str]:
        """Pide el framebuffer completo, lo lee y devuelve (png_bytes, md5_pixeles)."""
        try:
            return await self._pedir_y_leer()
        except (TimeoutError, websockets.ConnectionClosed, OSError):
            self._log.warning("WS caído durante captura; reconectando")
            await self._reconectar()
            return await self._pedir_y_leer()

    async def _pedir_y_leer(self) -> tuple[bytes, str]:
        # Petición NO incremental: el servidor siempre responde con la pantalla completa,
        # lo que hace deterministas el hash y la detección de frame estable.
        await self._send(framebuffer_update_request(False, 0, 0, self.width, self.height))
        await self._leer_un_framebuffer_update()
        return self.png_actual(), self.hash_actual()

    async def _leer_un_framebuffer_update(self) -> None:
        # Procesa mensajes del servidor hasta consumir un FramebufferUpdate (tipo 0).
        while True:
            tipo = (await self._take(1))[0]
            if tipo == 0:
                await self._leer_rectangulos()
                return
            elif tipo == 1:
                await self._leer_colourmap()
            elif tipo == 2:
                self._log.debug("Bell")  # campana: nada que hacer
            elif tipo == 3:
                await self._leer_cut_text()
            else:
                raise RFBError(f"mensaje de servidor desconocido: {tipo}")

    async def _leer_rectangulos(self) -> None:
        cab = await self._take(3)  # pad(1) + number-of-rectangles(2)
        (num,) = struct.unpack(">xH", cab)
        for _ in range(num):
            rh = await self._take(12)
            x, y, w, h, enc = struct.unpack(">HHHHi", rh)
            if enc == 0:  # Raw
                datos = await self._take(w * h * 4)
                self._blit(x, y, w, h, datos)
            else:
                raise RFBError(f"encoding {enc} no soportado (solo Raw)")

    def _blit(self, x: int, y: int, w: int, h: int, datos: bytes) -> None:
        assert self._fb is not None
        stride = self.width * 4
        fila_bytes = w * 4
        for fila in range(h):
            src = fila * fila_bytes
            dst = (y + fila) * stride + x * 4
            self._fb[dst : dst + fila_bytes] = datos[src : src + fila_bytes]

    async def _leer_colourmap(self) -> None:
        # SetColourMapEntries: pad(1) first-colour(2) number-of-colours(2) + 6*n
        cab = await self._take(5)
        _, _first, num = struct.unpack(">BHH", cab)
        if num:
            await self._take(num * 6)

    async def _leer_cut_text(self) -> None:
        # ServerCutText: pad(3) length(4) + texto
        cab = await self._take(7)
        (length,) = struct.unpack(">3xI", cab)
        if length:
            await self._take(length)

    # -- entradas (con reconexión) ------------------------------------------

    async def enviar_entrada(self, data: bytes) -> None:
        try:
            await self._send(data)
        except (websockets.ConnectionClosed, OSError):
            self._log.warning("WS caído enviando entrada; reconectando")
            await self._reconectar()
            await self._send(data)

    # -- render --------------------------------------------------------------

    def hash_actual(self) -> str:
        assert self._fb is not None
        return hashlib.md5(bytes(self._fb)).hexdigest()

    def png_actual(self) -> bytes:
        assert self._fb is not None
        # El framebuffer está en BGRX; rawmode "BGRX" descarta el byte X.
        img = Image.frombuffer(
            "RGB", (self.width, self.height), bytes(self._fb), "raw", "BGRX", 0, 1
        )
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
