FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 1) Solo dependencias: esta capa se reutiliza mientras pyproject.toml no cambie
#    (así el redespliegue tras cambiar código tarda segundos, no minutos).
COPY pyproject.toml ./
RUN mkdir rookie && touch rookie/__init__.py \
    && pip install . \
    && pip uninstall -y rookie && rm -rf rookie

# 2) El código del agente.
COPY rookie/ rookie/
COPY config/ config/
RUN pip install --no-deps .

EXPOSE 8000
# Un solo worker: el estado de las tareas vive en memoria del proceso; con más,
# GET /tasks/{id} caería en otro proceso y no encontraría la tarea.
CMD ["sh", "-c", "exec uvicorn rookie.api.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
