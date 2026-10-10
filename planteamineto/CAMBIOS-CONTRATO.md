# Cambios al contrato §2

Cada entrada: fecha y hora, fase, qué cambia, por qué y si rompe algo.

## 2026-10-10 · Fase B · `Ejecutor` puede devolver también el plan (aditivo, no rompe nada)

- **Qué:** `Motor` acepta que el ejecutor devuelva `Resultado` (como dice §2) **o** la tupla
  `(Resultado, Plan | None)`.
- **Por qué:** `armar_resumen(resultado, plan, secretos)` necesita el plan para poner el número
  primero (`pregunta.formato == "numero"`), separar dos datos con `;` y nombrar el módulo. El
  Motor solo recibe lo que devuelve el ejecutor, así que sin esto el resumen se arma con
  `plan=None`: la respuesta se escribe tal cual y el «Hecho:» sale de `resultado.detalle`.
- **Para la fase D:** `ejecutar_tarea` puede seguir devolviendo solo `Resultado`. Si devuelve
  `(resultado, plan)`, el formato de la consulta queda garantizado por código. Con `plan=None`,
  escribe en `resultado.respuesta` el número primero y dos datos ya unidos con `"; "`, y en
  `resultado.detalle` qué se hizo y dónde.
- **Plazo:** el `deadline` que recibe el ejecutor cuenta desde `POST /tasks`, no desde que se
  obtiene el lock del escritorio, porque el evaluador corta a los 5 minutos del POST.

## 2026-10-10 14:25 · Fase H · `Accion.coincidencias`: esquema JSON con campos fijos (no rompe nada)

- **Qué:** el tipo Python sigue siendo `list[dict[str, str]] | None`, pero el esquema JSON que se
  envía al modelo anuncia cada fila como `{nombre, identificador, texto}` (`additionalProperties:
  false`) vía `WithJsonSchema`.
- **Por qué:** con `dict[str, str]` el modo `strict` de OpenRouter/Anthropic devolvía `[{}]`
  (objetos vacíos): la compuerta de identidad nunca veía nombres ni documentos, el juez marcaba
  «identidad en disputa» siempre y el anti-duplicado de un alta no podía funcionar (visto en E1 sin
  reset). El ejecutor rechaza ahora filas vacías.
- **Para el juez:** `_misma_fila` y `_id_de_coincidencia` siguen funcionando (valores string; el
  `texto` lleva la fila completa con el documento).

## 2026-10-10 15:15 · Fase H · `Accion.compuerta` admite `"lectura"` (aditivo)

- **Qué:** nuevo valor `"lectura"`: la captura actual trae datos usados para responder una
  consulta (una página contada). No es crítica: no pasa por el juez de compuertas.
- **Por qué:** un conteo repartido en páginas no se verifica con una sola captura. El ejecutor
  junta las páginas marcadas, el verificador las ve todas y `juez.verificar_conteo` cuenta en cada
  una y suma identificadores únicos. Se probó un campo booleano aparte (`evidencia`), pero con un
  opcional más el modo strict del proveedor dejaba de emitir `resultado` en `terminar`.

## 2026-10-10 15:34 · Fase H · `Plan.filtros` e `identificacion`: esquema de pares (no rompe nada)

- **Qué:** en Python siguen siendo `dict[str, str]`; el esquema JSON que ve el modelo es una lista
  de `{clave, valor}` y un `BeforeValidator` la convierte a dict (un dict directo sigue valiendo).
- **Por qué:** igual que `coincidencias`, en modo strict un dict de claves libres salía vacío. En
  E4 el plan llegó con `filtros={}` y el juez rechazaba la exportación («hay otros filtros»).

## 2026-10-10 16:24 · Fase H2 · `GET /tasks` lista las tareas en curso (aditivo, no rompe nada)

- **Qué:** nueva ruta autenticada `GET /tasks` → `{"en_curso": ["<task_id>", …]}` (`Motor.en_curso()`).
  `/health`, `POST /tasks` y `GET /tasks/{id}` no cambian.
- **Por qué:** en la Fase H otro cliente mandaba tareas al mismo agente local y contaminaba las
  pruebas. El harness consulta esta ruta antes de cada prueba y aborta (exit 2) si hay tareas en
  curso ajenas. Contra un agente sin la ruta (404) avisa y sigue.
