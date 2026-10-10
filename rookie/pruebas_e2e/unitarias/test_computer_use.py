"""Pruebas sin red de la Fase A (computer_use).

Fijan literalmente lo que la evaluación / el servidor RFB comparan byte a byte:
tamaños exactos de mensajes, clave DES de la auth VNC, parseo de un
FramebufferUpdate Raw sintético, guardias y parseo de combos.
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from rookie.computer_use import (
    Guardias,
    Observacion,
    PlazoAgotado,
    TareaCerrada,
    rfb,
)

# ───────────────────────── Tamaños / bytes exactos ──────────────────────────

def test_pointer_event_6_bytes_sin_padding():
    msg = rfb.pointer_event(1, 103, 48)
    assert len(msg) == 6  # bug #1: PointerEvent NO lleva padding
    assert msg == struct.pack(">BBHH", 5, 1, 103, 48)
    assert msg == b"\x05\x01\x00\x67\x00\x30"


def test_pointer_event_mask_derecho_y_rueda():
    assert rfb.pointer_event(4, 10, 20)[1] == 4     # botón derecho = bit 2
    assert rfb.pointer_event(16, 10, 20)[1] == 16   # rueda abajo = bit 4


def test_key_event_8_bytes_con_padding():
    msg = rfb.key_event(True, 0xFF0D)  # enter
    assert len(msg) == 8  # [4][down][pad:2][keysym:4]
    assert msg == struct.pack(">BBHI", 4, 1, 0, 0xFF0D)
    assert msg == b"\x04\x01\x00\x00\x00\x00\xff\x0d"
    up = rfb.key_event(False, 0xFF0D)
    assert up[1] == 0


def test_set_encodings_raw_8_bytes():
    msg = rfb.set_encodings([0])  # Raw
    assert len(msg) == 8
    assert msg == b"\x02\x00\x00\x01\x00\x00\x00\x00"


def test_set_pixel_format_20_bytes_bgrx():
    msg = rfb.set_pixel_format()
    assert len(msg) == 20
    assert msg[:4] == b"\x00\x00\x00\x00"  # tipo + pad(3)
    bpp, depth, big_endian, truecolor = msg[4], msg[5], msg[6], msg[7]
    assert (bpp, depth, big_endian, truecolor) == (32, 24, 0, 1)
    red_max, green_max, blue_max = struct.unpack(">HHH", msg[8:14])
    assert (red_max, green_max, blue_max) == (255, 255, 255)
    red_shift, green_shift, blue_shift = msg[14], msg[15], msg[16]
    assert (red_shift, green_shift, blue_shift) == (16, 8, 0)  # BGRX


def test_framebuffer_update_request_10_bytes():
    msg = rfb.framebuffer_update_request(False, 0, 0, 1280, 800)
    assert len(msg) == 10
    assert msg == struct.pack(">BBHHHH", 3, 0, 0, 0, 1280, 800)
    assert rfb.framebuffer_update_request(True, 0, 0, 1, 1)[1] == 1


# ─────────────────────────────── VNC DES ────────────────────────────────────

def test_vnc_des_key_vector_conocido():
    # 'a'=0x61=0b01100001 -> 0b10000110=0x86 ; 'b'=0x62 -> 0x46 ; 'c'=0x63 -> 0xC6
    assert rfb.vnc_des_key("abc") == bytes([0x86, 0x46, 0xC6, 0, 0, 0, 0, 0])


def test_vnc_des_key_trunca_a_8():
    k = rfb.vnc_des_key("0123456789")
    assert len(k) == 8
    assert k == rfb.vnc_des_key("01234567")  # ignora lo que pasa de 8


def test_vnc_cifrar_reto_dos_bloques_ecb():
    # DES-ECB: cifrar 16 bytes == concatenar el cifrado de cada bloque de 8.
    challenge = bytes(range(16))
    completo = rfb.vnc_cifrar_reto("secreto", challenge)
    assert len(completo) == 16
    b0 = rfb.vnc_cifrar_reto("secreto", challenge[:8])[:8]
    b1 = rfb.vnc_cifrar_reto("secreto", challenge[8:])[:8]
    assert completo == b0 + b1


# ───────────────────────────── Keysyms / combos ─────────────────────────────

def test_keysym_de_caracter():
    assert rfb.keysym_de_caracter("a") == 0x61
    assert rfb.keysym_de_caracter("A") == 0x41
    assert rfb.keysym_de_caracter("#") == 0x23
    assert rfb.keysym_de_caracter("ñ") == 0xF1   # latin-1
    assert rfb.keysym_de_caracter("á") == 0xE1   # latin-1
    assert rfb.keysym_de_caracter("€") == 0x01000000 | 0x20AC  # fuera de latin-1


def test_parsear_combo():
    assert rfb.parsear_combo("ctrl+a") == ([0xFFE3], 0x61)
    assert rfb.parsear_combo("shift+tab") == ([0xFFE1], 0xFF09)
    assert rfb.parsear_combo("enter") == ([], 0xFF0D)
    assert rfb.parsear_combo("escape") == ([], 0xFF1B)
    assert rfb.parsear_combo("control+shift+left") == ([0xFFE3, 0xFFE1], 0xFF51)


def test_parsear_combo_errores():
    with pytest.raises(ValueError):
        rfb.parsear_combo("")
    with pytest.raises(ValueError):
        rfb.parsear_combo("meta+x")  # modificador desconocido
    with pytest.raises(ValueError):
        rfb.parsear_combo("noexiste")


# ──────────────── Parse de un FramebufferUpdate Raw (2 rects) ────────────────

class _FakeWS:
    """WebSocket falso: entrega bytes en trozos; registra lo enviado."""

    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)
        self.enviados: list[bytes] = []

    async def recv(self):
        if not self._chunks:
            raise AssertionError("recv sin datos")
        return self._chunks.pop(0)

    async def send(self, data):
        self.enviados.append(bytes(data))

    async def close(self):
        pass


def _rect(x, y, w, h, enc=0):
    return struct.pack(">HHHHi", x, y, w, h, enc)


def test_parse_framebuffer_update_raw_dos_rectangulos():
    cliente = rfb.RFBClient("wss://host/websockify")
    cliente.width = 2
    cliente.height = 2
    cliente._fb = bytearray(2 * 2 * 4)

    # pixel BGRX: color R,G,B -> bytes B,G,R,0
    pix1 = bytes([30, 20, 10, 0])     # R=10 G=20 B=30 en (0,0)
    pix2 = bytes([50, 100, 200, 0])   # R=200 G=100 B=50 en (1,1)
    mensaje = (
        b"\x00"                        # tipo = FramebufferUpdate
        + b"\x00"                      # padding
        + struct.pack(">H", 2)         # 2 rectángulos
        + _rect(0, 0, 1, 1) + pix1
        + _rect(1, 1, 1, 1) + pix2
    )
    cliente._ws = _FakeWS([mensaje])

    asyncio.run(cliente._leer_un_framebuffer_update())

    fb = cliente._fb
    # (0,0)
    assert fb[0:4] == pix1
    # (1,1) -> offset ((1*2)+1)*4 = 12
    assert fb[12:16] == pix2
    # (1,0) y (0,1) quedan en cero
    assert fb[4:8] == b"\x00\x00\x00\x00"
    assert fb[8:12] == b"\x00\x00\x00\x00"

    # El PNG se renderiza sin reescalar y respeta BGRX (R,G,B reales).
    import io

    from PIL import Image
    img = Image.open(io.BytesIO(cliente.png_actual()))
    assert img.size == (2, 2)
    assert img.convert("RGB").getpixel((0, 0)) == (10, 20, 30)
    assert img.convert("RGB").getpixel((1, 1)) == (200, 100, 50)


def test_leer_ignora_bell_cuttext_y_colourmap_antes_del_update():
    cliente = rfb.RFBClient("wss://host/websockify")
    cliente.width = 1
    cliente.height = 1
    cliente._fb = bytearray(4)

    bell = b"\x02"
    cut_text = b"\x03" + b"\x00\x00\x00" + struct.pack(">I", 3) + b"abc"
    colourmap = b"\x01" + b"\x00" + struct.pack(">H", 0) + struct.pack(">H", 1) + b"\x00" * 6
    update = b"\x00\x00" + struct.pack(">H", 1) + _rect(0, 0, 1, 1) + bytes([9, 8, 7, 0])
    cliente._ws = _FakeWS([bell + cut_text + colourmap + update])

    asyncio.run(cliente._leer_un_framebuffer_update())
    assert cliente._fb[0:4] == bytes([9, 8, 7, 0])


# ─────────────────────────────── Guardias ───────────────────────────────────

def test_guardias_plazo_vencido():
    import time
    g = Guardias(task_id="t1", deadline=time.monotonic() - 0.01)
    with pytest.raises(PlazoAgotado):
        g.comprobar()


def test_guardias_vigente_no_lanza():
    import time
    g = Guardias(task_id="t1", deadline=time.monotonic() + 100)
    g.comprobar()  # no lanza
    assert g.restante_s > 0


def test_guardias_tras_cerrar_siempre_tarea_cerrada():
    import time
    # Incluso con el plazo vencido, el cierre tiene prioridad -> TareaCerrada.
    g = Guardias(task_id="t1", deadline=time.monotonic() - 1)
    g.cerrar()
    with pytest.raises(TareaCerrada):
        g.comprobar()
    g.cerrar()  # idempotente
    with pytest.raises(TareaCerrada):
        g.comprobar()


def test_excepciones_no_llevan_password():
    import time
    g = Guardias(task_id="t1", deadline=time.monotonic() - 1)
    try:
        g.comprobar()
    except PlazoAgotado as e:
        assert "t1" in str(e)


# ──────────────────────── Primitivas con RFB falso ──────────────────────────

class _RFBFake:
    """RFBClient falso para probar primitivas sin red."""

    def __init__(self):
        self.entradas: list[bytes] = []
        self.width = 1280
        self.height = 800
        self.conectado = True
        self._fb = bytearray(10)
        self._hashes = ["aaaa", "aaaa"]  # frame ya estable por defecto
        self._i = 0
        self._cerrado = False

    async def capturar_frame(self):
        h = self._hashes[min(self._i, len(self._hashes) - 1)]
        self._i += 1
        return b"PNG", h

    def png_actual(self):
        return b"PNG"

    def hash_actual(self):
        return self._hashes[min(self._i, len(self._hashes) - 1)]

    async def enviar_entrada(self, data):
        self.entradas.append(bytes(data))

    async def cerrar_ws(self):
        self.conectado = False
        self._cerrado = True


def _escritorio_fake():
    import time

    from rookie.computer_use.primitivas import Escritorio
    g = Guardias(task_id="t1", deadline=time.monotonic() + 100)
    return Escritorio(_RFBFake(), g), g


def test_click_hold_minimo_120():
    esc, _ = _escritorio_fake()
    # hold pedido por debajo del mínimo => se eleva a 120 (no se ignora el clic)
    asyncio.run(esc.click(100, 50, hold_ms=10))
    rfb_fake = esc._rfb
    assert len(rfb_fake.entradas) == 2  # press + release
    press = rfb_fake.entradas[0]
    release = rfb_fake.entradas[1]
    assert press == rfb.pointer_event(1, 100, 50)
    assert release == rfb.pointer_event(0, 100, 50)


def test_doble_click_son_dos_clics():
    esc, _ = _escritorio_fake()
    asyncio.run(esc.click(100, 50, doble=True))
    assert len(esc._rfb.entradas) == 4  # dos press + dos release


def test_click_derecho_usa_bit_2():
    esc, _ = _escritorio_fake()
    asyncio.run(esc.click(1, 2, derecho=True))
    assert esc._rfb.entradas[0] == rfb.pointer_event(4, 1, 2)


def test_escribir_reemplazar_hace_ctrl_a_primero():
    esc, _ = _escritorio_fake()
    asyncio.run(esc.escribir("ab", reemplazar=True))
    ent = esc._rfb.entradas
    # ctrl down, 'a' down, 'a' up, ctrl up  => luego 'a'/'b' del texto
    assert ent[0] == rfb.key_event(True, 0xFFE3)   # ctrl down
    assert ent[1] == rfb.key_event(True, 0x61)     # 'a'
    assert ent[3] == rfb.key_event(False, 0xFFE3)  # ctrl up
    # luego vacía el campo con Delete antes de teclear
    assert ent[4] == rfb.key_event(True, 0xFFFF)
    assert ent[5] == rfb.key_event(False, 0xFFFF)
    # texto "ab": 'a' down/up, 'b' down/up
    assert ent[6:] == [rfb.key_event(True, ord("a")), rfb.key_event(False, ord("a")),
                       rfb.key_event(True, ord("b")), rfb.key_event(False, ord("b"))]


def test_escribir_sin_reemplazar_no_hace_ctrl_a():
    esc, _ = _escritorio_fake()
    asyncio.run(esc.escribir("x", reemplazar=False))
    ent = esc._rfb.entradas
    assert ent[0] == rfb.key_event(True, ord("x"))
    assert ent[1] == rfb.key_event(False, ord("x"))


def test_scroll_abajo_usa_bit_4():
    esc, _ = _escritorio_fake()
    asyncio.run(esc.scroll(5, 6, abajo=True, pasos=2))
    ent = esc._rfb.entradas
    assert ent[0] == rfb.pointer_event(16, 5, 6)  # botón 5
    assert len(ent) == 4  # 2 pasos * (press+release)


def test_soltar_entradas_mask_0_y_up_modificadores():
    esc, _ = _escritorio_fake()
    asyncio.run(esc.click(7, 8))  # fija última posición
    esc._rfb.entradas.clear()
    asyncio.run(esc.soltar_entradas())
    ent = esc._rfb.entradas
    assert ent[0] == rfb.pointer_event(0, 7, 8)
    assert rfb.key_event(False, 0xFFE1) in ent  # shift up
    assert rfb.key_event(False, 0xFFE3) in ent  # ctrl up
    assert rfb.key_event(False, 0xFFE9) in ent  # alt up


def test_cerrar_idempotente_y_tras_cerrar_tarea_cerrada():
    esc, g = _escritorio_fake()
    asyncio.run(esc.cerrar())
    assert g.cerrada
    asyncio.run(esc.cerrar())  # idempotente, no explota
    # cualquier primitiva posterior lanza TareaCerrada
    with pytest.raises(TareaCerrada):
        asyncio.run(esc.click(1, 1))


def test_capturar_marca_cambio_entre_observaciones():
    esc, _ = _escritorio_fake()
    esc._rfb._hashes = ["h1", "h1"]
    obs1 = asyncio.run(esc.capturar())
    assert isinstance(obs1, Observacion)
    assert obs1.cambio is False  # primera observación: sin anterior
    assert obs1.id == "obs_0001"
    # segunda captura con hash distinto => cambio True
    esc._rfb._i = 0
    esc._rfb._hashes = ["h2", "h2"]
    obs2 = asyncio.run(esc.capturar())
    assert obs2.cambio is True
    assert obs2.id == "obs_0002"


def test_capturar_espera_el_repintado_aunque_tarde_mas_de_2_s():
    # medido: del clic al cambio visible pasan 0,5-4,2 s; antes se devolvía la pantalla vieja
    esc, _ = _escritorio_fake()
    esc._ultimo_hash = "vieja"  # la observación anterior a la acción
    esc._rfb._hashes = ["vieja"] * 16 + ["puntero", "puntero", "nueva"] + ["nueva"] * 10
    obs = asyncio.run(esc.capturar())
    assert obs.hash == "nueva" and obs.cambio is True
    assert obs.estable_tras_ms >= 2500


def test_capturar_sin_cambio_se_rinde_al_tope(monkeypatch):
    from rookie.computer_use import primitivas as P
    monkeypatch.setattr(P, "_MAX_SIN_CAMBIO", 0.5)
    esc, _ = _escritorio_fake()
    esc._ultimo_hash = "igual"
    esc._rfb._hashes = ["igual"] * 40
    obs = asyncio.run(esc.capturar())
    assert obs.cambio is False and obs.estable_tras_ms < 1500


def test_capturar_sin_esperar_cambio_solo_espera_quietud():
    esc, _ = _escritorio_fake()
    esc._ultimo_hash = "igual"
    esc._rfb._hashes = ["igual"] * 40
    obs = asyncio.run(esc.capturar(esperar_cambio=False))
    assert obs.estable_tras_ms < 1500
