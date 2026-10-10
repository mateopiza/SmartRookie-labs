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
    monkeypatch.setattr(E, "_registro_juez", aprobar)

    async def no_visible(*args, **kwargs):
        return Veredicto(aprobado=False, razon="prueba: no se ve")
    monkeypatch.setattr(E, "_texto_visible_juez", no_visible)


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

    async def capturar(self, **kw):
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
        self.acciones.append(("escribir", texto, kw))

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


# ──────────────────────── Fase H: anti-duplicado y juez final ────────────────

def _plan_alta():
    return _plan(
        tipo="crear", identificacion={"nombre": "Papelería Los Andes", "nit": "901234567"},
        textos_permitidos=["Papelería Los Andes", "901234567"],
        cambios_autorizados=[Cambio(entidad="clientes", campo="nombre", valor="Papelería Los Andes"),
                             Cambio(entidad="clientes", campo="ciudad", valor="Pereira")],
    )


def test_crear_con_registro_ya_existente_falla_nombrando_el_identificador():
    accion = Accion(tipo="click", compuerta="identidad", coincidencias=[
        {"codigo": "CL-0141", "nombre": "Papelería Los Andes", "nit": "901234567"}])
    estado = _Estado()
    permitido, _m, terminal = _aplicar_compuertas(accion, _plan_alta(), estado)
    assert permitido is False and estado.puede_crear is False
    assert terminal is not None and terminal.estado == "fallida"
    assert "ya existe" in terminal.detalle
    assert "CL-0141" in " ".join(terminal.datos_clave)


def test_crear_con_coincidencia_parcial_no_es_duplicado():
    # búsqueda por subcadena: 'Los Andes Norte' contiene el nombre pero es otro registro
    accion = Accion(tipo="click", compuerta="identidad", coincidencias=[
        {"codigo": "CL-0009", "nombre": "Papelería Los Andes Norte", "ciudad": "Pereira"}])
    estado = _Estado()
    permitido, _m, terminal = _aplicar_compuertas(accion, _plan_alta(), estado)
    assert permitido is True and terminal is None
    assert estado.puede_crear is True


def test_crear_sin_identificacion_usa_nombre_de_los_cambios_y_no_la_ciudad():
    plan = _plan(tipo="crear", cambios_autorizados=[
        Cambio(entidad="clientes", campo="nombre", valor="Papelería Los Andes"),
        Cambio(entidad="clientes", campo="ciudad", valor="Pereira")])
    otra = Accion(tipo="click", compuerta="identidad",
                  coincidencias=[{"nombre": "Ferretería Sur", "ciudad": "Pereira"}])
    assert _aplicar_compuertas(otra, plan, _Estado())[2] is None
    misma = Accion(tipo="click", compuerta="identidad",
                   coincidencias=[{"nombre": "papeleria los andes", "codigo": "CL-0141"}])
    assert "ya existe" in _aplicar_compuertas(misma, plan, _Estado())[2].detalle


class _EscritorioPngNumerado(FakeEscritorio):
    async def capturar(self, **kw):
        obs = await super().capturar()
        return Observacion(id=obs.id, png=f"PNG{self._n}".encode(), hash=obs.hash,
                           cambio=obs.cambio, estable_tras_ms=0)


def test_juez_final_ve_la_captura_sobre_la_que_se_decidio_terminar(monkeypatch):
    vistos: list[bytes] = []

    async def juez_final(resultado, plan, png, cliente):
        vistos.append(png)
        return Veredicto(aprobado=True, razon="ok")
    monkeypatch.setattr(E, "_veredicto_final_juez", juez_final)
    plan = _plan_alta()
    acciones = [
        Accion(tipo="click", compuerta="identidad", coincidencias=[], x=1, y=1, elemento="buscar"),
        Accion(tipo="click", compuerta="crear", x=2, y=2, elemento="botón Guardar"),
        Accion(tipo="terminar", resultado=Resultado(estado="terminada", escrituras=list(
            plan.cambios_autorizados))),
    ]
    fake = _EscritorioPngNumerado()
    resultado, _ = _correr_con(FakeCliente(plan, acciones), fake)
    assert resultado.estado == "terminada"
    # obs0 + 2 acciones = PNG3 al terminar; la verificación captura PNG4 (sin el aviso efímero)
    assert vistos == [b"PNG3"]


def test_identidad_con_coincidencias_vacias_se_rechaza_sin_terminar():
    accion = Accion(tipo="click", compuerta="identidad", coincidencias=[{}])
    permitido, motivo, terminal = _aplicar_compuertas(accion, _plan_alta(), _Estado())
    assert permitido is False and terminal is None
    assert "coincidencias vacías" in motivo


def test_esquema_de_coincidencias_anuncia_campos_fijos_al_modelo():
    # con dict[str, str] el modo estricto del proveedor devuelve objetos vacíos
    esquema = Accion.model_json_schema()["properties"]["coincidencias"]
    items = esquema["anyOf"][0]["items"]
    assert items["additionalProperties"] is False
    assert set(items["required"]) == {"nombre", "identificador", "texto"}
    a = Accion.model_validate({"tipo": "click", "compuerta": "identidad", "coincidencias": [
        {"nombre": "Ana Torres", "identificador": "52345678", "texto": "Ana Torres 52345678"}]})
    assert a.coincidencias[0]["identificador"] == "52345678"


def test_tres_rechazos_del_juez_en_guardar_terminan_fallida_sin_quemar_el_plazo(monkeypatch):
    llamadas = _juez_que(monkeypatch, compuerta="guardar", aprobado=False,
                         razon="formulario no coincide: otro campo cambió")
    guardar = [Accion(tipo="click", compuerta="guardar", x=400 + i, y=400, elemento="botón Guardar")
               for i in range(10)]
    acciones = [
        Accion(tipo="click", compuerta="identidad", x=300, y=100, elemento="fila",
               coincidencias=[{"nombre": "Ana Torres", "identificador": "52345678", "texto": "x"}]),
        *guardar,
    ]
    fake = FakeEscritorio()
    resultado, _ = _correr_con(FakeCliente(_plan_actualizar_telefono(), acciones), fake)
    assert resultado.estado == "fallida"
    assert "rechazó guardar 3 veces" in resultado.detalle
    assert llamadas.count("guardar") == 3
    assert all(a[1] < 400 for a in _clics(fake))  # ningún Guardar llegó al escritorio


def test_verificador_recibe_la_captura_de_terminar_y_la_actual():
    class _Cli(FakeCliente):
        async def estructurado(self, rol, *, esquema, imagenes=(), **kw):
            if esquema is Resultado:
                self.imagenes_verificador = list(imagenes)
            return await super().estructurado(rol, esquema=esquema, imagenes=imagenes, **kw)

    plan = _plan_alta()
    acciones = [
        Accion(tipo="click", compuerta="identidad", coincidencias=[], x=1, y=1, elemento="buscar"),
        Accion(tipo="click", compuerta="crear", x=2, y=2, elemento="botón Guardar"),
        Accion(tipo="terminar", resultado=Resultado(estado="terminada")),
    ]
    cli = _Cli(plan, acciones, Resultado(estado="terminada", detalle="visible"))
    _correr_con(cli, _EscritorioPngNumerado())
    assert cli.imagenes_verificador == [b"PNG3", b"PNG4"]


def test_consulta_en_varias_paginas_usa_el_conteo_por_paginas_del_juez(monkeypatch):
    vistos: dict[str, object] = {}

    async def conteo(resultado, plan, pngs, cliente):
        vistos["pngs"] = list(pngs)
        return Veredicto(aprobado=True, razon="ok")

    async def final_no_debe_llamarse(*a, **k):
        raise AssertionError("con varias páginas manda el conteo por páginas")
    monkeypatch.setattr(E, "_conteo_juez", conteo)
    monkeypatch.setattr(E, "_veredicto_final_juez", final_no_debe_llamarse)
    plan = _plan(tipo="consultar", modulo="facturas", identificacion={"cliente": "X"})
    acciones = [
        Accion(tipo="click", x=1, y=1, elemento="Siguiente", compuerta="lectura"),   # página 1 (PNG1)
        Accion(tipo="click", x=1, y=1, elemento="Siguiente", compuerta="lectura"),   # página 2 (PNG2)
        Accion(tipo="terminar", compuerta="lectura",                                  # página 3 (PNG3)
               resultado=Resultado(estado="terminada", respuesta="45")),
    ]
    cli = FakeCliente(plan, acciones, Resultado(estado="terminada", respuesta="45"))
    resultado, _ = _correr_con(cli, _EscritorioPngNumerado())
    assert resultado.estado == "terminada" and resultado.respuesta == "45"
    assert vistos["pngs"] == [b"PNG1", b"PNG2", b"PNG3"]


def test_clic_en_cero_cero_no_llega_al_escritorio():
    plan = _plan(tipo="consultar", modulo="facturas")
    acciones = [Accion(tipo="click", x=0, y=0, elemento=""),
                Accion(tipo="click", x=5, y=6, elemento="botón Buscar")]
    fake = FakeEscritorio()
    _correr_con(FakeCliente(plan, acciones), fake)
    assert [c[1:3] for c in _clics(fake)] == [(5, 6)]


def test_escribir_sin_coordenadas_reenfoca_el_campo_del_clic_anterior():
    plan = _plan(tipo="consultar", modulo="facturas", textos_permitidos=["Ferretería Ruiz"])
    acciones = [Accion(tipo="click", x=98, y=131, elemento="campo Cliente"),
                Accion(tipo="escribir", texto="Ferretería Ruiz", elemento="campo Cliente")]
    fake = FakeEscritorio()
    _correr_con(FakeCliente(plan, acciones), fake)
    # clic del paso, y la escritura reselecciona el mismo campo (triple clic) antes de teclear
    assert fake.acciones[0][:3] == ("click", 98, 131)
    assert fake.acciones[1] == ("escribir", "Ferretería Ruiz", {"reemplazar": True, "x": 98, "y": 131})


def test_terminar_sin_resultado_se_rechaza_y_se_puede_repetir():
    plan = _plan(tipo="consultar", modulo="facturas")
    acciones = [Accion(tipo="terminar"),
                Accion(tipo="terminar", resultado=Resultado(estado="terminada", respuesta="45"))]
    cli = FakeCliente(plan, acciones, Resultado(estado="terminada", respuesta="45"))
    resultado, _ = _correr_con(cli, FakeEscritorio())
    assert resultado.estado == "terminada" and resultado.respuesta == "45"


def test_clic_en_boton_exportar_sin_compuerta_pasa_por_la_compuerta(monkeypatch):
    llamadas = _juez_que(monkeypatch, compuerta="exportar", aprobado=False, razon="filtros no coinciden")
    plan = _plan(tipo="exportar", modulo="pedidos", filtros={"estado": "Entregado"})
    acciones = [Accion(tipo="click", x=176, y=166, elemento="botón Exportar")]  # sin compuerta
    fake = FakeEscritorio()
    _correr_con(FakeCliente(plan, acciones), fake)
    assert llamadas == ["exportar"]
    assert _clics(fake) == []  # el juez lo rechazó: no llegó al escritorio


def test_boton_guardar_en_un_alta_se_trata_como_crear():
    plan = _plan(tipo="crear")
    a = E._compuerta_inferida(Accion(tipo="click", x=1, y=1, elemento="botón Guardar"), plan)
    assert a.compuerta == "crear"
    b = E._compuerta_inferida(Accion(tipo="click", x=1, y=1, elemento="menú Comercial"), plan)
    assert b.compuerta is None


def test_plan_acepta_filtros_e_identificacion_como_pares_del_esquema_estricto():
    esquema = Plan.model_json_schema()["properties"]
    assert esquema["filtros"]["type"] == "array"
    assert esquema["identificacion"]["items"]["additionalProperties"] is False
    plan = Plan.model_validate({
        "tipo": "exportar", "modulo": "pedidos",
        "filtros": [{"clave": "estado", "valor": "Entregado"}, {"clave": "desde", "valor": "2026-09-01"}],
        "identificacion": [{"clave": "nombre", "valor": "Ana Torres"}],
    })
    assert plan.filtros == {"estado": "Entregado", "desde": "2026-09-01"}
    assert plan.identificacion == {"nombre": "Ana Torres"}
    assert Plan(tipo="crear", modulo="x", filtros={"a": "b"}).filtros == {"a": "b"}  # dict sigue valiendo


def test_valor_de_un_cambio_autorizado_se_puede_escribir_aunque_falte_en_textos_permitidos():
    plan = _plan(tipo="actualizar", identificacion={"nombre": "Distribuciones El Faro"},
                 textos_permitidos=["Distribuciones El Faro"],
                 cambios_autorizados=[Cambio(entidad="clientes", campo="telefono", valor="3001112233")])
    ok = Accion(tipo="escribir", texto="3001112233", x=1, y=1)
    assert _aplicar_compuertas(ok, plan, _Estado())[0] is True
    otro = Accion(tipo="escribir", texto="3009999999", x=1, y=1)
    assert _aplicar_compuertas(otro, plan, _Estado())[0] is False


def test_solo_teclas_de_la_lista_blanca_llegan_al_escritorio():
    plan = _plan(tipo="consultar", modulo="facturas")
    for prohibida in ("end", "home", "f12", "alt+f4", "ctrl+w", "ctrl+shift+i", "delete"):
        permitido, motivo, terminal = _aplicar_compuertas(
            Accion(tipo="tecla", combo=prohibida), plan, _Estado())
        assert permitido is False and terminal is None and "no está permitida" in motivo
    for ok in ("enter", "Escape", "esc", "shift+tab", "ctrl+a", "Control+A", "backspace", "down"):
        assert _aplicar_compuertas(Accion(tipo="tecla", combo=ok), plan, _Estado())[0] is True
    acciones = [Accion(tipo="tecla", combo="End"), Accion(tipo="tecla", combo="ESC")]
    fake = FakeEscritorio()
    _correr_con(FakeCliente(plan, acciones), fake)
    assert [a for a in fake.acciones if a[0] == "tecla"] == [("tecla", "escape")]


def test_prompt_del_ejecutor_trae_la_regla_generica_de_dialogos():
    from rookie.roles.prompts import EJECUTOR_SYSTEM
    assert "DIÁLOGOS INESPERADOS" in EJECUTOR_SYSTEM
    assert "Entendido" in EJECUTOR_SYSTEM and "NUNCA lo aceptes" in EJECUTOR_SYSTEM


def test_clic_en_cerrar_un_aviso_no_pasa_por_compuertas_criticas():
    plan = _plan(tipo="actualizar")
    for elemento in ("botón Entendido", "botón Aceptar del aviso de novedades", "botón Cerrar"):
        a = E._compuerta_inferida(Accion(tipo="click", x=1, y=1, elemento=elemento), plan)
        assert a.compuerta is None


def test_guardar_que_abre_otro_registro_alerta_y_exige_releer_la_identidad(monkeypatch):
    async def otro_registro(plan, png, cliente):
        return Veredicto(aprobado=False, razon="la pantalla muestra otro registro: CL-0046")
    monkeypatch.setattr(E, "_registro_juez", otro_registro)
    plan = _plan(tipo="actualizar", identificacion={"nombre": "Distribuciones El Faro"},
                 cambios_autorizados=[Cambio(entidad="clientes", campo="telefono", valor="3001112233")])
    fila = {"nombre": "Distribuciones El Faro", "identificador": "CL-0007", "texto": "CL-0007"}
    acciones = [
        Accion(tipo="click", x=1, y=1, elemento="fila", compuerta="identidad", coincidencias=[fila]),
        Accion(tipo="click", x=9, y=9, elemento="botón Guardar", compuerta="guardar"),
        Accion(tipo="click", x=9, y=9, elemento="botón Guardar", compuerta="guardar"),
    ]
    fake = FakeEscritorio()
    cli = FakeCliente(plan, acciones)
    historiales = []
    original = E._pedir_accion

    async def espiar(cliente, plan, historial, *a, **k):
        historiales.append(list(historial))
        return await original(cliente, plan, historial, *a, **k)
    monkeypatch.setattr(E, "_pedir_accion", espiar)
    _correr_con(cli, fake)
    assert [c[1:3] for c in _clics(fake)] == [(1, 1), (9, 9)]  # el 2º guardar no se ejecutó
    assert any("RECHAZADO/ALERTA" in h and "CL-0046" in h for h in historiales[-1])
    assert any(h.startswith("paso 3: RECHAZADO") for h in historiales[-1])
    # aunque el modelo lea algo "limpio" entre medias, guardar exige reconfirmar la identidad
    estado = E._Estado(identidad_confirmada=False)
    guardar = Accion(tipo="click", x=9, y=9, elemento="botón Guardar", compuerta="guardar")
    assert "confirmar la identidad" in _aplicar_compuertas(guardar, plan, estado)[1]


class _CliPlanInvalido(FakeCliente):
    def __init__(self, plan, fallos):
        super().__init__(plan, [])
        self.fallos = fallos
        self.textos: list[str] = []

    async def estructurado(self, rol, *, system, mensajes, esquema, **kw):
        if esquema is Plan:
            self.textos.append(mensajes[-1]["content"])
            if self.fallos > 0:
                self.fallos -= 1
                raise E.ModeloNoDisponible(
                    "JSON inválido para orquestador: 1 validation error for Plan\ntipo\n  Input "
                    "should be 'crear' [type=literal_error, input_value='hunter2', input_type=str]"
                )
        return await super().estructurado(rol, system=system, mensajes=mensajes, esquema=esquema, **kw)


def test_plan_invalido_se_reintenta_una_vez_pasando_el_error():
    cli = _CliPlanInvalido(_plan(tipo="imposible"), fallos=1)
    resultado, plan = _correr_con(cli, FakeEscritorio())
    assert plan is not None and len(cli.textos) == 2
    assert "no fue válido" in cli.textos[1] and "Input should be 'crear'" in cli.textos[1]
    assert "hunter2" not in cli.textos[1]


def test_plan_invalido_dos_veces_termina_fallida_con_el_motivo():
    cli = _CliPlanInvalido(_plan(), fallos=2)
    resultado, plan = _correr_con(cli, FakeEscritorio())
    assert plan is None and resultado.estado == "fallida"
    assert resultado.detalle.startswith("no se pudo planear: ")
    assert "Input should be 'crear'" in resultado.detalle and "hunter2" not in resultado.detalle


def test_prompt_prohibe_inventar_campos_exigidos_y_pide_nombrarlos():
    from rookie.roles.prompts import EJECUTOR_SYSTEM
    assert "CAMPOS EXIGIDOS QUE LA TAREA NO PIDE" in EJECUTOR_SYSTEM
    assert "las opciones que se ven" in EJECUTOR_SYSTEM


def test_fallida_del_ejecutor_por_campo_exigido_conserva_campo_y_opciones():
    plan = _plan(tipo="actualizar", identificacion={"codigo": "PD-0012"})
    fallida = Resultado(estado="fallida", detalle="la app exige el campo Motivo, que la tarea no da",
                        datos_clave=["Motivo", "Cliente desistió", "Error de digitación"])
    resultado, _ = _correr_con(FakeCliente(plan, [Accion(tipo="terminar", resultado=fallida)]),
                               FakeEscritorio())
    assert resultado.estado == "fallida" and resultado.datos_clave == fallida.datos_clave


def test_identidad_distingue_documento_con_puntos():
    plan = _plan(tipo="actualizar", identificacion={"nombre": "Ana Torres", "cedula": "1019876543"})
    filas = [{"nombre": "Ana Torres", "identificador": "1.019.876.543", "texto": "CL-0019"},
             {"nombre": "Ana Torres", "identificador": "52.345.678", "texto": "CL-0007"}]
    accion = Accion(tipo="click", x=1, y=1, compuerta="identidad", coincidencias=filas)
    estado = _Estado()
    permitido, _m, terminal = _aplicar_compuertas(accion, plan, estado)
    assert permitido and terminal is None and estado.identidad_confirmada


def test_disputas_de_identidad_repetidas_terminan_sin_quemar_el_plazo(monkeypatch):
    _juez_que(monkeypatch, compuerta="identidad", aprobado=False, razon="identidad en disputa")
    plan = _plan(tipo="actualizar", identificacion={"nombre": "Ana Torres"})
    fila = {"nombre": "Ana Torres", "identificador": "CL-0007", "texto": "CL-0007"}
    acciones = [Accion(tipo="click", x=1, y=i, compuerta="identidad", coincidencias=[fila])
                for i in range(1, 10)]
    cli = FakeCliente(plan, acciones)
    resultado, _ = _correr_con(cli, FakeEscritorio())
    assert resultado.estado == "fallida" and "no se pudo confirmar el registro" in resultado.detalle
    assert len(cli.acciones) == 5  # paró a la 4.ª disputa


def test_dato_leido_en_pantalla_se_puede_escribir_si_el_juez_lo_ve(monkeypatch):
    vistos = []

    async def visible(texto, png, cliente):
        vistos.append(texto)
        return Veredicto(aprobado=texto == "P-0066", razon="")
    monkeypatch.setattr(E, "_texto_visible_juez", visible)
    plan = _plan(tipo="actualizar", identificacion={"nombre": "Hugo Restrepo"})
    acciones = [Accion(tipo="escribir", x=5, y=5, texto="P-0066", elemento="campo Código"),
                Accion(tipo="escribir", x=5, y=5, texto="X-9999", elemento="campo Código"),
                Accion(tipo="escribir", x=5, y=5, texto="a" * 41, elemento="campo Notas")]
    fake = FakeEscritorio()
    _correr_con(FakeCliente(plan, acciones), fake)
    assert [a[1] for a in fake.acciones if a[0] == "escribir"] == ["P-0066"]
    assert sorted(set(vistos)) == ["P-0066", "X-9999"]  # el texto largo ni se consulta


def test_prompts_identificacion_precisa_y_varios_registros_de_uno_en_uno():
    from rookie.roles.prompts import EJECUTOR_SYSTEM, ORQUESTADOR_SYSTEM
    assert "Datos de contexto" in ORQUESTADOR_SYSTEM and "van en `filtros`" in ORQUESTADOR_SYSTEM
    assert "VARIOS REGISTROS" in EJECUTOR_SYSTEM


def test_acciones_descartadas_en_bucle_terminan_con_el_motivo():
    plan = _plan(tipo="consultar", modulo="facturas")
    acciones = [Accion(tipo="click", elemento="botón") for _ in range(20)]  # sin coordenadas
    cli = FakeCliente(plan, acciones)
    fake = FakeEscritorio()
    resultado, _ = _correr_con(cli, fake)
    assert resultado.estado == "fallida" and "sin avance" in resultado.detalle
    assert "acciones seguidas descartadas" in resultado.detalle
    assert len(cli.acciones) == 20 - E.MAX_DESCARTES_SEGUIDOS


def test_terminar_sin_resultado_dos_veces_deja_decidir_al_verificador():
    plan = _plan(tipo="consultar", modulo="facturas")
    cli = FakeCliente(plan, [Accion(tipo="terminar"), Accion(tipo="terminar")],
                      Resultado(estado="terminada", respuesta="45"))
    resultado, _ = _correr_con(cli, FakeEscritorio())
    assert resultado.estado == "terminada" and resultado.respuesta == "45"


def test_varios_candidatos_con_criterio_de_la_instruccion_no_es_ambiguedad():
    plan = _plan(tipo="actualizar", identificacion={"paciente": "Ana Torres", "concepto": "Control"},
                 filtros={"criterio": "anular solo la factura duplicada"})
    filas = [{"nombre": "Ana Torres", "identificador": "F-2301", "texto": "F-2301 Control"},
             {"nombre": "Ana Torres", "identificador": "F-2302", "texto": "F-2302 Control"}]
    estado = _Estado()
    permitido, motivo, terminal = _aplicar_compuertas(
        Accion(tipo="click", x=1, y=1, compuerta="identidad", coincidencias=filas), plan, estado)
    assert not permitido and terminal is None and "criterio" in motivo
    permitido, _m, terminal = _aplicar_compuertas(
        Accion(tipo="click", x=1, y=1, compuerta="identidad", coincidencias=filas[1:]), plan, estado)
    assert permitido and estado.identidad_confirmada
    # sin criterio, los homónimos siguen siendo ambigüedad (E5)
    sin = _plan(tipo="actualizar", identificacion={"paciente": "Ana Torres"})
    assert "ambigua" in _aplicar_compuertas(
        Accion(tipo="click", x=1, y=1, compuerta="identidad", coincidencias=filas), sin, _Estado())[2].detalle


def test_disputa_uno_contra_cero_se_resuelve_si_el_identificador_se_ve(monkeypatch):
    _juez_que(monkeypatch, compuerta="identidad", aprobado=False, razon="identidad en disputa")
    pedidos = []

    async def visible(texto, png, cliente):
        pedidos.append(texto)
        return Veredicto(aprobado=texto == "C-1459", razon="")
    monkeypatch.setattr(E, "_texto_visible_juez", visible)
    plan = _plan(tipo="actualizar", identificacion={"paciente": "Natalia Rincón"},
                 cambios_autorizados=[Cambio(entidad="citas", campo="hora", valor="08:40")])
    fila = {"nombre": "Natalia Rincón", "identificador": "C-1459", "texto": "C-1459 Natalia Rincón 21/10"}
    acciones = [Accion(tipo="click", x=1, y=1, compuerta="identidad", coincidencias=[fila]),
                Accion(tipo="click", x=9, y=9, elemento="botón Guardar", compuerta="guardar")]
    fake = FakeEscritorio()
    _correr_con(FakeCliente(plan, acciones), fake)
    assert pedidos == ["C-1459"]
    assert [c[1:3] for c in _clics(fake)] == [(1, 1), (9, 9)]  # identidad y guardado ejecutados


def test_disputa_con_fila_que_no_trae_la_identificacion_no_se_resuelve(monkeypatch):
    _juez_que(monkeypatch, compuerta="identidad", aprobado=False, razon="identidad en disputa")
    plan = _plan(tipo="actualizar", identificacion={"paciente": "Natalia Rincón"})
    fila = {"nombre": "Otro Paciente", "identificador": "C-1459", "texto": "C-1459"}
    fake = FakeEscritorio()
    _correr_con(FakeCliente(plan, [Accion(tipo="click", x=1, y=1, compuerta="identidad",
                                          coincidencias=[fila])]), fake)
    assert _clics(fake) == []


def test_dato_leido_en_una_pantalla_anterior_se_puede_escribir(monkeypatch):
    async def visible(texto, png, cliente):
        return Veredicto(aprobado=png == b"PNG1", razon="")  # solo en la primera captura
    monkeypatch.setattr(E, "_texto_visible_juez", visible)
    plan = _plan(tipo="actualizar", identificacion={"nombre": "Hugo Restrepo"})
    acciones = [Accion(tipo="click", x=3, y=3, elemento="menú"),
                Accion(tipo="escribir", x=5, y=5, texto="C-1459", elemento="campo Código")]
    fake = _EscritorioPngNumerado()
    _correr_con(FakeCliente(plan, acciones), fake)
    assert [a[1] for a in fake.acciones if a[0] == "escribir"] == ["C-1459"]


def test_fila_comprobada_acepta_el_nombre_en_otro_orden(monkeypatch):
    async def visible(texto, png, cliente):
        return Veredicto(aprobado=texto == "P-0066", razon="")
    monkeypatch.setattr(E, "_texto_visible_juez", visible)
    plan = _plan(tipo="actualizar", identificacion={"nombre": "Hugo Restrepo"})
    fila = {"nombre": "Restrepo, Hugo", "identificador": "P-0066", "texto": "P-0066 Restrepo, Hugo"}
    acc = Accion(tipo="click", compuerta="identidad", coincidencias=[fila])
    v = Veredicto(aprobado=False, razon="identidad en disputa")
    assert asyncio.run(E._fila_comprobada(acc, plan, v, b"PNG", None))
    otra = {"nombre": "Restrepo, Ana", "identificador": "P-0066", "texto": "P-0066 Restrepo, Ana"}
    acc2 = Accion(tipo="click", compuerta="identidad", coincidencias=[otra])
    assert not asyncio.run(E._fila_comprobada(acc2, plan, v, b"PNG", None))
