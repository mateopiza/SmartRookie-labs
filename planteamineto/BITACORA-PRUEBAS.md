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

Notas:
- Un tercero (otro agente de código, «pi», en una terminal) manda tareas a :8077 sobre el mismo
  escritorio (c5db6fbec34a 14:27, 1c703316ea20 14:38). Reportado al usuario.
- El formulario de un alta guardada se queda abierto con `Código: CL-0141`; NO es un borrador
  (antes de guardar el formulario «Nuevo cliente» no tiene código). `__check` sin duplicados ✔.
- Nunca usar `pkill -f` con un patrón que aparezca en la propia línea de comando: mata el shell.
