"""Juez: compuertas críticas (M6/M7) y veredicto final (M9) con un modelo de OTRO proveedor (rol "juez").

El modelo solo LEE la captura y contesta una pregunta cerrada; la decisión la toma este código
comparando lo leído con lo que propuso el ejecutor (INFORME-estado §5).

Reglas de código:
- Cada lectura: timeout 30 s y 1 reintento. Si el modelo no responde, la política es por tipo:
  identidad → aprobado (manda el conteo del ejecutor); guardar / crear / exportar / final →
  rechazado con razón "juez no disponible" (ante la duda, fallida).
- `datos_clave` solo lleva valores leídos de la captura (por el juez o, en identidad, por el
  ejecutor); nunca valores del plan ni texto generado.
- Ninguna excepción sale de aquí: el ejecutor recibe siempre un Veredicto.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
import time
import unicodedata
from typing import TypeVar

from pydantic import BaseModel, ConfigDict

from rookie.esquemas.mensajes import Accion, Cambio, Plan, Resultado, Veredicto
from rookie.modelos.cliente import ClienteModelos

log = logging.getLogger("rookie.roles.juez")

ROL = "juez"
TIMEOUT_S = 30.0  # lecturas medidas de 12–15 s en pantallas cargadas; 20 s cortaba algunas
INTENTOS = 2  # 1 llamada + 1 reintento
NO_DISPONIBLE = "juez no disponible"

T = TypeVar("T", bound=BaseModel)

JUEZ_SYSTEM = (
    "Eres un verificador visual independiente. Recibes UNA captura de pantalla de una aplicación "
    "de escritorio y una pregunta cerrada sobre ella. Solo describes lo que SE VE en la captura: "
    "copia textos, códigos y valores exactamente como aparecen, sin corregirlos, completarlos, "
    "traducirlos ni deducirlos. Si un dato no se ve con claridad, márcalo como no visible y deja "
    "su valor vacío; nunca lo inventes. No propones acciones. Responde solo con el objeto JSON "
    "del esquema."
)


# ── esquemas de lectura (internos; todos los campos obligatorios para modo estricto) ──────

class _Estricto(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Fila(_Estricto):
    identificador: str  # el valor que distingue la fila (código, documento, id) tal como se ve
    texto: str  # la fila completa tal como se ve


class _LecturaIdentidad(_Estricto):
    cantidad: int
    filas: list[_Fila]


class _ValorLeido(_Estricto):
    clave: str  # copiada tal cual de la pregunta
    visible: bool
    valor: str  # tal como se ve; "" si no es visible


class _LecturaFormulario(_Estricto):
    valores: list[_ValorLeido]
    otros_campos_cambiados: bool
    razon: str


class _LecturaExistencia(_Estricto):
    existe: bool
    identificadores: list[str]
    razon: str


class _LecturaFiltros(_Estricto):
    valores: list[_ValorLeido]
    hay_otros_filtros: bool
    razon: str


class _LecturaConteo(_Estricto):
    filas_que_cumplen: int
    identificadores: list[str]  # el código o id de cada fila contada, tal como se ve
    razon: str


class _LecturaRegistro(_Estricto):
    muestra_un_registro: bool  # ficha, formulario o detalle de UN registro (no un listado)
    nombre: str
    identificador: str


class _LecturaFinal(_Estricto):
    valores: list[_ValorLeido]
    evidencia: bool
    razon: str


# ── normalización y comparación (código, no modelo) ───────────────────────────

def _norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", s).strip().lower()


def _compacto(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", _norm(s))


_FECHAS = (
    (re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$"), ("a", "m", "d")),
    (re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$"), ("d", "m", "a")),  # dd/mm/aaaa
)


def _fecha_iso(s: str | None) -> str | None:
    texto = _norm(s)
    for patron, orden in _FECHAS:
        m = patron.match(texto)
        if m:
            partes = dict(zip(orden, m.groups(), strict=True))
            return f"{int(partes['a']):04d}-{int(partes['m']):02d}-{int(partes['d']):02d}"
    return None


def _numero(s: str | None) -> float | None:
    """Primer número del texto; '.' o ',' seguidos de exactamente 3 dígitos son de miles."""
    m = re.search(r"-?\d[\d.,]*", s or "")
    if not m:
        return None
    crudo = m.group(0).rstrip(".,")
    if "." in crudo and "," in crudo:
        decimal = "." if crudo.rfind(".") > crudo.rfind(",") else ","
        miles = "," if decimal == "." else "."
        crudo = crudo.replace(miles, "").replace(decimal, ".")
    elif "." in crudo or "," in crudo:
        sep = "." if "." in crudo else ","
        grupos = crudo.split(sep)
        if len(grupos) > 2 or len(grupos[-1]) == 3:
            crudo = crudo.replace(sep, "")
        else:
            crudo = crudo.replace(sep, ".")
    try:
        return float(crudo)
    except ValueError:
        return None


# Una etiqueta de una o dos palabras SIN dígitos ("Consultorio", "Tel.", "Ext:") delante del valor.
_ETIQUETA = re.compile(r"[a-z]+(?: [a-z]+)?[:.#]?")


def _con_etiqueta(esperado: str | None, visto: str | None) -> bool:
    """'Consultorio 305' vale por '305': el esperado es la(s) palabra(s) completa(s) del final,
    precedido de una etiqueta separada por espacio. 'Carrera 15Calle 80…' (concatenado, sin
    espacio, o con un prefijo que lleva números) NO vale."""
    e, v = _norm(esperado), _norm(visto)
    if not e or not v.endswith(" " + e):
        return False
    etiqueta = v[: -len(e) - 1].strip()
    return bool(_ETIQUETA.fullmatch(etiqueta))


def _igual(esperado: str | None, visto: str | None, *, numerico: bool = False) -> bool:
    if not _compacto(visto):
        return False
    fe, fv = _fecha_iso(esperado), _fecha_iso(visto)
    if fe and fv:
        return fe == fv
    if numerico:
        ne, nv = _numero(esperado), _numero(visto)
        return ne is not None and nv is not None and abs(ne - nv) < 1e-9
    return _compacto(esperado) == _compacto(visto) or _con_etiqueta(esperado, visto)


def _texto_coincide(esperado: str | None, visto: str | None) -> bool:
    """Para respuestas de texto: igualdad o contención (la más corta con ≥ 3 caracteres)."""
    a, b = _compacto(esperado), _compacto(visto)
    if not a or not b:
        return False
    if a == b:
        return True
    corto, largo = (a, b) if len(a) <= len(b) else (b, a)
    return len(corto) >= 3 and corto in largo


def _parecido(esperado: str | None, visto: str | None) -> bool:
    """¿El valor visto es una versión estropeada del esperado (contiene o está contenido,
    o se le parece mucho)? Un valor sin relación no lo es."""
    a, b = _compacto(esperado), _compacto(visto)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.6


def _describir(d: dict[str, str]) -> str:
    return ", ".join(f"{k} = {v!r}" for k, v in d.items()) or "(sin identificación explícita)"


def _dedup(valores: list[str]) -> list[str]:
    vistos: set[str] = set()
    out: list[str] = []
    for v in valores:
        v = (v or "").strip()
        clave = _compacto(v)
        if clave and clave not in vistos:
            vistos.add(clave)
            out.append(v)
    return out


def _buscar(valores: list[_ValorLeido], clave: str, indice: int) -> _ValorLeido | None:
    """Empareja por clave normalizada; si el modelo la reescribió, por posición."""
    objetivo = _compacto(clave)
    for v in valores:
        if _compacto(v.clave) == objetivo:
            return v
    if indice < len(valores):
        return valores[indice]
    return None


# ── llamada al modelo con timeout y reintento ─────────────────────────────────

async def _leer(
    cliente: ClienteModelos, esquema: type[T], pregunta: str, png: bytes, etiqueta: str
) -> T | None:
    """Devuelve la lectura validada o None si el modelo no respondió tras el reintento."""
    for intento in range(1, INTENTOS + 1):
        inicio = time.perf_counter()
        try:
            lectura = await asyncio.wait_for(
                cliente.estructurado(
                    ROL,
                    system=JUEZ_SYSTEM,
                    mensajes=[{"role": "user", "content": pregunta}],
                    esquema=esquema,
                    imagenes=[png],
                    timeout_s=TIMEOUT_S,
                ),
                timeout=TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001 - cualquier fallo del modelo = no disponible
            log.warning(
                "juez %s intento %d/%d falló: %s", etiqueta, intento, INTENTOS, type(exc).__name__
            )
            continue
        log.info(
            "juez %s lectura en %.0f ms (intento %d)",
            etiqueta, (time.perf_counter() - inicio) * 1000, intento,
        )
        return lectura
    return None


def _no_disponible(aprobado: bool) -> Veredicto:
    return Veredicto(aprobado=aprobado, razon=NO_DISPONIBLE)


# ── compuertas ────────────────────────────────────────────────────────────────

def _id_de_coincidencia(fila: dict[str, str]) -> str:
    """El valor más identificador de una fila leída por el ejecutor (el que lleva dígitos)."""
    valores = [str(v).strip() for v in fila.values() if str(v).strip()]
    con_digitos = [v for v in valores if re.search(r"\d", v)]
    if con_digitos:
        return max(con_digitos, key=len)
    return " ".join(valores)


def _misma_fila(del_ejecutor: dict[str, str], del_juez: _Fila) -> bool:
    ident = _compacto(del_juez.identificador)
    texto = _compacto(f"{del_juez.texto} {del_juez.identificador}")
    valores = [_compacto(str(v)) for v in del_ejecutor.values()]
    valores = [v for v in valores if len(v) >= 3]
    if not valores:
        return False
    if ident and any(ident == v or (len(ident) >= 3 and ident in v) for v in valores):
        return True
    return all(v in texto for v in valores)


def _emparejan(ejecutor: list[dict[str, str]], juez: list[_Fila]) -> bool:
    """¿Hay una biyección fila a fila? (emparejamiento bipartito; n es pequeño)."""
    if len(ejecutor) != len(juez):
        return False
    asignada: dict[int, int] = {}  # índice ejecutor → índice juez

    def aumentar(j: int, visitadas: set[int]) -> bool:
        for e, fila in enumerate(ejecutor):
            if e in visitadas or not _misma_fila(fila, juez[j]):
                continue
            visitadas.add(e)
            if e not in asignada or aumentar(asignada[e], visitadas):
                asignada[e] = j
                return True
        return False

    return all(aumentar(j, set()) for j in range(len(juez)))


async def _identidad(accion: Accion, plan: Plan, png: bytes, cliente: ClienteModelos) -> Veredicto:
    if not any(_compacto(v) for v in plan.identificacion.values()):
        # sin identificación no hay con qué comparar: el juez listaría todo el listado y lo
        # tomaría por ambigüedad (visto: 16 facturas). Manda la compuerta de código.
        return Veredicto(aprobado=True, razon="sin identificación explícita: nada que comparar")
    pregunta = (
        f"Identificación buscada: {_describir(plan.identificacion)}.\n"
        "¿Cuántas filas o registros visibles en la pantalla coinciden con esa identificación? "
        "Una fila coincide si los datos de la identificación que SE VEN en ella son iguales; un "
        "dato de la identificación que la fila no muestra (p. ej. quién o cuándo la modificó) no "
        "la descarta; solo la descarta un dato visible distinto. Si la pantalla muestra la ficha "
        "o el detalle de un único registro, ese registro cuenta como una fila. "
        "Lista cada uno: en `identificador` el dato que lo distingue de los demás (código, "
        "documento, número o id) y en `texto` la fila completa, ambos tal como se ven. "
        "`cantidad` es el número de filas listadas; si no hay ninguna, 0 y lista vacía."
    )
    lectura = await _leer(cliente, _LecturaIdentidad, pregunta, png, "identidad")
    if lectura is None:
        return _no_disponible(True)

    del_ejecutor = accion.coincidencias or []
    ids_ejecutor = [_id_de_coincidencia(f) for f in del_ejecutor]
    # una fila que ambos leyeron cuenta una vez (con el id del ejecutor); el juez solo añade las nuevas
    ids_juez = [
        f.identificador.strip() or f.texto.strip()
        for f in lectura.filas
        if not any(_misma_fila(e, f) for e in del_ejecutor)
    ]

    criterio = next((v for k, v in plan.filtros.items() if "criterio" in _norm(k) and _norm(v)), None)
    if (
        criterio
        and del_ejecutor
        and lectura.filas
        and all(any(_misma_fila(e, f) for f in lectura.filas) for e in del_ejecutor)
    ):
        # con un criterio de elección, el ejecutor enumera candidatos (o la fila elegida) que el
        # juez también ve: no es ambigüedad (visto: 2 facturas anuladas, elegir la sin reemplazo)
        return Veredicto(
            aprobado=True,
            razon=(
                f"{len(del_ejecutor)} fila(s) del ejecutor entre {len(lectura.filas)} candidato(s) "
                "para aplicar el criterio de la tarea"
            ),
        )
    if lectura.cantidad != len(lectura.filas):
        razon = (
            f"lectura inconsistente del juez: dice {lectura.cantidad} filas y lista "
            f"{len(lectura.filas)}"
        )
    elif not _emparejan(del_ejecutor, lectura.filas):
        razon = (
            f"identidad en disputa: el ejecutor leyó {len(del_ejecutor)} coincidencia(s) y el "
            f"juez ve {len(lectura.filas)}"
        )
    else:
        return Veredicto(
            aprobado=True,
            razon=f"identidad confirmada: {len(lectura.filas)} coincidencia(s), las mismas",
        )
    # Solo hay ambigüedad (y datos para una fallida) si alguno de los dos ve 2 o más registros.
    # Un 1 contra 1 distinto o un 1 contra 0 es una disputa: el ejecutor vuelve a leer (en
    # evaluación, juntar los ids de un 1 contra 1 terminó tareas como «ambiguas» sin serlo).
    # (0 del ejecutor contra ≥1 del juez antes de un alta es un posible duplicado: se nombra)
    if not criterio and (
        max(len(lectura.filas), len(del_ejecutor)) >= 2 or (not del_ejecutor and lectura.filas)
    ):
        return Veredicto(aprobado=False, razon=razon, datos_clave=_dedup(ids_ejecutor + ids_juez))
    return Veredicto(aprobado=False, razon=razon)


async def _guardar(plan: Plan, png: bytes, cliente: ClienteModelos) -> Veredicto:
    cambios = plan.cambios_autorizados
    if not cambios:
        return Veredicto(aprobado=False, razon="el plan no tiene cambios autorizados que guardar")
    lista = "\n".join(f"- clave {c.campo!r}: valor esperado {c.valor!r}" for c in cambios)
    pregunta = (
        "Se va a guardar el formulario en pantalla. Cambios autorizados:\n"
        f"{lista}\n"
        "Para cada clave, en el mismo orden, copia la clave tal cual y escribe en `valor` lo que "
        "muestra ahora ese campo del formulario, tal como se ve. Pon `otros_campos_cambiados` "
        "en true SOLO si se ve un error de escritura en OTRO campo: alguno de los valores "
        "autorizados escrito en un campo que no es el suyo, o un campo con texto pegado o "
        "duplicado (dos valores concatenados). Con una sola captura no puedes saber qué tenían "
        "antes los demás campos: notas, observaciones u otros textos que ya traía el registro "
        "NO cuentan como cambio, aunque te parezcan extraños."
    )
    lectura = await _leer(cliente, _LecturaFormulario, pregunta, png, "guardar")
    if lectura is None:
        return _no_disponible(False)

    # Tareas de varios pasos: no todos los cambios autorizados están en el formulario que se
    # guarda ahora. Bloquea un valor PARECIDO pero distinto (errata, concatenado, a medias);
    # uno ajeno o no visible pertenece a otro paso. Hace falta al menos uno correcto a la vista.
    fallos: list[str] = []
    leidos: list[str] = []
    notas: list[str] = []
    correctos = 0
    for i, c in enumerate(cambios):
        v = _buscar(lectura.valores, c.campo, i)
        if v is None or not v.visible or not _compacto(v.valor):
            notas.append(f"{c.campo}: no visible")
            continue
        if _igual(c.valor, v.valor):
            correctos += 1
        elif _parecido(c.valor, v.valor):
            fallos.append(f"{c.campo}: en pantalla {v.valor!r}")
            leidos.append(f"{c.campo}: {v.valor}")
        else:
            notas.append(f"{c.campo}: en pantalla {v.valor!r} (otro paso)")
    if lectura.otros_campos_cambiados:
        fallos.append("otro campo cambió")
    if not correctos and not fallos:
        fallos.extend(notas or ["ningún valor autorizado visible"])
    if fallos:
        return Veredicto(
            aprobado=False, razon="formulario no coincide: " + "; ".join(fallos), datos_clave=leidos
        )
    return Veredicto(aprobado=True, razon="formulario con exactamente los valores autorizados")


async def _crear(plan: Plan, png: bytes, cliente: ClienteModelos) -> Veredicto:
    ident = plan.identificacion or {c.campo: c.valor for c in plan.cambios_autorizados}
    pregunta = (
        f"Registro que se va a crear: {_describir(ident)}.\n"
        "¿Ya hay en pantalla un registro EXISTENTE (en una lista, tabla o ficha ya guardada) con "
        "esa identificación? No cuentes los campos del formulario de alta que se está llenando. "
        "Si existe, lista en `identificadores` el código o dato distintivo de cada registro "
        "existente, tal como se ve."
    )
    lectura = await _leer(cliente, _LecturaExistencia, pregunta, png, "crear")
    if lectura is None:
        return _no_disponible(False)
    if lectura.existe or lectura.identificadores:
        return Veredicto(
            aprobado=False,
            razon="ya hay un registro visible con esa identificación (duplicado)",
            datos_clave=_dedup(lectura.identificadores),
        )
    return Veredicto(aprobado=True, razon="no se ve un registro existente con esa identificación")


async def _exportar(plan: Plan, png: bytes, cliente: ClienteModelos) -> Veredicto:
    filtros = plan.filtros
    if filtros:
        lista = "\n".join(f"- clave {k!r}: valor esperado {v!r}" for k, v in filtros.items())
        pregunta = (
            "Se va a exportar con estos filtros:\n"
            f"{lista}\n"
            "Para cada clave, en el mismo orden, copia la clave tal cual y escribe en `valor` el "
            "valor que muestra el filtro correspondiente en pantalla, tal como se ve (la clave "
            "puede llamarse distinto en pantalla: 'desde' = 'Fecha desde'). Pon "
            "`hay_otros_filtros` en true SOLO si un filtro que NO corresponde a ninguna clave de "
            "la lista tiene un valor escrito o elegido; vacío o 'Todos' no cuenta."
        )
    else:
        pregunta = (
            "Se va a exportar SIN filtros. Deja `valores` vacío e indica en `hay_otros_filtros` "
            "si hay algún filtro aplicado con valor no vacío."
        )
    lectura = await _leer(cliente, _LecturaFiltros, pregunta, png, "exportar")
    if lectura is None:
        return _no_disponible(False)

    fallos: list[str] = []
    leidos: list[str] = []
    for i, (k, esperado) in enumerate(filtros.items()):
        v = _buscar(lectura.valores, k, i)
        if v is None or not v.visible:
            fallos.append(f"{k}: no visible")
            continue
        if not _igual(esperado, v.valor):
            fallos.append(f"{k}: en pantalla {v.valor!r}")
            leidos.append(f"{k}: {v.valor}")
    if lectura.hay_otros_filtros:
        fallos.append("hay otros filtros aplicados")
    if fallos:
        return Veredicto(
            aprobado=False, razon="filtros no coinciden: " + "; ".join(fallos), datos_clave=leidos
        )
    return Veredicto(aprobado=True, razon="filtros visibles exactamente los del plan")


async def juzgar_compuerta(
    accion: Accion, plan: Plan, observacion_png: bytes, cliente: ClienteModelos
) -> Veredicto:
    inicio = time.perf_counter()
    try:
        if accion.compuerta == "identidad":
            veredicto = await _identidad(accion, plan, observacion_png, cliente)
        elif accion.compuerta == "guardar":
            veredicto = await _guardar(plan, observacion_png, cliente)
        elif accion.compuerta == "crear":
            veredicto = await _crear(plan, observacion_png, cliente)
        elif accion.compuerta == "exportar":
            veredicto = await _exportar(plan, observacion_png, cliente)
        elif accion.compuerta == "eliminar":
            veredicto = Veredicto(aprobado=False, razon="eliminar no se autoriza nunca")
        else:
            veredicto = Veredicto(aprobado=True, razon="acción sin compuerta crítica")
    except Exception as exc:  # noqa: BLE001 - defensa final; nunca propagar al ejecutor
        log.warning("juez compuerta %s error interno: %s", accion.compuerta, type(exc).__name__)
        veredicto = _no_disponible(accion.compuerta == "identidad")
    log.info(
        "juez compuerta=%s aprobado=%s ms=%.0f razon=%s",
        accion.compuerta, veredicto.aprobado, (time.perf_counter() - inicio) * 1000, veredicto.razon,
    )
    return veredicto


# ── texto leído de la pantalla (lista blanca ampliada) ────────────────────────

async def texto_visible(texto: str, png: bytes, cliente: ClienteModelos) -> Veredicto:
    """¿`texto` aparece tal cual en la pantalla como dato de la app (un código, una hora, un
    nombre en una fila)? Permite escribir datos leídos de la app en tareas de varios pasos."""
    pregunta = (
        f"Texto buscado: {texto!r}.\n¿Aparece ese dato en la pantalla como dato de la aplicación "
        "(en una fila, una ficha, una etiqueta o una opción), y NO solo dentro de un campo de "
        "entrada que se esté llenando? Una fecha cuenta aunque se vea en otro formato (p. ej. "
        "2026-10-14 por 14/10/2026). En `valores` pon una entrada con clave 'texto' y el dato tal "
        "como se ve (vacío y visible=false si no está); `evidencia` = true solo si aparece."
    )
    lectura = await _leer(cliente, _LecturaFinal, pregunta, png, "texto-visible")
    if lectura is None:
        return _no_disponible(False)
    v = lectura.valores[0] if lectura.valores else None
    mismo = v is not None and (_compacto(v.valor) == _compacto(texto) or (
        _fecha_iso(texto) is not None and _fecha_iso(texto) == _fecha_iso(v.valor)
    ))
    if lectura.evidencia and v is not None and v.visible and mismo:
        return Veredicto(aprobado=True, razon="el texto se ve en la pantalla")
    return Veredicto(aprobado=False, razon="el texto no se ve en la pantalla")


# ── registro en pantalla tras guardar / crear ─────────────────────────────────

_CAMPOS_QUE_IDENTIFICAN = ("nombre", "razon", "nit", "documento", "cedula", "codigo", "id")


def _claves_del_registro(plan: Plan) -> list[str]:
    if plan.identificacion:
        return [v for v in plan.identificacion.values() if _compacto(v)]
    return [
        c.valor for c in plan.cambios_autorizados
        if _compacto(c.valor) and any(p in _norm(c.campo) for p in _CAMPOS_QUE_IDENTIFICAN)
    ]


async def registro_tras_guardar(plan: Plan, png: bytes, cliente: ClienteModelos) -> Veredicto:
    """Tras guardar/crear, ¿la pantalla muestra OTRO registro? (visto: un clic en Guardar abrió
    otra ficha). aprobado=False solo si se ve con claridad UN registro cuyo nombre y código no
    contienen ninguna clave del plan. Sin juez o sin registro concreto en pantalla: aprobado."""
    claves = [_compacto(c) for c in _claves_del_registro(plan)]
    claves = [c for c in claves if len(c) >= 3]
    if not claves:
        return Veredicto(aprobado=True, razon="sin claves para comparar el registro")
    pregunta = (
        "¿La pantalla muestra la ficha, el formulario o el detalle de UN registro concreto (no un "
        "listado con varias filas)? Si sí, `muestra_un_registro`=true y copia su nombre y su "
        "código o identificador tal como se ven; si no, false y ambos vacíos."
    )
    lectura = await _leer(cliente, _LecturaRegistro, pregunta, png, "registro")
    if lectura is None:
        return _no_disponible(True)
    visto = _compacto(f"{lectura.nombre} {lectura.identificador}")
    if not lectura.muestra_un_registro or not visto:
        return Veredicto(aprobado=True, razon="no se ve un registro concreto")
    if any(c in visto for c in claves):
        return Veredicto(aprobado=True, razon="la pantalla muestra el registro de la tarea")
    otro = " ".join(v for v in (lectura.nombre.strip(), lectura.identificador.strip()) if v)
    return Veredicto(
        aprobado=False,
        razon=f"la pantalla muestra otro registro: {otro}",
        datos_clave=_dedup([lectura.identificador, lectura.nombre]),
    )


# ── veredicto final (M9) ──────────────────────────────────────────────────────

def _fuera_de_lista_blanca(escrituras: list[Cambio], plan: Plan) -> list[Cambio]:
    autorizados = [c.valor for c in plan.cambios_autorizados] + list(plan.textos_permitidos)
    return [e for e in escrituras if not any(_igual(a, e.valor) for a in autorizados)]


async def _final_consulta(resultado: Resultado, plan: Plan, png: bytes, cliente: ClienteModelos) -> Veredicto:
    if not (resultado.respuesta or "").strip():
        return Veredicto(aprobado=False, razon="consulta sin respuesta")
    numerico = plan.pregunta is not None and plan.pregunta.formato == "numero"
    datos = list(plan.pregunta.datos) if plan.pregunta and plan.pregunta.datos else ["respuesta"]
    contexto = {"modulo": plan.modulo, "identificacion": plan.identificacion, "filtros": plan.filtros}
    pregunta = (
        f"Contexto de la consulta: {json.dumps(contexto, ensure_ascii=False)}.\n"
        "Lee en la pantalla el valor de cada uno de estos datos, en el mismo orden, copiando la "
        "clave tal cual y el valor tal como se ve:\n"
        + "\n".join(f"- {d}" for d in datos)
        + "\nIndica en `evidencia` si la pantalla muestra claramente esos datos."
    )
    lectura = await _leer(cliente, _LecturaFinal, pregunta, png, "final-consulta")
    if lectura is None:
        return _no_disponible(False)

    propuestas = [p.strip() for p in (resultado.respuesta or "").split(";") if p.strip()]
    leidos: list[str] = []
    fallos: list[str] = []
    for i, d in enumerate(datos):
        v = _buscar(lectura.valores, d, i)
        if v is None or not v.visible or not _compacto(v.valor):
            fallos.append(f"{d}: no visible")
            continue
        leidos.append(v.valor.strip())
        propuesta = propuestas[i] if len(propuestas) == len(datos) else (resultado.respuesta or "")
        if numerico and i == 0:
            ok = _igual(propuesta, v.valor, numerico=True)
        else:
            ok = _texto_coincide(propuesta, v.valor) or _igual(propuesta, v.valor, numerico=True)
        if not ok:
            fallos.append(f"{d}: en pantalla {v.valor!r}")
    if fallos:
        return Veredicto(
            aprobado=False,
            razon="la respuesta no coincide con la pantalla: " + "; ".join(fallos),
            datos_clave=_dedup(leidos),
        )
    return Veredicto(aprobado=True, razon="respuesta releída en pantalla", datos_clave=[])


async def _final_escritura(resultado: Resultado, plan: Plan, png: bytes, cliente: ClienteModelos) -> Veredicto:
    fuera = _fuera_de_lista_blanca(resultado.escrituras, plan)
    if fuera:
        return Veredicto(
            aprobado=False,
            razon="escrituras fuera de lo autorizado: "
            + "; ".join(f"{e.campo}={e.valor!r}" for e in fuera),
        )
    esperadas = resultado.escrituras or plan.cambios_autorizados
    if esperadas:
        lista = "\n".join(f"- clave {c.campo!r}: valor esperado {c.valor!r}" for c in esperadas)
        pregunta = (
            f"Se acaba de {'crear' if plan.tipo == 'crear' else 'actualizar'} un registro de "
            f"{plan.modulo!r} ({_describir(plan.identificacion)}). Valores que deberían haber "
            f"quedado guardados:\n{lista}\n"
            "Para cada clave, en el mismo orden, copia la clave tal cual y escribe en `valor` lo "
            "que muestra la pantalla para ese registro, tal como se ve. Indica en `evidencia` si "
            "la pantalla muestra el registro guardado (no un formulario a medio llenar ni un error)."
        )
    else:
        pregunta = (
            f"Se acaba de {'crear' if plan.tipo == 'crear' else 'actualizar'} un registro de "
            f"{plan.modulo!r} ({_describir(plan.identificacion)}). Deja `valores` vacío e indica "
            "en `evidencia` si la pantalla muestra ese registro guardado (no un error)."
        )
    lectura = await _leer(cliente, _LecturaFinal, pregunta, png, "final-escritura")
    if lectura is None:
        return _no_disponible(False)

    # Un campo que la pantalla final no muestra no tumba el resultado (visto en evaluación: alta
    # verificada rechazada solo por «nombre: no visible»). Sí lo tumban: un valor visible
    # distinto, la falta de evidencia o que se vean correctos menos de la mitad de los esperados.
    fallos: list[str] = []
    leidos: list[str] = []
    no_visibles: list[str] = []
    correctos = 0
    for i, c in enumerate(esperadas):
        v = _buscar(lectura.valores, c.campo, i)
        if v is None or not v.visible or not _compacto(v.valor):
            no_visibles.append(f"{c.campo}: no visible")
        elif not _igual(c.valor, v.valor):
            fallos.append(f"{c.campo}: en pantalla {v.valor!r}")
            leidos.append(f"{c.campo}: {v.valor}")
        else:
            correctos += 1
    if esperadas and correctos * 2 < len(esperadas):
        fallos.extend(no_visibles)
    if not lectura.evidencia:
        fallos.append("la pantalla no muestra el registro guardado")
    if fallos:
        return Veredicto(
            aprobado=False, razon="sin evidencia del resultado: " + "; ".join(fallos), datos_clave=leidos
        )
    return Veredicto(aprobado=True, razon="valores guardados visibles en pantalla")


async def _final_exportacion(plan: Plan, png: bytes, cliente: ClienteModelos) -> Veredicto:
    filtros = plan.filtros
    lista = "\n".join(f"- clave {k!r}: valor esperado {v!r}" for k, v in filtros.items())
    pregunta = (
        "Se acaba de exportar un reporte. Indica en `evidencia` si la pantalla muestra que la "
        "exportación se completó (mensaje de éxito, archivo generado o listado de exportaciones "
        "con la nueva). "
        + (
            "Para cada filtro, en el mismo orden, copia la clave tal cual y el valor visible si se "
            f"ve en pantalla (si no se ve, visible=false):\n{lista}"
            if filtros
            else "Deja `valores` vacío."
        )
    )
    lectura = await _leer(cliente, _LecturaFinal, pregunta, png, "final-exportar")
    if lectura is None:
        return _no_disponible(False)

    fallos: list[str] = []
    leidos: list[str] = []
    for i, (k, esperado) in enumerate(filtros.items()):
        v = _buscar(lectura.valores, k, i)
        if v is not None and v.visible and _compacto(v.valor) and not _igual(esperado, v.valor):
            fallos.append(f"{k}: en pantalla {v.valor!r}")
            leidos.append(f"{k}: {v.valor}")
    if not lectura.evidencia:
        fallos.append("no se ve la exportación completada")
    if fallos:
        return Veredicto(
            aprobado=False, razon="sin evidencia de la exportación: " + "; ".join(fallos), datos_clave=leidos
        )
    return Veredicto(aprobado=True, razon="exportación visible con los filtros del plan")


async def verificar_conteo(
    resultado: Resultado, plan: Plan, pngs: list[bytes], cliente: ClienteModelos
) -> Veredicto:
    """Consulta numérica repartida en varias capturas (páginas de un listado): el juez cuenta en
    cada captura por separado y el CÓDIGO suma los identificadores únicos (una página capturada
    dos veces no cuenta doble)."""
    esperado = _numero(resultado.respuesta)
    if esperado is None:
        return Veredicto(aprobado=False, razon="consulta sin número que verificar")
    datos = ", ".join(plan.pregunta.datos) if plan.pregunta and plan.pregunta.datos else "filas"
    pregunta = (
        f"Esta captura es UNA página de un listado. Se cuentan: {datos}.\n"
        f"Registro: {_describir(plan.identificacion)}. Filtros: {_describir(plan.filtros)}.\n"
        "Cuenta las filas VISIBLES en esta captura que cumplen TODO: el registro es EXACTAMENTE "
        "el indicado (no cuentes nombres parecidos o más largos que lo contienen) y cumple lo que "
        "se cuenta (estado, fechas). Si un dato del registro o de los filtros NO aparece como "
        "columna (el listado ya viene filtrado por él), no descartes la fila por eso; solo la "
        "descarta un dato visible distinto. En `identificadores` copia el código o id de cada fila "
        "contada; `filas_que_cumplen` es cuántas listaste. Si la captura no es un listado, 0."
    )
    inicio = time.perf_counter()
    lecturas = await asyncio.gather(
        *(_leer(cliente, _LecturaConteo, pregunta, png, f"conteo-{i + 1}") for i, png in enumerate(pngs))
    )
    if any(lec is None for lec in lecturas):
        return _no_disponible(False)
    unicos: dict[str, None] = {}
    for lec in lecturas:
        for ident in lec.identificadores:
            if _compacto(ident):
                unicos.setdefault(_compacto(ident), None)
    total = len(unicos)
    por_pagina = [lec.filas_que_cumplen for lec in lecturas]
    log.info("juez conteo paginas=%s total_unicos=%d esperado=%s ms=%.0f",
             por_pagina, total, esperado, (time.perf_counter() - inicio) * 1000)
    if total == 0 and esperado > 0:
        # ninguna fila contada en ninguna página: el dato no sale de contar filas (un contador o
        # total en pantalla) o el listado no muestra las columnas; se lee el valor directamente
        # (visto en evaluación: respuesta 47 confirmada por el verificador y el juez contó 0,0,0)
        log.info("juez conteo: 0 filas en todas las páginas; se lee el valor en la última captura")
        return await _final_consulta(resultado, plan, pngs[-1], cliente)
    if abs(total - esperado) > 1e-9:
        return Veredicto(
            aprobado=False,
            razon=f"el conteo del juez por páginas es {total} ({por_pagina}), no {resultado.respuesta}",
        )
    return Veredicto(aprobado=True, razon=f"conteo por páginas confirmado: {total}")


async def veredicto_final(
    resultado: Resultado, plan: Plan, observacion_png: bytes, cliente: ClienteModelos
) -> Veredicto:
    """¿La pantalla muestra evidencia del resultado propuesto? aprobado=False ⇒ el ejecutor
    lo convierte en fallida. Un resultado ya fallido no se discute (no hay éxito que certificar)."""
    inicio = time.perf_counter()
    try:
        if resultado.estado == "fallida":
            veredicto = Veredicto(aprobado=True, razon="resultado fallido: no hay éxito que certificar")
        elif plan.tipo == "imposible":
            veredicto = Veredicto(aprobado=False, razon="el plan es imposible: no puede terminar con éxito")
        elif plan.tipo == "consultar":
            veredicto = await _final_consulta(resultado, plan, observacion_png, cliente)
        elif plan.tipo == "exportar":
            veredicto = await _final_exportacion(plan, observacion_png, cliente)
        else:
            veredicto = await _final_escritura(resultado, plan, observacion_png, cliente)
    except Exception as exc:  # noqa: BLE001 - defensa final; nunca propagar al ejecutor
        log.warning("juez final error interno: %s", type(exc).__name__)
        veredicto = _no_disponible(False)
    log.info(
        "juez final tipo=%s aprobado=%s ms=%.0f razon=%s",
        plan.tipo, veredicto.aprobado, (time.perf_counter() - inicio) * 1000, veredicto.razon,
    )
    return veredicto


__all__ = [
    "NO_DISPONIBLE",
    "TIMEOUT_S",
    "juzgar_compuerta",
    "registro_tras_guardar",
    "texto_visible",
    "veredicto_final",
    "verificar_conteo",
]
