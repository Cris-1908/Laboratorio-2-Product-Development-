# Imagen de la API y del contenedor de arranque inicial (mismo código, distinto comando).
FROM python:3.12.8-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# primero las dependencias, para que Docker reuse esta capa si solo cambia el código
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY pyproject.toml .
COPY src ./src
RUN pip install --no-deps .

# usuario sin privilegios; /cache guarda la última copia del modelo por si MLflow no está al arrancar
RUN useradd --system --uid 10001 --create-home app \
    && mkdir -p /cache \
    && chown app:app /cache
USER app

EXPOSE 8000
CMD ["uvicorn", "novamercado.api:app", "--host", "0.0.0.0", "--port", "8000"]
