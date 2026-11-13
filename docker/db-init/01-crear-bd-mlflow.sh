#!/bin/sh
# Postgres ejecuta esto solo la primera vez que se crea el volumen.
# MLflow guarda sus metadatos en una base aparte dentro del mismo servidor del producto.
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
    CREATE DATABASE ${MLFLOW_DB_NAME};
SQL
