"""Fixtures compartidas. Nada aquí necesita MLflow, Airflow ni Postgres."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from novamercado import db
from novamercado.api import Contexto, crear_app
from novamercado.catalogos import cargar_catalogos
from novamercado.config import cargar_config
from novamercado.estado import EstadoServicio, ModeloActivo
from novamercado.orquestador import OrquestadorNoDisponible
from novamercado.predictor import Predictor

DATOS = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture(scope="session")
def cat():
    return cargar_catalogos(DATOS)


class ModeloFalso:
    """Devuelve siempre lo mismo: la media 100 y una sugerencia de 120. Basta para probar el contrato."""

    def predict(self, df):
        n = len(df)
        return pd.DataFrame({"venta_esperada": np.full(n, 100.0), "cantidad_sugerida": np.full(n, 120.0)}, index=df.index)


class OrquestadorFalso:
    """Imita a Airflow: guarda las ejecuciones que se disparan y deja fijar su estado."""

    def __init__(self):
        self.runs: dict[str, str] = {}
        self.caido = False

    def disparar(self):
        if self.caido:
            raise OrquestadorNoDisponible("Airflow no responde")
        run_id = f"api__{len(self.runs) + 1}"
        self.runs[run_id] = "running"
        return run_id

    def estado(self, run_id):
        if self.caido:
            raise OrquestadorNoDisponible("Airflow no responde")
        return self.runs.get(run_id)

    def salud(self):
        return (not self.caido), ("ok" if not self.caido else "Airflow no responde")


@pytest.fixture()
def engine(tmp_path):
    e = db.crear_engine(f"sqlite:///{tmp_path / 'prueba.db'}")
    db.crear_tablas(e)
    # un histórico mínimo: 60 días para las 10 tiendas y 12 familias, con la forma del real
    h = pd.read_csv(DATOS / "historico_ventas.csv")
    h = h[(h["fecha"] >= "2025-09-01") & (h["fecha"] <= "2025-11-16")].copy()
    h["fecha"] = pd.to_datetime(h["fecha"]).dt.date
    with e.begin() as con:
        con.execute(db.historico_ventas.insert(), h.to_dict("records"))
    return e


@pytest.fixture()
def ctx(engine, cat):
    cfg = cargar_config()
    estado = EstadoServicio()
    estado.poner_modelo(ModeloActivo(ModeloFalso(), "novamercado-ventas", "produccion", "7", "mlflow"))
    orq = OrquestadorFalso()
    return Contexto(cfg=cfg, engine=engine, cat=cat, estado=estado, predictor=Predictor(engine, cat, estado), orquestador=orq)


@pytest.fixture()
def cliente(ctx):
    return TestClient(crear_app(ctx))


def cargar_ejemplo(nombre: str) -> dict:
    return json.loads((DATOS / "ejemplos" / nombre).read_text())


@pytest.fixture()
def pred_valida():
    return cargar_ejemplo("prediccion_valida.json")


@pytest.fixture()
def obs_valida():
    return cargar_ejemplo("observacion_valida.json")
