"""Ejecutor: bucle ejecutar -> comprobar -> ajustar con compuertas aplicadas por CÓDIGO.

Flujo (contrato §2, DISENO §2/§9):
  Guardias(deadline - margen) -> Escritorio.conectar + primera captura -> planear ->
  bucle [capturar | rol "ejecutor" (plan, historial, 2 últimas obs) -> Accion ->
         compuertas de código -> (hook juez en acciones críticas) -> primitiva -> observar] ->
  verificación final obligatoria -> cierre SIEMPRE (finally).

Nada que el modelo diga salta las compuertas: lista blanca de escritura, identidad, autorización
de guardar/crear/exportar, no-repetir guardados inciertos, límites de intentos y presupuesto.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from rookie.banco import Grabador
from rookie.computer_use import (
    Escritorio,
    Guardias,
    Observacion,
    PlazoAgotado,
    TareaCerrada,
)
from rookie.esquemas.mensajes import (
    Accion,
    Plan,
    Resultado,
    TareaEntrada,
    Veredicto,
)
from rookie.modelos.cliente import ClienteModelos, ModeloNoDisponible
from rookie.roles.orquestador import PlanNoValido, planear
from rookie.roles.prompts import EJECUTOR_SYSTEM, VERIFICADOR_SYSTEM

log = logging.getLogger("rookie.roles.ejecutor")

# ── presupuesto y límites (segundos / pasos) ──────────────────────────────────
MARGEN_S = 10.0           # Guardias cierra 10 s antes del deadline real
UMBRAL_CIERRE_S = 35.0    # por debajo: el prompt pide "cierra con lo verificable"
UMBRAL_FALLIDA_S = 10.0   # por debajo: fallida "plazo agotado"
MAX_PASOS = 60            # tope duro de pasos lógicos
MAX_SIN_CAMBIO = 6        # 6 pasos sin que cambie el hash -> fallida
MAX_REPETICION = 3        # misma acción 3 veces sobre el mismo hash -> forzar cambio
MAX_EVIDENCIAS = 6        # capturas-evidencia (páginas) que se pasan al verificar
MAX_RECHAZOS_JUEZ = 3     # 3 rechazos del juez en la misma compuerta irreversible -> fallida
MAX_DISPUTAS_IDENTIDAD = 4  # el juez no confirma la identidad 4 veces -> fallida (no quemar el plazo)

DETALLE_PLAZO = "plazo agotado"
Criticas = {"identidad", "guardar", "crear", "exportar", "eliminar"}

Conectar = Callable[[str, str, Guardias], Awaitable[Escritorio]]


# ── normalización de texto para la lista blanca y la identidad ────────────────

def _norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", s).strip().lower()


def _texto_autorizado(texto: str | None, plan: Plan) -> bool:
    if texto is None:
        return False
    # los valores de los cambios, la identificación y los filtros son autorizados por definición
    # aunque el orquestador olvide copiarlos a textos_permitidos (visto en evaluación: el
    # teléfono pedido quedó fuera y el ejecutor no pudo escribirlo)
    candidatos = [
        *plan.textos_permitidos,
        *(c.valor for c in plan.cambios_autorizados),
        *plan.identificacion.values(),
        *plan.filtros.values(),
    ]
    permitidos = {_norm(t) for t in candidatos if _norm(t)}
    return _norm(texto) in permitidos


def _identificadores(coincidencias: list[dict]) -> list[str]:
    out: list[str] = []
    for fila in coincidencias:
        partes = [f"{k}={v}" for k, v in fila.items() if str(v).strip()]
        out.append("; ".join(partes) if partes else json.dumps(fila, ensure_ascii=False))
    return out


def _compacto(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", _norm(s))


def _distingue_una(plan: Plan, coincidencias: list[dict]) -> bool:
    # forma compacta: una cédula "1.019.876.543" en pantalla es la "1019876543" del plan
    ident = [_compacto(v) for v in plan.identificacion.values() if _compacto(v)]
    if not ident:
        return False

    def coincide(fila: dict) -> bool:
        texto = _compacto(" ".join(str(v) for v in fila.values()))
        return all(i in texto for i in ident)

    return sum(1 for f in coincidencias if coincide(f)) == 1


_CAMPOS_IDENTIFICADORES = ("nombre", "razon", "nit", "documento", "cedula", "codigo", "id", "rut")


def _claves_identidad(plan: Plan) -> set[str]:
    """Valores que identifican el registro de un alta: la identificación del plan o, si viene
    vacía, los valores de los campos tipo nombre/documento/código de los cambios autorizados."""
    claves = {_norm(v) for v in plan.identificacion.values() if _norm(v)}
    if not claves:
        claves = {
            _norm(c.valor) for c in plan.cambios_autorizados
            if _norm(c.valor) and any(p in _norm(c.campo) for p in _CAMPOS_IDENTIFICADORES)
        }
    return claves


def _duplicados(plan: Plan, coincidencias: list[dict]) -> list[dict]:
    """Filas con algún valor IGUAL (no solo contenido) a un identificador del alta: ya existe.
    Igualdad exacta normalizada, para que 'Los Andes Norte' no cuente como 'Los Andes'."""
    claves = _claves_identidad(plan)
    return [f for f in coincidencias if any(_norm(str(v)) in claves for v in f.values())]


_VERBOS_CRITICOS = (
    ("eliminar", "eliminar"), ("borrar", "eliminar"), ("anular", "eliminar"),
    ("exportar", "exportar"), ("guardar", "guardar"),
)


def _compuerta_inferida(accion: Accion, plan: Plan) -> Accion:
    """Un clic sobre un botón que NOMBRA una acción irreversible pasa por su compuerta aunque el
    modelo no la marque (visto en E4: tras dos rechazos del juez pulsó Exportar sin compuerta)."""
    if accion.compuerta is not None or accion.tipo not in {"click", "doble_click"}:
        return accion
    elemento = _norm(accion.elemento)
    for verbo, compuerta in _VERBOS_CRITICOS:
        if verbo in elemento:
            if compuerta == "guardar" and plan.tipo == "crear":
                compuerta = "crear"
            return accion.model_copy(update={"compuerta": compuerta})
    return accion


# Teclas que el ejecutor puede pulsar. Cualquier otra (End, Home, F1–F12, alt+…, ctrl+… salvo
# ctrl+a) se rechaza: End sacó al agente de un formulario y una tecla de función abrió DevTools.
# Delete NO: en el escritorio con el tema nuevo inserta un carácter basura (□) en el campo.
TECLAS_PERMITIDAS = {
    "enter", "escape", "tab", "shift+tab", "ctrl+a", "backspace",
    "up", "down", "left", "right",
}
_ALIAS_TECLA = {
    "return": "enter", "intro": "enter", "esc": "escape", "control": "ctrl", "supr": "delete",
    "del": "delete", "arrowup": "up", "arrowdown": "down", "arrowleft": "left",
    "arrowright": "right",
}


def _tecla_normalizada(combo: str | None) -> str | None:
    """El combo en forma canónica si está en la lista blanca; None si no se permite."""
    partes = [p.strip().lower().replace(" ", "").replace("_", "") for p in (combo or "").split("+")]
    partes = [_ALIAS_TECLA.get(p, p) for p in partes if p]
    canonico = "+".join(partes)
    return canonico if canonico in TECLAS_PERMITIDAS else None


def _campo_de(accion: Accion, plan: Plan) -> str:
    if len(plan.cambios_autorizados) == 1:
        return _norm(plan.cambios_autorizados[0].campo)
    return _norm(accion.elemento) or "campo"


# ── estado mutable del bucle ──────────────────────────────────────────────────

@dataclass
class _Estado:
    identidad_confirmada: bool = False
    puede_crear: bool = False           # pasó identidad con 0 coincidencias (anti-duplicado)
    creaciones: int = 0
    exportaciones: int = 0
    guardados_por_campo: set[str] = field(default_factory=set)
    verificacion_pendiente: bool = False  # último guardar/crear sin confirmar


# ── compuertas de código (en orden, antes de cada acción) ─────────────────────

def _aplicar_compuertas(
    accion: Accion, plan: Plan, estado: _Estado
) -> tuple[bool, str | None, Resultado | None]:
    """(permitido, motivo_rechazo_para_historial, resultado_terminal_fallida)."""
    # b. Lista blanca de escritura
    if accion.tipo == "escribir" and not _texto_autorizado(accion.texto, plan):
        return (
            False,
            (
                f"RECHAZADO: el texto {accion.texto!r} no está en los textos permitidos del plan; "
                "solo puedes escribir valores autorizados."
            ),
            None,
        )

    # a. Lista blanca de teclas
    if accion.tipo == "tecla" and _tecla_normalizada(accion.combo) is None:
        return (
            False,
            (
                f"RECHAZADO: la tecla {accion.combo!r} no está permitida (puede sacarte del "
                "formulario, abrir otra ventana o meter un carácter basura). Solo: enter, escape, "
                "tab, shift+tab, ctrl+a, backspace y flechas; para vaciar un campo usa escribir."
            ),
            None,
        )

    # eliminar nunca se autoriza
    if accion.compuerta == "eliminar":
        return False, "RECHAZADO: eliminar no está autorizado en ninguna tarea.", None

    # c. Compuerta de identidad
    if accion.compuerta == "identidad":
        coincidencias = accion.coincidencias or []
        if any(not any(str(v).strip() for v in f.values()) for f in coincidencias):
            return (
                False,
                (
                    "RECHAZADO: hay coincidencias vacías; rellena nombre, identificador y texto de "
                    "cada fila tal como se ven en pantalla."
                ),
                None,
            )
        if len(coincidencias) == 0:
            if plan.tipo == "actualizar":
                return False, None, Resultado(
                    estado="fallida",
                    detalle="no existe un registro que coincida con la identificación",
                )
            estado.puede_crear = True  # crear: 0 coincidencias = no hay duplicado
            return True, "identidad: 0 coincidencias; no hay duplicado, se puede crear.", None
        if plan.tipo == "crear":
            # anti-duplicado: si ya existe un registro con ese nombre/documento, no se crea otro
            dup = _duplicados(plan, coincidencias)
            if dup:
                ids = _identificadores(dup)
                return False, None, Resultado(
                    estado="fallida",
                    detalle="ya existe un registro con esa identificación; no se crea un duplicado",
                    datos_clave=ids,
                )
            estado.puede_crear = True
            return (
                True,
                "identidad: las coincidencias parciales no son el mismo registro; se puede crear.",
                None,
            )
        if len(coincidencias) == 1 or _distingue_una(plan, coincidencias):
            estado.identidad_confirmada = True
            return True, "identidad: 1 registro distinguido; confirmada.", None
        # > 1 y el plan no distingue exactamente uno -> ambigüedad
        return False, None, Resultado(
            estado="fallida",
            detalle="identidad ambigua: más de un registro coincide con la identificación",
            datos_clave=_identificadores(coincidencias),
        )

    # d. Autorización de guardar / crear / exportar
    if estado.verificacion_pendiente and accion.compuerta in {"guardar", "crear"}:
        return (
            False,
            (
                "RECHAZADO: el último guardado/creación no se ha verificado. Lee el registro para "
                "confirmar el resultado; NO repitas el guardado."
            ),
            None,
        )

    if accion.compuerta == "crear":
        if plan.tipo != "crear":
            return False, "RECHAZADO: el plan no autoriza crear.", None
        if not estado.puede_crear:
            return (
                False,
                (
                    "RECHAZADO: antes de crear debes buscar el registro para descartar duplicados "
                    "(compuerta identidad con 0 coincidencias)."
                ),
                None,
            )
        if estado.creaciones >= 1:
            return False, "RECHAZADO: ya se creó un registro en esta tarea.", None

    if accion.compuerta == "guardar":
        if plan.tipo != "actualizar":
            return False, "RECHAZADO: el plan no autoriza guardar cambios.", None
        if not estado.identidad_confirmada:
            return (
                False,
                (
                    "RECHAZADO: antes de guardar debes confirmar la identidad del registro "
                    "(compuerta identidad con exactamente 1 coincidencia)."
                ),
                None,
            )
        if _campo_de(accion, plan) in estado.guardados_por_campo:
            return False, "RECHAZADO: ese campo ya se guardó; no se repite.", None

    if accion.compuerta == "exportar":
        if plan.tipo != "exportar":
            return False, "RECHAZADO: el plan no autoriza exportar.", None
        if estado.exportaciones >= 1:
            return False, "RECHAZADO: ya se exportó en esta tarea.", None

    return True, None, None


def _contabilizar(accion: Accion, plan: Plan, estado: _Estado) -> None:
    """Tras ejecutar una acción crítica, registra que ocurrió (límites de una sola vez)."""
    if accion.compuerta == "crear":
        estado.creaciones += 1
        estado.verificacion_pendiente = True
    elif accion.compuerta == "guardar":
        estado.guardados_por_campo.add(_campo_de(accion, plan))
        estado.verificacion_pendiente = True
    elif accion.compuerta == "exportar":
        estado.exportaciones += 1
        estado.verificacion_pendiente = True
    else:
        estado.verificacion_pendiente = False  # una lectura/otra acción "limpia" la duda


# ── hook del juez (import perezoso; aprueba mientras no exista) ────────────────

async def _compuerta_juez(
    accion: Accion, plan: Plan, observacion_png: bytes, cliente: ClienteModelos
) -> Veredicto:
    try:
        from rookie.roles.juez import juzgar_compuerta
    except ImportError:
        log.info("juez no disponible; compuerta %s aprobada por defecto", accion.compuerta)
        return Veredicto(aprobado=True, razon="juez no disponible")
    try:
        return await juzgar_compuerta(accion, plan, observacion_png, cliente)
    except Exception as exc:  # noqa: BLE001 - un fallo del juez no debe tumbar la tarea
        razon = f"juez no disponible: {type(exc).__name__}"
        if accion.compuerta == "identidad":
            # en identidad manda el conteo del ejecutor (ya pasó la compuerta de código)
            log.warning("juez falló (%s) en identidad; se aprueba", type(exc).__name__)
            return Veredicto(aprobado=True, razon=razon)
        # guardar/crear/exportar son irreversibles: sin juez no se ejecutan
        log.warning("juez falló (%s) en %s; se rechaza", type(exc).__name__, accion.compuerta)
        return Veredicto(aprobado=False, razon=razon)


async def _veredicto_final_juez(
    resultado: Resultado, plan: Plan, observacion_png: bytes, cliente: ClienteModelos
) -> Veredicto:
    try:
        from rookie.roles.juez import veredicto_final
    except ImportError:
        return Veredicto(aprobado=True, razon="juez no disponible")
    try:
        return await veredicto_final(resultado, plan, observacion_png, cliente)
    except Exception as exc:  # noqa: BLE001 - sin confirmación independiente no hay terminada
        log.warning("juez falló (%s) en el veredicto final; no se confirma", type(exc).__name__)
        return Veredicto(aprobado=False, razon=f"juez no disponible: {type(exc).__name__}")


async def _conteo_juez(
    resultado: Resultado, plan: Plan, pngs: list[bytes], cliente: ClienteModelos
) -> Veredicto:
    try:
        from rookie.roles.juez import verificar_conteo
    except ImportError:
        return Veredicto(aprobado=True, razon="juez no disponible")
    try:
        return await verificar_conteo(resultado, plan, pngs, cliente)
    except Exception as exc:  # noqa: BLE001 - sin confirmación independiente no hay terminada
        log.warning("juez falló (%s) en el conteo; no se confirma", type(exc).__name__)
        return Veredicto(aprobado=False, razon=f"juez no disponible: {type(exc).__name__}")


async def _registro_juez(plan: Plan, observacion_png: bytes, cliente: ClienteModelos) -> Veredicto:
    try:
        from rookie.roles.juez import registro_tras_guardar
    except ImportError:
        return Veredicto(aprobado=True, razon="juez no disponible")
    try:
        return await registro_tras_guardar(plan, observacion_png, cliente)
    except Exception as exc:  # noqa: BLE001 - sin lectura no se alerta (la verificación final decide)
        log.warning("juez falló (%s) leyendo el registro tras guardar", type(exc).__name__)
        return Veredicto(aprobado=True, razon=f"juez no disponible: {type(exc).__name__}")


# ── primitivas ────────────────────────────────────────────────────────────────

async def _ejecutar_primitiva(escritorio: Escritorio, accion: Accion) -> None:
    t = accion.tipo
    x = accion.x if accion.x is not None else 640
    y = accion.y if accion.y is not None else 400
    if t == "click":
        await escritorio.click(x, y)
    elif t == "doble_click":
        await escritorio.click(x, y, doble=True)
    elif t == "escribir":
        # selección (triple clic en el campo) y tecleo en la MISMA primitiva: entre dos pasos
        # (~4 s) la app puede quitar el foco al campo y el texto se pierde o se concatena
        await escritorio.escribir(accion.texto or "", reemplazar=True, x=accion.x, y=accion.y)
    elif t == "tecla":
        combo = _tecla_normalizada(accion.combo)
        if combo is not None:  # las compuertas ya rechazaron las demás; defensa en profundidad
            await escritorio.tecla(combo)
    elif t == "scroll":
        await escritorio.scroll(x, y, abajo=True)
    elif t == "esperar":
        await asyncio.sleep(0.4)


# ── llamadas al modelo ────────────────────────────────────────────────────────

async def _pedir_accion(
    cliente: ClienteModelos,
    plan: Plan,
    historial: list[str],
    obs_prev: Observacion | None,
    obs_actual: Observacion,
    cerrar_pronto: bool,
    task_id: str,
) -> Accion:
    partes = [
        "PLAN AUTORIZADO (no salgas de aquí):",
        plan.model_dump_json(),
        "",
        "HISTORIAL (acciones y su resultado):",
        "\n".join(historial[-24:]) if historial else "(sin pasos aún)",
        "",
    ]
    if cerrar_pronto:
        partes.append("AVISO: queda POCO tiempo. Cierra con lo verificable (terminar).")
    if obs_prev is not None:
        partes.append("Te muestro la observación ANTERIOR y la ACTUAL (la última es la vigente).")
    else:
        partes.append("Te muestro la observación ACTUAL (es la primera).")
    partes.append("Decide la SIGUIENTE acción (una sola).")

    imagenes: list[bytes] = []
    if obs_prev is not None:
        imagenes.append(obs_prev.png)
    imagenes.append(obs_actual.png)

    return await cliente.estructurado(
        "ejecutor",
        system=EJECUTOR_SYSTEM,
        mensajes=[{"role": "user", "content": "\n".join(partes)}],
        esquema=Accion,
        imagenes=imagenes,
        task_id=task_id,
    )


async def _verificar_final(
    cliente: ClienteModelos,
    plan: Plan,
    propuesto: Resultado | None,
    escritorio: Escritorio,
    guardias: Guardias,
    task_id: str,
    grabador: Grabador,
    obs_terminar: Observacion | None = None,
    evidencias: list[Observacion] | None = None,
) -> Resultado:
    try:
        guardias.comprobar()
        obs = await escritorio.capturar(esperar_cambio=False)  # sin acción previa
    except (PlazoAgotado, TareaCerrada):
        return propuesto or Resultado(estado="fallida", detalle="no se pudo verificar: tarea cerrada")

    contexto = {
        "plan": json.loads(plan.model_dump_json()),
        "resultado_propuesto": json.loads(propuesto.model_dump_json()) if propuesto else None,
    }
    mensajes = [{
        "role": "user",
        "content": (
            "PLAN y RESULTADO PROPUESTO por el ejecutor:\n"
            f"{json.dumps(contexto, ensure_ascii=False)}\n\n"
            + (
                "Te muestro DOS capturas: la del momento en que el ejecutor decidió terminar "
                "(puede traer avisos efímeros de éxito o error) y la ACTUAL (la última). Confirma "
                "el resultado de forma independiente con lo que se ve en ellas."
                if obs_terminar is not None
                else "Relee SOLO la pantalla actual y confirma el resultado de forma independiente."
            )
        ),
    }]
    imagenes = [obs_terminar.png, obs.png] if obs_terminar is not None else [obs.png]
    paginas = [e.png for e in (evidencias or [])]
    if len(paginas) >= 2:
        # dato repartido en varias capturas (páginas): el verificador las ve todas, en orden
        mensajes[0]["content"] += (
            f"\n\nAdemás te muestro primero {len(paginas)} capturas que el ejecutor marcó como "
            "evidencia (páginas de un listado, en orden). Si el dato se reparte entre ellas, "
            "cuenta en cada una y suma, sin contar dos veces la misma fila."
        )
        imagenes = paginas + [obs.png]
    try:
        ver = await cliente.estructurado(
            "ejecutor", system=VERIFICADOR_SYSTEM, mensajes=mensajes,
            esquema=Resultado, imagenes=imagenes, task_id=task_id,
        )
    except ModeloNoDisponible:
        return Resultado(estado="fallida", detalle="no se pudo verificar el resultado")

    grabador.paso(
        png=obs.png,
        accion={"tipo": "verificar"},
        meta={"razon": "verificación final", "resultado": json.loads(ver.model_dump_json())},
    )

    if plan.tipo == "consultar":
        if not (ver.estado == "terminada" and ver.respuesta and ver.respuesta.strip()):
            return Resultado(
                estado="fallida", detalle=ver.detalle or "no se pudo leer la respuesta en pantalla"
            )
        final = ver
    elif ver.estado == "terminada":
        escrituras = ver.escrituras or (propuesto.escrituras if propuesto else [])
        final = Resultado(
            estado="terminada",
            escrituras=escrituras,
            detalle=ver.detalle or (propuesto.detalle if propuesto else ""),
        )
    else:
        return Resultado(
            estado="fallida",
            detalle=ver.detalle or "no se pudo verificar el resultado en pantalla",
            datos_clave=ver.datos_clave,
        )

    if plan.tipo == "consultar" and len(paginas) >= 2:
        # un conteo repartido en páginas no se lee en una sola captura: el juez cuenta cada
        # página por separado y el código suma
        veredicto = await _conteo_juez(final, plan, paginas, cliente)
        grabador.paso(
            png=None,
            accion={"tipo": "veredicto_conteo"},
            meta={"aprobado": veredicto.aprobado, "razon": veredicto.razon, "paginas": len(paginas)},
        )
        if not veredicto.aprobado:
            return Resultado(
                estado="fallida", detalle=f"el juez no confirmó el conteo: {veredicto.razon}",
                datos_clave=list(veredicto.datos_clave),
            )
        return final

    # segunda comprobación independiente (juez, otro proveedor): sin ella no hay terminada.
    # Ve la captura sobre la que el ejecutor decidió terminar: los avisos de éxito ("… creado")
    # son efímeros y ya no están en la captura de la verificación, pero el estado es el mismo.
    png_juez = obs_terminar.png if obs_terminar is not None else obs.png
    veredicto = await _veredicto_final_juez(final, plan, png_juez, cliente)
    grabador.paso(
        png=None,
        accion={"tipo": "veredicto_final"},
        meta={"aprobado": veredicto.aprobado, "razon": veredicto.razon,
              "datos_clave": list(veredicto.datos_clave)},
    )
    if not veredicto.aprobado:
        log.info("task_id=%s el juez no confirmó el resultado: %s", task_id, veredicto.razon)
        return Resultado(
            estado="fallida",
            detalle=f"el juez no confirmó el resultado: {veredicto.razon}",
            datos_clave=list(veredicto.datos_clave),
        )
    return final


# ── bucle principal (núcleo testeable) ────────────────────────────────────────

def _firma(accion: Accion) -> tuple:
    return (accion.tipo, accion.x, accion.y, _norm(accion.texto), _norm(accion.combo))


async def _bucle(
    task_id: str,
    plan: Plan,
    cliente: ClienteModelos,
    escritorio: Escritorio,
    guardias: Guardias,
    grabador: Grabador,
    obs0: Observacion,
) -> Resultado:
    # setup/reconocimiento: el primer contexto del ejecutor le recuerda que tiene tiempo para
    # CONOCER la interfaz (orientarse, confirmar la ruta del menú y leer los campos del
    # formulario) antes de escribir, en vez de lanzarse a clics a ciegas.
    historial: list[str] = [
        (
            "reconocimiento: aún no has actuado. Tienes tiempo para conocer la interfaz: "
            "oriéntate, confirma la ruta del menú a tu módulo mirando cada captura y, dentro "
            "de un formulario, identifica los campos antes de escribir."
        )
    ]
    estado = _Estado()
    obs_prev: Observacion | None = None
    obs_actual = obs0
    repeticiones: dict[tuple, int] = {}
    pasos_sin_cambio = 0
    fallos_modelo = 0
    rechazos_juez: dict[str, int] = {}
    evidencias: list[Observacion] = []
    ultimo_click: tuple[int, int] | None = None  # campo enfocado por el paso anterior

    for paso in range(1, MAX_PASOS + 1):
        guardias.comprobar()  # a. plazo / cerrada
        restante = guardias.restante_s
        if restante < UMBRAL_FALLIDA_S:  # h. presupuesto
            return Resultado(estado="fallida", detalle=DETALLE_PLAZO)
        cerrar_pronto = restante < UMBRAL_CIERRE_S

        try:
            t0 = time.monotonic()
            accion = await _pedir_accion(
                cliente, plan, historial, obs_prev, obs_actual, cerrar_pronto, task_id
            )
            latencia_ms = int((time.monotonic() - t0) * 1000)
            fallos_modelo = 0
        except ModeloNoDisponible as exc:
            fallos_modelo += 1
            # el texto es la validación de la salida del modelo (nunca lleva la contraseña)
            log.warning("paso %d: modelo no disponible: %s", paso, str(exc)[:400])
            if fallos_modelo >= 2:
                return Resultado(estado="fallida", detalle="modelo no disponible")
            historial.append(f"paso {paso}: el modelo no respondió; reintento.")
            continue

        grabador.paso(
            png=obs_actual.png,
            accion=json.loads(accion.model_dump_json()),
            meta={"paso": paso, "latencia_ms": latencia_ms, "restante_s": round(restante, 1),
                  "hash": obs_actual.hash},
        )

        if accion.compuerta == "lectura" and all(e.hash != obs_actual.hash for e in evidencias):
            evidencias.append(obs_actual)
            evidencias = evidencias[-MAX_EVIDENCIAS:]

        # e. terminar -> verificación final obligatoria
        if accion.tipo == "terminar":
            propuesto = accion.resultado
            if propuesto is None:
                historial.append(
                    f"paso {paso}: RECHAZADO: `terminar` necesita `resultado` (estado y, en una "
                    "consulta, la `respuesta` leída). Repite terminar con el resultado completo."
                )
                continue
            if propuesto.estado == "fallida":
                return propuesto  # el ejecutor declara que no se pudo; no se "verifica" un fallo
            log.info("task_id=%s terminar propuesto; verificando", task_id)
            return await _verificar_final(
                cliente, plan, propuesto, escritorio, guardias, task_id, grabador,
                obs_terminar=obs_actual, evidencias=evidencias,
            )

        accion = _compuerta_inferida(accion, plan)

        # g. detección de bucle sobre pantalla sin cambios
        clave_rep = (_firma(accion), obs_actual.hash)
        repeticiones[clave_rep] = repeticiones.get(clave_rep, 0) + 1
        if repeticiones[clave_rep] >= MAX_REPETICION:
            historial.append(
                f"paso {paso}: repetiste la misma acción {accion.tipo} sobre una pantalla que no "
                "cambió; CAMBIA de estrategia (otro elemento, teclado u otra ruta)."
            )
            if repeticiones[clave_rep] > MAX_REPETICION:
                continue  # no la volvemos a ejecutar

        # b-d. compuertas de código
        permitido, motivo, terminal = _aplicar_compuertas(accion, plan, estado)
        if terminal is not None:
            log.info("task_id=%s compuerta terminal: %s", task_id, terminal.detalle)
            return terminal
        if not permitido:
            historial.append(f"paso {paso}: {motivo}")
            log.info("task_id=%s acción rechazada: %s", task_id, motivo)
            continue
        if motivo:  # nota informativa (p. ej. identidad confirmada)
            historial.append(f"paso {paso}: {motivo}")

        # d. hook del juez en acciones críticas
        if accion.compuerta in Criticas:
            veredicto = await _compuerta_juez(accion, plan, obs_actual.png, cliente)
            if not veredicto.aprobado:
                if accion.compuerta == "identidad":
                    # la compuerta de código ya la había marcado; el juez no la confirma
                    estado.identidad_confirmada = False
                    estado.puede_crear = False
                    # el juez devuelve en datos_clave la unión de lo que vio él y el ejecutor
                    ids = list(veredicto.datos_clave)
                    if len(ids) >= 2:
                        log.info("task_id=%s identidad ambigua según el juez: %s", task_id, ids)
                        return Resultado(
                            estado="fallida",
                            detalle=veredicto.razon or "identidad ambigua: el juez no la confirmó",
                            datos_clave=ids,
                        )
                    n = rechazos_juez.get("identidad", 0) + 1
                    rechazos_juez["identidad"] = n
                    if n >= MAX_DISPUTAS_IDENTIDAD:
                        return Resultado(
                            estado="fallida",
                            detalle=f"no se pudo confirmar el registro: {veredicto.razon}",
                            datos_clave=_identificadores(accion.coincidencias or []),
                        )
                if accion.compuerta != "identidad":
                    n = rechazos_juez.get(accion.compuerta, 0) + 1
                    rechazos_juez[accion.compuerta] = n
                    if n >= MAX_RECHAZOS_JUEZ:
                        # insistir solo quema el plazo; la acción irreversible no se ejecutó
                        return Resultado(
                            estado="fallida",
                            detalle=(
                                f"el juez rechazó {accion.compuerta} {n} veces: {veredicto.razon}"
                            ),
                            datos_clave=list(veredicto.datos_clave),
                        )
                historial.append(f"paso {paso}: el juez rechazó la acción: {veredicto.razon}")
                continue

        # un clic sin coordenadas o en (0,0) ("por error") caería en cualquier cosa: no se hace
        if accion.tipo in {"click", "doble_click", "scroll"} and (
            accion.x is None or accion.y is None or (accion.x, accion.y) == (0, 0)
        ):
            historial.append(
                f"paso {paso}: RECHAZADO: un {accion.tipo} necesita las coordenadas del objetivo."
            )
            continue

        if accion.tipo == "escribir" and (accion.x is None or accion.y is None) and ultimo_click:
            accion = accion.model_copy(update={"x": ultimo_click[0], "y": ultimo_click[1]})

        # ejecutar la primitiva (PlazoAgotado/TareaCerrada propagan al cierre del finally)
        await _ejecutar_primitiva(escritorio, accion)
        ultimo_click = (accion.x, accion.y) if accion.tipo == "click" else None
        _contabilizar(accion, plan, estado)

        # observar el efecto
        obs_prev = obs_actual
        obs_actual = await escritorio.capturar()

        linea = (
            f"paso {paso}: {accion.tipo} '{accion.elemento}' "
            f"(x={accion.x},y={accion.y}) razon={accion.razon!r} -> "
            f"cambio_pantalla={obs_actual.cambio}"
        )
        historial.append(linea)

        if accion.compuerta in {"guardar", "crear"}:
            # un clic en Guardar abrió OTRA ficha (visto en evaluación): no se sigue sobre ella
            registro = await _registro_juez(plan, obs_actual.png, cliente)
            grabador.paso(
                png=None,
                accion={"tipo": "registro_tras_guardar"},
                meta={"aprobado": registro.aprobado, "razon": registro.razon},
            )
            if not registro.aprobado:
                log.info("task_id=%s ALERTA tras %s: %s", task_id, accion.compuerta, registro.razon)
                historial.append(
                    f"paso {paso}: RECHAZADO/ALERTA: tras {accion.compuerta} {registro.razon}. "
                    "NO edites ni guardes nada sobre ese registro: vuelve al registro de la "
                    "identificación del plan y LÉELO (compuerta identidad) antes de cualquier otra "
                    f"cosa. No repitas {accion.compuerta} sin haberlo leído."
                )
                if accion.compuerta == "guardar":
                    # el guardado no consta sobre el registro correcto: solo se repite tras
                    # reconfirmar la identidad (y pasar otra vez por el juez de guardar)
                    estado.identidad_confirmada = False
                    estado.guardados_por_campo.discard(_campo_de(accion, plan))

        if obs_actual.cambio:
            pasos_sin_cambio = 0
        else:
            pasos_sin_cambio += 1
            historial.append(f"paso {paso}: la pantalla NO cambió tras la acción.")
            if pasos_sin_cambio >= MAX_SIN_CAMBIO:
                return Resultado(
                    estado="fallida",
                    detalle="sin avance: la pantalla no cambió tras varios intentos",
                )

    return Resultado(estado="fallida", detalle="se alcanzó el máximo de pasos sin terminar")


# ── entrada pública (contrato §2) ─────────────────────────────────────────────

async def _correr(
    task_id: str,
    entrada: TareaEntrada,
    deadline: float,
    *,
    cliente: ClienteModelos,
    conectar: Conectar,
) -> tuple[Resultado, Plan | None]:
    guardias = Guardias(task_id=task_id, deadline=deadline - MARGEN_S)
    grabador = Grabador(task_id)
    escritorio: Escritorio | None = None
    plan: Plan | None = None
    try:
        url = entrada.escritorio.url
        password = entrada.escritorio.password.get_secret_value()
        escritorio = await conectar(url, password, guardias)
        obs0 = await escritorio.capturar()
        plan = await planear(entrada.tarea, cliente, obs0.png, task_id=task_id)
        if plan.tipo == "imposible":
            resultado = Resultado(
                estado="fallida",
                detalle="la tarea no se puede o no se debe hacer",
            )
        else:
            resultado = await _bucle(task_id, plan, cliente, escritorio, guardias, grabador, obs0)
    except (PlazoAgotado, TareaCerrada):
        resultado = Resultado(estado="fallida", detalle=DETALLE_PLAZO)
    except PlanNoValido as exc:
        resultado = Resultado(estado="fallida", detalle=f"no se pudo planear: {exc.motivo}")
    except ModeloNoDisponible:
        resultado = Resultado(estado="fallida", detalle="modelo no disponible")
    except Exception as exc:  # noqa: BLE001 - solo la clase, nunca la traza (puede llevar secretos)
        log.warning("task_id=%s error interno: %s", task_id, type(exc).__name__)
        resultado = Resultado(estado="fallida", detalle=f"error interno: {type(exc).__name__}")
    finally:
        # f. cierre SIEMPRE: guardias.cerrar + soltar_entradas + cerrar (cerrar() hace las tres)
        if escritorio is not None:
            try:
                await escritorio.cerrar()
            except Exception as exc:  # noqa: BLE001
                log.warning("task_id=%s fallo al cerrar: %s", task_id, type(exc).__name__)
        else:
            guardias.cerrar()

    return resultado, plan


async def ejecutar_tarea(task_id: str, entrada: TareaEntrada, deadline: float) -> Resultado:
    """Punto de entrada que inyecta el Motor (rookie.roles.ejecutor.ejecutar_tarea).

    Devuelve (Resultado, Plan) para que el Motor arme el resumen con el plan (extensión aditiva
    del Ejecutor documentada en CAMBIOS-CONTRATO.md).
    """
    cliente = ClienteModelos()
    return await _correr(task_id, entrada, deadline, cliente=cliente, conectar=Escritorio.conectar)


__all__ = ["DETALLE_PLAZO", "ejecutar_tarea", "planear"]
