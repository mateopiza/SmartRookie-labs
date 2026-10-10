# INFORME DE ESTADO · Reto 1 — El agente que opera lo que no tiene API

**Fecha/hora:** sáb 10 oct 2026, ~12:25 (‑05) · **Entrega:** 20:00 · **Restante al escribir:** ~7 h 35 min
**Repo:** `/home/iamdexter/frotinerla1|/reto1`
**Escritorio propio:** `frontier-r1-tm65-production.up.railway.app` (VNC sobre `/websockify`)
**Credenciales:** en `rookie/.env` (gitignoreado). Nunca en este documento ni en el código.

---

## 1. Veredicto ejecutivo

> **El camino crítico está roto en cero puntos y validado en los cuatro que importan.**
> Se puede construir el agente hoy. El riesgo ya no es "¿funciona el VNC?" sino
> "¿cuántos pasos de modelo caben en 285 s?".

| Hito | Estado |
|---|---|
| Conectar a `/websockify` y autenticar (VNC auth) | ✅ Validado |
| Recibir framebuffer completo 1280×800 y renderizarlo a PNG | ✅ Validado |
| Enviar clic (hold 150 ms) y que la app reaccione | ✅ Validado (menú Comercial se abrió) |
| Código del agente | ❌ **0 líneas** (31 `.py` con solo docstring) |

---

## 2. Evidencia de lo validado (sondas desechables en `/tmp`, fuera del repo)

```
GET /health            → {"ok":true}
RFB ProtocolVersion    → "RFB 003.008"
SecurityTypes          → [2]  (VNC auth, único tipo ofrecido)
SecurityResult         → 0 = OK
ServerInit             → 1280×800, name "6fd58361a587:0"
pixelFormat            → 32 bpp, depth 24, little-endian, truecolor, shifts (16,8,0) = BGRA
Encoding negociado     → Raw (0)   [Tight/ZRLE desactivados a propósito: más simple y suficiente]
Clic (103,48) hold150  → pantalla cambió (md5 71badc1b → 112cc8c4); menú Comercial abierto
```

Entorno local: Python **3.14** (el `pyproject.toml` pide `>=3.11`; funciona, pero `opencv-python-headless`
y `rapidocr-onnxruntime` probablemente no tengan rueda para 3.14 — otro motivo para haber descartado
la capa de OCR/OpenCV). Venv en `rookie/.venv/` con `websockets numpy pillow httpx pydantic fastapi
uvicorn pycryptodome`.

**Ajuste 12:45.** `pyproject.toml` limpiado para que coincida con la decisión de §5: fuera
`opencv-python-headless`, `rapidocr-onnxruntime`, `imagehash`, `numpy` e `instructor`; dentro
`pycryptodome` (DES de la auth VNC) y `python-dotenv`. `instructor` además rompería con los modelos
Claude actuales (fuerza `tool_choice`, que esos modelos rechazan con 400). `Dockerfile` alineado a
`python:3.14-slim` para que local y contenedor sean el mismo intérprete.

Las sondas y capturas (`/tmp/rfb_probe.py`, `/tmp/click.py`, `/tmp/cap*.png`) viven en `/tmp`, que es
volátil, y las sondas tienen la contraseña hardcodeada: la Fase A las copia al repo **sin** ella.

---

## 3. Dos bugs que costaron ~20 min y que el diseño debe recoger

1. **`PointerEvent` no lleva byte de padding.** Son 6 bytes: `[tipo=5][button-mask][x:2][y:2]`.
   Mandar 7 desalinea el stream y **el servidor cierra la conexión** (`Target closed`).
   `KeyEvent` sí lleva 2 bytes de padding (8 en total).
   → En `primitivas.py` esto va con un test unitario que fije el tamaño exacto de cada mensaje.
2. **Hay que negociar encodings con `SetEncodings` antes del primer `FramebufferUpdateRequest`.**
   Sin eso el servidor manda Tight y el parser se rompe. → Forzar `Raw` siempre.

**Comportamiento observado del transporte:** parece admitir una conexión a la vez y las reconexiones
muy seguidas a veces caen. → **Una conexión persistente por tarea**, con reconnect + backoff,
no una conexión por captura.

3. **Un cliente VNC a la vez.** Consecuencia práctica de lo anterior: si alguien tiene noVNC abierto
   en el navegador, el agente no conecta o se cae a mitad de tarea. Regla de equipo: **nadie mira el
   escritorio por noVNC mientras corre el agente o el harness**; para probar en paralelo, cada sesión
   usa el escritorio de un miembro distinto (su propio `.env`).

---

## 4. Lo que ya sé de SIGECOM (vale oro para los prompts)

De las capturas:

- Título: **SIGECOM · Distribuidora Andina v3.9**
- **`Hoy: 14/10/2026`** arriba a la derecha → es la fecha de referencia del reto
  ("ayer", "el sábado", "septiembre de 2026" se cuentan desde ahí).
- Usuario: `operador2`.
- Menú superior: `Archivo · Comercial · Cartera · Herramientas · Ayuda`.
- `Comercial` → submenús `Maestros` y `Ventas`
  (→ clientes bajo Maestros, pedidos bajo Ventas; facturas probablemente bajo Cartera).
- La pantalla de inicio es una **lista de reportes** ("No hay reportes generados") con columnas
  `ID · Nombre · Filtros · Fecha · Usuario`.
- ⚠️ Nota visible: *"Las descargas se habilitan desde el puesto del supervisor."*
  → posible trampa de **E4 (exportar)**: si el botón de exportar está deshabilitado para `operador2`,
  la tarea puede ser `fallida`. **Comprobarlo a mano antes de asumir que E4 pasa.**
- **Lectura más probable de E4 (ajuste 12:45):** la pantalla de inicio es una lista de **reportes
  generados** (`ID · Nombre · Filtros · Fecha · Usuario`) y `E4.json` espera `accion: export` con
  `filtros`. Lo que `__check` puede verificar es que exista un reporte generado con esos filtros, no
  una descarga. Es decir: "exportar" = **generar el reporte** que queda en esa lista; la nota de las
  descargas sería un distractor, no un bloqueo. Sigue siendo obligatorio confirmarlo a mano (Fase H).

Coordenadas de referencia del menú superior (captura 1280×800):
`Archivo (~33,48) · Comercial (~103,48) · Cartera (~163,48) · Herramientas (~233,48) · Ayuda (~303,48)`.

---

## 5. Decisión de arquitectura (revisada 12:45: grounding por modelo de visión vía OpenRouter,
##    sin capa OCR/OpenCV)

Se confirmó que el reto permite usar modelos que devuelven coordenadas desde la captura. Dos
precisiones que cambian el "cómo" respecto a la versión anterior de este informe:

- **Proveedor: OpenRouter, decisión cerrada** (`OPENROUTER_API_KEY` en `rookie/.env`). Las
  herramientas nativas de computer use de Anthropic (`computer_toolset_20260801` en la API actual;
  `computer_20250124` ya no existe) y la de OpenAI **no pasan por OpenRouter**. Por tanto el grounding
  se hace con un **esquema JSON propio** (`Accion` con `x, y, elemento, razon`, ver `PLAN-FASES.md` §2)
  sobre un modelo con visión servido por OpenRouter. Cualquier modelo que devuelva coordenadas desde
  la captura funciona; la pantalla va sin reescalar (1280×800).
- **El modelo del ejecutor se elige midiendo**, no por nombre: la sonda de la Fase M manda `cap1.png`
  a cada candidato y anota el error en píxeles al localizar "Comercial" (≈ 123,48) y la latencia.

Los modelos de visión **devuelven coordenadas; no se conectan al VNC**. Por tanto:

| Pieza | ¿Cambia? |
|---|---|
| `computer_use/rfb.py` (RFB sobre WebSocket) | ❌ Sigue obligatoria; es el camino crítico |
| `computer_use/primitivas.py` (clic ≥120 ms, Ctrl+A, frame estable) | ❌ Sigue; el modelo no sabe que un clic <100 ms se ignora |
| `percepcion/ocr.py`, `vision_cv.py`, `marcas.py` | ✅ **Se caen enteras** — el modelo devuelve `x, y` desde la captura (esquema `Accion`) |
| `roles/ejecutor.py` | ✅ Se simplifica: bucle captura → modelo (JSON) → compuertas de código → primitiva |
| `memoria/`, explorador completo, banco de modelos | ✅ Fuera (recorte por presupuesto; la evaluación usa otra app y resetea). Solo queda la **grabación** de observaciones a disco, que es gratis y sirve para depurar |
| `modelos/cliente.py` | 🔁 Un solo camino: `chat/completions` de OpenRouter con imagen en base64 y `response_format` JSON; sin SDK de Anthropic ni `instructor` |

**Ahorro neto: ~2,5 h.** La captura va sin reescalar (1280×800).

**Lo que NO se delega en el modelo, aunque el reto lo permita:**

1. **Compuerta de homónimos (E5):** coincidencias > 1 → `fallida` con los identificadores leídos de la
   captura. Decisión por código. Un modelo "elige el primero" y produce un falso éxito.
2. **Formato del resumen** (`respuesta:`, número primero, `;`, sin secretos): plantilla de código.
   La rúbrica lo compara literal.
3. **Juez** (modelo distinto del ejecutor) solo en las 4 compuertas críticas:
   guardar / crear / exportar / identidad. "Sin acciones fuera de la tarea" vale tanto como acertar.

---

## 6. Riesgos ordenados por probabilidad × impacto

| # | Riesgo | Mitigación |
|---|---|---|
| 1 | **Latencia de modelo**: cada paso = 1 llamada con imagen (~2–4 s) + ~1 s de repintado → **~45–60 pasos máx** en 285 s | Juez solo en compuertas; capturas sin reescalar; cerrar con lo verificable al agotar presupuesto |
| 2 | **E4 puede ser imposible** (descargas solo desde supervisor) | Probarlo a mano hoy; si es imposible, el agente debe detectarlo y reportar `fallida` con la razón |
| 3 | **Deploy a las 20:00** | Contenedor desplegado a las **16:00** aunque `/tasks` aún no funcione |
| 4 | Falsos éxitos en homónimos (E5) | Compuerta por código (ver §5) |
| 5 | El agente sigue operando tras reportar | `release_inputs()` + `disconnect()` + bloqueo de comandos al cerrar; si sigue, cae en la tarea siguiente y cuenta como acción destructiva |
| 6 | **Identificadores inventados** por el modelo en E5 (los `datos_clave` salen de la captura, ya no de un OCR) | El juez (otro proveedor) relee la misma captura; si las listas difieren → `fallida` con la unión; el resumen nunca lleva un número que no esté en pantalla |
| 7 | JSON inválido o modelo lento vía OpenRouter | `response_format` json_schema con respaldo json_object, un reintento, timeout 25 s; el modelo se elige por latencia y error de píxel medidos (Fase M) |
| 8 | Dos sesiones probando contra el mismo escritorio se pisan (un cliente VNC a la vez) | Una sesión de pruebas por escritorio; noVNC cerrado mientras corre el agente |

---

## 7. Plan de las ~7 h restantes (revisado 12:45: en olas paralelas)

El plan secuencial anterior se reemplaza por **tres olas** con fases independientes que corren en
simultáneo, cada una con su prompt y sus archivos propios. Detalle, contrato de interfaces y prompts
listos para copiar: [`PLAN-FASES.md`](PLAN-FASES.md).

| Ola | Hora | Fases (en simultáneo) | Qué queda en `main` |
|---|---|---|---|
| 1 | 12:45–14:30 | **A** computer_use · **B** núcleo HTTP · **M** modelos · **F** harness · **E** despliegue | Mano, API con ejecutor de relleno, cliente de modelos elegido por medición, harness, `/health` público (15:00) |
| 2 | 14:30–16:30 | **D** orquestador + ejecutor · **G** juez | E1 pasando; juez listo para integrarse (17:30); redespliegue con `/tasks` real (16:45) |
| 3 | 16:30–19:15 | **H** E1–E5 + endurecimiento (una sesión por escritorio) | 5/5 dos veces seguidas; bitácora |
| — | 19:15–19:45 | Congelar + checklist (`PLAN-FASES.md` §4) | Entrega |

Dependencias duras: D necesita A + B + M; G necesita B + M; H necesita D + F; E redespliega cuando D
está en `main`. Todo lo demás es paralelo.

---

## 8. Próximo paso

Arrancar la **ola 1** en cinco sesiones (A, B, M, F, E) con los prompts de `PLAN-FASES.md` §3. Con
tres personas: persona 1 = A, persona 2 = B + E, persona 3 = M + F. Pendiente de la clave de
OpenRouter corregida en `rookie/.env` (solo la necesita M; A, B, F y E arrancan sin ella).
