# Arquitectura · Reto 1 — Agente que opera un sistema sin API

> **Reemplazado**: el documento de diseño vigente es `../planteamineto/DISENO-ROOKIE.md`.
> Este archivo se conserva como primer borrador; ante cualquier diferencia, manda Rookie.

## 1. El problema en una frase

Construir un servicio HTTP que recibe tareas en lenguaje natural, las ejecuta operando una app de
escritorio legada **solo por VNC** (ver pantalla + mouse + teclado), y reporta estado y resumen —
sobre **cualquier** app de este estilo, incluida una que nunca ha visto.

## 2. Restricciones que mandan sobre el diseño

| Restricción (del README)                              | Consecuencia de diseño                                              |
| ----------------------------------------------------- | ------------------------------------------------------------------- |
| App sin API; solo VNC 1280×800 (`/websockify`, RFB)   | Necesitamos un cliente RFB propio: framebuffer + eventos de input   |
| Repintado tarda hasta 1 s tras un clic                | Toda acción espera/verifica repintado antes de capturar             |
| Clic < 100 ms se ignora                               | Clic con press→release ≥ 120 ms                                     |
| Escribir concatena sobre texto existente              | Siempre `Ctrl+A` (o triple clic) antes de escribir en un campo      |
| `POST /tasks` responde en < 2 s                       | La tarea corre en background; respuesta inmediata con `task_id`     |
| 5 min por tarea, luego resetean el escritorio         | Presupuesto interno ~4:30 y **detenerse** al reportar estado final  |
| La evaluación usa una app distinta y desconocida      | Cero hardcodeo de SIGECOM: el agente explora y aprende la UI al vuelo |
| Un cambio de más = acción destructiva; sin duplicados | Política "mínimo cambio": verificar antes de crear/guardar          |
| Falso éxito penaliza                                  | Verificación post-acción obligatoria antes de reportar `terminada`  |
| El resumen no puede filtrar la contraseña/secretos    | Filtro de redacción sobre todo texto que sale del agente            |
| Sin herramientas/proveedores externos que "lo hagan"  | Nada de RPA/computer-use llave en mano; librerías de bajo nivel sí  |

## 3. Vista de componentes

```
                    ┌─────────────────────────────────────────────┐
  Evaluador ──HTTP──│  API (FastAPI)                              │
  Bearer <token>    │  GET /health · POST /tasks · GET /tasks/{id}│
                    └───────────────┬─────────────────────────────┘
                                    │ encola (task_id, tarea, escritorio)
                    ┌───────────────▼─────────────────────────────┐
                    │  Task Manager                                │
                    │  estado en memoria · timeout 4:30 · 1 worker │
                    │  por tarea · kill limpio al reportar         │
                    └───────────────┬─────────────────────────────┘
                                    │
                    ┌───────────────▼─────────────────────────────┐
                    │  Orquestador (loop del agente)               │
                    │  observar → decidir → actuar → verificar     │
                    │  + Política de tarea (clasificación, guard-  │
                    │    rails, formato del resumen)               │
                    └──────┬───────────────────────┬──────────────┘
                           │                       │
            ┌──────────────▼───────┐   ┌───────────▼──────────────┐
            │  Percepción (LLM     │   │  Driver VNC (cliente RFB │
            │  visión: screenshot  │   │  sobre websocket):       │
            │  → qué hay y dónde)  │   │  capture / click / type /│
            │                      │   │  keys / scroll + timing  │
            └──────────────────────┘   └───────────┬──────────────┘
                                                   │ wss://…/websockify
                                            Escritorio (noVNC kiosko)
```

## 4. Módulos

### 4.1 API (contrato del reto)

- `GET /health` → 200 cuando el agente está listo.
- `POST /tasks` → valida Bearer, encola, responde `{"task_id": …}` en milisegundos.
- `GET /tasks/{task_id}` → `{"estado": "en_curso|terminada|fallida", "resumen": "…"}`.
- El escritorio **siempre** es el que llega en el body (`escritorio.url`, `escritorio.password`);
  nunca uno fijo.
- Estado en memoria (dict) basta: el evaluador consulta cada 5 s y una tarea vive ≤ 5 min.

### 4.2 Driver VNC (la "mano")

Cliente RFB propio sobre el websocket (`asyncio` + `websockets`, protocolo RFB 3.8 con auth VNC).
Primitivas:

- `screenshot()` → PNG del framebuffer. Tras cada acción: esperar ≥ 1.2 s **y/o** comparar frames
  hasta que la pantalla se estabilice.
- `click(x, y)` → press, sleep 150 ms, release. `double_click`, `right_click`.
- `type_text(s)` → siempre precedido de `Ctrl+A` sobre el campo enfocado; tipeo con pausas cortas.
- `key(combo)` → Enter, Esc, Tab, flechas (fallback cuando el clic no funciona).
- `scroll(dir)`.

### 4.3 Percepción (los "ojos")

- Screenshot 1280×800 → modelo de visión (Claude) que devuelve: elementos visibles, textos, y la
  coordenada del elemento objetivo.
- Verificación cruzada: tras pedir "clic en X", recapturar y confirmar que pasó lo esperado
  (ventana abrió, campo enfocado, etc.). Si no, reintentar con estrategia alterna (máx. 2 veces).

### 4.4 Orquestador (el "cerebro")

Loop tipo ReAct con presupuesto:

1. **Clasificar la tarea**: `crear | actualizar | pregunta | exportar | imposible/ambigua`.
2. **Explorar si hace falta**: en app desconocida, abrir menús y mapear módulos (clientes,
   pedidos, facturas…) antes de actuar. El mapa vive solo durante la tarea (el escritorio se
   resetea entre tareas).
3. **Plan corto** (3–7 pasos) y ejecución paso a paso: observar → actuar → verificar.
4. **Verificación final**: releer en pantalla el registro creado/modificado antes de reportar.
5. **Cierre**: reportar `terminada`/`fallida` + resumen, y **matar el worker** (regla de los 5 min).

### 4.5 Política de tarea (guardrails — aquí se gana o pierde la evaluación)

- **Anti-duplicados**: antes de crear, buscar si ya existe (E1). Antes de guardar, revisar que no
  haya quedado un registro a medias.
- **Mínimo cambio**: solo tocar los campos que pide la tarea (E2: solo dirección, nada más). Si un
  popup pide algo extra, resolverlo sin alterar datos.
- **Ambigüedad / peligro ⇒ `fallida`** (E5): si hay 2+ candidatos (homónimos) o la tarea es
  imposible, no tocar nada y explicar en el resumen **nombrando los datos** que lo demuestran
  (los `claves_razon`, p. ej. los dos NIT/teléfonos de las Ana Torres).
- **Preguntas** (E3): el resumen lleva `respuesta: <valor>` con el **número primero** en la línea;
  dos datos → `respuesta: <dato1>; <dato2>` en el orden preguntado; pregunta irrespondible → sin
  línea `respuesta:` y estado `fallida` con razones.
- **Exportar** (E4): aplicar filtros exactos (estado + rango de fechas) y exportar una sola vez.
- **Fechas relativas**: "hoy/ayer/el sábado" se calculan desde la fecha que **muestra la app**, no
  la del servidor.
- **Redacción de secretos**: filtro final que borra contraseña del escritorio, token y URLs
  internas de cualquier resumen.
- **Nunca falso éxito**: si la verificación final no confirma el cambio, reportar `fallida` con lo
  observado.

## 5. Flujo de una tarea (secuencia)

```
POST /tasks ──► task_id (≤2s) ──► worker:
  conectar VNC ─► screenshot ─► clasificar ─► (explorar app)
  ─► por cada paso: [screenshot → decidir acción → ejecutar → esperar repintado → verificar]
  ─► verificación final del diff ─► construir resumen (formato + redacción)
  ─► estado = terminada|fallida ─► cerrar VNC y detener worker
  (watchdog a 4:30 ⇒ corta, reporta fallida con lo alcanzado)
```

## 6. Stack propuesto

| Pieza          | Elección                                   | Por qué                                         |
| -------------- | ------------------------------------------ | ----------------------------------------------- |
| Lenguaje       | Python 3.12 + asyncio                      | Rapidez de desarrollo, buen soporte websockets   |
| API            | FastAPI + uvicorn                          | Contrato trivial, background tasks nativas       |
| VNC            | Cliente RFB propio (`websockets` + struct) | Control total del timing; sin dependencias RPA   |
| Imagen         | Pillow (encode PNG, diff de frames)        | Ligero                                           |
| Razonamiento   | API de Claude (visión + texto)             | Un solo modelo para ver, decidir y redactar      |
| Despliegue     | Docker → VPS/Fly/Railway con HTTPS         | Accesible por internet 20:00 → fin del demo      |
| Config         | Env vars: `TEAM_TOKEN`, `ANTHROPIC_API_KEY`| Nada secreto en el repo                          |
| Observabilidad | Log por tarea (acciones + screenshots)     | Depurar fallos del demo en caliente              |

## 7. Estructura de código prevista

```
reto1/
├─ planeacion/            # este documento
├─ pruebas/               # casos dados (E1–E5)
└─ agente/
   ├─ main.py             # FastAPI: /health, /tasks
   ├─ manager.py          # cola, estados, watchdog 4:30
   ├─ orquestador.py      # loop observar→decidir→actuar→verificar
   ├─ politica.py         # clasificación, guardrails, formato de resumen, redacción
   ├─ percepcion.py       # llamadas al modelo de visión, grounding de coordenadas
   ├─ vnc/
   │  ├─ rfb.py           # protocolo RFB sobre websocket (auth, framebuffer, input)
   │  └─ driver.py        # click/type/keys/screenshot con las reglas de timing
   ├─ harness.py          # corre E1–E5 contra el escritorio propio + POST /__check
   └─ Dockerfile
```

## 8. Plan de pruebas (con el escritorio propio)

1. `POST /__reset` → correr una prueba E* → `POST /__check` con el JSON de la prueba.
2. Harness que itera E1–E5 y saca tabla de `checks` (cambios esperados / sin acciones de más /
   sin duplicados).
3. Pruebas de robustez: campos con texto previo, popups inesperados, lentitud de repintado.
4. Ensayo general: desplegar, apuntar el evaluador casero al endpoint público y medir tiempos.

## 9. Riesgos y mitigaciones

| Riesgo                                        | Mitigación                                                      |
| --------------------------------------------- | ---------------------------------------------------------------- |
| Coordenadas del modelo imprecisas             | Verificar efecto de cada clic; reintento con zoom/crop de la zona |
| App de evaluación con UI muy distinta         | Fase de exploración genérica; cero selectores/posiciones fijas    |
| Latencia LLM come los 5 min                   | Pocos pasos grandes > muchos micro-pasos; screenshots solo cuando aportan |
| Acciones colgadas tras timeout                | Watchdog corta worker y cierra la conexión VNC                    |
| Duplicado por reintento de "Guardar"          | Verificar en pantalla si guardó antes de reintentar               |
| Caída del servicio en demo                    | Docker `restart: always`, estado por tarea aislado, logs          |

## 10. Pendientes / lo que falta recibir

- **Tarjeta del escritorio** (`/evento`): URL, contraseña y token del equipo — sin esto no se puede
  probar nada real.
- Confirmar proveedor de despliegue y dominio público.
- Definir reparto del equipo (driver VNC / orquestador+política / despliegue+harness).
