# SmartRookie · Diagramas de arquitectura

> Vistas completas del sistema. GitHub renderiza mermaid directamente; en local se pueden ver con
> la extensión de mermaid de VS Code o en [mermaid.live](https://mermaid.live).
> Diseño detrás de cada vista: [`README.md`](README.md) y [`DISENO-ROOKIE.md`](DISENO-ROOKIE.md).

---

## 1. Vista de componentes (completa)

Cada subgrafo es un paquete real del repositorio (§14). Las fronteras importan: `computer_use/` es
la única pieza que conoce la contraseña del escritorio, y los roles solo hablan por esquemas
tipados — nunca ven coordenadas ni secretos.

```mermaid
flowchart TB
    EV["Evaluador / cliente<br/>Authorization: Bearer token"]

    subgraph SRV["Contenedor Rookie"]
        direction TB

        subgraph HTTP["Frontera HTTP"]
            API["api/<br/>FastAPI: /health · /tasks · /tasks/:id"]
            MOT["motor/<br/>cola · watchdog 285 s · bloqueo por escritorio"]
        end

        subgraph ROLES["roles/ — piensan; sin coordenadas ni secretos"]
            ORQ["Orquestador<br/>instrucción → plan M2"]
            JUEZ["Juez (proveedor distinto)<br/>mandato M3 · compuertas M7 · veredicto M9"]
            EXE["Ejecutor<br/>bucle ejecutar → comprobar → ajustar"]
            EXP["Explorador<br/>escaneo de solo lectura"]
        end

        ESQ["esquemas/<br/>pydantic M1–M10: toda comunicación entre roles pasa por aquí"]

        subgraph MANO["computer_use/ — única pieza con la contraseña"]
            GUA["guardias.py<br/>plazo · tarea activa · observación vigente · lista de bloqueo"]
            PRI["primitivas.py<br/>click ≥120 ms · reemplazar_texto (Ctrl+A) · scroll · release"]
            RFB["rfb.py<br/>cliente RFB sobre WebSocket"]
        end

        subgraph OJOS["percepcion/ — dice dónde; ningún modelo escribe coordenadas"]
            CV["vision_cv.py<br/>OpenCV: campos, botones, estabilidad de frame"]
            OCR["ocr.py<br/>RapidOCR: textos con rectángulo exacto"]
            SOM["marcas.py<br/>set-of-marks: lista numerada de elementos"]
        end

        subgraph MEM["memoria/ — libros por app (escritura única, §7.5)"]
            HUE["huella.py<br/>pHash + hash del menú"]
            LIB["libros.py<br/>app.json · procedimientos · resumen.md · historial/"]
            BIT["bitacora.py<br/>SQLite: ejecuciones y evidencias"]
        end

        LLM["modelos/cliente.py<br/>rol → proveedor + modelo (config/modelos.yaml)<br/>timeout ~20 s · un reintento · caché"]
        REP["reporte/<br/>plantillas + filtro de secretos (solo código)"]
    end

    DESK["Escritorio recibido en POST /tasks<br/>wss://…/websockify · 1280×800"]
    PROV["Groq · OpenRouter"]

    EV -->|"M1 tarea"| API
    API --> MOT
    MOT --> ORQ
    ORQ -->|"M2 plan"| JUEZ
    JUEZ -->|"M3 mandato"| EXE
    EXE -->|"M4 comando"| GUA
    GUA --> PRI
    PRI --> RFB
    RFB <-->|"RFB: framebuffer + input"| DESK
    RFB --> CV
    OCR --> SOM
    CV --> SOM
    SOM -->|"M5 observación"| EXE
    EXE -.->|"M6 compuerta crítica"| JUEZ
    JUEZ -.->|"M7 veredicto"| EXE
    EXE -->|"M8 resultado"| JUEZ
    JUEZ -->|"M9 veredicto final"| REP
    REP -->|"M10 reporte"| API
    EXP -->|"libros en supuesto"| LIB
    HUE --> LIB
    LIB -->|"app.json"| ORQ
    LIB -->|"módulo de la tarea"| EXE
    EXE -->|"candidatos"| LIB
    JUEZ -->|"promueve a comprobado"| LIB
    EXE --> BIT
    ORQ --> LLM
    JUEZ --> LLM
    EXE --> LLM
    EXP --> LLM
    LLM --> PROV
```

**Lectura del diagrama:**

- Flechas sólidas = flujo normal de una tarea (M1 → … → M10).
- Flechas punteadas = compuertas: antes de guardar, crear, exportar, eliminar o confirmar
  identidad, el ejecutor pide veredicto al juez (D1: segunda comprobación independiente).
- La contraseña entra por `POST /tasks` y muere en `rfb.py`; el resto del sistema usa
  `escritorio_ref`.

---

## 2. Máquina de estados de una tarea

```mermaid
stateDiagram-v2
    [*] --> Recibida
    Recibida --> Conectando
    Conectando --> Reconociendo: huella de app
    Reconociendo --> Explorando: app o módulo desconocido
    Reconociendo --> Planificando: libro disponible
    Explorando --> Planificando
    Planificando --> Ejecutando
    Ejecutando --> Compuerta: acción crítica
    Compuerta --> Ejecutando: aprobada
    Compuerta --> Fallida: impedimento
    Ejecutando --> Recuperando: resultado inesperado
    Recuperando --> Ejecutando: nueva estrategia (máx. 3 por paso)
    Ejecutando --> VerificacionFinal
    VerificacionFinal --> Terminada: evidencia completa
    VerificacionFinal --> Fallida: falta evidencia
    Terminada --> [*]
    Fallida --> [*]
```

Desde cualquier estado activo, el plazo (285 s) o la cancelación llevan al cierre: soltar entradas,
desconectar, publicar resultado, bloquear nuevos comandos.

---

## 3. Secuencia de una tarea (contrato M1–M10)

```mermaid
sequenceDiagram
    participant EV as Evaluador
    participant API as API + Motor
    participant O as Orquestador
    participant J as Juez
    participant E as Ejecutor
    participant CU as Computer Use
    participant D as Escritorio VNC

    EV->>API: POST /tasks (tarea + escritorio)
    API-->>EV: task_id (< 2 s)
    API->>O: M1 tarea
    Note over API,D: conexión VNC en paralelo con el análisis (0–10 s)
    O->>J: M2 plan (tipo, módulo, cambios autorizados)
    J->>E: M3 mandato (criterios + compuertas + escrituras permitidas)
    loop bucle ejecutar → comprobar → ajustar
        E->>CU: M4 comando (elemento_id de la observación vigente)
        CU->>D: primitiva (click ≥120 ms / Ctrl+A + escribir)
        D-->>CU: framebuffer estable
        CU-->>E: M5 observación (lista numerada de elementos)
        alt acción crítica (identidad, guardar, crear, exportar)
            E->>J: M6 solicitud de compuerta + evidencia
            J-->>E: M7 veredicto (aprobada / finalizar_fallida)
        end
    end
    E->>J: M8 resultado propuesto + evidencia final
    J-->>O: M9 veredicto (criterios + escrituras ⊆ permitidas + sin duplicados)
    O-->>API: M10 reporte (plantilla + filtro de secretos)
    EV->>API: GET /tasks/task_id (cada 5 s)
    API-->>EV: estado + resumen
```

---

## 4. Ciclo de vida de la memoria (libros)

```mermaid
flowchart LR
    EXP["Explorador<br/>escaneo de solo lectura"] -->|crea| SUP["supuesto"]
    SUP -->|"juez valida con evidencia"| COM["comprobado v1<br/>(escritura única)"]
    COM -->|"uso exitoso"| COM
    COM -->|"falla confirmada<br/>(2 observaciones, no es percepción)"| CAND["candidato v2<br/>coexiste con v1"]
    CAND -->|"funciona + juez confirma"| COM2["comprobado v2"]
    COM2 --> HIST["v1 → historial/"]
    CAND -->|"v1 también funciona"| DESC["descartado<br/>(no se toca la memoria)"]
```

| Actor | Permisos |
|---|---|
| Explorador | Crear `supuesto`; nunca promover |
| Ejecutor | Detectar fallas confirmadas y proponer `candidato` |
| Juez | **Único** que promueve a `comprobado` e invalida |
| Código | Criterio de tres condiciones, escritura atómica, historial |

---

## 5. Presupuesto de tiempo de una tarea (5 min)

```mermaid
flowchart LR
    T0["0–10 s<br/>conexión VNC ∥ análisis<br/>de la instrucción"] --> T1["10–150 s<br/>exploración completa<br/>(solo app nueva o cambiada)"]
    T1 --> T2["hasta 240 s<br/>ejecución + compuertas"]
    T2 --> T3["240–270 s<br/>verificación final +<br/>recuperación limitada"]
    T3 --> T4["285 s · CORTE DURO<br/>release_inputs · disconnect<br/>publicar resultado"]
```

Con libro disponible, la fase de exploración se salta y la ejecución empieza de inmediato: la
primera tarea de una app paga la exploración, las siguientes la amortizan.
