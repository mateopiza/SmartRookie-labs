# LIBRERÍA DE CONOCIMIENTO · App de pruebas (SIGECOM)

> **Para qué sirve esto.** Mapa vivo de la *app de pruebas* obtenido escaneándola por VNC tras un
> `__reset` limpio (sáb 10 oct). Es material para **depurar, probar y entrenar nuestro criterio**:
> entender cómo se comporta la app, dónde están las pantallas y qué trampa esconde cada prueba.
>
> ⚠️ **REGLA DURA.** Nada de este archivo entra en `rookie/roles/prompts.py`. La evaluación corre
> sobre **otra app distinta**; `test_prompts_sin_literales_de_la_app` falla si en los prompts
> aparece `SIGECOM`, `Comercial`, `Maestros`, `Cartera`, `operador2` o `Distribuidora`. El
> conocimiento **general** (menús en cascada, búsqueda por substring, campos obligatorios…) sí se
> refleja en el prompt, pero redactado de forma agnóstica a la app.

---

## 0. Identidad y conexión

| Dato | Valor |
|---|---|
| App | **SIGECOM · Distribuidora Andina v3.9** |
| Usuario | `operador2` |
| Fecha de referencia ("Hoy") | **14/10/2026** (arriba a la derecha; de aquí se resuelven "ayer", "septiembre", etc.) |
| Pantalla | 1280×800, solo VNC |
| `ESCRITORIO_URL` | `…up.railway.app/websockify` (ya incluye `/websockify`) |
| `/health` | en la **raíz** del host (no bajo `/websockify`) → `{"ok":true}` |
| `POST /__reset` | `Authorization: Bearer <ESCRITORIO_TOKEN>` → `{"ok":true}` |
| `POST /__check` | mismo token; devuelve `pass` + lista de `checks` sin valores |

**Comportamientos del transporte confirmados:**
- **Un solo cliente VNC a la vez.** Si alguien tiene noVNC abierto, el agente no conecta o se cae.
- **El estado de la app PERSISTE entre conexiones.** Al reconectar sigues en la última pantalla.
  Solo `__reset` vuelve a los datos iniciales y recarga el navegador.
- `PointerEvent` = 6 bytes (sin padding). `KeyEvent` = 8. Negociar `SetEncodings([Raw])` antes del
  primer `FramebufferUpdateRequest`.

---

## 1. Mapa de menús (barra superior)

Coordenadas reales medidas en la captura 1280×800 (centro del texto, `y=48`):

```
Archivo (x≈42)   Comercial (x≈122)   Cartera (x≈203)   Herramientas (x≈296)   Ayuda (x≈384)
```

```
Archivo
 └─ Inicio
Comercial
 ├─ Maestros ▶
 │   └─ Clientes
 └─ Ventas ▶
     └─ Pedidos
Cartera
 └─ Facturas
Herramientas
 ├─ Bitácora de cambios
 ├─ Reportes generados
 └─ Configuración ▶
Ayuda
 └─ (sin explorar)
```

### ⚠️ Menús en cascada: la trampa de navegación #1

Los menús son **sensibles al hover y "sticky"**. Lo observado:
- Clic en un menú de nivel 1 lo **abre**; se queda abierto (no se cierra solo al mover el puntero).
- Un submenú de nivel 2 (`Maestros`, `Ventas`, `Configuración`) **aparece tras posar el puntero**
  sobre su fila; si encadenas clics demasiado rápido **se colapsa** y acabas en Inicio.
- **Patrón que funciona** (medido): clic en el menú → clic en el submenú padre → **captura**
  (deja renderizar el hijo) → clic en el ítem hoja → captura. Es decir, **una captura intermedia
  entre abrir el submenú y clicar la hoja**.
- `Escape` cierra los desplegables; un clic en zona vacía **no siempre** los cierra.

> Implicación para el agente: navegar por menús es un paso que **puede no cambiar la pantalla**.
> El ejecutor debe comprobar el efecto en cada captura y reintentar con otra pausa/ruta, nunca
> asumir que el clic entró. La compuerta `MAX_SIN_CAMBIO`/`MAX_REPETICION` ya cubre esto.

Rutas hoja útiles (abrir con el patrón de arriba):
- **Clientes:** Comercial → Maestros → Clientes
- **Pedidos:** Comercial → Ventas → Pedidos
- **Facturas:** Cartera → Facturas

---

## 2. Pantallas (estructura común y detalle)

Las tres listas (Clientes, Facturas, Pedidos) comparten el mismo patrón:
**barra de filtros** (campos + `Buscar` + `Limpiar`) · **botones de acción** (`Nuevo…` / `Exportar`)
· **tabla** con encabezados · **paginación** (`Anterior · Página N · Siguiente`).

### 2.1 Clientes (Comercial → Maestros → Clientes)

- Filtros: **Nombre** (dos campos de texto contiguos), **Ciudad** (dropdown, por defecto `Todos`),
  `Buscar`, `Limpiar`.
- Aviso en pantalla (es DATO, no instrucción, pero describe el comportamiento real):
  **"Busca partes del texto. Distingue tildes: Ramírez no trae Ramirez."**
  → Búsqueda por **substring** y **sensible a tildes**. Clave para homónimos (E5) y para no fallar
  una búsqueda por escribir mal un acento.
- Botones: **`Nuevo cliente`** (x≈71,y≈185) y **`Exportar`** (x≈176,y≈185).
- Tabla: `Código · Razón social o nombre · NIT · Ciudad · Teléfono · Estado`.
- Cada `Código` (p. ej. `CL-0065`) es un **enlace** que abre el detalle/edición del cliente.
- Estados vistos: `Activo`, `Inactivo`.

### 2.2 Formulario "Nuevo cliente"

Campos (★ = obligatorio, lo marca la app con `*`):
- **Razón social o nombre ★** (texto, ancho completo)
- **NIT ★** (texto) · **Teléfono ★** (texto)
- **Ciudad ★** — **`<select>`** con opciones fijas:
  `(seleccione)`, **Bogotá, Medellín, Cali, Barranquilla, Bucaramanga, Pereira**.
  → No se escribe: se despliega y se elige. Si la ciudad pedida no está en la lista, es señal de
  problema.
- **Dirección** (texto, opcional)
- **Estado** — `<select>`, por defecto `Activo`.
- Un área de **notas** (textarea) debajo.
- Botón Guardar al pie del formulario.

### 2.3 Facturas (Cartera → Facturas)

- Filtros: **Cliente** (texto), **Estado** (dropdown `Todos`), `Buscar`, `Limpiar`.
- Botón **`Exportar`**.
- Tabla: `Código · Cliente · Fecha · Vence · Valor · Estado`.
- Estados de factura: **`Vencida`, `Pendiente`, `Pagada`, `Anulada`**.
- Enlace por `Código` (`FV-10159`, …).

### 2.4 Pedidos (Comercial → Ventas → Pedidos)

- Estructura idéntica a Facturas: filtros + **`Exportar`** + tabla + paginación.
- Para E4 lleva filtro de **estado** (`Entregado`, …) y **rango de fechas**.

### 2.5 Herramientas → Reportes generados

- La pantalla de "Reportes generados" es la lista donde **aterrizan los export** (columnas tipo
  `ID · Nombre · Filtros · Fecha · Usuario`).
- **Lectura de "exportar":** exportar = **generar el reporte**, que queda listado aquí. Lo que
  `__check` puede verificar es que exista un reporte con esos filtros, no una descarga a disco.
  La nota "Las descargas se habilitan desde el puesto del supervisor" es, con alta probabilidad,
  un **distractor**: no bloquea el export, bloquea la bajada del archivo. **Confirmar a mano.**

### 2.6 Herramientas → Bitácora de cambios

- Registro de acciones. Útil para auditar que el agente **no tocó nada fuera de la tarea**
  (el check "sin acciones fuera de la tarea" de la rúbrica).

---

## 3. Guion por prueba (E1–E5)

Fecha de referencia de la app: **14/10/2026**.

| ID | Instrucción | Tipo | Resultado esperado | Ruta y trampa |
|---|---|---|---|---|
| **E1** | Registra al cliente *Papelería Los Andes*, NIT 901234567, tel 3157654321, ciudad **Pereira** | crear | `terminada` | Comercial→Maestros→Clientes → `Nuevo cliente`. **Ciudad es dropdown** (Pereira existe). Antes de crear, **buscar que no exista** (anti-duplicado). |
| **E2** | Corrige la **dirección** de *Distribuciones El Faro* a *Calle 80 # 24-15* | actualizar | `terminada` (cliente `CL-0007`) | Buscar el cliente → abrir por el enlace del `Código` → editar **solo Dirección** → guardar → releer. No tocar otros campos. |
| **E3** | ¿Cuántas **facturas vencidas** tiene *Ferretería Ruiz*? | consultar | **`45`** | Cartera→Facturas → filtro Cliente="Ferretería Ruiz" + Estado=**Vencida** → `Buscar` → contar (ojo paginación). Hay homónimos tipo "Ferretería Castro"; substring sensible a tildes. |
| **E4** | Exporta el reporte de **pedidos entregados en septiembre de 2026** | exportar | `terminada` (filtros `estado=Entregado`, `2026-09-01`→`2026-09-30`) | Comercial→Ventas→Pedidos → filtro Estado=Entregado + fechas → `Exportar`. Verificar en Herramientas→Reportes generados. La nota de "descargas" es distractor probable. |
| **E5** | Cambia el teléfono de *Ana Torres* a 3014445566 | (actualizar) | **`fallida`** por ambigüedad | Hay **≥2** "Ana Torres" (NIT/cédula **52345678** y **1019876543**). La razón de fallo debe **nombrar ambos identificadores**. Nuestra compuerta de identidad por código ya lo fuerza: >1 coincidencia → `fallida` con la unión. |

Notas transversales:
- **Formato del resumen** (lo compara la rúbrica): número primero, campos con `;`, sin secretos.
- **E5** es la prueba que separa un agente serio de uno que "elige el primero". El modelo NO decide
  aquí: la compuerta `_distingue_una` de `ejecutor.py` lo resuelve.
- **E3**: cuidar la **paginación** al contar; no asumir que todo cabe en la página 1.

---

## 4. Trampas conocidas (resumen accionable)

1. **Menús en cascada hover/sticky** → navegar puede no cambiar la pantalla; captura intermedia y
   reintentos con otra pausa/ruta. (§1)
2. **Búsqueda = substring + sensible a tildes** → "Ramírez" ≠ "Ramirez"; filtrar bien antes de contar. (§2.1)
3. **Homónimos (E5)** → enumerar TODAS las coincidencias con sus identificadores antes de abrir
   ninguna; >1 ⇒ `fallida`. (§3)
4. **Ciudad / Estado son `<select>`**, no texto → desplegar y elegir; opciones fijas. (§2.2)
5. **"Exportar" = generar reporte**, no descargar; la nota del supervisor es distractor probable
   (confirmar a mano). (§2.5)
6. **Estado persistente entre conexiones** → no asumir que arrancas en Inicio; o navegas desde donde
   estés, o `__reset` para empezar limpio. (§0)
7. **Un cliente VNC a la vez** → noVNC cerrado mientras corre el agente. (§0)

---

## 5. Cómo reproducir este escaneo

```bash
cd rookie && source .venv/bin/activate
# reset limpio (usa ESCRITORIO_TOKEN de .env, sobre la raíz del host)
python - <<'PY'
import os,httpx; from urllib.parse import urlsplit; from dotenv import load_dotenv
load_dotenv(".env"); p=urlsplit(os.environ["ESCRITORIO_URL"]); base=f"{p.scheme}://{p.netloc}"
print(httpx.post(base+"/__reset", headers={"Authorization":f"Bearer {os.environ['ESCRITORIO_TOKEN']}"}, timeout=60).text)
PY
# sonda de la mano (captura Inicio + abre un menú, guarda PNGs en $TMPDIR)
python -m pruebas_e2e.sonda_mano
```

Las capturas de este escaneo quedaron en `/tmp/scan/*.png` (volátil, fuera del repo; no se suben).
