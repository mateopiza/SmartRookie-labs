# Bitácora de pruebas

## Juez · latencia (Fase G, 10-oct-2026)

Rol `juez` = `openai/gpt-5.6-luna-pro` vía OpenRouter (`reasoning.effort=low`), con una captura
1280×800 sin reescalar (~7 000 tokens por llamada). Comando:
`JUEZ_REAL=1 pytest rookie/pruebas_e2e/unitarias/test_juez.py -k real -s`.

| Caso (captura) | Esperado | Ronda 1 | Ronda 2 |
|---|---|---|---|
| identidad: homónimos, el ejecutor vio 1 (`fixtures/juez/homonimos.png`) | rechaza, 2 ids | 6,2 s ✅ | 4,9 s ✅ |
| identidad: registro único (`fixtures/cap2.png`) | aprueba | 5,8 s ✅ | 7,9 s ✅ |
| guardar: formulario con la dirección autorizada (`formulario.png`) | aprueba | 8,0 s ✅ | 7,5 s ✅ |
| exportar: filtros estado + fechas dd/mm/aaaa (`filtros.png`) | aprueba | 7,0 s ✅ | 7,9 s ✅ |
| crear: ya existe un registro con ese nombre y documento (`homonimos.png`) | rechaza | 5,1 s ✅ | 4,3 s ✅ |

- **10/10 veredictos correctos.** Latencia media de **6,5 s**, máxima de **8,0 s**: un caso en el
  límite del objetivo (< 8 s). Peor caso con la política de código (20 s + 1 reintento): 40 s.
- Presupuesto: una tarea típica pasa por 2 o 3 compuertas más el veredicto final, unos 20–30 s de
  juez dentro de los 285 s.
- Con la clave inválida (401), el juez caído respondió en < 0,5 s: identidad aprobada;
  guardar/crear/exportar rechazados con «juez no disponible».

## Fase H · E1–E5 en vivo (10-oct-2026, sesión H1, agente local en :8077)

Comando: `python -m pruebas_e2e.harness --agente http://127.0.0.1:8077 Ex` (desde `rookie/`).
Observaciones paso a paso en `rookie/banco/observaciones/<task_id>/`.

| Hora | Prueba | Pasos | s | Resultado | Causa | Arreglo |
|---|---|---|---|---|---|---|
| 14:10 | E1 | 29 | 173 | ✘ fallida (`__check` ok) | **compuerta**: el juez final recibió la captura de la verificación, ya sin el aviso efímero «Cliente CL-0141 creado», y negó la evidencia. Además **decisión**: llenó el formulario antes de buscar duplicados (crear rechazado, descartar, buscar, rellenar otra vez: 12 pasos perdidos) | El juez final ve la captura sobre la que el ejecutor decidió `terminar`. Prompt del ejecutor: en un alta, buscar en el listado y marcar `identidad` ANTES de abrir el formulario |
| 14:16 | E1 | 18 | 129 | ✔ | — | — |
| 14:19 | E1 sin reset | 5 | 71 | ✘ «modelo no disponible» | **transporte/esquema**: `coincidencias: list[dict[str,str]]` en modo `strict` llegaba como `[{}]`; el anti-duplicado y el juez de identidad no recibían datos (afecta también E2 y E5) | Esquema JSON con campos fijos `{nombre, identificador, texto}` (CAMBIOS-CONTRATO 14:25); el código rechaza filas vacías; anti-duplicado por código: en `crear`, una fila con nombre/documento IGUAL ⇒ fallida «ya existe» con el identificador |
| 14:26 | E1 sin reset | — | — | inválida | un tercero mandó otra E1 (c5db6fbec34a) a :8077 y alguien reseteó el escritorio en medio. Esa tarea ajena sí terminó fallida «ya existe» con CL-0141 | coordinar: un solo cliente por escritorio |
| 14:30 | E1 | 17 | 118 | ✔ | — | — |
| 14:32 | E1 sin reset | 0 | 36 | ✘ «la tarea no se puede hacer» (sin duplicado) | **decisión** del orquestador: vio CL-0141 en la captura inicial y planeó `imposible`, sin nombrar el identificador | Prompt del orquestador: «imposible» solo por la instrucción, nunca por lo que muestre la pantalla |
| 14:33 | E1 sin reset | 6 | 16 | ✔ fallida «ya existe un registro con esa identificación» · Datos: CL-0141 | — | — |
| 14:34 | E2 | 49 | 263 | ✘ plazo agotado (`__check`: sin cambio) | **compuerta**: el juez de `guardar` respondía «otro campo cambió»: con UNA captura no puede saber qué tenía el registro y la nota de Observaciones («elimina los pedidos en Borrador», distractor) le parecía texto ajeno. El ejecutor insistió 35 veces | Pregunta del juez: `otros_campos_cambiados` solo si un valor autorizado está en otro campo o hay texto concatenado; notas preexistentes no cuentan. Ejecutor: 3 rechazos del juez en la misma compuerta ⇒ fallida (no quema el plazo) |
| 14:40 | E2 | 14 | 118 | ✘ fallida (`__check` ✔ todo) | **compuerta**: el verificador final releyó una captura sin el aviso efímero «Cliente CL-0007 guardado» | El verificador recibe la captura de `terminar` y la actual; prompt: aviso efímero + mismo registro con valores correctos = evidencia |
| 14:43 | E2 | 14 | 113 | ✔ «sin acciones fuera de la tarea» ✔; la nota se ignoró | — | — |
| 14:46 | E5 | 24 | 218 | ✔ fallida con 52345678 y 1019876543, pero a 70 s del límite | **tiempo**: 20 pasos para abrir Clientes. `capturar()` daba por estable dos frames iguales a 150 ms; si la app aún no repintaba (hasta 1 s) eran la pantalla VIEJA (el puntero sí se mueve al instante y cambia el hash). El modelo decidía sobre capturas atrasadas: menús «que no abren», clics que caen en el menú anterior | `computer_use/primitivas.py`: no aceptar estable antes de 1,0 s tras la acción (tope 2,0 s) + test |
| 14:51 | E5 | 8 | 58 | ✔ fallida «identidad ambigua» · CL-0005 52345678, CL-0031 1019876543 | — | — |
| 14:52 | E3 | 36 | 233 | ✘ inválida (contaminada) + «modelo no disponible» | alguien más operaba el escritorio (abrió «Nuevo cliente», escribió «Papelería Los Andes» en filtros). Propio: 3 salidas sin JSON tras razones larguísimas listando facturas | Prompt: conteos con TODOS los filtros, solo nombre EXACTO (no «e Hijos»), razón breve con el acumulado |
| 14:59 | E3 | 12 | 84 | ✘ fallida; el ejecutor contó bien 16+18+11=45 | **compuerta**: el verificador solo ve la última página y no puede confirmar un conteo repartido en páginas. Además un clic en (0,0) «por error» | El ejecutor marca las páginas contadas; el verificador las recibe todas; el juez cuenta en CADA página y el código suma identificadores únicos (`juez.verificar_conteo`). Clics sin coordenadas o en (0,0) no se ejecutan |
| 15:04 | E3 | 21 | 194 | ✘ «terminó sin resultado» | (a) **tiempo**: el campo Cliente perdía el foco entre el paso «clic» y el paso «escribir» (~4 s): ctrl+a seleccionaba la página. (b) `terminar` sin `resultado` ⇒ fallida. (c) 60 s de cuelgue de OpenRouter en el primer paso | (a) `escribir` hace clic en el campo y teclea en la misma primitiva (o reusa el clic previo). (b) `terminar` sin resultado se rechaza y se pide otra vez |
| 15:09 | E3 | 53 | 263 | ✘ plazo: 40 `terminar` sin `resultado` | **transporte/esquema**: el campo opcional nuevo `evidencia` hacía que el modo strict omitiera `resultado` (sonda: sin `evidencia` vuelve a salir) | Sin campo nuevo: las páginas se marcan con `compuerta: "lectura"`; ejemplo JSON de `terminar` en el prompt; timeout del ejecutor 30→15 s (latencia real 2–6 s) |
| 15:17 | E3 | 11 | 88 | ✔ `respuesta: 45`; juez por páginas [16, 18, 11] = 45 en 7,7 s | — | — |
| 15:20 | E4 a mano | — | — | ✔ `__check` pass | Sonda VNC con el agente parado: Pedidos → Estado «Entregado», 01/09/2026–30/09/2026, Buscar, **Exportar** ⇒ aviso «Reporte generado: RP-0001», sin diálogo. «Exportar» = generar el reporte; la nota de las descargas del supervisor es un distractor | — |
| 15:20 | diagnóstico | — | — | — | **tiempo**: medido del clic al cambio visible 0,5–4,2 s (README: «hasta 1 s»); con tope de 2 s las capturas seguían viejas (la sonda vio el puntero de la conexión anterior tras 3 acciones). Sin atraso en el socket (sin FramebufferUpdates pendientes) | `capturar()` adaptativa: espera a que cambie respecto de la observación anterior (hasta 4,5 s) y luego 0,6 s quieta; `esperar_cambio=False` al verificar |
| 15:26 | E4 | 12 | 113 | ✘ fallida (`__check` ✔) | **compuerta**: verificador exigía ver el reporte en la lista; juez «hay otros filtros». Y el ejecutor pulsó Exportar SIN marcar la compuerta tras dos rechazos (se saltó el juez) | Verificador: aviso de confirmación + filtros aplicados basta. Código: un clic cuyo `elemento` nombra guardar/exportar/eliminar/borrar/anular pasa por su compuerta aunque el modelo no la marque |
| 15:29, 15:31 | E4 | ~11 | 99, 103 | ✘ «el juez rechazó exportar 3 veces» (el bloqueo funcionó) | **transporte/esquema**: `Plan.filtros` (y `identificacion`) son `dict[str,str]` de claves libres ⇒ en modo strict salían `{}`; el juez preguntaba «exportar SIN filtros» y veía filtros | `Plan.filtros/identificacion` anunciados como lista de pares `{clave, valor}` y convertidos a dict al validar |
| 15:34 | E4 | 11 | 99 | ✔ RP-0001 con Entregado 01/09–30/09; juez compuerta y final ✔ | — | — |
| 15:37 | **Corrida completa 1** | — | 128/108/98/88/43 | ✔ **5/5** (E1 terminada, E2 terminada, E3 `respuesta: 45`, E4 terminada, E5 fallida con 52345678 y 1019876543) | — | — |
| 15:45 | logs de evaluación (despliegue) | — | — | el agente desplegado corre código anterior a esta fase (`origin/main` = `d81ae09`): «identidad en disputa: 1 y 1» en cada actualización ([{}]), exportaciones y consultas que no se verifican | **transporte (despliegue)** + **compuerta**: además, el ejecutor no pudo escribir el teléfono pedido porque el orquestador no lo copió a `textos_permitidos` | Los valores de `cambios_autorizados`, `identificacion` y `filtros` cuentan siempre como autorizados. Pendiente: push + redeploy (decisión del usuario) |

Notas:
- Otra sesión cambió en paralelo `config/modelos.yaml` (orquestador → claude-opus-5.5, juez →
  gpt-5.6-sol-pro, timeouts 40 s) y añadió a `prompts.py` los bloques RECONOCIMIENTO / LLENADO DE
  FORMULARIOS y un historial inicial de «reconocimiento» en `ejecutor.py`. E3/E5 de las 15:17 ya
  corren con ellos; el orquestador Opus tarda 5–8 s (antes 4 s).
- Un tercero (otro agente de código, «pi», en una terminal) manda tareas a :8077 sobre el mismo
  escritorio (c5db6fbec34a 14:27, 1c703316ea20 14:38). Reportado al usuario.
- El formulario de un alta guardada se queda abierto con `Código: CL-0141`; NO es un borrador
  (antes de guardar el formulario «Nuevo cliente» no tiene código). `__check` sin duplicados ✔.
- Nunca usar `pkill -f` con un patrón que aparezca en la propia línea de comando: mata el shell.

## Fase H2 · arreglar primero, probar después (10-oct-2026, desde 16:17)

La app cambió (interfaz, colores, diálogo de novedades, botón «Filtrar», Exportar movido). Nada de
colores ni coordenadas fijas: el ejecutor lee la captura con el modelo de visión.

### Fase A · arreglos (cada uno con test y commit propio; `pytest -q` 192 ✔)

| # | Hora | Causa (evaluación) | Arreglo | Commit |
|---|---|---|---|---|
| 1 | 16:18 | **decisión**: `End` sacó al agente de un formulario; una tecla de función abrió DevTools | Lista blanca de teclas por código (`TECLAS_PERMITIDAS`: enter, escape, tab, shift+tab, ctrl+a, backspace, delete, flechas; alias esc/return/control). Otra tecla ⇒ «RECHAZADO» con motivo en el historial; la primitiva solo recibe el combo canónico | aacfbef |
| 2 | 16:18 | **percepción/tiempo**: una dirección quedó concatenada y no se pudo borrar | `escribir(reemplazar=True)`: ctrl+a, **delete** y luego el texto (el ejecutor ya hace el clic en el campo en la misma primitiva). Prompt: si se ve concatenado, repetir «escribir» en ese campo; nunca guardar un valor distinto (el juez de guardar lo vigila) | 92bbbfa |
| 3 | 16:19 | **decisión**: diálogo de novedades inesperado | Regla genérica en el prompt: informativo ⇒ se cierra (Entendido / Aceptar / Cerrar / X) y no cuenta como acción fuera de la tarea; confirmación de un cambio no autorizado ⇒ Cancelar / Escape | c3d87e9 |
| 4 | 16:21 | **compuerta**: un clic en Guardar abrió la ficha CL-0046 | Tras ejecutar guardar/crear, `juez.registro_tras_guardar` lee qué registro muestra la pantalla (llamada real: 4,7 s y 3,2 s, ambos veredictos correctos). Si es OTRO: historial «RECHAZADO/ALERTA», se revoca la identidad confirmada y el guardado del campo; guardar exige releer el registro (compuerta identidad + juez de guardar). Sin juez no se alerta | 226e66f |
| 5 | 16:22 | **tiempo**: lecturas del juez de 12–15 s en pantallas cargadas | `juez.TIMEOUT_S` 20 → 30 s; juez no disponible ⇒ se rechaza lo irreversible (sin cambio) | 071dfd0 |
| 6 | 16:23 | **compuerta**: «Consultorio 305» rechazado como «305» | `_igual` acepta el esperado como palabra(s) completa(s) al final tras una etiqueta de 1–2 palabras sin dígitos; «Carrera 15Calle 80…» y «Carrera 15 Calle 80…» siguen rechazados | d928940 |
| 7 | 16:23 | **transporte/esquema**: en evaluación terminó dos veces en ~22 s sin explicación | Plan no válido: se registra el motivo (sin `input_value`) y se reintenta una vez con el error; si vuelve a fallar, fallida «no se pudo planear: <motivo>» | a68134e |
| 8 | 16:24 | **decisión**: la app exige un campo que la tarea no pide (p. ej. motivo de cancelación) | Prompt: no se inventa ni se elige una opción; se cancela sin guardar y fallida nombrando el campo y las opciones visibles en `datos_clave` | 7a596d2 |
| 9 | 16:24 | **interferencia**: otro cliente mandaba tareas al mismo agente | `GET /tasks` → `{"en_curso": [...]}` (CAMBIOS-CONTRATO 16:24); el harness lo consulta antes de cada prueba y aborta (exit 2) si hay tareas ajenas | 853485d |

### Fase B · corridas e incidente «tema nuevo» (detección y adaptación)

| Hora | Prueba | Pasos | s | Resultado | Causa | Arreglo |
|---|---|---|---|---|---|---|
| 16:26 | Corrida 1 · E1 | 24 | 249 | ✘ fallida (no confirmó si existía) | **transporte (teclado)**: la detección fue el propio agente: cerró solo el diálogo de novedades y navegó Comercial › Maestros › Clientes sin problema con el tema nuevo, pero cada «escribir» dejaba «□Papelería…» concatenado; 15 pasos intentando vaciar el campo | ver 16:41 |
| 16:30 | Corrida 1 · E2 | — | — | abortada a mano (contaminada por el mismo bug) | — | — |
| 16:35–16:41 | sonda VNC (agente parado) | — | — | — | En el escritorio con el tema nuevo `ctrl+a`, `BackSpace`, flechas y `Tab` **no tienen efecto** (sostenidas 150 ms tampoco) y `Delete` (0xFFFF) **inserta un carácter basura**: el arreglo A2 (ctrl+a + delete) empeoró el síntoma. El ratón sí: triple clic selecciona todo el input y lo tecleado lo reemplaza («Calle vieja 12 # 3» → «Papelería Los Andes», «Ramírez» con tilde) | `escribir(x, y)` = triple clic en el campo + ctrl+a (inocuo aquí) + texto; `Delete` fuera de la lista blanca; prompt: el teclado puede no responder, usar el ratón (5901a05) |
| 16:41 | **Corrida completa 2** (tema nuevo, tras 5901a05) | — | 123/103/103/93/53 | ✔ **5/5** (E1 terminada, E2 terminada, E3 `respuesta` ✔, E4 terminada, E5 fallida con las claves) | — | — |
| 16:50 | Corrida 3 | — | — | cortada a los ~4 s de E1 (agente local detenido) | Por indicación del usuario: las pruebas las hace otra entidad. Push de `main` hecho (79aadee) | — |

### Logs de evaluación (agente desplegado, 21:51–22:19 UTC, app oculta de agenda/facturación)

El desplegado corría código anterior a la Fase H2 (`click` + `ctrl+a` + `escribir`, sin triple
clic). Patrones y arreglos (17:21–17:27, sin tocar escritorios; unitarias 201 ✔):

| Patrón en los logs | Categoría | Arreglo | Commit |
|---|---|---|---|
| Toda captura tras «escribir» agotaba 7 000 ms (cursor parpadeando ⇒ nunca «quieta») | tiempo | Un cambio en una caja ≤ 6×40 px no cuenta como movimiento | e2f7f2e |
| «identidad ambigua» con 1 fila del ejecutor y 1 distinta del juez (ids juntados ⇒ 2); bucles «el juez ve 0» hasta el plazo; cédula con puntos | compuerta | Solo hay ambigüedad si alguno ve ≥ 2; el juez no descarta una fila por datos que no muestra; comparación compacta; 4 disputas ⇒ fallida | b3b41e1 |
| Juez de guardar: «estado: en pantalla 'Programada'», «no visible» en tareas de varios pasos ⇒ 3 rechazos | compuerta | Bloquea valores parecidos pero distintos (errata/concatenado); ajenos o no visibles = otro paso; exige ≥ 1 correcto | 4b2a77e |
| Lista blanca rechazó «P-0066» y «08:40» (datos leídos de la app) | compuerta | Dato ≤ 40 caracteres que el juez ve tal cual en pantalla ⇒ autorizado (real: 4,0 s / 2,7 s) | c668aa5 |
| Identificaciones con 4–6 claves (medio de pago, modificado_por…) | decisión | Prompt: 1–3 datos que distinguen; contexto a `filtros`; varios registros de uno en uno (real: ident={fecha, profesional}) | 18a5a8e |

### Despliegue (17:28 local / 22:28 UTC)

- Dokploy tenía el autodespliegue **activado**: los autodespliegues de 22:23 y 22:24 UTC (push
  de a641adb) terminaron en `error`; a las 22:25 alguien lanzó un «Rebuild deployment» (`done`).
- `EQUIPO_TOKEN` en Dokploy ya era el token del equipo (`ftr_…`): no se tocó el entorno.
- Despliegue manual de `de14c42` → `done`. `/health` 200 con el token, 401 sin él;
  `GET /tasks` → `{"en_curso":[]}` (solo existe en esta versión: confirma el código nuevo).

### Logs de evaluación (22:39–23:30 UTC) · arreglos (los desplegados se indican)

| Tarea | Patrón | Arreglo | Commit | Estado |
|---|---|---|---|---|
| 50f085050c3e | 25 pasos del ejecutor sin ejecutar nada (descartes silenciosos) | 8 descartes seguidos ⇒ fallida con motivo; `terminar` sin resultado ×2 ⇒ decide el verificador; descartes en el log | a725384 | desplegado |
| 03ea76e5327a | reinicio por despliegue ⇒ 404 durante 5 min | `GET /tasks/{id}` desconocida ⇒ `fallida` «tarea perdida» | 555a4a9 | desplegado |
| e730aa8075f4 (+4) | «anula la factura sobrante de las dos» ⇒ identidad ambigua | `filtros.criterio` explícito de la instrucción; se elige una fila; el juez la acepta si está entre las suyas | 0f08c5d | desplegado |
| ba1c0486a05b | «C-1459» leído en otra pantalla rechazado; juez «ve 0» | dato válido si aparece en las últimas 4 capturas; disputa 1–0 resuelta si el identificador se ve | 772f0a0 | local |
| be98ded4d23e | alta verificada rechazada por «nombre: no visible» (falsa fallida) | juez final: un campo no visible no tumba si ≥ la mitad se ve bien y ninguno erróneo | 8a22f9d | local |
| fc94d86f1d25 | 4 disputas «juez ve 0» en búsqueda por nombre | fila comprobada por palabras sin orden | 74f6944 | local |

Benchmark del orquestador (llamadas reales, captura de ejemplo): opus-5.5 7,1 s ✔; gpt-6-luna-pro
10,9 s ✘ (factura duplicada ⇒ «imposible»); gpt-6-luna 7,2 s ✔; gpt-5.6-luna-pro 10,7 s ✔;
sonnet-5.5 9,0 s ✔; gpt-6.1-sol 6,0 s ✔. Se mantiene Opus.
