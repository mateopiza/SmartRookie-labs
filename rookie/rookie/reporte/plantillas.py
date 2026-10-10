"""Resumen por plantilla (M10). Ningún resumen sale directo de un modelo.

Reglas que la evaluación compara LITERAL (README "Cómo se evalúa"):
- Consulta terminada: primera línea `respuesta: <valor>`; si el formato es número, el número va
  antes que cualquier otro carácter; dos datos → `respuesta: <d1>; <d2>`.
- Fallida: nunca lleva línea `respuesta:`; nombra los datos clave.
- Ningún secreto (ni sus variantes url-encoded o base64) puede aparecer: filtrar_secretos se aplica
  siempre al final.
"""

from __future__ import annotations

import base64
import re
from urllib.parse import quote, quote_plus, urlsplit

from rookie.esquemas.mensajes import Plan, Resultado

OCULTO = "[oculto]"
_LARGO_MINIMO_SECRETO = 4  # un secreto de 1-3 caracteres taparía medio resumen
_NUMERO = re.compile(r"[-+]?\d+(?:[.,]\d+)*")
_SEPARADORES_DATOS = re.compile(r"\s*(?:;|\n|\|)\s*")
_ESPACIOS = re.compile(r"\s+")


# ── utilidades de texto ──────────────────────────────────────────────────────


def _linea(texto: str | None) -> str:
    """Una sola línea, espacios colapsados (un salto de línea partiría `respuesta:`)."""
    return _ESPACIOS.sub(" ", texto or "").strip()


def _sin_punto_final(texto: str) -> str:
    return _linea(texto).rstrip(" .")


_ETIQUETA_RESPUESTA = re.compile(r"respuesta\s*:", re.IGNORECASE)


def _neutralizar(texto: str) -> str:
    """Quita `respuesta:` de textos libres: solo la plantilla de consulta puede escribir esa línea."""
    return _ETIQUETA_RESPUESTA.sub("respuesta", texto)


def _numero_primero(valor: str) -> str:
    """Garantiza que el primer carácter del valor sea el primer número que contiene."""
    valor = _linea(valor)
    m = _NUMERO.search(valor)
    if not m:
        return valor
    if m.start() == 0:
        return valor
    numero = m.group(0)
    return f"{numero} ({valor})"


def _partes_respuesta(respuesta: str, plan: Plan | None) -> list[str]:
    n_datos = len(plan.pregunta.datos) if plan and plan.pregunta else 1
    if n_datos > 1:
        partes = [p for p in _SEPARADORES_DATOS.split(respuesta.strip()) if p.strip()]
        if partes:
            return [_linea(p) for p in partes]
    return [_linea(respuesta)]


def linea_respuesta(respuesta: str, plan: Plan | None) -> str:
    partes = _partes_respuesta(respuesta, plan)
    if plan and plan.pregunta and plan.pregunta.formato == "numero":
        partes = [_numero_primero(p) for p in partes]
    return "respuesta: " + "; ".join(partes)


def _describir_cambios(resultado: Resultado) -> str:
    return ", ".join(f"{c.entidad} {c.campo} = {c.valor}" for c in resultado.escrituras)


def _verificado(resultado: Resultado) -> str:
    return ", ".join(_linea(c.valor) for c in resultado.escrituras)


_VERBO = {
    "crear": "se creó",
    "actualizar": "se actualizó",
    "exportar": "se exportó",
}


# ── plantillas ───────────────────────────────────────────────────────────────


def _resumen_fallida(resultado: Resultado) -> str:
    detalle = _neutralizar(_sin_punto_final(resultado.detalle)) or "sin detalle"
    datos = [_neutralizar(_linea(d)) for d in resultado.datos_clave if _linea(d)]
    texto = f"No se pudo completar: {detalle}."
    if datos:
        texto += " Datos: " + ", ".join(datos)
    return texto


def _resumen_consulta(resultado: Resultado, plan: Plan | None) -> str:
    lineas = []
    if resultado.respuesta is not None and _linea(resultado.respuesta):
        lineas.append(linea_respuesta(resultado.respuesta, plan))
    detalle = _sin_punto_final(resultado.detalle)
    if not detalle:
        datos = ", ".join(plan.pregunta.datos) if plan and plan.pregunta else "el dato pedido"
        donde = f" en {plan.modulo}" if plan and plan.modulo else ""
        detalle = f"se leyó {datos}{donde}"
    lineas.append(f"Hecho: {_neutralizar(detalle)}.")
    return "\n".join(lineas)


def _resumen_cambio(resultado: Resultado, plan: Plan | None) -> str:
    detalle = _sin_punto_final(resultado.detalle)
    if not detalle:
        verbo = _VERBO.get(plan.tipo if plan else "", "se completó la tarea")
        cambios = _describir_cambios(resultado)
        detalle = f"{verbo} {cambios}".strip() if cambios else verbo
    if plan and plan.modulo and plan.modulo.lower() not in detalle.lower():
        detalle = f"{detalle} en {_linea(plan.modulo)}"
    texto = f"Hecho: {detalle}."
    verificado = _verificado(resultado)
    if verificado:
        texto += f" Verificado: {verificado}."
    return _neutralizar(texto)


def armar_resumen(resultado: Resultado, plan: Plan | None, secretos: list[str]) -> str:
    """Resumen final de una tarea, ya filtrado de secretos.

    Sin plan (el Motor solo recibe un Resultado), una tarea terminada con respuesta se trata como
    consulta y el valor se escribe tal cual.
    """
    if resultado.estado == "fallida":
        texto = _resumen_fallida(resultado)
    elif plan is not None and plan.tipo == "consultar":
        texto = _resumen_consulta(resultado, plan)
    elif plan is None and resultado.respuesta is not None and _linea(resultado.respuesta):
        texto = _resumen_consulta(resultado, None)
    else:
        texto = _resumen_cambio(resultado, plan)
    return filtrar_secretos(texto, secretos)


# ── filtro de secretos ───────────────────────────────────────────────────────


def _b64_variantes(s: str) -> set[str]:
    crudo = s.encode()
    out = set()
    for cod in (base64.b64encode(crudo).decode(), base64.urlsafe_b64encode(crudo).decode()):
        out.add(cod)
        out.add(cod.rstrip("="))
    return out


def _variantes(secreto: str) -> tuple[set[str], set[str]]:
    """(variantes exactas, variantes url-encoded que se comparan sin mayúsculas)."""
    exactas = {secreto}
    codificadas = {quote(secreto, safe=""), quote_plus(secreto, safe=""), quote(secreto)}
    exactas |= _b64_variantes(secreto)
    # Una URL de escritorio también se delata por su host.
    if "://" in secreto:
        partes = urlsplit(secreto)
        if partes.netloc:
            exactas.add(partes.netloc)
            exactas.add(partes.hostname or partes.netloc)
    codificadas -= exactas
    return exactas, codificadas


def filtrar_secretos(texto: str, secretos: list[str]) -> str:
    """Reemplaza cada secreto, y sus variantes url-encoded y base64, por "[oculto]"."""
    if not texto:
        return texto
    patrones: list[tuple[str, int]] = []
    for secreto in secretos:
        if not secreto or len(secreto) < _LARGO_MINIMO_SECRETO:
            continue
        exactas, codificadas = _variantes(secreto)
        patrones += [(v, 0) for v in exactas if len(v) >= _LARGO_MINIMO_SECRETO]
        patrones += [(v, re.IGNORECASE) for v in codificadas if len(v) >= _LARGO_MINIMO_SECRETO]
    # Los más largos primero: así una URL entera se tapa antes que solo su host.
    for variante, flags in sorted(patrones, key=lambda p: len(p[0]), reverse=True):
        texto = re.sub(re.escape(variante), OCULTO, texto, flags=flags)
    return texto


__all__ = ["OCULTO", "armar_resumen", "filtrar_secretos", "linea_respuesta"]
