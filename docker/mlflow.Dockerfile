# Servidor de MLflow con PostgreSQL como almacén de metadatos.
FROM python:3.12.8-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install "mlflow==3.18.0" "psycopg2-binary==2.9.10"

RUN useradd --system --uid 10002 --create-home mlflow \
    && mkdir -p /mlflow/artifacts \
    && chown -R mlflow:mlflow /mlflow
USER mlflow
WORKDIR /mlflow

EXPOSE 5000
