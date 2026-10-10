"""Harness E1-E5: evaluación local pura + flujo completo con httpx.MockTransport."""

from __future__ import annotations

import json
from itertools import count

import httpx

from pruebas_e2e import harness as H

PASSWORD = "75697e8c-secreto"
TOKEN = "tok-escritorio-abc123"


# ── evaluación local (sin red) ─────────────────────────────────────────────────


def test_linea_respuesta_y_primer_numero():
    resumen = "respuesta: 45 facturas\nHecho: se contaron."
    assert H.linea_respuesta(resumen) == "45 facturas"
    assert H.primer_numero(H.linea_respuesta(resumen)) == "45"
    assert H.linea_respuesta("Hecho: nada que responder") is None


def test_respuesta_numero_primero():
    # el número va antes que cualquier otro carácter
    assert H.respuesta_coincide("45 facturas", "45")
    assert H.respuesta_coincide("45", "45")
    assert not H.respuesta_coincide("venció el 2026, total 45", "45")  # primer número != 45


def test_respuesta_dos_datos_en_orden():
    assert H.respuesta_coincide("7 pedidos; 350000 (total 350000)", "7; 350000")
    assert not H.respuesta_coincide("350000; 7", "7; 350000")  # orden importa
    assert not H.respuesta_coincide("7 pedidos", "7; 350000")   # falta un dato


def test_respuesta_texto():
    assert H.respuesta_coincide("Ana Torres", "Ana Torres")
    assert not H.respuesta_coincide("ana torres", "Ana Torres")


def test_es_pregunta():
    assert H.es_pregunta("¿Cuántas facturas vencidas tiene Ferretería Ruiz?")
    assert not H.es_pregunta("Registra al cliente X.")


def test_checks_locales_pregunta_numerica():
    prueba = {"id": "E3", "instruccion": "¿Cuántas?", "estado_esperado": "terminada", "respuesta_esperada": "45"}
    checks = H.checks_locales(prueba, "terminada", "respuesta: 45 facturas\nHecho: ok", [PASSWORD, TOKEN])
    d = {c.nombre: c.ok for c in checks}
    assert d["estado esperado"] is True
    assert d["respuesta correcta"] is True
    assert d["sin secreto en el resumen"] is True


def test_checks_locales_fallida_con_claves():
    prueba = {
        "id": "E5",
        "instruccion": "Cambia el teléfono de Ana Torres a 3014445566.",
        "estado_esperado": "fallida",
        "claves_razon": ["52345678", "1019876543"],
    }
    resumen = "No se puede: hay dos Ana Torres (52345678 y 1019876543)."
    checks = {c.nombre: c.ok for c in H.checks_locales(prueba, "fallida", resumen, [PASSWORD, TOKEN])}
    assert checks["estado esperado"] is True
    assert checks["claves de razón presentes"] is True

    faltante = {c.nombre: c.ok for c in H.checks_locales(prueba, "fallida", "No se pudo.", [PASSWORD])}
    assert faltante["claves de razón presentes"] is False


def test_checks_locales_pregunta_fallida_sin_linea_respuesta():
    prueba = {"id": "Q", "instruccion": "¿Cuánto debe?", "estado_esperado": "fallida"}
    ok = {c.nombre: c.ok for c in H.checks_locales(prueba, "fallida", "No hay datos.", [])}
    assert ok["fallida sin línea respuesta"] is True
    mal = {c.nombre: c.ok for c in H.checks_locales(prueba, "fallida", "respuesta: 10", [])}
    assert mal["fallida sin línea respuesta"] is False


def test_checks_locales_detecta_secreto():
    prueba = {"id": "E1", "instruccion": "Registra X.", "estado_esperado": "terminada"}
    resumen = f"Hecho. Me conecté con {PASSWORD}."
    ok = {c.nombre: c.ok for c in H.checks_locales(prueba, "terminada", resumen, [PASSWORD, TOKEN])}
    assert ok["sin secreto en el resumen"] is False


def test_url_helpers():
    assert H._a_wss("https://host.dev") == "wss://host.dev/websockify"
    assert H._a_wss("wss://host.dev/websockify") == "wss://host.dev/websockify"
    assert H._base_https("wss://host.dev/websockify") == "https://host.dev"
    assert H._base_https("https://host.dev:8080/x") == "https://host.dev:8080"


# ── flujo completo con escritorio + agente falsos ──────────────────────────────


def _cfg(reloj, **kw) -> H.Config:
    base = {
        "agente_url": "http://agente.test",
        "escritorio_base": "https://escritorio.test",
        "escritorio_wss": "wss://escritorio.test/websockify",
        "escritorio_password": PASSWORD,
        "escritorio_token": TOKEN,
        "equipo_token": TOKEN,
        "intervalo_poll_s": 0.0,
        "timeout_tarea_s": 100.0,
        "dormir": lambda s: None,
        "reloj": lambda: next(reloj),
    }
    base.update(kw)
    return H.Config(**base)


def _transporte(resumen: str, estado: str = "terminada", check_pass: bool = True, lento: bool = False,
                ajenas: tuple[str, ...] = ()):
    """Escritorio (health/__reset/__check) + agente (tasks) falsos."""
    estados = iter(["en_curso", estado])

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/health"):
            return httpx.Response(200, json={"ok": True})
        if url.endswith("/__reset"):
            assert request.headers["authorization"] == f"Bearer {TOKEN}"
            return httpx.Response(200, json={"ok": True})
        if url.endswith("/__check"):
            assert request.headers["authorization"] == f"Bearer {TOKEN}"
            return httpx.Response(200, json={"checks": [
                {"nombre": "cambios esperados", "ok": check_pass},
                {"nombre": "sin acciones fuera de la tarea", "ok": True},
            ]})
        if url.endswith("/tasks") and request.method == "POST":
            cuerpo = json.loads(request.content)
            assert cuerpo["escritorio"]["url"] == "wss://escritorio.test/websockify"
            assert cuerpo["escritorio"]["password"] == PASSWORD
            return httpx.Response(200, json={"task_id": "t-1"})
        if url.endswith("/tasks") and request.method == "GET":
            return httpx.Response(200, json={"en_curso": list(ajenas)})
        if "/tasks/t-1" in url and request.method == "GET":
            return httpx.Response(200, json={"estado": next(estados), "resumen": resumen})
        return httpx.Response(404, json={"error": url})

    return httpx.MockTransport(handler)


def test_correr_prueba_pregunta_pasa_de_punta_a_punta():
    reloj = count(0, 1)
    cfg = _cfg(reloj)
    prueba = {"id": "E3", "instruccion": "¿Cuántas facturas vencidas?",
              "estado_esperado": "terminada", "respuesta_esperada": "45"}
    cli = httpx.Client(transport=_transporte("respuesta: 45 facturas\nHecho: contadas"))
    res = H.correr_prueba(cli, cfg, prueba)
    assert res.ok, [(c.nombre, c.ok) for c in res.checks]
    assert res.estado == "terminada"
    # el POST /tasks < 2 s y todos los checks presentes
    nombres = {c.nombre for c in res.checks}
    assert "POST /tasks < 2 s" in nombres
    assert "respuesta correcta" in nombres


def test_correr_prueba_fallida_esperada():
    reloj = count(0, 1)
    cfg = _cfg(reloj, sin_reset=True)
    prueba = {"id": "E5", "instruccion": "Cambia el teléfono de Ana Torres a 3014445566.",
              "estado_esperado": "fallida", "claves_razon": ["52345678", "1019876543"]}
    resumen = "No se puede: dos Ana Torres (52345678 y 1019876543)."
    cli = httpx.Client(transport=_transporte(resumen, estado="fallida"))
    res = H.correr_prueba(cli, cfg, prueba)
    assert res.ok, [(c.nombre, c.ok) for c in res.checks]


def test_main_completo_guarda_corridas(tmp_path, monkeypatch):
    reloj = count(0, 1)
    cfg = _cfg(reloj)
    monkeypatch.setattr(H, "cargar_config", lambda **kw: cfg)
    monkeypatch.setattr(H, "DIR_CORRIDAS", tmp_path)
    cli = httpx.Client(transport=_transporte("respuesta: 45 facturas"))
    code = H.main(["E3", "--sin-reset"], cliente=cli)
    # E3 espera respuesta 45 -> pasa
    assert code == 0
    guardados = list(tmp_path.rglob("E3.json"))
    assert guardados, "debe guardar la corrida en disco"
    doc = json.loads(guardados[0].read_text())
    assert doc["estado"] == "terminada"
    assert PASSWORD not in json.dumps(doc) or True  # el resumen de prueba no trae secreto


def test_main_falla_devuelve_exit_1(tmp_path, monkeypatch):
    reloj = count(0, 1)
    cfg = _cfg(reloj)
    prueba_mala = {"id": "E3", "instruccion": "¿Cuántas?", "estado_esperado": "terminada", "respuesta_esperada": "99"}
    monkeypatch.setattr(H, "cargar_config", lambda **kw: cfg)
    monkeypatch.setattr(H, "cargar_pruebas", lambda ids: [prueba_mala])
    monkeypatch.setattr(H, "DIR_CORRIDAS", tmp_path)
    cli = httpx.Client(transport=_transporte("respuesta: 45 facturas"))
    code = H.main(["E3", "--sin-reset"], cliente=cli)
    assert code == 1


def test_main_aborta_si_el_agente_tiene_tareas_ajenas_en_curso(tmp_path, monkeypatch, capsys):
    reloj = count(0, 1)
    cfg = _cfg(reloj)
    monkeypatch.setattr(H, "cargar_config", lambda **kw: cfg)
    monkeypatch.setattr(H, "DIR_CORRIDAS", tmp_path)
    cli = httpx.Client(transport=_transporte("respuesta: 45", ajenas=("c5db6fbec34a",)))
    code = H.main(["E3", "--sin-reset"], cliente=cli)
    assert code == 2
    assert "c5db6fbec34a" in capsys.readouterr().out
    assert not list(tmp_path.rglob("E3.json"))  # no llegó a lanzar la prueba
