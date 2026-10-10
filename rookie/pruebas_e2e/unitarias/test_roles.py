"""Pruebas sin red del orquestador/ejecutor (Fase D).

Cliente de modelos falso + Escritorio falso (grabar/reproducir). Cubren: lista blanca,
compuerta de identidad, autorización de guardar/crear, cierre garantizado, plazo, y el grep
anti-literales de la app propia en prompts.py.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from rookie.computer_use import Observacion, TareaCerrada
from rookie.esquemas.mensajes import (
    Accion,
    Cambio,
    EscritorioEntrada,
    Plan,
    Resultado,
    TareaEntrada,
    Veredicto,
)
from rookie.roles import ejecutor as E
from rookie.roles.ejecutor import _aplicar_compuertas, _correr, _Estado

# referencias a los hooks reales antes de que el fixture los reemplace
_COMPUERTA_JUEZ_REAL = E._compuerta_juez


@pytest.fixture(autouse=True)
def juez_aprueba(monkeypatch):
    """Por defecto el juez aprueba: aquí se prueba al ejecutor; el juez tiene test_juez.py."""
    async def aprobar(*args, **kwargs):
        return Veredicto(aprobado=True, razon="prueba")
    monkeypatch.setattr(E, "_compuerta_juez", aprobar)
    monkeypatch.setattr(E, "_veredicto_final_juez", aprobar)


def _juez_que(monkeypatch, *, compuerta, aprobado, razon="", datos_clave=()):
    """Juez de compuertas falso: devuelve `aprobado` solo para `compuerta`; aprueba el resto."""
    llamadas: list[str | None] = []

    async def juzgar(accion, plan, png, cliente):
        llamadas.append(accion.compuerta)
        if accion.compuerta == compuerta:
            return Veredicto(aprobado=aprobado, razon=razon, datos_clave=list(datos_clave))
        return Veredicto(aprobado=True, razon="ok")

    monkeypatch.setattr(E, "_compuerta_juez", juzgar)
    return llamadas


def _clics(fake):
    return [a[:3] for a in fake.acciones if a[0] == "click"]

# ──────────────────────────── dobles de prueba ──────────────────────────────

class FakeCliente:
    def __init__(self, plan, acciones, verificacion=None):
        self.plan = plan
        self.acciones = list(acciones)
        self.verificacion = verificacion
        self.roles_llamados: list[str] = []
        self.lanzar_en_accion = False

    async def estructurado(self, rol, *, system, mensajes, esquema, imagenes=(), timeout_s=None, task_id=None):
        self.roles_llamados.append(rol)
        if esquema is Plan:
            return self.plan
        if esquema is Accion:
            if self.lanzar_en_accion:
                raise RuntimeError("fallo del modelo con secreto adentro: hunter2")
            if self.acciones:
                return self.acciones.pop(0)
            return Accion(tipo="terminar", resultado=Resultado(estado="fallida", detalle="sin más acciones"))
        if esquema is Resultado:
            return self.verificacion or Resultado(estado="terminada", detalle="ok", respuesta="ok")
        raise AssertionError(f"esquema inesperado: {esquema}")


class FakeEscritorio:
    def __init__(self, guardias=None):
        self.guardias = guardias
        self.acciones: list[tuple] = []
        self.cerrado = False
        self._n = 0
        self._prev_hash = None

    async def capturar(self):
        if self.guardias:
            self.guardias.comprobar()
        self._n += 1
        h = f"h{self._n}"
        cambio = self._prev_hash is not None and h != self._prev_hash
        self._prev_hash = h
        return Observacion(id=f"obs_{self._n:04d}", png=b"PNG", hash=h, cambio=cambio, estable_tras_ms=0)

    async def click(self, x, y, **kw):
        if self.guardias:
            self.guardias.comprobar()
        self.acciones.append(("click", x, y, kw))

    async def escribir(self, texto, **kw):
        if self.guardias:
            self.guardias.comprobar()
        self.acciones.append(("escribir", texto))

    async def tecla(self, combo):
        if self.guardias:
            self.guardias.comprobar()
        self.acciones.append(("tecla", combo))

    async def scroll(self, x, y, **kw):
        if self.guardias:
            self.guardias.comprobar()
        self.acciones.append(("scroll", x, y))

    async def soltar_entradas(self):
        self.acciones.append(("soltar",))

    async def cerrar(self):
        self.cerrado = True
        if self.guardias:
            self.guardias.cerrar()


def _conectar_con(fake):
    async def conectar(url, password, guardias):
        fake.guardias = guardias
        return fake
    return conectar


def _entrada():
    return TareaEntrada(
        tarea="haz algo",
        escritorio=EscritorioEntrada(url="wss://x/websockify", password="secreta123"),
    )


def _plan(**kw):
    base = {"tipo": "crear", "modulo": "clientes", "textos_permitidos": [], "cambios_autorizados": []}
    base.update(kw)
    return Plan(**base)


# ──────────────────────── compuertas por código ─────────────────────────────

def test_lista_blanca_rechaza_texto_no_autorizado():
    plan = _plan(textos_permitidos=["Pereira"])
    accion = Accion(tipo="escribir", texto="DROP TABLE clientes", x=1, y=1)
    permitido, motivo, terminal = _aplicar_compuertas(accion, plan, _Estado())
    assert permitido is False
    assert terminal is None
    assert "no está en los textos permitidos" in motivo


def test_lista_blanca_normaliza_tildes_y_mayusculas():
    plan = _plan(textos_permitidos=["Papelería Los Andes"])
    accion = Accion(tipo="escribir", texto="papeleria los andes", x=1, y=1)
    permitido, _m, _t = _aplicar_compuertas(accion, plan, _Estado())
    assert permitido is True


def test_identidad_dos_coincidencias_falla_con_datos_clave():
    plan = _plan(tipo="actualizar", identificacion={"nombre": "Ana Torres"},
                 cambios_autorizados=[Cambio(entidad="clientes", campo="telefono", valor="3014445566")])
    accion = Accion(
        tipo="click", compuerta="identidad",
        coincidencias=[
            {"nombre": "Ana Torres", "documento": "52345678"},
            {"nombre": "Ana Torres", "documento": "1019876543"},
        ],
    )
    permitido, _m, terminal = _aplicar_compuertas(accion, plan, _Estado())
    assert permitido is False
    assert terminal is not None and terminal.estado == "fallida"
    unido = " ".join(terminal.datos_clave)
    assert "52345678" in unido and "1019876543" in unido


def test_identidad_una_coincidencia_confirma():
    plan = _plan(tipo="actualizar", identificacion={"nombre": "Marta Ríos"})
    estado = _Estado()
    accion = Accion(tipo="click", compuerta="identidad",
                    coincidencias=[{"nombre": "Marta Ríos", "documento": "99"}])
    permitido, _m, terminal = _aplicar_compuertas(accion, plan, estado)
    assert permitido is True and terminal is None
    assert estado.identidad_confirmada is True


def test_identidad_cero_en_actualizar_falla_no_existe():
    plan = _plan(tipo="actualizar", identificacion={"nombre": "Nadie"})
    accion = Accion(tipo="click", compuerta="identidad", coincidencias=[])
    permitido, _m, terminal = _aplicar_compuertas(accion, plan, _Estado())
    assert permitido is False
    assert terminal is not None and "no existe" in terminal.detalle


def test_guardar_sin_identidad_confirmada_rechazado():
    plan = _plan(tipo="actualizar",
                 cambios_autorizados=[Cambio(entidad="clientes", campo="telefono", valor="3")])
    accion = Accion(tipo="click", compuerta="guardar", elemento="Guardar")
    permitido, motivo, _t = _aplicar_compuertas(accion, plan, _Estado())
    assert permitido is False
    assert "confirmar la identidad" in motivo


def test_guardar_con_identidad_confirmada_pasa():
    plan = _plan(tipo="actualizar",
                 cambios_autorizados=[Cambio(entidad="clientes", campo="telefono", valor="3")])
    estado = _Estado(identidad_confirmada=True)
    accion = Accion(tipo="click", compuerta="guardar", elemento="Guardar")
    permitido, _m, terminal = _aplicar_compuertas(accion, plan, estado)
    assert permitido is True and terminal is None


def test_crear_sin_buscar_antes_rechazado():
    plan = _plan(tipo="crear")
    accion = Accion(tipo="click", compuerta="crear", elemento="Guardar")
    permitido, motivo, _t = _aplicar_compuertas(accion, plan, _Estado())
    assert permitido is False
    assert "descartar duplicados" in motivo


def test_eliminar_nunca_autorizado():
    plan = _plan(tipo="actualizar")
    accion = Accion(tipo="click", compuerta="eliminar", elemento="Borrar")
    permitido, motivo, _t = _aplicar_compuertas(accion, plan, _Estado())
    assert permitido is False and "eliminar no está autorizado" in motivo


def test_no_repite_guardado_incierto():
    plan = _plan(tipo="actualizar",
                 cambios_autorizados=[Cambio(entidad="clientes", campo="telefono", valor="3")])
    estado = _Estado(identidad_confirmada=True, verificacion_pendiente=True)
    accion = Accion(tipo="click", compuerta="guardar", elemento="Guardar")
    permitido, motivo, _t = _aplicar_compuertas(accion, plan, estado)
    assert permitido is False and "no se ha verificado" in motivo


# ──────────────────────── bucle completo (integración) ──────────────────────

def test_flujo_crear_termina_terminada_y_cierra():
    plan = _plan(
        tipo="crear", modulo="clientes",
        textos_permitidos=["Papelería Los Andes", "901234567"],
        cambios_autorizados=[Cambio(entidad="clientes", campo="nombre", valor="Papelería Los Andes")],
    )
    acciones = [
        Accion(tipo="click", x=100, y=48, elemento="menú clientes"),
        Accion(tipo="escribir", x=200, y=200, texto="Papelería Los Andes", elemento="campo nombre"),
        Accion(tipo="click", compuerta="identidad", coincidencias=[], elemento="buscar", x=300, y=100),
        Accion(tipo="click", compuerta="crear", x=400, y=400, elemento="botón Guardar"),
        Accion(tipo="terminar", resultado=Resultado(
            estado="terminada",
            escrituras=[Cambio(entidad="clientes", campo="nombre", valor="Papelería Los Andes")],
            detalle="cliente creado",
        )),
    ]
    verificacion = Resultado(estado="terminada",
                             escrituras=[Cambio(entidad="clientes", campo="nombre", valor="Papelería Los Andes")],
                             detalle="visible en la lista")
    fake = FakeEscritorio()
    cliente = FakeCliente(plan, acciones, verificacion)

    resultado, plan_out = asyncio.run(
        _correr("t-crear", _entrada(), time.monotonic() + 300,
                cliente=cliente, conectar=_conectar_con(fake))
    )
    assert resultado.estado == "terminada"
    assert plan_out is not None and plan_out.tipo == "crear"
    # la creación ocurrió de verdad: el clic de guardar llegó al escritorio
    assert ("click", 400, 400) in _clics(fake)
    assert fake.cerrado is True
    # cierre verificado: tras cerrar, cualquier primitiva lanza TareaCerrada
    with pytest.raises(TareaCerrada):
        asyncio.run(fake.click(1, 1))


def test_texto_no_autorizado_no_llega_al_escritorio():
    plan = _plan(tipo="crear", textos_permitidos=["Pereira"])
    acciones = [
        Accion(tipo="escribir", x=1, y=1, texto="rm -rf /", elemento="campo"),
        Accion(tipo="terminar", resultado=Resultado(estado="fallida", detalle="no se pudo")),
    ]
    fake = FakeEscritorio()
    cliente = FakeCliente(plan, acciones)
    resultado, _p = asyncio.run(
        _correr("t-wl", _entrada(), time.monotonic() + 300,
                cliente=cliente, conectar=_conectar_con(fake))
    )
    assert resultado.estado == "fallida"
    assert not any(a[0] == "escribir" for a in fake.acciones)


def test_cierre_se_llama_aunque_el_modelo_lance():
    plan = _plan(tipo="crear")
    fake = FakeEscritorio()
    cliente = FakeCliente(plan, [])
    cliente.lanzar_en_accion = True
    resultado, _p = asyncio.run(
        _correr("t-exc", _entrada(), time.monotonic() + 300,
                cliente=cliente, conectar=_conectar_con(fake))
    )
    assert resultado.estado == "fallida"
    assert resultado.detalle.startswith("error interno:")
    assert "hunter2" not in resultado.detalle  # ni la traza ni el mensaje
    assert fake.cerrado is True


def test_plazo_agotado_devuelve_fallida():
    plan = _plan(tipo="crear")
    fake = FakeEscritorio()
    cliente = FakeCliente(plan, [])
    # deadline ya vencido una vez restado el margen de 15 s
    resultado, _p = asyncio.run(
        _correr("t-plazo", _entrada(), time.monotonic(),
                cliente=cliente, conectar=_conectar_con(fake))
    )
    assert resultado.estado == "fallida"
    assert resultado.detalle == E.DETALLE_PLAZO
    assert fake.cerrado is True


def test_imposible_falla_sin_bucle():
    plan = _plan(tipo="imposible")
    fake = FakeEscritorio()
    cliente = FakeCliente(plan, [])
    resultado, _p = asyncio.run(
        _correr("t-imp", _entrada(), time.monotonic() + 300,
                cliente=cliente, conectar=_conectar_con(fake))
    )
    assert resultado.estado == "fallida"
    assert fake.acciones == [] or all(a[0] == "soltar" for a in fake.acciones)


# ───────────────────── prompts sin literales de la app propia ─────────────────

def test_prompts_sin_literales_de_la_app():
    ruta = Path(E.__file__).resolve().parents[1] / "roles" / "prompts.py"
    texto = ruta.read_text(encoding="utf-8")
    prohibidos = ["SIGECOM", "Comercial", "Maestros", "Cartera", "operador2", "Distribuidora"]
    presentes = [p for p in prohibidos if p in texto]
    assert presentes == [], f"prompts.py contiene literales de la app propia: {presentes}"


# ──────────────────────── integración con el juez (hooks) ───────────────────

def _plan_actualizar_telefono():
    return Plan(
        tipo="actualizar", modulo="clientes", identificacion={"nombre": "Ana Torres"},
        textos_permitidos=["3014445566"],
        cambios_autorizados=[Cambio(entidad="clientes", campo="telefono", valor="3014445566")],
    )


def _correr_con(cliente, fake, task_id="t-juez"):
    return asyncio.run(
        _correr(task_id, _entrada(), time.monotonic() + 300,
                cliente=cliente, conectar=_conectar_con(fake))
    )


def test_juez_rechaza_crear_y_la_primitiva_no_se_ejecuta(monkeypatch):
    llamadas = _juez_que(monkeypatch, compuerta="crear", aprobado=False,
                         razon="ya hay un registro igual en pantalla")
    plan = Plan(tipo="crear", modulo="clientes", textos_permitidos=["Papelería Los Andes"])
    acciones = [
        Accion(tipo="click", compuerta="identidad", coincidencias=[], elemento="buscar", x=300, y=100),
        Accion(tipo="click", compuerta="crear", x=400, y=400, elemento="botón Guardar"),
        Accion(tipo="terminar", resultado=Resultado(estado="fallida", detalle="no se pudo crear")),
    ]
    fake = FakeEscritorio()
    resultado, _ = _correr_con(FakeCliente(plan, acciones), fake)

    assert resultado.estado == "fallida"
    assert ("click", 400, 400) not in _clics(fake)
    assert llamadas == ["identidad", "crear"]
    assert fake.cerrado is True


def test_juez_ve_dos_registros_en_identidad_termina_fallida_con_claves(monkeypatch):
    _juez_que(monkeypatch, compuerta="identidad", aprobado=False,
              razon="hay dos filas con ese nombre", datos_clave=["52345678", "1019876543"])
    acciones = [
        Accion(tipo="click", compuerta="identidad", x=300, y=100, elemento="fila Ana Torres",
               coincidencias=[{"nombre": "Ana Torres", "documento": "52345678"}]),
        Accion(tipo="click", compuerta="guardar", x=400, y=400, elemento="botón Guardar"),
        Accion(tipo="terminar", resultado=Resultado(estado="terminada")),
    ]
    fake = FakeEscritorio()
    resultado, _ = _correr_con(FakeCliente(_plan_actualizar_telefono(), acciones), fake)

    assert resultado.estado == "fallida"
    assert set(resultado.datos_clave) == {"52345678", "1019876543"}
    assert ("click", 400, 400) not in _clics(fake)   # no se guardó nada
    assert fake.cerrado is True


def test_juez_rechaza_identidad_sin_claves_revierte_la_confirmacion(monkeypatch):
    _juez_que(monkeypatch, compuerta="identidad", aprobado=False, razon="la fila no coincide")
    acciones = [
        Accion(tipo="click", compuerta="identidad", x=300, y=100, elemento="fila",
               coincidencias=[{"nombre": "Ana Torres", "documento": "52345678"}]),
        # el ejecutor insiste en guardar: sin identidad confirmada, el código lo rechaza
        Accion(tipo="click", compuerta="guardar", x=400, y=400, elemento="botón Guardar"),
        Accion(tipo="terminar", resultado=Resultado(estado="fallida", detalle="sin identidad confirmada")),
    ]
    fake = FakeEscritorio()
    resultado, _ = _correr_con(FakeCliente(_plan_actualizar_telefono(), acciones), fake)

    assert resultado.estado == "fallida"
    assert ("click", 400, 400) not in _clics(fake)


def test_veredicto_final_rechazado_convierte_terminada_en_fallida(monkeypatch):
    async def rechazar(resultado, plan, png, cliente):
        return Veredicto(aprobado=False, razon="el teléfono en pantalla es otro",
                         datos_clave=["3010000000"])
    monkeypatch.setattr(E, "_veredicto_final_juez", rechazar)
    cambio = Cambio(entidad="clientes", campo="telefono", valor="3014445566")
    acciones = [
        Accion(tipo="click", compuerta="identidad", x=300, y=100, elemento="fila",
               coincidencias=[{"nombre": "Ana Torres", "documento": "52345678"}]),
        Accion(tipo="escribir", x=200, y=200, texto="3014445566", elemento="campo teléfono"),
        Accion(tipo="click", compuerta="guardar", x=400, y=400, elemento="botón Guardar"),
        Accion(tipo="terminar", resultado=Resultado(estado="terminada", escrituras=[cambio])),
    ]
    verificacion = Resultado(estado="terminada", escrituras=[cambio], detalle="visible")
    fake = FakeEscritorio()
    resultado, _ = _correr_con(FakeCliente(_plan_actualizar_telefono(), acciones, verificacion), fake)

    assert resultado.estado == "fallida"
    assert "el teléfono en pantalla es otro" in resultado.detalle
    assert resultado.datos_clave == ["3010000000"]
    assert fake.cerrado is True


def test_compuerta_juez_con_excepcion_rechaza_irreversibles_y_aprueba_identidad(monkeypatch):
    import rookie.roles.juez as J

    async def explota(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(J, "juzgar_compuerta", explota)

    plan = Plan(tipo="crear", modulo="clientes")
    crear = Accion(tipo="click", compuerta="crear", x=1, y=1)
    identidad = Accion(tipo="click", compuerta="identidad", x=1, y=1, coincidencias=[])

    assert asyncio.run(_COMPUERTA_JUEZ_REAL(crear, plan, b"PNG", None)).aprobado is False
    assert asyncio.run(_COMPUERTA_JUEZ_REAL(identidad, plan, b"PNG", None)).aprobado is True
