"""Sonda manual de la Fase A contra el escritorio REAL.

Lee rookie/.env (ESCRITORIO_URL base https, ESCRITORIO_PASSWORD), convierte la URL
a wss://<host>/websockify, conecta por RFB, captura, hace clic en el segundo ítem
del menú superior (~103,48 en SIGECOM), vuelve a capturar y guarda ambos PNG en
$TMPDIR. Imprime ms hasta frame estable y si cambió el hash.

Regla de equipo: antes de correrla, nadie debe tener noVNC abierto sobre el mismo
escritorio (un solo cliente VNC a la vez).

Uso:
    python -m pruebas_e2e.sonda_mano
La contraseña nunca se imprime ni se escribe en disco.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

# Permite ejecutar como script o con -m desde rookie/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rookie.computer_use import Escritorio, Guardias


def _cargar_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    raiz = Path(__file__).resolve().parents[1]
    env = raiz / ".env"
    if env.exists():
        load_dotenv(env)


def _a_wss(url_base: str) -> str:
    """https://host[:p]/...  ->  wss://host[:p]/websockify."""
    partes = urlsplit(url_base)
    esquema = "wss" if partes.scheme in ("https", "wss") else "ws"
    host = partes.netloc or partes.path
    return f"{esquema}://{host}/websockify"


async def _main() -> int:
    _cargar_env()
    url_base = os.environ.get("ESCRITORIO_URL", "").strip()
    password = os.environ.get("ESCRITORIO_PASSWORD", "").strip()
    if not url_base or not password:
        print("Faltan ESCRITORIO_URL / ESCRITORIO_PASSWORD en rookie/.env", file=sys.stderr)
        return 2

    wss = _a_wss(url_base)
    print(f"Conectando a {urlsplit(wss).hostname} /websockify ...")

    guardias = Guardias(task_id="sonda", deadline=time.monotonic() + 120)
    esc = await Escritorio.conectar(wss, password, guardias)

    tmp = Path(os.environ.get("TMPDIR", tempfile.gettempdir()))
    try:
        obs1 = await esc.capturar()
        (tmp / "cap1.png").write_bytes(obs1.png)
        print(f"cap1: estable_tras_ms={obs1.estable_tras_ms} hash={obs1.hash}")

        # segundo ítem del menú superior (Comercial ~103,48 en SIGECOM)
        await esc.click(103, 48)
        print("clic en (103,48)")

        obs2 = await esc.capturar()
        (tmp / "cap2.png").write_bytes(obs2.png)
        print(f"cap2: estable_tras_ms={obs2.estable_tras_ms} hash={obs2.hash} cambio={obs2.cambio}")

        print(f"PNGs en: {tmp / 'cap1.png'} , {tmp / 'cap2.png'}")
        print("CAMBIÓ LA PANTALLA" if obs2.cambio else "sin cambio de pantalla")
        return 0 if obs2.cambio else 1
    finally:
        await esc.cerrar()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
