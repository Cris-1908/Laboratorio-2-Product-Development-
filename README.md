# NovaMercado: servicio de pronóstico de ventas

Servicio que recibe, por tienda y día, las promociones planeadas por familia y responde cuánto se espera vender y cuánto conviene surtir. Guarda todo lo que recibe, reentrena el modelo con Airflow y lo registra en MLflow.

Las decisiones de diseño están en [DECISIONES.md](DECISIONES.md).

## Qué levanta `docker compose`

| Servicio | Para qué | Puerto |
|---|---|---|
| `api` | FastAPI con el contrato del laboratorio | 8000 |
| `db` | PostgreSQL del producto (y metadatos de MLflow) | — |
| `mlflow` | Seguimiento de experimentos y Model Registry | 5000 |
| `init` | Corre una vez: crea tablas, carga el histórico y entrena el primer modelo | — |
| `airflow-webserver`, `airflow-scheduler` | DAG `reentrenar_modelo` | 8080 |
| `airflow-db` | PostgreSQL de Airflow, separado del del producto | — |

## Cómo levantarlo desde cero

Hace falta Docker con Compose v2.

```bash
cp .env.example .env          # y cambie las tres claves y la cadena secreta
# copie a ./data los CSV publicados (historico_ventas.csv, tiendas.csv, familias.csv, costos.csv, feriados.csv)
docker compose up --build
```

Los datos y las credenciales no están en el repositorio (`data/` y `.env` están en `.gitignore`).

Cuando `init` termine (menos de un minuto de entrenamiento, más lo que tarde en construir las imágenes la primera vez):

```bash
curl localhost:8000/health
curl localhost:8000/modelo
```

`/health` debe responder `{"estado":"ok", ...}`. Airflow queda en http://localhost:8080 con el usuario y clave de `.env`, y MLflow en http://localhost:5000.

Para apagar sin perder nada: `docker compose down`. Para borrar todo y empezar de cero: `docker compose down -v`.

## Probarlo a mano

```bash
# pronóstico
curl -s localhost:8000/v1/prediccion -H 'content-type: application/json' -d '{
  "tienda_id": 5, "fecha": "2025-11-17",
  "familias": [{"familia_id": "lacteos", "en_promocion": 2}, {"familia_id": "panaderia", "en_promocion": 0}]
}'

# observaciones de ventas (se pueden reenviar sin duplicar)
curl -s localhost:8000/v1/observaciones -H 'content-type: application/json' -d '{
  "tienda_id": 5, "fecha": "2025-11-17",
  "ventas": [{"familia_id": "lacteos", "ventas": 812}]
}'

# reentrenar (responde 202 con un id) y consultar el resultado
curl -s -X POST localhost:8000/admin/reentrenar
curl -s localhost:8000/admin/reentrenar/<id>
```

Documentación interactiva: http://localhost:8000/docs.

Los ids de familia y los nombres de campo exactos son los del Anexo del enunciado; si algún ejemplo de arriba no coincide con su versión, la fuente de verdad es `src/novamercado/contrato.py`.

## Pruebas y simulador

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest                         # 134 pruebas; no necesitan Docker, MLflow ni Airflow
```

`scripts/simular_operacion.py` manda varios días de tráfico con defectos de calidad y cambios de demanda a una API que ya esté corriendo, y reentrena por HTTP. Mire `--help`.

## Estructura

```
src/novamercado/
  api.py             endpoints y manejo de errores
  contrato.py        modelos Pydantic de entrada y salida
  limpieza.py        validación, reparación, advertencias y rechazos
  catalogos.py       tiendas, familias, costos y feriados
  features.py        variables del modelo (sin fuga de información)
  modelo.py          baseline, modelo de ML y costo de negocio
  predictor.py       arma el pronóstico de una solicitud
  repositorio.py     todo el acceso a la base
  db.py              tablas
  registro_modelos.py, envoltura.py   MLflow
  estado.py          vigilante: salud y cambio de modelo sin reiniciar
  orquestador.py     cliente de Airflow
  reentrenar.py      pasos del DAG
  bootstrap.py       arranque inicial
dags/reentrenar_modelo.py
docker/              Dockerfiles y script de inicialización de la base
tests/
scripts/
```

## Si algo falla

- **`init` se queda esperando**: mire `docker compose logs mlflow db`. Casi siempre es que falta algún CSV en `./data` o que una clave de `.env` tiene caracteres raros; use solo letras y números.
- **`/health` responde 503**: todavía no hay modelo cargado. Revise `docker compose logs init`.
- **`/health` dice `degradado`**: el campo `motivo` dice qué servicio no responde. La API sigue prediciendo con el modelo que ya tiene.
- **Cambió `.env` y no pasa nada**: `docker compose up -d --force-recreate`.
- **MLflow responde 403**: el host con el que se le llama no está en `--allowed-hosts` del compose.
