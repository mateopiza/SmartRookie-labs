from __future__ import annotations

import asyncio
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

from rookie.modelos import ClienteModelos, ModeloNoDisponible


class Prueba(BaseModel):
    titulo: str
    fecha_hoy: str
    menu: list[str]
    x_comercial: int
    y_comercial: int


PREGUNTA = (
    "¿Qué aplicación es, qué fecha muestra, qué ítems tiene el menú superior "
    "y en qué píxel (x,y) está el centro de la palabra 'Comercial'?"
)


async def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    load_dotenv(repo_root / ".env", override=False)

    img_path = repo_root / "pruebas_e2e" / "fixtures" / "cap1.png"
    if not img_path.is_file():
        alt = Path("/tmp/cap1.png")
        if alt.is_file():
            img_path = alt
        else:
            raise SystemExit("No existe cap1.png ni en fixtures ni en /tmp")

    imagen = img_path.read_bytes()
    cliente = ClienteModelos(config_path=str(repo_root / "config" / "modelos.yaml"))

    roles = ["orquestador", "ejecutor", "juez", "vision_respaldo"]
    ref_x, ref_y = 123, 48

    print(f"Imagen: {img_path}")
    print("Referencia Comercial esperada: (123, 48)")

    for rol in roles:
        try:
            out = await cliente.estructurado(
                rol,
                system="Responde solo JSON válido según el esquema.",
                mensajes=[{"role": "user", "content": PREGUNTA}],
                esquema=Prueba,
                imagenes=[imagen],
                task_id=f"sonda-{rol}",
            )
        except ModeloNoDisponible as exc:
            print(f"\n[{rol}] ERROR: {exc}")
            continue

        met = cliente.ultima_llamada or {}
        dx = out.x_comercial - ref_x
        dy = out.y_comercial - ref_y
        err = (dx * dx + dy * dy) ** 0.5

        print(f"\n[{rol}] modelo={met.get('modelo')}")
        print(f"latencia_ms={met.get('latencia_ms')} tokens={met.get('total_tokens')}")
        print(f"respuesta={out.model_dump()}")
        print(f"error_px={err:.2f} (dx={dx}, dy={dy})")


if __name__ == "__main__":
    asyncio.run(main())
