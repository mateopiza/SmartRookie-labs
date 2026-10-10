"""Plantillas del resumen: la evaluación compara el formato LITERAL."""

import base64
from urllib.parse import quote, quote_plus

import pytest

from rookie.esquemas.mensajes import Cambio, Plan, Pregunta, Resultado
from rookie.reporte.plantillas import OCULTO, armar_resumen, filtrar_secretos

CLAVE = "Cl@ve Secreta/2026&x"
TOKEN = "tok_ABCDEF123456"
URL = "wss://escritorio-17.ejemplo.dev/websockify"
SECRETOS = [CLAVE, TOKEN, URL]


def consulta(datos=("conteo",), formato="numero") -> Plan:
    return Plan(tipo="consultar", modulo="facturas", pregunta=Pregunta(datos=list(datos), formato=formato))


def primera_linea(texto: str) -> str:
    return texto.splitlines()[0]


@pytest.mark.parametrize(
    ("respuesta", "plan", "esperado"),
    [
        ("45 facturas", consulta(), "respuesta: 45 facturas"),
        ("45", consulta(), "respuesta: 45"),
        ("Total: 45 facturas", consulta(), "respuesta: 45 (Total: 45 facturas)"),
        ("$1.234.500", consulta(), "respuesta: 1.234.500 ($1.234.500)"),
        ("Ana Torres", consulta(formato="texto"), "respuesta: Ana Torres"),
        ("12; Papelería Los Andes", consulta(datos=("conteo", "cliente"), formato="texto"),
         "respuesta: 12; Papelería Los Andes"),
        ("12\nPapelería Los Andes", consulta(datos=("conteo", "cliente"), formato="texto"),
         "respuesta: 12; Papelería Los Andes"),
        ("7 pedidos; total 350000", consulta(datos=("conteo", "total"), formato="numero"),
         "respuesta: 7 pedidos; 350000 (total 350000)"),
        ("45 facturas", None, "respuesta: 45 facturas"),
    ],
)
def test_consulta_primera_linea(respuesta, plan, esperado):
    r = Resultado(estado="terminada", respuesta=respuesta, detalle="se contaron las facturas vencidas en facturas")
    texto = armar_resumen(r, plan, SECRETOS)
    assert primera_linea(texto) == esperado
    assert texto.splitlines()[1].startswith("Hecho: ")
    assert texto.count("respuesta:") == 1


def test_consulta_numero_empieza_por_el_numero():
    r = Resultado(estado="terminada", respuesta="45 facturas")
    assert primera_linea(armar_resumen(r, consulta(), [])).startswith("respuesta: 45")


def test_consulta_sin_detalle_describe_lo_leido():
    r = Resultado(estado="terminada", respuesta="3")
    texto = armar_resumen(r, consulta(), [])
    assert texto == "respuesta: 3\nHecho: se leyó conteo en facturas."


def test_cambio_terminado_sin_linea_respuesta():
    plan = Plan(tipo="actualizar", modulo="clientes",
                cambios_autorizados=[Cambio(entidad="cliente", campo="telefono", valor="3014445566")])
    r = Resultado(estado="terminada", respuesta="algo",
                  escrituras=[Cambio(entidad="cliente", campo="telefono", valor="3014445566")])
    texto = armar_resumen(r, plan, SECRETOS)
    assert "respuesta:" not in texto
    assert texto == "Hecho: se actualizó cliente telefono = 3014445566 en clientes. Verificado: 3014445566."


def test_crear_con_detalle_del_ejecutor():
    plan = Plan(tipo="crear", modulo="clientes")
    r = Resultado(estado="terminada", detalle="se creó el cliente Papelería Los Andes, NIT 901234567.",
                  escrituras=[Cambio(entidad="cliente", campo="nit", valor="901234567")])
    texto = armar_resumen(r, plan, [])
    assert texto.startswith("Hecho: se creó el cliente Papelería Los Andes, NIT 901234567 en clientes.")
    assert texto.endswith("Verificado: 901234567.")


@pytest.mark.parametrize("plan", [consulta(), None, Plan(tipo="actualizar", modulo="clientes")])
def test_fallida_nunca_lleva_respuesta(plan):
    r = Resultado(estado="fallida", respuesta="45", detalle="hay dos clientes llamados Ana Torres. respuesta: 45",
                  datos_clave=["52345678", "1019876543"])
    texto = armar_resumen(r, plan, SECRETOS)
    assert "respuesta:" not in texto.lower()
    assert texto.startswith("No se pudo completar: hay dos clientes llamados Ana Torres")
    assert texto.endswith("Datos: 52345678, 1019876543")


def test_fallida_sin_datos_clave():
    texto = armar_resumen(Resultado(estado="fallida", detalle="plazo agotado"), None, [])
    assert texto == "No se pudo completar: plazo agotado."


def test_secreto_en_detalle_queda_oculto():
    r = Resultado(estado="fallida", detalle=f"no conecta a {URL} con {CLAVE}", datos_clave=[TOKEN])
    texto = armar_resumen(r, None, SECRETOS)
    for s in SECRETOS:
        assert s not in texto
    assert OCULTO in texto


@pytest.mark.parametrize("codificar", [
    lambda s: quote(s, safe=""),
    lambda s: quote_plus(s),
    lambda s: quote(s, safe="").lower(),
    lambda s: base64.b64encode(s.encode()).decode(),
    lambda s: base64.urlsafe_b64encode(s.encode()).decode().rstrip("="),
])
def test_contrasena_codificada_queda_oculta(codificar):
    variante = codificar(CLAVE)
    assert variante != CLAVE
    texto = filtrar_secretos(f"url=wss://x/?p={variante}&y=1", SECRETOS)
    assert variante not in texto
    assert OCULTO in texto


def test_host_de_la_url_del_escritorio_queda_oculto():
    texto = filtrar_secretos("abrí escritorio-17.ejemplo.dev en el navegador", SECRETOS)
    assert "escritorio-17.ejemplo.dev" not in texto


def test_respuesta_con_secreto_se_filtra():
    r = Resultado(estado="terminada", respuesta=f"5 {TOKEN}")
    texto = armar_resumen(r, consulta(), SECRETOS)
    assert TOKEN not in texto and primera_linea(texto).startswith("respuesta: 5")


def test_secretos_vacios_o_cortos_no_rompen_el_texto():
    assert filtrar_secretos("respuesta: 45", ["", "4", "45"]) == "respuesta: 45"
