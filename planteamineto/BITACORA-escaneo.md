# BITÁCORA · Sesión de revisión end-to-end + escaneo del portal

**Fecha:** sáb 10 oct 2026 (~13:45) · **Autor:** sesión de agente (pi) · **Rama:** `main`
**Commits de la sesión:** `ad1becc` (sync informes) · `d3c2281` (conocimiento + prompt)

> Anotaciones crudas de lo que hice, vi y decidí. Complementa `CONOCIMIENTO-SIGECOM.md`
> (el mapa limpio de la app) y `INFORME-estado.md` (el veredicto ejecutivo).

---

## 1. Qué revisé de la cadena VNC → modelo → ejecución

Leído y verificado por código (no solo por el informe, que estaba desactualizado: decía "0 líneas"
pero ya hay ~2.400 L reales):

| Archivo | L | Veredicto |
|---|---|---|
| `computer_use/rfb.py` | 283 | Handshake RFB, auth DES, `SetEncodings([Raw])`, blit de rectángulos, PNG + hash. OK. |
| `computer_use/primitivas.py` | 172 | `Escritorio`: click(hold), escribir(reemplazar), tecla, scroll, capturar(frame estable + `cambio`), cerrar(suelta+desconecta). OK. |
| `computer_use/guardias.py` | 31 | Plazo + cierre; corta 15 s antes del deadline. OK. |
| `roles/ejecutor.py` | 432 | Bucle completo + compuertas de código + verificación final + cierre en `finally`. **Núcleo del reto.** OK. |
| `roles/prompts.py` | 113→enriquecido | 3 system prompts (orquestador/ejecutor/verificador), agnósticos a la app. |
| `modelos/cliente.py` | 352 | OpenRouter `chat/completions`, imagen base64, `response_format` JSON, reintento, timeout. OK. |
| `roles/juez.py` | **0** | ❌ **Falta** (Fase G). Hook en ejecutor lo aprueba por defecto mientras no exista. |

**Estado de pruebas:** `150 passed, 5 skipped` (los skip son de red). Lint `ruff`: limpio.

Conclusión: el camino crítico está íntegro. El hueco real es el **juez (G)** y correr **E1–E5 en vivo (H)**.

---

## 2. Conexión: lo que me costó entender

- `ESCRITORIO_URL` en `.env` **ya termina en `/websockify`**. Si le añades `/health` da 404
  (`{"error":"no encontrado"}`). El servidor SÍ está vivo.
- `/health` y `/__reset` cuelgan de la **raíz del host**, no bajo `/websockify`:
  - `GET  https://<host>/health`  → `200 {"ok":true}`
  - `GET  https://<host>/`        → noVNC (HTML)
  - `POST https://<host>/__reset` con `Authorization: Bearer <ESCRITORIO_TOKEN>` → `{"ok":true}`
- `sonda_mano._a_wss()` ya normaliza a `wss://<host>/websockify` usando `netloc` (ignora el path),
  así que funciona aunque la URL base traiga `/websockify`.

Mini-receta que usé para el reset (sobre la raíz, no sobre la URL cruda):
```python
p = urlsplit(os.environ["ESCRITORIO_URL"]); base = f"{p.scheme}://{p.netloc}"
httpx.post(base+"/__reset", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=60)
```

---

## 3. Escaneo del portal: secuencia real y qué falló

Reseteé desde 0 y conecté por VNC. Capturas en `/tmp/scan/*.png` (volátil, no va al repo).

**Coordenadas medidas de la barra superior** (y=48): las del informe estaban mal.
- Informe decía: Archivo 33 · Comercial 103 · Cartera 163 · Herramientas 233 · Ayuda 303.
- **Reales:** Archivo ≈42 · Comercial ≈122 · Cartera ≈203 · Herramientas ≈296 · Ayuda ≈384.
  → Corregir cualquier sonda que use las viejas.

**Lo que falló (y por qué importa):**
- Primer barrido de menús: los hashes salieron **desfasados una posición** y varios `cambio=False`.
  Causa: `Escape` **no cerraba** el desplegable anterior y los clics siguientes togglean.
- Navegar a una hoja encadenando clics (`Comercial→Maestros→Clientes` sin pausa) **cae en Inicio**:
  el submenú se **colapsa** si no se deja renderizar.
- **Patrón que SÍ funcionó** para abrir Clientes:
  `click(122,48)` → `click(120,76)` → `capturar()` (aquí aparece "Clientes") → `click(326,76)` → `capturar()`.
  Es decir: **una captura entre abrir el submenú y clicar la hoja**.
- El flag `obs.cambio` sale `False` en la primera captura de cada conexión (no hay frame previo con
  qué comparar). No confundirlo con "no pasó nada".

**Coordenadas útiles que dejé anotadas:**
- Submenú Maestros→Clientes: hoja en ≈(326,76).
- Submenú Ventas→Pedidos: Ventas ≈(113,103), hoja Pedidos ≈(322,103).
- Cartera→Facturas: Cartera (203,48), hoja Facturas ≈(207,76).
- En Clientes: botón `Nuevo cliente` ≈(71,185), `Exportar` ≈(176,185).

---

## 4. Observaciones de comportamiento (las que cambian el diseño)

1. **Estado persistente entre conexiones.** Reconecté varias veces y la app seguía en la última
   pantalla (incluso con el formulario "Nuevo cliente" a medio abrir). Solo `__reset` limpia.
   → El agente no puede asumir que arranca en Inicio.
2. **Un solo cliente VNC.** Mientras sondeaba, ninguna otra sesión podía mirar por noVNC.
3. **Menús sticky + hover.** Ya detallado arriba; es la fuente de fallos de navegación #1.
4. **Búsqueda substring + sensible a tildes.** Lo dice la propia pantalla de Clientes
   ("Ramírez no trae Ramirez"). Afecta E3 (contar) y E5 (homónimos).
5. **Ciudad/Estado son `<select>`** con opciones fijas (Bogotá, Medellín, Cali, Barranquilla,
   Bucaramanga, Pereira). No se escriben.
6. **"Exportar" = generar reporte** que aterriza en Herramientas→Reportes generados; la nota de
   "descargas desde el supervisor" parece distractor. Falta confirmarlo a mano (Fase H/E4).

---

## 5. Qué dejé hecho en el repo

- **`CONOCIMIENTO-SIGECOM.md`** — mapa limpio de la app + guion E1–E5 + trampas (material de depuración,
  explícitamente fuera de los prompts).
- **Prompt del ejecutor enriquecido** (`roles/prompts.py`) con lo genérico y reutilizable en otra app:
  menús en cascada, búsqueda substring/tildes + paginación, selects, estado persistente.
  Verificado que `test_prompts_sin_literales_de_la_app` sigue pasando (nada de `SIGECOM`,
  `Comercial`, `Maestros`, `Cartera`, `operador2`, `Distribuidora`).
- **Esta bitácora.**

---

## 6. Pendientes concretos (orden sugerido)

1. **Fase G — juez** (`roles/juez.py`): releer la captura en las 4 compuertas críticas
   (guardar/crear/exportar/identidad) con un modelo distinto del ejecutor. El hook ya existe en
   `ejecutor._compuerta_juez`; hoy aprueba por defecto.
2. **Fase H — E1–E5 en vivo** contra el escritorio real (una sesión por escritorio, noVNC cerrado).
   Empezar por **E3** (consulta, sin escritura, bajo riesgo) para validar la cadena, luego E1, E2,
   E5 y por último **E4** (confirmar si el export es posible para `operador2`).
3. Confirmar el **deploy** (`/tasks` real) y re-chequear `/health` público.
