# CLAUDE.md

Servicio de pronóstico de ventas de NovaMercado (laboratorio 2). Ver `README.md` para levantarlo y `DECISIONES.md` para el porqué de cada cosa.

## Comandos

```bash
pip install -r requirements-dev.txt
pytest                                   # sin Docker, sin MLflow, sin Airflow
uvicorn novamercado.api:app --reload     # necesita DATABASE_URL, MLFLOW_TRACKING_URI y DATA_DIR
docker compose config                    # valida el compose (necesita .env)
docker compose up --build
python scripts/simular_operacion.py --help
```

## Estructura

- `contrato.py` modelos de entrada y salida. `limpieza.py` convierte lo crudo en datos limpios o rechaza.
- `features.py` y `modelo.py` son puros (sin red ni base). `repositorio.py` es el único que habla SQL.
- `registro_modelos.py` es el único que habla con MLflow. `orquestador.py` el único que habla con Airflow.
- `estado.py` tiene el hilo vigilante: revisa dependencias y cambia de modelo cuando cambia el alias.
- `reentrenar.py` son los pasos que ejecuta el DAG (`entrenar`, `decidir`, `cerrar`). La regla de reemplazo es `decidir()`, una función pura.

## Reglas que no se rompen

1. **Nada de reloj del sistema** en validaciones. Los límites salen de los datos (inicio del histórico, apertura de la tienda, fin de `feriados.csv`).
2. **Sin fuga temporal**: las variables del día D solo usan ventas anteriores a D. `tests/test_features.py` lo comprueba; si cambia `features.py`, esa prueba debe seguir pasando.
3. **Las pruebas no tocan MLflow ni Airflow**: usan `ModeloFalso` y `OrquestadorFalso` de `tests/conftest.py` y SQLite.
4. Todo defecto de datos nuevo se clasifica como reparar, advertir o rechazar (ver tabla en `DECISIONES.md`), con prueba en `test_limpieza.py`, y se documenta ahí.
5. El alias del modelo solo se mueve en `decidir_promocion`.
6. Nombres de dominio en español, como en el contrato.

## Cosas que muerden

- Los imports de `mlflow` van dentro de funciones: importarlo tarda y las pruebas no lo necesitan.
- `EnvolturaModelo` vive en `envoltura.py` a propósito; si se define dentro de una función, cloudpickle la serializa por valor y se rompe entre versiones de Python.
- MLflow 3 devuelve 403 si el header `Host` no está en `--allowed-hosts`.
- La tabla de observaciones se escribe con upsert; hay variantes SQLite/Postgres en `db.py` y `repositorio.py` (JSON vs JSONB, BigInteger vs Integer). Cambios de SQL se prueban en ambos dialectos.
- `TestClient` solo ejecuta el `lifespan` si se usa como `with TestClient(app)`.
- No use `pkill -f 'mlflow server'` en scripts: el patrón aparece en su propia línea de comandos y mata el shell.
- Claves de `.env`: solo letras y números, van dentro de URLs de conexión.
