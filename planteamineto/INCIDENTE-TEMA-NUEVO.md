# Incidente · la app cambió de tema (10-oct-2026)

Guion corto para el video y el debrief: qué cambió, cómo lo detectamos, qué adaptamos y con qué
evidencia. El detalle hora a hora está en `BITACORA-PRUEBAS.md` (Fase H2).

## Qué cambió en la app

| Cambio anunciado | Cómo lo vio el agente | ¿Rompió algo? |
|---|---|---|
| Botones movidos de lugar (p. ej. Exportar) | El ejecutor decide cada clic leyendo la captura con un modelo de visión: no hay coordenadas guardadas ni selectores | No |
| Etiquetas distintas (p. ej. «Filtrar» en vez de «Buscar») | Los prompts no tienen literales de la app (un test lo vigila); las reglas hablan de «el botón que aplica los filtros» | No |
| Modal nuevo al abrir («Novedades v4.2») | Regla genérica: un aviso informativo se cierra con su botón (Entendido / Aceptar / Cerrar / X); una confirmación de un cambio no autorizado se cancela. En la corrida, el primer paso del agente fue cerrar el aviso | No |
| **No anunciado:** el teclado especial dejó de responder | `ctrl+a`, `BackSpace`, flechas y `Tab` no hacen nada; `Delete` mete un carácter basura (□) | **Sí**: cada escritura quedaba concatenada |

## Cómo lo detectamos

1. **Corrida completa E1–E5** en cuanto supimos del cambio. El agente se orientó solo en la interfaz
   nueva (cerró el modal, encontró el módulo por el menú), pero E1 terminó fallida a los 249 s.
2. **Observaciones paso a paso** (`rookie/banco/observaciones/<task_id>/`): cada paso guarda la
   captura, la acción y la razón del modelo. El ejecutor mismo lo dejó escrito: «el campo muestra un
   carácter extraño antes de 'Papelería Los Andes'… repito la escritura». 15 de 24 pasos se fueron
   en intentar vaciar un campo.
3. **Sonda aislada** (agente parado, un solo cliente VNC): en el campo de búsqueda probamos cada
   tecla por separado y con pulsación sostenida. Resultado: ninguna tecla especial tiene efecto y
   `Delete` inserta basura; el ratón sí funciona y el **triple clic** selecciona todo el input.

## Cómo adaptamos el agente

- `escribir(x, y)`: triple clic en el campo + texto, en la misma primitiva. Reemplaza el valor viejo
  sin depender del teclado (verificado en el escritorio real con espacios, `#` y tildes).
- `Delete` sale de la lista blanca de teclas (todo lo demás que no sea enter, escape, tab,
  shift+tab, ctrl+a, backspace o flechas ya se rechazaba por código).
- Prompt del ejecutor: «el teclado puede no funcionar; si una tecla no cambia la pantalla, usa el
  ratón». Nada específico de esta app: sirve para las 5 apps del cliente.
- Las compuertas siguen intactas: el juez revisa el formulario antes de guardar (un valor
  concatenado no se guarda) y, después de guardar, comprueba que la pantalla muestra el registro
  correcto.

## Por qué el agente aguanta este tipo de cambio

- **Percepción por visión, no por estructura**: no leemos el DOM ni guardamos posiciones; cada paso
  mira la pantalla actual.
- **Reglas genéricas**: diálogos, menús en cascada, búsquedas parciales y campos exigidos se
  describen por su función, no por su texto.
- **Instrumentación**: cada paso queda grabado con su razón; diagnosticar fue leer, no adivinar.
- **Seguridad primero**: ante la duda, `fallida` con los datos que lo explican; nunca un falso éxito.

## Resultados tras la adaptación

(se completa con las corridas posteriores)
