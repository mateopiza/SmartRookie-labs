"""Pruebas del juez (Fase G).

Sin red: cliente falso que devuelve lecturas por esquema; cubren las cuatro compuertas, la
discrepancia de identificadores (unión), la política "juez caído" por tipo y el veredicto final.

Con red (opt-in): JUEZ_REAL=1 pytest -k real -s → llama al rol "juez" real sobre las capturas de
fixtures/ y mide la latencia por veredicto (objetivo < 8 s).
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from rookie.esquemas.mensajes import Accion, Cambio, Plan, Pregunta, Resultado
from rookie.roles import juez as J
from rookie.roles.juez import NO_DISPONIBLE, juzgar_compuerta, veredicto_final

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PNG = b"\x89PNG-falso"


# ──────────────────────────── dobles de prueba ──────────────────────────────

class FakeCliente:
    """Devuelve la lectura preparada para el esquema pedido; o falla N veces antes."""

    def __init__(self, lecturas: dict | None = None, *, fallos: int = 0, colgar: bool = False):
        self.lecturas = lecturas or {}
        self.fallos = fallos
        self.colgar = colgar
        self.llamadas: list[dict] = []

    async def estructurado(self, rol, *, system, mensajes, esquema, imagenes=(), timeout_s=None, task_id=None):
        self.llamadas.append({"rol": rol, "esquema": esquema, "texto": mensajes[-1]["content"],
                              "imagenes": list(imagenes), "timeout_s": timeout_s})
        if self.colgar:
            await asyncio.sleep(3600)
        if self.fallos > 0:
            self.fallos -= 1
            raise RuntimeError("modelo caído")
        return esquema.model_validate(self.lecturas[esquema.__name__])


def _run(coro):
    return asyncio.run(coro)


def _plan(**kw) -> Plan:
    base = {"tipo": "actualizar", "modulo": "clientes"}
    base.update(kw)
    return Plan(**base)


ANA_1 = {"nombre": "Ana Torres", "documento": "52345678", "ciudad": "Bogotá"}
ANA_2 = {"nombre": "Ana Torres", "documento": "1019876543", "ciudad": "Cali"}


def _lect_identidad(*filas):
    return {"_LecturaIdentidad": {"cantidad": len(filas), "filas": [
        {"identificador": i, "texto": t} for i, t in filas
    ]}}


# ──────────────────────────── identidad ──────────────────────────────────────

def test_identidad_coincide_aprueba():
    cli = FakeCliente(_lect_identidad(("CL-0007", "CL-0007 Ana Torres 52345678 Bogotá")))
    acc = Accion(tipo="click", compuerta="identidad", coincidencias=[ANA_1])
    v = _run(juzgar_compuerta(acc, _plan(identificacion={"nombre": "Ana Torres"}), PNG, cli))
    assert v.aprobado and v.datos_clave == []
    llamada = cli.llamadas[0]
    assert llamada["rol"] == "juez" and llamada["imagenes"] == [PNG]
    assert "Ana Torres" in llamada["texto"] and llamada["timeout_s"] == 30.0


def test_identidad_ejecutor_ve_una_juez_dos_rechaza_con_union():
    cli = FakeCliente(_lect_identidad(
        ("52345678", "CL-0007 Ana Torres 52345678 Bogotá"),
        ("1019876543", "CL-0019 Ana Torres 1019876543 Cali"),
    ))
    acc = Accion(tipo="click", compuerta="identidad", coincidencias=[ANA_1])
    v = _run(juzgar_compuerta(acc, _plan(identificacion={"nombre": "Ana Torres"}), PNG, cli))
    assert not v.aprobado
    assert "1" in v.razon and "2" in v.razon
    assert v.datos_clave == ["52345678", "1019876543"]  # unión sin duplicados


def test_identidad_union_no_repite_la_fila_comun_con_otro_identificador():
    """El juez usa el código y el ejecutor el documento: la fila común cuenta una vez."""
    cli = FakeCliente(_lect_identidad(
        ("CL-0007", "CL-0007 Ana Torres 52345678 Bogotá"),
        ("CL-0019", "CL-0019 Ana Torres 1019876543 Cali"),
    ))
    acc = Accion(tipo="click", compuerta="identidad", coincidencias=[ANA_1])
    v = _run(juzgar_compuerta(acc, _plan(identificacion={"nombre": "Ana Torres"}), PNG, cli))
    assert not v.aprobado and v.datos_clave == ["52345678", "CL-0019"]


def test_identidad_mismo_conteo_otro_registro_rechaza_con_union():
    cli = FakeCliente(_lect_identidad(("CL-0019", "CL-0019 Ana Torres 1019876543 Cali")))
    acc = Accion(tipo="click", compuerta="identidad", coincidencias=[ANA_1])
    v = _run(juzgar_compuerta(acc, _plan(identificacion={"nombre": "Ana Torres"}), PNG, cli))
    assert not v.aprobado
    assert v.datos_clave == ["52345678", "CL-0019"]


def test_identidad_dos_y_dos_homonimos_emparejan():
    cli = FakeCliente(_lect_identidad(
        ("1019876543", "CL-0019 Ana Torres 1019876543 Cali"),
        ("52345678", "CL-0007 Ana Torres 52345678 Bogotá"),
    ))
    acc = Accion(tipo="click", compuerta="identidad", coincidencias=[ANA_1, ANA_2])
    v = _run(juzgar_compuerta(acc, _plan(identificacion={"nombre": "Ana Torres"}), PNG, cli))
    assert v.aprobado


def test_identidad_cero_y_juez_ve_uno_rechaza():
    """Antes de crear el ejecutor dice 0; si el juez ve uno, hay duplicado."""
    cli = FakeCliente(_lect_identidad(("CL-0040", "CL-0040 Pedro Gil 79111222")))
    acc = Accion(tipo="click", compuerta="identidad", coincidencias=[])
    v = _run(juzgar_compuerta(acc, _plan(tipo="crear", identificacion={"nombre": "Pedro Gil"}), PNG, cli))
    assert not v.aprobado and v.datos_clave == ["CL-0040"]


def test_identidad_lectura_inconsistente_rechaza():
    cli = FakeCliente({"_LecturaIdentidad": {"cantidad": 2, "filas": [
        {"identificador": "52345678", "texto": "Ana Torres 52345678"}]}})
    acc = Accion(tipo="click", compuerta="identidad", coincidencias=[ANA_1])
    v = _run(juzgar_compuerta(acc, _plan(identificacion={"nombre": "Ana Torres"}), PNG, cli))
    assert not v.aprobado and "inconsistente" in v.razon


# ──────────────────────────── guardar ────────────────────────────────────────

PLAN_DIR = _plan(identificacion={"nombre": "Jorge Ramírez"},
                 cambios_autorizados=[Cambio(entidad="cliente", campo="dirección", valor="Calle 80 # 24-15")])


def _lect_form(valor, *, visible=True, otros=False, clave="dirección"):
    return {"_LecturaFormulario": {"valores": [{"clave": clave, "visible": visible, "valor": valor}],
                                   "otros_campos_cambiados": otros, "razon": ""}}


def test_guardar_valores_exactos_aprueba_tolerando_espacios():
    cli = FakeCliente(_lect_form("Calle 80 #24-15", clave="Direccion"))
    v = _run(juzgar_compuerta(Accion(tipo="click", compuerta="guardar"), PLAN_DIR, PNG, cli))
    assert v.aprobado
    assert "Calle 80 # 24-15" in cli.llamadas[0]["texto"]


def test_guardar_valor_distinto_rechaza():
    cli = FakeCliente(_lect_form("Calle 80 # 24-51"))
    v = _run(juzgar_compuerta(Accion(tipo="click", compuerta="guardar"), PLAN_DIR, PNG, cli))
    assert not v.aprobado and "24-51" in v.razon
    assert v.datos_clave == ["dirección: Calle 80 # 24-51"]


def test_guardar_otro_campo_cambiado_rechaza():
    cli = FakeCliente(_lect_form("Calle 80 # 24-15", otros=True))
    v = _run(juzgar_compuerta(Accion(tipo="click", compuerta="guardar"), PLAN_DIR, PNG, cli))
    assert not v.aprobado and "otro campo" in v.razon


def test_guardar_no_visible_rechaza():
    cli = FakeCliente(_lect_form("", visible=False))
    v = _run(juzgar_compuerta(Accion(tipo="click", compuerta="guardar"), PLAN_DIR, PNG, cli))
    assert not v.aprobado and v.datos_clave == []


def test_guardar_sin_cambios_autorizados_rechaza_sin_llamar():
    cli = FakeCliente()
    v = _run(juzgar_compuerta(Accion(tipo="click", compuerta="guardar"), _plan(), PNG, cli))
    assert not v.aprobado and cli.llamadas == []


# ──────────────────────────── crear ──────────────────────────────────────────

PLAN_CREAR = _plan(tipo="crear", identificacion={"nombre": "Pedro Gil", "documento": "79111222"})


def test_crear_no_existe_aprueba():
    cli = FakeCliente({"_LecturaExistencia": {"existe": False, "identificadores": [], "razon": ""}})
    v = _run(juzgar_compuerta(Accion(tipo="click", compuerta="crear"), PLAN_CREAR, PNG, cli))
    assert v.aprobado
    assert "79111222" in cli.llamadas[0]["texto"]


def test_crear_ya_existe_rechaza_con_identificadores_leidos():
    cli = FakeCliente({"_LecturaExistencia": {"existe": True, "identificadores": ["CL-0040"], "razon": ""}})
    v = _run(juzgar_compuerta(Accion(tipo="click", compuerta="crear"), PLAN_CREAR, PNG, cli))
    assert not v.aprobado and v.datos_clave == ["CL-0040"]


# ──────────────────────────── exportar ───────────────────────────────────────

PLAN_EXP = _plan(tipo="exportar", modulo="pedidos",
                 filtros={"estado": "Entregado", "desde": "2026-09-01", "hasta": "2026-09-30"})


def _lect_filtros(desde="01/09/2026", otros=False):
    return {"_LecturaFiltros": {"valores": [
        {"clave": "estado", "visible": True, "valor": "Entregado"},
        {"clave": "desde", "visible": True, "valor": desde},
        {"clave": "hasta", "visible": True, "valor": "30/09/2026"},
    ], "hay_otros_filtros": otros, "razon": ""}}


def test_exportar_filtros_exactos_aprueba_con_fechas_en_otro_formato():
    v = _run(juzgar_compuerta(Accion(tipo="click", compuerta="exportar"), PLAN_EXP, PNG,
                              FakeCliente(_lect_filtros())))
    assert v.aprobado


def test_exportar_fecha_distinta_rechaza():
    v = _run(juzgar_compuerta(Accion(tipo="click", compuerta="exportar"), PLAN_EXP, PNG,
                              FakeCliente(_lect_filtros(desde="01/08/2026"))))
    assert not v.aprobado and v.datos_clave == ["desde: 01/08/2026"]


def test_exportar_filtro_extra_rechaza():
    v = _run(juzgar_compuerta(Accion(tipo="click", compuerta="exportar"), PLAN_EXP, PNG,
                              FakeCliente(_lect_filtros(otros=True))))
    assert not v.aprobado and "otros filtros" in v.razon


def test_eliminar_nunca_se_aprueba():
    cli = FakeCliente()
    v = _run(juzgar_compuerta(Accion(tipo="click", compuerta="eliminar"), _plan(), PNG, cli))
    assert not v.aprobado and cli.llamadas == []


# ──────────────────────────── juez caído ─────────────────────────────────────

@pytest.mark.parametrize("compuerta,aprobado", [
    ("identidad", True), ("guardar", False), ("crear", False), ("exportar", False),
])
def test_juez_caido_politica_por_tipo(compuerta, aprobado):
    cli = FakeCliente(fallos=99)
    plan = PLAN_EXP if compuerta == "exportar" else (PLAN_CREAR if compuerta == "crear" else PLAN_DIR)
    acc = Accion(tipo="click", compuerta=compuerta, coincidencias=[ANA_1] if compuerta == "identidad" else None)
    v = _run(juzgar_compuerta(acc, plan, PNG, cli))
    assert v.aprobado is aprobado
    assert v.razon == NO_DISPONIBLE and v.datos_clave == []
    assert len(cli.llamadas) == 2  # 1 llamada + 1 reintento


def test_reintento_recupera_tras_un_fallo():
    cli = FakeCliente({"_LecturaExistencia": {"existe": False, "identificadores": [], "razon": ""}}, fallos=1)
    v = _run(juzgar_compuerta(Accion(tipo="click", compuerta="crear"), PLAN_CREAR, PNG, cli))
    assert v.aprobado and len(cli.llamadas) == 2


def test_timeout_cuenta_como_no_disponible(monkeypatch):
    monkeypatch.setattr(J, "TIMEOUT_S", 0.05)
    cli = FakeCliente(colgar=True)
    inicio = time.perf_counter()
    v = _run(juzgar_compuerta(Accion(tipo="click", compuerta="guardar"), PLAN_DIR, PNG, cli))
    assert not v.aprobado and v.razon == NO_DISPONIBLE
    assert len(cli.llamadas) == 2 and time.perf_counter() - inicio < 2


def test_timeout_del_juez_es_30_s_y_se_pasa_al_cliente():
    assert J.TIMEOUT_S == 30.0
    cli = FakeCliente(_lect_registro(False))
    _run(J.registro_tras_guardar(_plan(identificacion={"nombre": "Ana Torres"}), PNG, cli))
    assert cli.llamadas[0]["timeout_s"] == 30.0


# ──────────────────────────── veredicto final ────────────────────────────────

PLAN_CONSULTA = _plan(tipo="consultar", modulo="facturas",
                      pregunta=Pregunta(datos=["número de facturas vencidas"], formato="numero"))


def _lect_final(valores, evidencia=True):
    return {"_LecturaFinal": {"valores": [
        {"clave": k, "visible": True, "valor": v} for k, v in valores
    ], "evidencia": evidencia, "razon": ""}}


def test_final_consulta_mismo_numero_aprueba_y_no_filtra_la_respuesta():
    cli = FakeCliente(_lect_final([("número de facturas vencidas", "3")]))
    res = Resultado(estado="terminada", respuesta="3 facturas")
    v = _run(veredicto_final(res, PLAN_CONSULTA, PNG, cli))
    assert v.aprobado
    assert "3 facturas" not in cli.llamadas[0]["texto"]  # el juez relee sin ancla


def test_final_consulta_numero_distinto_rechaza_con_lo_leido():
    cli = FakeCliente(_lect_final([("número de facturas vencidas", "4")]))
    v = _run(veredicto_final(Resultado(estado="terminada", respuesta="3"), PLAN_CONSULTA, PNG, cli))
    assert not v.aprobado and v.datos_clave == ["4"]


def test_final_consulta_dos_datos():
    plan = _plan(tipo="consultar", modulo="facturas",
                 pregunta=Pregunta(datos=["total adeudado", "cliente"], formato="numero"))
    cli = FakeCliente(_lect_final([("total adeudado", "$ 1.250.000"), ("cliente", "Ana Torres")]))
    v = _run(veredicto_final(Resultado(estado="terminada", respuesta="1250000; Ana Torres"), plan, PNG, cli))
    assert v.aprobado


def test_final_consulta_sin_respuesta_rechaza_sin_llamar():
    cli = FakeCliente()
    v = _run(veredicto_final(Resultado(estado="terminada"), PLAN_CONSULTA, PNG, cli))
    assert not v.aprobado and cli.llamadas == []


def test_final_actualizar_valor_persistido_aprueba():
    res = Resultado(estado="terminada", escrituras=list(PLAN_DIR.cambios_autorizados))
    cli = FakeCliente(_lect_final([("dirección", "Calle 80 # 24-15")]))
    assert _run(veredicto_final(res, PLAN_DIR, PNG, cli)).aprobado


def test_final_actualizar_sin_evidencia_rechaza():
    res = Resultado(estado="terminada", escrituras=list(PLAN_DIR.cambios_autorizados))
    cli = FakeCliente(_lect_final([("dirección", "Calle 80 # 24-15")], evidencia=False))
    assert not _run(veredicto_final(res, PLAN_DIR, PNG, cli)).aprobado


def test_final_escritura_fuera_de_lista_blanca_rechaza_sin_llamar():
    res = Resultado(estado="terminada", escrituras=[Cambio(entidad="cliente", campo="ciudad", valor="Cali")])
    cli = FakeCliente()
    v = _run(veredicto_final(res, PLAN_DIR, PNG, cli))
    assert not v.aprobado and "fuera de lo autorizado" in v.razon and cli.llamadas == []


def test_final_exportar_con_evidencia_aprueba():
    cli = FakeCliente(_lect_final([("estado", "Entregado")]))
    assert _run(veredicto_final(Resultado(estado="terminada"), PLAN_EXP, PNG, cli)).aprobado


def test_final_fallida_no_se_discute():
    cli = FakeCliente()
    v = _run(veredicto_final(Resultado(estado="fallida", datos_clave=["x"]), PLAN_DIR, PNG, cli))
    assert v.aprobado and cli.llamadas == []


def test_final_juez_caido_rechaza():
    res = Resultado(estado="terminada", escrituras=list(PLAN_DIR.cambios_autorizados))
    v = _run(veredicto_final(res, PLAN_DIR, PNG, FakeCliente(fallos=99)))
    assert not v.aprobado and v.razon == NO_DISPONIBLE


# ──────────────────────────── normalización ──────────────────────────────────

@pytest.mark.parametrize("a,b,num,esperado", [
    ("2026-09-01", "01/09/2026", False, True),
    ("2026-09-01", "09/01/2026", False, False),
    ("1250000", "$ 1.250.000", True, True),
    ("2,5", "2.5", True, True),
    ("Bogotá", "BOGOTA", False, True),
    ("Calle 80 # 24-15", "Calle 80 # 24-51", False, False),
    ("x", "", False, False),
])
def test_igual(a, b, num, esperado):
    assert J._igual(a, b, numerico=num) is esperado


# ──────────────────────────── juez real (opt-in, mide latencia) ──────────────

REAL = pytest.mark.skipif(not os.environ.get("JUEZ_REAL"), reason="JUEZ_REAL=1 para llamar al modelo")


def _cliente_real():
    from rookie.modelos.cliente import ClienteModelos
    return ClienteModelos()


def _medir(coro):
    inicio = time.perf_counter()
    v = asyncio.run(coro)
    ms = (time.perf_counter() - inicio) * 1000
    print(f"\n  latencia={ms:.0f} ms aprobado={v.aprobado} razon={v.razon!r} datos_clave={v.datos_clave}")
    return v, ms


@REAL
def test_real_identidad_homonimos():
    png = (FIXTURES / "juez" / "homonimos.png").read_bytes()
    acc = Accion(tipo="click", compuerta="identidad", coincidencias=[ANA_1])  # el ejecutor "vio" una
    v, _ms = _medir(juzgar_compuerta(acc, _plan(identificacion={"nombre": "Ana Torres"}), png, _cliente_real()))
    assert not v.aprobado and len(v.datos_clave) == 2  # una entrada por homónimo
    assert v.datos_clave[0] == "52345678"
    assert J._compacto(v.datos_clave[1]) in {"1019876543", "cl0019"}


@REAL
def test_real_identidad_cap2_registro_repetido():
    png = (FIXTURES / "cap2.png").read_bytes()
    acc = Accion(tipo="click", compuerta="identidad",
                 coincidencias=[{"registro": "Cliente CL-0031", "campo": "Ciudad"}])
    v, _ms = _medir(juzgar_compuerta(acc, _plan(identificacion={"registro": "Cliente CL-0031"}), png,
                                    _cliente_real()))
    assert v.aprobado


@REAL
def test_real_guardar_formulario():
    png = (FIXTURES / "juez" / "formulario.png").read_bytes()
    plan = _plan(identificacion={"documento": "80123456"},
                 cambios_autorizados=[Cambio(entidad="cliente", campo="Dirección", valor="Calle 80 # 24-15")])
    v, _ms = _medir(juzgar_compuerta(Accion(tipo="click", compuerta="guardar"), plan, png, _cliente_real()))
    assert v.aprobado


@REAL
def test_real_exportar_filtros():
    png = (FIXTURES / "juez" / "filtros.png").read_bytes()
    v, _ms = _medir(juzgar_compuerta(Accion(tipo="click", compuerta="exportar"), PLAN_EXP, png, _cliente_real()))
    assert v.aprobado


@REAL
def test_real_crear_duplicado():
    png = (FIXTURES / "juez" / "homonimos.png").read_bytes()
    plan = _plan(tipo="crear", identificacion={"nombre": "Ana Torres", "documento": "52345678"})
    v, _ms = _medir(juzgar_compuerta(Accion(tipo="click", compuerta="crear"), plan, png, _cliente_real()))
    assert not v.aprobado


# ──────────────────────── Fase H: conteo repartido en páginas ─────────────────

class _ClientePorPagina:
    """Devuelve una lectura de conteo distinta según la imagen (b"p1", b"p2"...)."""

    def __init__(self, por_png: dict[bytes, dict]):
        self.por_png = por_png

    async def estructurado(self, rol, *, esquema, imagenes=(), **kw):
        return esquema.model_validate(self.por_png[imagenes[0]])


def _conteo(ids):
    return {"filas_que_cumplen": len(ids), "identificadores": ids, "razon": ""}


PLAN_CONTEO = _plan(tipo="consultar", modulo="facturas", identificacion={"cliente": "Ferretería Ruiz"},
                    pregunta=Pregunta(datos=["facturas vencidas"], formato="numero"))


def test_conteo_por_paginas_suma_identificadores_unicos():
    cli = _ClientePorPagina({b"p1": _conteo(["FV-1", "FV-2"]), b"p2": _conteo(["FV-3"]),
                             b"p2bis": _conteo(["FV-3"])})  # la misma página capturada dos veces
    v = _run(J.verificar_conteo(Resultado(estado="terminada", respuesta="3"), PLAN_CONTEO,
                                [b"p1", b"p2", b"p2bis"], cli))
    assert v.aprobado is True


def test_conteo_por_paginas_distinto_rechaza_con_el_total_del_juez():
    cli = _ClientePorPagina({b"p1": _conteo(["FV-1", "FV-2"]), b"p2": _conteo(["FV-3"])})
    v = _run(J.verificar_conteo(Resultado(estado="terminada", respuesta="45"), PLAN_CONTEO,
                                [b"p1", b"p2"], cli))
    assert v.aprobado is False and "es 3" in v.razon


# ──────────────────────── registro tras guardar ──────────────────────────────

def _lect_registro(muestra, nombre="", identificador=""):
    return {"_LecturaRegistro": {"muestra_un_registro": muestra, "nombre": nombre,
                                 "identificador": identificador}}


def test_registro_tras_guardar_detecta_otra_ficha():
    plan = _plan(identificacion={"nombre": "Distribuciones El Faro"})
    v = _run(J.registro_tras_guardar(plan, PNG, FakeCliente(_lect_registro(True, "Ferretería Ruiz", "CL-0046"))))
    assert not v.aprobado and "CL-0046" in v.datos_clave


def test_registro_tras_guardar_aprueba_el_mismo_registro_un_listado_o_sin_juez():
    plan = _plan(identificacion={"nombre": "Distribuciones El Faro"})
    mismo = FakeCliente(_lect_registro(True, "Distribuciones El Faro", "CL-0007"))
    assert _run(J.registro_tras_guardar(plan, PNG, mismo)).aprobado
    assert _run(J.registro_tras_guardar(plan, PNG, FakeCliente(_lect_registro(False)))).aprobado
    assert _run(J.registro_tras_guardar(plan, PNG, FakeCliente(fallos=5))).aprobado
    alta = _plan(tipo="crear", cambios_autorizados=[
        Cambio(entidad="clientes", campo="nombre", valor="Papelería Los Andes"),
        Cambio(entidad="clientes", campo="ciudad", valor="Pereira")])
    nueva = FakeCliente(_lect_registro(True, "Papelería Los Andes", "CL-0141"))
    assert _run(J.registro_tras_guardar(alta, PNG, nueva)).aprobado


# ──────────────────────── comparación de valores ─────────────────────────────

def test_igual_acepta_valor_con_etiqueta_y_rechaza_concatenado():
    assert J._igual("305", "Consultorio 305")
    assert J._igual("Consultorio 305", "Consultorio 305")
    assert J._igual("3001112233", "Tel. 3001112233")
    assert not J._igual("Calle 80 # 12-34", "Carrera 15Calle 80 # 12-34")
    assert not J._igual("Calle 80 # 12-34", "Carrera 15 Calle 80 # 12-34")  # prefijo con números
    assert not J._igual("305", "Consultorio 3050")
    assert not J._igual("305", "305 Consultorio")


def test_guardar_acepta_campo_leido_con_su_etiqueta():
    plan = _plan(identificacion={"nombre": "Ana Torres"},
                 cambios_autorizados=[Cambio(entidad="clientes", campo="oficina", valor="305")])
    lect = {"_LecturaFormulario": {"valores": [{"clave": "oficina", "visible": True,
            "valor": "Consultorio 305"}], "otros_campos_cambiados": False, "razon": ""}}
    v = _run(juzgar_compuerta(Accion(tipo="click", compuerta="guardar"), plan, PNG, FakeCliente(lect)))
    assert v.aprobado
