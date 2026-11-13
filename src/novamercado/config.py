"""Configuración del producto. Todo sale de variables de entorno (ver .env.example)."""
import os
from dataclasses import dataclass
from pathlib import Path


def _entero(nombre: str, defecto: int) -> int:
    return int(os.environ.get(nombre, defecto))


@dataclass(frozen=True)
class Config:
    database_url: str
    data_dir: Path
    mlflow_uri: str
    modelo_nombre: str
    modelo_alias: str
    experimento: str
    airflow_url: str
    airflow_usuario: str
    airflow_clave: str
    dag_id: str
    cache_modelo_dir: Path
    # cada cuántos segundos la API revisa MLflow, Airflow y el alias vigente
    intervalo_vigilancia: int
    # reentrenamiento
    dias_validacion: int
    mejora_minima: float


def cargar_config() -> Config:
    return Config(
        database_url=os.environ.get("DATABASE_URL", "sqlite:///novamercado_dev.db"),
        data_dir=Path(os.environ.get("DATA_DIR", "data")),
        mlflow_uri=os.environ.get("MLFLOW_TRACKING_URI", "http://mlflow:5000"),
        modelo_nombre=os.environ.get("MODELO_NOMBRE", "novamercado-ventas"),
        modelo_alias=os.environ.get("MODELO_ALIAS", "produccion"),
        experimento=os.environ.get("MLFLOW_EXPERIMENTO", "novamercado-pronostico"),
        airflow_url=os.environ.get("AIRFLOW_URL", "http://airflow-webserver:8080"),
        airflow_usuario=os.environ.get("AIRFLOW_USUARIO", "admin"),
        airflow_clave=os.environ.get("AIRFLOW_CLAVE", "admin"),
        dag_id=os.environ.get("AIRFLOW_DAG_ID", "reentrenar_modelo"),
        cache_modelo_dir=Path(os.environ.get("CACHE_MODELO_DIR", "/tmp/novamercado_modelo")),
        intervalo_vigilancia=_entero("INTERVALO_VIGILANCIA", 15),
        dias_validacion=_entero("DIAS_VALIDACION", 7),
        mejora_minima=float(os.environ.get("MEJORA_MINIMA", 0.01)),
    )
