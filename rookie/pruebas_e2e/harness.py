"""Harness E1-E5 sobre el escritorio propio.

Por cada prueba de `pruebas/`:
  1. POST {ESCRITORIO_URL}/__reset  (Bearer ESCRITORIO_TOKEN) y espera GET /health ok.
  2. POST {AGENTE_URL}/tasks  (Bearer EQUIPO_TOKEN) con la instrucción y el escritorio
     (url wss://<host>/websockify, password); exige respuesta en < 2 s.
  3. Poll GET /tasks/{id} cada 5 s hasta estado != en_curso o 300 s.
  4. POST {ESCRITORIO_URL}/__check con el JSON de la prueba.
  5. Checks locales sobre estado y resumen.
  6. Tabla por prueba + resumen N/5; exit 1 si algo falla.

Cada corrida se guarda en rookie/pruebas_e2e/corridas/<timestamp>/<id>.json.

Reglas duras:
  - Nunca abre una conexión VNC desde el harness.
  - Nunca imprime la contraseña ni el token del escritorio.

Uso:
    python -m pruebas_e2e.harness [--agente URL] [--repeticiones N] [--sin-reset] [E1 E2 ...]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

# Permite ejecutar como script o con -m desde rookie/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RAIZ = Path(__file__).resolve().parents[1]          # rookie/
REPO = RAIZ.parent                                   # raíz del reto
DIR_PRUEBAS = REPO / "pruebas"
DIR_CORRIDAS = RAIZ / "pruebas_e2e" / "corridas"

IDS_POR_DEFECTO = ["E1", "E2", "E3", "E4", "E5"]

_NUMERO = re.compile(r"[-+]?\d+(?:[.,]\d+)*")
_SOLO_NUMERO = re.compile(r"^[-+]?\d+(?:[.,]\d+)*$")
_ETIQUETA = re.compile(r"^\s*respuesta\s*:\s*", re.IGNORECASE)


# ── carga de entorno ──────────────────────────────────────────────────────────


def _cargar_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    env = RAIZ / ".env"
    if env.exists():
        load_dotenv(env)


def _base_https(url: str) -> str:
    """Devuelve el origen https://host[:p] de una URL base o websockify."""
    partes = urlsplit(url)
    host = partes.netloc or partes.path
    esquema = "https" if partes.scheme in ("https", "wss") else "http"
    # si venía sin esquema, path podría traer una ruta; nos quedamos con el host
    host = host.split("/", 1)[0]
    return f"{esquema}://{host}"


def _a_wss(url: str) -> str:
    """https://host[:p]/...  ->  wss://host[:p]/websockify."""
    partes = urlsplit(url)
    host = partes.netloc or partes.path
    host = host.split("/", 1)[0]
    esquema = "wss" if partes.scheme in ("https", "wss") else "ws"
    return f"{esquema}://{host}/websockify"


# ── configuración ─────────────────────────────────────────────────────────────


@dataclass
class Config:
    agente_url: str
    escritorio_base: str            # https://host
    escritorio_wss: str             # wss://host/websockify
    escritorio_password: str
    escritorio_token: str
    equipo_token: str
    sin_reset: bool = False
    repeticiones: int = 1
    intervalo_poll_s: float = 5.0
    timeout_tarea_s: float = 300.0
    plazo_post_tasks_s: float = 2.0
    salud_timeout_s: float = 60.0
    dormir: Callable[[float], None] = time.sleep
    reloj: Callable[[], float] = time.monotonic

    @property
    def secretos(self) -> list[str]:
        return [s for s in (self.escritorio_password, self.escritorio_token) if s]


def cargar_config(
    *,
    agente: str | None = None,
    repeticiones: int = 1,
    sin_reset: bool = False,
) -> Config:
    _cargar_env()
    escritorio_url = (os.environ.get("ESCRITORIO_URL") or "").strip()
    if not escritorio_url:
        raise SystemExit("Falta ESCRITORIO_URL en rookie/.env")
    escritorio_token = (os.environ.get("ESCRITORIO_TOKEN") or "").strip()
    equipo_token = (os.environ.get("EQUIPO_TOKEN") or "").strip() or escritorio_token
    agente_url = (agente or os.environ.get("AGENTE_URL") or "http://localhost:8000").strip()
    return Config(
        agente_url=agente_url.rstrip("/"),
        escritorio_base=_base_https(escritorio_url),
        escritorio_wss=_a_wss(escritorio_url),
        escritorio_password=(os.environ.get("ESCRITORIO_PASSWORD") or "").strip(),
        escritorio_token=escritorio_token,
        equipo_token=equipo_token,
        sin_reset=sin_reset,
        repeticiones=max(1, repeticiones),
    )


# ── análisis del resumen (puro, testeable sin red) ────────────────────────────


def es_pregunta(instruccion: str) -> bool:
    return "?" in instruccion or "¿" in instruccion


def linea_respuesta(resumen: str) -> str | None:
    """Texto tras `respuesta:` de la primera línea que empieza así; None si no hay."""
    for linea in (resumen or "").splitlines():
        if _ETIQUETA.match(linea):
            return _ETIQUETA.sub("", linea, count=1).strip()
    return None


def primer_numero(texto: str) -> str | None:
    m = _NUMERO.search(texto or "")
    return m.group(0) if m else None


def _es_numerico(texto: str) -> bool:
    return bool(_SOLO_NUMERO.match((texto or "").strip()))


def respuesta_coincide(valor_linea: str, esperado: str) -> bool:
    """Compara la parte de datos (`<d1>; <d2>`) con lo esperado.

    Cada dato: si lo esperado es numérico, basta que el primer número coincida;
    si no, igualdad de texto. Dos datos van separados por ';' y en orden.
    """
    esps = [p.strip() for p in str(esperado).split(";")]
    acts = [p.strip() for p in (valor_linea or "").split(";")]
    if len(acts) < len(esps):
        return False
    for esp, act in zip(esps, acts):
        if _es_numerico(esp):
            na, ne = primer_numero(act), primer_numero(esp)
            if na is None or ne is None or na != ne:
                return False
        elif act != esp:
            return False
    return True


def contiene_secreto(resumen: str, secretos: list[str]) -> bool:
    texto = resumen or ""
    return any(s and s in texto for s in secretos)


# ── checks locales ────────────────────────────────────────────────────────────


@dataclass
class Check:
    nombre: str
    ok: bool
    fuente: str = "local"   # "local" o "__check"


def checks_locales(prueba: dict[str, Any], estado: str, resumen: str, secretos: list[str]) -> list[Check]:
    checks: list[Check] = []
    esperado = prueba.get("estado_esperado", "")
    checks.append(Check("estado esperado", estado == esperado))

    pregunta = es_pregunta(prueba.get("instruccion", ""))

    if "respuesta_esperada" in prueba:
        valor = linea_respuesta(resumen)
        ok = valor is not None and respuesta_coincide(valor, prueba["respuesta_esperada"])
        checks.append(Check("respuesta correcta", ok))

    if prueba.get("claves_razon"):
        faltan = [c for c in prueba["claves_razon"] if str(c) not in (resumen or "")]
        checks.append(Check("claves de razón presentes", not faltan))

    if esperado == "fallida" and pregunta:
        checks.append(Check("fallida sin línea respuesta", linea_respuesta(resumen) is None))

    checks.append(Check("sin secreto en el resumen", not contiene_secreto(resumen, secretos)))
    return checks


# ── pasos con red ─────────────────────────────────────────────────────────────


def esperar_salud(cliente: httpx.Client, cfg: Config) -> bool:
    fin = cfg.reloj() + cfg.salud_timeout_s
    while True:
        try:
            r = cliente.get(f"{cfg.escritorio_base}/health", timeout=10.0)
            if r.status_code == 200 and r.json().get("ok") is True:
                return True
        except (httpx.HTTPError, ValueError):
            pass
        if cfg.reloj() >= fin:
            return False
        cfg.dormir(1.0)


def resetear(cliente: httpx.Client, cfg: Config) -> None:
    cliente.post(
        f"{cfg.escritorio_base}/__reset",
        headers={"Authorization": f"Bearer {cfg.escritorio_token}"},
        timeout=30.0,
    )


def lanzar_tarea(cliente: httpx.Client, cfg: Config, instruccion: str) -> tuple[str, float]:
    cuerpo = {
        "tarea": instruccion,
        "escritorio": {"url": cfg.escritorio_wss, "password": cfg.escritorio_password},
    }
    t0 = cfg.reloj()
    r = cliente.post(
        f"{cfg.agente_url}/tasks",
        json=cuerpo,
        headers={"Authorization": f"Bearer {cfg.equipo_token}"},
        timeout=10.0,
    )
    dt = cfg.reloj() - t0
    r.raise_for_status()
    task_id = str(r.json().get("task_id", ""))
    return task_id, dt


def esperar_resultado(cliente: httpx.Client, cfg: Config, task_id: str) -> dict[str, Any]:
    fin = cfg.reloj() + cfg.timeout_tarea_s
    ultimo: dict[str, Any] = {"estado": "en_curso", "resumen": ""}
    while True:
        try:
            r = cliente.get(
                f"{cfg.agente_url}/tasks/{task_id}",
                headers={"Authorization": f"Bearer {cfg.equipo_token}"},
                timeout=15.0,
            )
            ultimo = r.json()
        except (httpx.HTTPError, ValueError):
            pass
        if ultimo.get("estado") != "en_curso":
            return ultimo
        if cfg.reloj() >= fin:
            ultimo.setdefault("estado", "en_curso")
            return ultimo
        cfg.dormir(cfg.intervalo_poll_s)


def check_escritorio(cliente: httpx.Client, cfg: Config, prueba: dict[str, Any]) -> list[Check]:
    r = cliente.post(
        f"{cfg.escritorio_base}/__check",
        json=prueba,
        headers={"Authorization": f"Bearer {cfg.escritorio_token}"},
        timeout=30.0,
    )
    datos = r.json()
    return [Check(c.get("nombre", "?"), bool(c.get("ok")), "__check") for c in datos.get("checks", [])]


# ── corrida de una prueba ─────────────────────────────────────────────────────


@dataclass
class ResultadoCorrida:
    id: str
    instruccion: str
    estado: str
    resumen: str
    segundos: float
    post_tasks_s: float
    checks: list[Check] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and all(c.ok for c in self.checks)


def tareas_ajenas(cliente: httpx.Client, cfg: Config) -> list[str] | None:
    """Ids de tareas en_curso que ya tiene el agente (de otro cliente). None si el agente no
    expone GET /tasks (versión anterior): no se puede comprobar."""
    r = cliente.get(f"{cfg.agente_url}/tasks",
                    headers={"Authorization": f"Bearer {cfg.equipo_token}"}, timeout=10.0)
    if r.status_code in (404, 405):
        return None
    r.raise_for_status()
    return [str(t) for t in r.json().get("en_curso", [])]


def correr_prueba(cliente: httpx.Client, cfg: Config, prueba: dict[str, Any]) -> ResultadoCorrida:
    pid = prueba.get("id", "?")
    instruccion = prueba.get("instruccion", "")
    t0 = cfg.reloj()
    checks: list[Check] = []
    estado, resumen, post_dt = "en_curso", "", 0.0
    try:
        if not cfg.sin_reset:
            resetear(cliente, cfg)
            if not esperar_salud(cliente, cfg):
                return ResultadoCorrida(pid, instruccion, "en_curso", "", cfg.reloj() - t0, 0.0,
                                        error="el escritorio no respondió /health tras __reset")
        task_id, post_dt = lanzar_tarea(cliente, cfg, instruccion)
        checks.append(Check("POST /tasks < 2 s", post_dt < cfg.plazo_post_tasks_s))
        if not task_id:
            return ResultadoCorrida(pid, instruccion, "en_curso", "", cfg.reloj() - t0, post_dt,
                                    checks=checks, error="POST /tasks no devolvió task_id")
        estado_tarea = esperar_resultado(cliente, cfg, task_id)
        estado = str(estado_tarea.get("estado", "en_curso"))
        resumen = str(estado_tarea.get("resumen", ""))
        checks.extend(check_escritorio(cliente, cfg, prueba))
        checks.extend(checks_locales(prueba, estado, resumen, cfg.secretos))
    except httpx.HTTPError as exc:
        return ResultadoCorrida(pid, instruccion, estado, resumen, cfg.reloj() - t0, post_dt,
                                checks=checks, error=f"{type(exc).__name__}: {exc}")
    segundos = cfg.reloj() - t0
    return ResultadoCorrida(pid, instruccion, estado, resumen, segundos, post_dt, checks=checks)


# ── presentación y persistencia ───────────────────────────────────────────────


def guardar_corrida(dir_destino: Path, res: ResultadoCorrida, nombre: str) -> None:
    dir_destino.mkdir(parents=True, exist_ok=True)
    doc = {
        "instruccion": res.instruccion,
        "estado": res.estado,
        "resumen": res.resumen,
        "segundos": round(res.segundos, 2),
        "error": res.error,
        "checks": [{"nombre": c.nombre, "ok": c.ok, "fuente": c.fuente} for c in res.checks],
    }
    (dir_destino / f"{nombre}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2))


def _fmt_checks(res: ResultadoCorrida) -> str:
    if res.error:
        return f"ERROR: {res.error}"
    if not res.checks:
        return "(sin checks)"
    return "  ".join(f"{'✔' if c.ok else '✘'} {c.nombre}" for c in res.checks)


def imprimir_tabla(resultados: list[ResultadoCorrida]) -> None:
    print()
    print("=" * 72)
    for res in resultados:
        marca = "OK " if res.ok else "FALLA"
        print(f"[{marca}] {res.id:<4} estado={res.estado:<10} {res.segundos:6.1f}s  (POST {res.post_tasks_s:.2f}s)")
        print(f"         {_fmt_checks(res)}")
    print("=" * 72)


def resumen_final(resultados: list[ResultadoCorrida]) -> int:
    ok = sum(1 for r in resultados if r.ok)
    print(f"\nResultado: {ok}/{len(resultados)} pruebas correctas")
    return 0 if ok == len(resultados) else 1


# ── carga de pruebas ──────────────────────────────────────────────────────────


def cargar_pruebas(ids: list[str]) -> list[dict[str, Any]]:
    disponibles = {}
    for ruta in sorted(DIR_PRUEBAS.glob("E*.json")):
        datos = json.loads(ruta.read_text())
        disponibles[datos["id"]] = datos
    pruebas = []
    for pid in ids:
        if pid not in disponibles:
            raise SystemExit(f"No existe la prueba {pid} en {DIR_PRUEBAS}")
        pruebas.append(disponibles[pid])
    return pruebas


# ── CLI ───────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="pruebas_e2e.harness", description="Harness E1-E5")
    p.add_argument("--agente", default=None, help="URL del agente (default: AGENTE_URL o localhost:8000)")
    p.add_argument("--repeticiones", type=int, default=1, help="veces que se corre cada prueba")
    p.add_argument("--sin-reset", action="store_true", help="no llama a __reset ni espera /health")
    p.add_argument("ids", nargs="*", help="ids de prueba (sin ninguno = E1..E5)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None, *, cliente: httpx.Client | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    ids = [i.upper() for i in args.ids] or list(IDS_POR_DEFECTO)
    cfg = cargar_config(agente=args.agente, repeticiones=args.repeticiones, sin_reset=args.sin_reset)
    pruebas = cargar_pruebas(ids)

    sello = time.strftime("%Y%m%d-%H%M%S")
    dir_corrida = DIR_CORRIDAS / sello

    print(f"Agente: {cfg.agente_url}  Escritorio: {urlsplit(cfg.escritorio_base).hostname}  "
          f"reset={'no' if cfg.sin_reset else 'sí'}  repeticiones={cfg.repeticiones}")

    propio = cliente is None
    cli = cliente or httpx.Client()
    resultados: list[ResultadoCorrida] = []
    try:
        for rep in range(1, cfg.repeticiones + 1):
            for prueba in pruebas:
                # interferencia: otro cliente usando el mismo agente/escritorio contamina la prueba
                ajenas = tareas_ajenas(cli, cfg)
                if ajenas is None:
                    print("AVISO: el agente no expone GET /tasks; no se comprueban tareas ajenas")
                elif ajenas:
                    print(f"ABORTADO antes de {prueba.get('id')}: el agente tiene tareas en curso "
                          f"ajenas: {', '.join(ajenas)}")
                    imprimir_tabla(resultados)
                    return 2
                res = correr_prueba(cli, cfg, prueba)
                nombre = res.id if cfg.repeticiones == 1 else f"{res.id}-r{rep}"
                guardar_corrida(dir_corrida, res, nombre)
                resultados.append(res)
    finally:
        if propio:
            cli.close()

    imprimir_tabla(resultados)
    print(f"Corridas guardadas en {dir_corrida}")
    return resumen_final(resultados)


if __name__ == "__main__":
    raise SystemExit(main())
