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
    tipos = [(a[0], a[1:3]) if a[0] == "click" else (a[0],) for a in fake.acciones]
    # clic del paso, y otra vez clic justo antes de teclear
    assert tipos[:3] == [("click", (98, 131)), ("click", (98, 131)), ("escribir",)]


def test_terminar_sin_resultado_se_rechaza_y_se_puede_repetir():
    plan = _plan(tipo="consultar", modulo="facturas")
    acciones = [Accion(tipo="terminar"),
                Accion(tipo="terminar", resultado=Resultado(estado="terminada", respuesta="45"))]
    cli = FakeCliente(plan, acciones, Resultado(estado="terminada", respuesta="45"))
    resultado, _ = _correr_con(cli, FakeEscritorio())
    assert resultado.estado == "terminada" and resultado.respuesta == "45"
