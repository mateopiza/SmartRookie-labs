"""Genera las capturas sintéticas 1280×800 del juez (identidad, guardar, exportar).

Uso: python pruebas_e2e/fixtures/juez/generar.py  (desde rookie/). Sobrescribe los PNG.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

AQUI = Path(__file__).resolve().parent
FUENTE = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FUENTE_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _f(tam: int, negrita: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FUENTE_B if negrita else FUENTE, tam)


def _base(titulo: str) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (1280, 800), (216, 222, 232))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 1280, 34], fill=(32, 56, 96))
    d.text((12, 9), "Gestión Comercial · Empresa Demo   v1.2", font=_f(13, True), fill="white")
    d.text((1060, 9), "Hoy: 14/10/2026", font=_f(13), fill="white")
    d.rectangle([0, 34, 1280, 62], fill=(232, 236, 242))
    for x, t in ((18, "Archivo"), (90, "Clientes"), (170, "Ventas"), (240, "Reportes"), (320, "Ayuda")):
        d.text((x, 41), t, font=_f(13), fill=(20, 20, 20))
    d.text((14, 74), titulo, font=_f(16, True), fill=(20, 30, 50))
    return img, d


def _campo(d: ImageDraw.ImageDraw, x: int, y: int, etiqueta: str, valor: str, ancho: int = 300) -> None:
    d.text((x, y), etiqueta, font=_f(12), fill=(40, 40, 40))
    d.rectangle([x, y + 16, x + ancho, y + 40], fill="white", outline=(150, 150, 150))
    d.text((x + 6, y + 21), valor, font=_f(13), fill=(10, 10, 10))


def _tabla(d: ImageDraw.ImageDraw, y: int, columnas: list[tuple[str, int]], filas: list[list[str]]) -> None:
    x0 = 14
    d.rectangle([x0, y, 1265, y + 22], fill=(196, 206, 220), outline=(170, 180, 195))
    x = x0
    for nombre, ancho in columnas:
        d.text((x + 6, y + 4), nombre, font=_f(13), fill=(20, 20, 20))
        x += ancho
    for i, fila in enumerate(filas):
        yy = y + 22 + i * 28
        d.rectangle([x0, yy, 1265, yy + 28], fill="white" if i % 2 == 0 else (243, 245, 248),
                    outline=(220, 224, 230))
        x = x0
        for (_n, ancho), valor in zip(columnas, fila, strict=True):
            d.text((x + 6, yy + 7), valor, font=_f(13), fill=(10, 10, 10))
            x += ancho


def homonimos() -> Image.Image:
    img, d = _base("Clientes")
    _campo(d, 22, 100, "Buscar por nombre", "Ana Torres")
    d.rectangle([340, 116, 420, 140], fill=(52, 96, 168))
    d.text((356, 120), "Buscar", font=_f(13), fill="white")
    cols = [("Código", 110), ("Nombre", 260), ("Documento", 170), ("Ciudad", 160), ("Teléfono", 170),
            ("Dirección", 380)]
    _tabla(d, 160, cols, [
        ["CL-0007", "Ana Torres", "52345678", "Bogotá", "3014445566", "Calle 45 # 12-30"],
        ["CL-0019", "Ana Torres", "1019876543", "Cali", "3157778899", "Carrera 9 # 70-21"],
    ])
    d.text((14, 248), "2 resultados", font=_f(12), fill=(60, 60, 60))
    return img


def formulario() -> Image.Image:
    img, d = _base("Editar cliente CL-0031")
    _campo(d, 22, 110, "Nombre", "Jorge Ramírez")
    _campo(d, 360, 110, "Documento", "80123456")
    _campo(d, 22, 170, "Dirección", "Calle 80 # 24-15", ancho=638)
    _campo(d, 22, 230, "Ciudad", "Pereira")
    _campo(d, 360, 230, "Teléfono", "3102223344")
    _campo(d, 22, 290, "Correo", "jramirez@correo.com", ancho=638)
    d.rectangle([22, 360, 122, 388], fill=(52, 96, 168))
    d.text((44, 366), "Guardar", font=_f(13), fill="white")
    d.rectangle([136, 360, 236, 388], fill=(240, 240, 240), outline=(150, 150, 150))
    d.text((156, 366), "Cancelar", font=_f(13), fill=(20, 20, 20))
    return img


def filtros() -> Image.Image:
    img, d = _base("Reporte de pedidos")
    _campo(d, 22, 100, "Estado", "Entregado", ancho=200)
    _campo(d, 240, 100, "Desde", "01/09/2026", ancho=160)
    _campo(d, 420, 100, "Hasta", "30/09/2026", ancho=160)
    _campo(d, 600, 100, "Cliente", "", ancho=220)
    d.rectangle([840, 116, 940, 140], fill=(52, 96, 168))
    d.text((862, 120), "Filtrar", font=_f(13), fill="white")
    d.rectangle([952, 116, 1072, 140], fill=(40, 130, 80))
    d.text((970, 120), "Exportar CSV", font=_f(13), fill="white")
    cols = [("Pedido", 140), ("Cliente", 300), ("Fecha", 160), ("Estado", 180), ("Total", 200)]
    _tabla(d, 170, cols, [
        ["PD-0101", "Ana Torres", "03/09/2026", "Entregado", "$ 1.250.000"],
        ["PD-0107", "Jorge Ramírez", "12/09/2026", "Entregado", "$ 480.500"],
        ["PD-0113", "Lucía Pardo", "27/09/2026", "Entregado", "$ 2.010.000"],
    ])
    return img


if __name__ == "__main__":
    for nombre, fn in (("homonimos", homonimos), ("formulario", formulario), ("filtros", filtros)):
        ruta = AQUI / f"{nombre}.png"
        fn().save(ruta)
        print(ruta)
