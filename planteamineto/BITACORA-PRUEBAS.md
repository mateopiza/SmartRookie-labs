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
