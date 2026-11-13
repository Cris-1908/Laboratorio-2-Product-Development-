"""Todo lo que toca MLflow y el Model Registry.

Los imports de mlflow están dentro de las funciones: importarlo tarda varios segundos y las
pruebas del contrato no deberían necesitarlo.
"""
import json
import os
from dataclasses import dataclass
from pathlib import Path

import joblib

# sin esto, si MLflow se cae el cliente reintenta durante minutos y la API se queda colgada
os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "1")
os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "10")

from .config import Config  # noqa: E402


@dataclass
class VersionModelo:
    nombre: str
    alias: str
    version: str


class SinModeloVigente(Exception):
    pass


def _cliente(cfg: Config):
    from mlflow import MlflowClient

    return MlflowClient(tracking_uri=cfg.mlflow_uri, registry_uri=cfg.mlflow_uri)


def version_vigente(cfg: Config) -> VersionModelo:
    """La versión que hoy tiene el alias de producción. Falla si MLflow no responde o no hay alias."""
    from mlflow.exceptions import MlflowException

    try:
        mv = _cliente(cfg).get_model_version_by_alias(cfg.modelo_nombre, cfg.modelo_alias)
    except MlflowException as e:
        if "RESOURCE_DOES_NOT_EXIST" in str(e) or "not found" in str(e).lower() or "does not exist" in str(e).lower():
            raise SinModeloVigente(str(e)) from e
        raise
    return VersionModelo(cfg.modelo_nombre, cfg.modelo_alias, str(mv.version))


def cargar_modelo(cfg: Config, version: str):
    """Baja la versión del registry y devuelve el objeto ModeloVentas."""
    import mlflow

    mlflow.set_tracking_uri(cfg.mlflow_uri)
    mlflow.set_registry_uri(cfg.mlflow_uri)
    cargado = mlflow.pyfunc.load_model(f"models:/{cfg.modelo_nombre}/{version}")
    return cargado.unwrap_python_model().modelo


def poner_alias(cfg: Config, version: str) -> None:
    _cliente(cfg).set_registered_model_alias(cfg.modelo_nombre, cfg.modelo_alias, str(version))


def registrar_modelo(cfg: Config, modelo) -> str:
    """Guarda el modelo en el run activo y crea una nueva versión en el registry. Devuelve la versión."""
    import mlflow.pyfunc

    from .envoltura import EnvolturaModelo

    info = mlflow.pyfunc.log_model(
        name="modelo",
        python_model=EnvolturaModelo(modelo),
        registered_model_name=cfg.modelo_nombre,
        pip_requirements=["scikit-learn", "pandas", "numpy", "cloudpickle"],
    )
    return str(info.registered_model_version)


# ------------------------------------------------------------ caché local de respaldo
def guardar_cache(cfg: Config, version: str, modelo) -> None:
    cfg.cache_modelo_dir.mkdir(parents=True, exist_ok=True)
    tmp = cfg.cache_modelo_dir / "modelo.joblib.tmp"
    joblib.dump(modelo, tmp)
    os.replace(tmp, cfg.cache_modelo_dir / "modelo.joblib")
    (cfg.cache_modelo_dir / "version.json").write_text(json.dumps({"version": version}))


def leer_cache(cfg: Config) -> tuple[str, object] | None:
    ruta = cfg.cache_modelo_dir / "modelo.joblib"
    meta = cfg.cache_modelo_dir / "version.json"
    if not ruta.exists() or not meta.exists():
        return None
    try:
        return json.loads(meta.read_text())["version"], joblib.load(ruta)
    except Exception:
        return None
