# DESPLIEGUE · Rookie (Fase E)

> Estado: **borrador**. Lo marcado ⏳ falta verificarlo en vivo.

## Dónde vive

| Qué | Valor |
|---|---|
| URL pública | `https://rookie.s360t.app` ⏳ (falta el Public Hostname del túnel) |
| Panel | `https://dokploy.s360t.app` → proyecto **Rookie** → app **rookie-api** (`rookie-api-3l0s0g`) |
| applicationId | `_iWbSnjYfX1QTF2COqDyD` |
| Fuente | git público `https://github.com/mateopiza/SmartRookie-labs.git`, rama `main`, build path `/rookie`, `Dockerfile` |
| Réplicas | **1** (obligatorio: el estado de tareas vive en memoria de un único proceso uvicorn) |
| Autodeploy | apagado: se despliega a propósito (ver abajo) |
| Plataforma | Dokploy (elegida por el equipo). `rookie/fly.toml` queda como alternativa sin usar. |
| Red | Cloudflare (TLS) → Cloudflare Tunnel → Traefik `web` (http) → contenedor `:8000` |

Cloudflare corta peticiones a los 100 s; nos sobra (`POST /tasks` < 2 s, `GET /tasks/{id}` inmediato).

## Variables en la plataforma

Solo estas, en Dokploy → app → *Environment* (nunca en el repo):

| Variable | Origen |
|---|---|
| `EQUIPO_TOKEN` | token del equipo (hoy = `ESCRITORIO_TOKEN` de `rookie/.env`) |
| `OPENROUTER_API_KEY` | `rookie/.env` |
| `PORT` | `8000` |

Las credenciales de despliegue (`DOKPLOY_URL`, `DOKPLOY_API_KEY`) están solo en `rookie/.env` local
(gitignoreado y excluido de la imagen por `.dockerignore`).

## Comandos

Preparación en cada terminal:

```bash
cd rookie && set -a && . ./.env && set +a && cd ..
APP=_iWbSnjYfX1QTF2COqDyD
dk(){ curl -s -X POST -H "x-api-key: $DOKPLOY_API_KEY" -H 'Content-Type: application/json' "$DOKPLOY_URL/api/$1" -d "$2"; }
```

**Comprobar**

```bash
curl -s -H "Authorization: Bearer $EQUIPO_TOKEN" https://rookie.s360t.app/health   # {"ok":true}
curl -s -o /dev/null -w '%{http_code}\n' https://rookie.s360t.app/health           # 401
```

**Redesplegar** (tras `git push` a `main`):

```bash
dk application.deploy "{\"applicationId\":\"$APP\",\"title\":\"$(git rev-parse --short HEAD)\"}"
# esperar a que /health vuelva a 200 (bucle de comprobación arriba)
```

**Logs**: panel → app → *Logs* (contenedor) y *Deployments* (build). ⏳ comando por API.

**Rollback** ⏳: `git revert <commit>` + push + redesplegar (camino seguro). Rollback nativo de Dokploy por verificar.

**Rotar variables**: panel → app → *Environment* → editar → *Save* → **Redeploy** (las variables se leen al arrancar).
Si se rota `EQUIPO_TOKEN`, avisar al evaluador / harness (`AGENTE_URL`, token).

## Healthcheck

No hay healthcheck HTTP de plataforma: `/health` sin Bearer responde 401 y marcaría el servicio
como caído. Se vigila desde fuera con el `curl` con Bearer de arriba.

## Imagen

`rookie/Dockerfile`: `python:3.14-slim`; capa de dependencias separada de la del código (un cambio
solo de código reutiliza la capa de `pip`), `uvicorn ... --workers 1`, `${PORT:-8000}`.
Probado en local: 200 con Bearer / 401 sin Bearer; la imagen no contiene `.env`.
