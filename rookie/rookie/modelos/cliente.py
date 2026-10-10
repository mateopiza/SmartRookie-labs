"""Cliente de modelos vía OpenRouter (chat/completions OpenAI-compatible)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, TypeVar

import httpx
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

_log = logging.getLogger("rookie.modelos")


class ModeloNoDisponible(Exception):
    """No se pudo obtener una respuesta usable del modelo."""


class _Respuesta400(Exception):
    """El modelo rechazó la forma del request (útil para fallback de response_format)."""


class ClienteModelos:
    def __init__(self, config_path: str = "config/modelos.yaml") -> None:
        env_path = Path(__file__).resolve().parents[2] / ".env"
        if env_path.is_file():
            load_dotenv(env_path, override=False)

        self._api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not self._api_key:
            raise ModeloNoDisponible("OPENROUTER_API_KEY no configurada")

        self._api_base = "https://openrouter.ai/api/v1"
        self._config_path = self._resolver_config_path(config_path)
        self._roles = self._cargar_roles(self._config_path)
        self._stats_por_task: dict[str, dict[str, float | int]] = {}
        self._ultima_llamada: dict[str, Any] | None = None

    @property
    def ultima_llamada(self) -> dict[str, Any] | None:
        return self._ultima_llamada

    def estadisticas_task(self, task_id: str) -> dict[str, float | int] | None:
        return self._stats_por_task.get(task_id)

    async def estructurado(
        self,
        rol: str,
        *,
        system: str,
        mensajes: list[dict],
        esquema: type[T],
        imagenes: list[bytes] = [],
        timeout_s: float | None = None,
        task_id: str | None = None,
    ) -> T:
        rol_cfg = self._roles.get(rol)
        if not rol_cfg:
            raise ModeloNoDisponible(f"rol desconocido: {rol}")

        modelo = str(rol_cfg["modelo"])
        timeout_efectivo = float(timeout_s if timeout_s is not None else rol_cfg.get("timeout_s", 25))
        max_tokens = int(rol_cfg.get("max_tokens", 800))

        mensajes_base = self._preparar_mensajes(system=system, mensajes=mensajes, imagenes=imagenes)
        schema_dict = esquema.model_json_schema()
        parse_error: str | None = None

        for intento_parse in range(2):
            mensajes_llamada = [dict(m) for m in mensajes_base]
            if parse_error:
                mensajes_llamada.append(
                    {
                        "role": "user",
                        "content": (
                            "Tu respuesta anterior no validó contra el esquema. "
                            "Devuelve SOLO un objeto JSON válido, sin texto extra. "
                            f"Error: {parse_error}"
                        ),
                    }
                )

            contenido, metrica = await self._pedir_json(
                modelo=modelo,
                mensajes=mensajes_llamada,
                schema_dict=schema_dict,
                schema_name=self._nombre_esquema(esquema),
                timeout_s=timeout_efectivo,
                max_tokens=max_tokens,
            )
            self._registrar_metricas(task_id=task_id, rol=rol, metrica=metrica)

            json_txt = self._extraer_json(contenido)
            if not json_txt:
                parse_error = "No encontré un objeto JSON en la salida del modelo."
                if intento_parse == 0:
                    continue
                raise ModeloNoDisponible(parse_error)

            try:
                resultado = esquema.model_validate_json(json_txt)
            except ValidationError as exc:
                parse_error = str(exc)
                if intento_parse == 0:
                    continue
                raise ModeloNoDisponible(f"JSON inválido para {rol}: {exc}") from exc

            return resultado

        raise ModeloNoDisponible(f"No se obtuvo JSON válido para el rol {rol}")

    async def _pedir_json(
        self,
        *,
        modelo: str,
        mensajes: list[dict[str, Any]],
        schema_dict: dict[str, Any],
        schema_name: str,
        timeout_s: float,
        max_tokens: int,
    ) -> tuple[str, dict[str, Any]]:
        response_format_schema = {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "schema": schema_dict,
                "strict": True,
            },
        }

        payload = self._armar_payload(
            modelo=modelo,
            mensajes=mensajes,
            response_format=response_format_schema,
            max_tokens=max_tokens,
        )

        try:
            data, latencia_ms = await self._llamar_openrouter(payload=payload, timeout_s=timeout_s)
        except _Respuesta400:
            system_fallback = self._con_schema_en_system(
                self._system_de_mensajes(mensajes),
                schema_dict,
            )
            mensajes_fallback = [dict(m) for m in mensajes]
            if mensajes_fallback and mensajes_fallback[0].get("role") == "system":
                mensajes_fallback[0] = {"role": "system", "content": system_fallback}
            else:
                mensajes_fallback.insert(0, {"role": "system", "content": system_fallback})

            payload = self._armar_payload(
                modelo=modelo,
                mensajes=mensajes_fallback,
                response_format={"type": "json_object"},
                max_tokens=max_tokens,
            )
            data, latencia_ms = await self._llamar_openrouter(payload=payload, timeout_s=timeout_s)

        contenido = self._contenido_texto(data)
        usage = data.get("usage") or {}
        modelo_resp = data.get("model") or modelo
        metrica = {
            "modelo": modelo_resp,
            "latencia_ms": latencia_ms,
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        }
        return contenido, metrica

    async def _llamar_openrouter(self, *, payload: dict[str, Any], timeout_s: float) -> tuple[dict[str, Any], int]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        ultimo_error: Exception | None = None
        for intento in range(2):
            inicio = time.perf_counter()
            try:
                async with httpx.AsyncClient(base_url=self._api_base, headers=headers) as client:
                    resp = await client.post("/chat/completions", json=payload, timeout=timeout_s)
                latencia_ms = int((time.perf_counter() - inicio) * 1000)

                if resp.status_code == 400:
                    raise _Respuesta400(resp.text)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"status reintentable: {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )

                resp.raise_for_status()
                return resp.json(), latencia_ms
            except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError, httpx.HTTPStatusError) as exc:
                ultimo_error = exc
                reintentable = False
                if isinstance(exc, httpx.TimeoutException | httpx.ConnectError | httpx.ReadError):
                    reintentable = True
                elif isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
                    reintentable = exc.response.status_code in {429} or exc.response.status_code >= 500

                if reintentable and intento == 0:
                    await asyncio.sleep(1)
                    continue
                raise ModeloNoDisponible(f"OpenRouter no disponible: {exc}") from exc
            except _Respuesta400:
                raise

        raise ModeloNoDisponible(f"OpenRouter no disponible: {ultimo_error}")

    def _armar_payload(
        self,
        *,
        modelo: str,
        mensajes: list[dict[str, Any]],
        response_format: dict[str, Any],
        max_tokens: int,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": modelo,
            "messages": mensajes,
            "max_tokens": max_tokens,
            "response_format": response_format,
            "temperature": 0,
        }

        extra = self._parametros_baja_latencia(modelo)
        payload.update(extra)
        return payload

    def _parametros_baja_latencia(self, modelo: str) -> dict[str, Any]:
        # OpenAI recientes en OpenRouter exponen `reasoning.effort`.
        if modelo.startswith("openai/"):
            return {"reasoning": {"effort": "low"}}
        return {}

    def _registrar_metricas(self, *, task_id: str | None, rol: str, metrica: dict[str, Any]) -> None:
        self._ultima_llamada = {"rol": rol, **metrica}
        _log.info(
            "model_call rol=%s modelo=%s latencia_ms=%s tokens_total=%s",
            rol,
            metrica.get("modelo"),
            metrica.get("latencia_ms"),
            metrica.get("total_tokens"),
        )

        if not task_id:
            return

        acumulado = self._stats_por_task.setdefault(
            task_id,
            {
                "llamadas": 0,
                "latencia_ms_total": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        )
        acumulado["llamadas"] = int(acumulado["llamadas"]) + 1
        acumulado["latencia_ms_total"] = int(acumulado["latencia_ms_total"]) + int(metrica.get("latencia_ms", 0))
        acumulado["prompt_tokens"] = int(acumulado["prompt_tokens"]) + int(metrica.get("prompt_tokens", 0))
        acumulado["completion_tokens"] = int(acumulado["completion_tokens"]) + int(metrica.get("completion_tokens", 0))
        acumulado["total_tokens"] = int(acumulado["total_tokens"]) + int(metrica.get("total_tokens", 0))

    def _preparar_mensajes(self, *, system: str, mensajes: list[dict], imagenes: list[bytes]) -> list[dict[str, Any]]:
        salida = [{"role": "system", "content": system}] + [dict(m) for m in mensajes]

        if not imagenes:
            return salida

        idx_user = -1
        for idx in range(len(salida) - 1, -1, -1):
            if salida[idx].get("role") == "user":
                idx_user = idx
                break

        if idx_user < 0:
            salida.append({"role": "user", "content": ""})
            idx_user = len(salida) - 1

        contenido = salida[idx_user].get("content", "")
        partes: list[dict[str, Any]] = []

        if isinstance(contenido, str):
            texto = contenido.strip()
            if texto:
                partes.append({"type": "text", "text": texto})
        elif isinstance(contenido, list):
            for item in contenido:
                if isinstance(item, dict):
                    partes.append(dict(item))

        for img in imagenes:
            b64 = base64.b64encode(img).decode("ascii")
            partes.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})

        salida[idx_user]["content"] = partes
        return salida

    @staticmethod
    def _con_schema_en_system(system: str, schema_dict: dict[str, Any]) -> str:
        schema_txt = json.dumps(schema_dict, ensure_ascii=False)
        return (
            f"{system}\n\n"
            "IMPORTANTE: responde SOLO con un objeto JSON válido, sin markdown, sin explicaciones.\n"
            "Debe cumplir exactamente este JSON Schema:\n"
            f"{schema_txt}"
        )

    @staticmethod
    def _nombre_esquema(esquema: type[BaseModel]) -> str:
        nombre = re.sub(r"[^a-zA-Z0-9_-]", "_", esquema.__name__).lower()
        return nombre or "salida"

    @staticmethod
    def _system_de_mensajes(mensajes: list[dict[str, Any]]) -> str:
        for m in mensajes:
            if m.get("role") == "system":
                c = m.get("content")
                if isinstance(c, str):
                    return c
        return ""

    @staticmethod
    def _contenido_texto(data: dict[str, Any]) -> str:
        choices = data.get("choices") or []
        if not choices:
            return ""

        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            return content

        if isinstance(content, list):
            trozos: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    texto = item.get("text")
                    if isinstance(texto, str):
                        trozos.append(texto)
            return "\n".join(trozos)

        return ""

    def _extraer_json(self, texto: str) -> str | None:
        if not texto:
            return None

        fence = re.search(r"```json\s*(.*?)\s*```", texto, flags=re.IGNORECASE | re.DOTALL)
        if fence:
            extraido = self._primer_objeto_json(fence.group(1))
            if extraido:
                return extraido

        return self._primer_objeto_json(texto)

    @staticmethod
    def _primer_objeto_json(texto: str) -> str | None:
        inicio = texto.find("{")
        if inicio < 0:
            return None

        en_cadena = False
        escape = False
        nivel = 0
        for i in range(inicio, len(texto)):
            ch = texto[i]
            if en_cadena:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    en_cadena = False
                continue

            if ch == '"':
                en_cadena = True
                continue
            if ch == "{":
                nivel += 1
            elif ch == "}":
                nivel -= 1
                if nivel == 0:
                    return texto[inicio : i + 1]

        return None

    @staticmethod
    def _resolver_config_path(config_path: str) -> Path:
        candidato = Path(config_path)
        if candidato.is_file():
            return candidato
        base_repo = Path(__file__).resolve().parents[2]
        alterno = base_repo / config_path
        if alterno.is_file():
            return alterno
        raise ModeloNoDisponible(f"No existe el config de modelos: {config_path}")

    @staticmethod
    def _cargar_roles(config_path: Path) -> dict[str, dict[str, Any]]:
        with config_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ModeloNoDisponible("modelos.yaml inválido")
        return {k: v for k, v in data.items() if isinstance(v, dict)}
