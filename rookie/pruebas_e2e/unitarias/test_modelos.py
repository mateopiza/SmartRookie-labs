from __future__ import annotations

import base64

import httpx
import pytest
from pydantic import BaseModel

from rookie.modelos.cliente import ClienteModelos, ModeloNoDisponible


class Salida(BaseModel):
    titulo: str


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "modelos.yaml"
    path.write_text(
        """
ejecutor:
  modelo: anthropic/claude-sonnet-5.5
  timeout_s: 25
  max_tokens: 128
""".strip(),
        encoding="utf-8",
    )
    return path


@pytest.mark.asyncio
async def test_request_con_imagen_en_ultimo_user(monkeypatch, cfg):
    cliente = ClienteModelos(config_path=str(cfg))
    capturas: list[dict] = []

    async def fake_llamar(*, payload, timeout_s):
        capturas.append(payload)
        return (
            {
                "model": payload["model"],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                "choices": [{"message": {"content": '{"titulo":"ok"}'}}],
            },
            123,
        )

    monkeypatch.setattr(cliente, "_llamar_openrouter", fake_llamar)

    img = b"\x89PNG\r\n\x1a\n..."
    await cliente.estructurado(
        "ejecutor",
        system="sistema",
        mensajes=[
            {"role": "user", "content": "describe la pantalla"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "respuesta final"},
        ],
        esquema=Salida,
        imagenes=[img],
    )

    assert len(capturas) == 1
    msg_user = capturas[0]["messages"][-1]
    assert msg_user["role"] == "user"
    partes = msg_user["content"]
    assert any(p.get("type") == "image_url" for p in partes)
    imagen = [p for p in partes if p.get("type") == "image_url"][0]
    esperado = "data:image/png;base64," + base64.b64encode(img).decode("ascii")
    assert imagen["image_url"]["url"] == esperado


@pytest.mark.asyncio
async def test_parsea_json_con_fences(monkeypatch, cfg):
    cliente = ClienteModelos(config_path=str(cfg))

    async def fake_llamar(*, payload, timeout_s):
        return (
            {
                "model": payload["model"],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                "choices": [{"message": {"content": "```json\n{\"titulo\":\"hola\"}\n```"}}],
            },
            80,
        )

    monkeypatch.setattr(cliente, "_llamar_openrouter", fake_llamar)
    out = await cliente.estructurado(
        "ejecutor",
        system="s",
        mensajes=[{"role": "user", "content": "u"}],
        esquema=Salida,
    )
    assert out.titulo == "hola"


@pytest.mark.asyncio
async def test_reintenta_una_vez_json_invalido(monkeypatch, cfg):
    cliente = ClienteModelos(config_path=str(cfg))
    llamadas = {"n": 0}

    async def fake_llamar(*, payload, timeout_s):
        llamadas["n"] += 1
        if llamadas["n"] == 1:
            return (
                {
                    "model": payload["model"],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    "choices": [{"message": {"content": '{"otro":"valor"}'}}],
                },
                50,
            )
        return (
            {
                "model": payload["model"],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                "choices": [{"message": {"content": '{"titulo":"bien"}'}}],
            },
            60,
        )

    monkeypatch.setattr(cliente, "_llamar_openrouter", fake_llamar)
    out = await cliente.estructurado(
        "ejecutor",
        system="s",
        mensajes=[{"role": "user", "content": "u"}],
        esquema=Salida,
    )

    assert out.titulo == "bien"
    assert llamadas["n"] == 2


@pytest.mark.asyncio
async def test_timeout_reintento_y_modelo_no_disponible(monkeypatch, cfg):
    cliente = ClienteModelos(config_path=str(cfg))
    llamadas = {"n": 0}

    async def fake_post(*args, **kwargs):
        llamadas["n"] += 1
        raise httpx.TimeoutException("timeout")

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            return await fake_post(*args, **kwargs)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("rookie.modelos.cliente.asyncio.sleep", no_sleep)

    with pytest.raises(ModeloNoDisponible):
        await cliente.estructurado(
            "ejecutor",
            system="s",
            mensajes=[{"role": "user", "content": "u"}],
            esquema=Salida,
        )

    assert llamadas["n"] == 2
