FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml ./
COPY rookie/ rookie/
COPY config/ config/
RUN pip install --no-cache-dir .

# memoria/ es un volumen persistente: libros por app + bitácora SQLite
VOLUME ["/app/memoria"]

EXPOSE 8000
CMD ["uvicorn", "rookie.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
