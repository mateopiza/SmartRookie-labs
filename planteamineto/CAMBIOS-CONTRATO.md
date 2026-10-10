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
