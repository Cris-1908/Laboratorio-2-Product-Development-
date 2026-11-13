"""Modelos de pronóstico y métricas.

Hay dos cosas que se predicen por familia:
  - venta_esperada: la media de la demanda (pérdida Poisson).
  - cantidad_sugerida: el cuantil tau de la demanda, con tau = c_faltante / (c_faltante + c_sobrante).
    Es la solución clásica del problema del vendedor de periódicos: si quedarse corto
    cuesta más que sobrar (licores, tau=0.95) se sugiere más de lo esperado, y si sobrar
    cuesta más (panadería, tau=0.29) se sugiere menos.

Los dos modelos aprenden la razón ventas/nivel (nivel = promedio de las últimas 4 semanas) y
no las ventas crudas. Así una tienda grande y una express comparten forma y el modelo
se adapta mejor cuando el nivel de una tienda cambia.
"""
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

from .features import FEATURES

# nivel entra como variable más; la razón no lo contiene
COLUMNAS_MODELO = FEATURES + ["nivel"]

PARAMS_DEFECTO = {
    "max_iter": 250,
    "learning_rate": 0.05,
    "max_leaf_nodes": 15,
    "min_samples_leaf": 40,
    "l2_regularization": 1.0,
}


# ---------------------------------------------------------------- métricas
def costo_negocio(real, sugerido, c_faltante, c_sobrante) -> np.ndarray:
    real = np.asarray(real, dtype=float)
    sugerido = np.asarray(sugerido, dtype=float)
    return c_faltante * np.maximum(real - sugerido, 0) + c_sobrante * np.maximum(sugerido - real, 0)


def evaluar(df: pd.DataFrame, costos: pd.DataFrame) -> dict:
    """df trae familia_id, ventas (real), venta_esperada y cantidad_sugerida."""
    c = costos.loc[df["familia_id"], ["costo_faltante", "costo_sobrante"]].to_numpy()
    costo = costo_negocio(df["ventas"], df["cantidad_sugerida"], c[:, 0], c[:, 1])
    err = df["ventas"] - df["venta_esperada"]
    return {
        "costo_total": float(costo.sum()),
        "costo_medio": float(costo.mean()),
        "mae": float(err.abs().mean()),
        "wape": float(err.abs().sum() / max(df["ventas"].sum(), 1)),
        "sesgo": float(err.sum() / max(df["ventas"].sum(), 1)),
        "filas": int(len(df)),
    }


# ---------------------------------------------------------------- baseline
class ModeloBaseline:
    """Promedio de las últimas cuatro semanas del mismo día de la semana.

    No usa promociones ni calendario, y la cantidad sugerida es la misma media redondeada.
    Es la vara contra la que el modelo de ML tiene que ganar.
    """

    def __init__(self):
        self.media_historica: dict = {}

    def fit(self, df: pd.DataFrame):
        self.media_historica = df.groupby(["tienda_id", "familia_id"])["ventas"].mean().to_dict()
        return self

    def predict(self, df: pd.DataFrame) -> pd.DataFrame:
        base = df["media_dow"].copy()
        base = base.fillna(df["media28"])
        faltan = base.isna()
        if faltan.any():
            llaves = list(zip(df.loc[faltan, "tienda_id"], df.loc[faltan, "familia_id"]))
            base.loc[faltan] = [self.media_historica.get(k, 0.0) for k in llaves]
        return pd.DataFrame(
            {"venta_esperada": base.to_numpy(), "cantidad_sugerida": np.rint(base.to_numpy()).clip(min=0)},
            index=df.index,
        )


# ---------------------------------------------------------------- modelo de ML
class ModeloVentas:
    def __init__(self, tau: dict[str, float], params: dict | None = None):
        self.tau = dict(tau)
        self.params = dict(params or PARAMS_DEFECTO)
        self.medio: dict[str, HistGradientBoostingRegressor] = {}
        self.cuantil: dict[str, HistGradientBoostingRegressor] = {}
        self.nivel_historico: dict = {}
        self.entrenado_hasta = None  # última fecha de ventas usada para entrenar
        self.n_filas = 0
        # el modelo recuerda con qué columnas se entrenó, así uno viejo sigue sirviendo si el código cambia
        self.columnas = list(COLUMNAS_MODELO)

    def _preparar(self, df: pd.DataFrame) -> pd.DataFrame:
        x = df[self.columnas].copy()
        # si no hay nivel reciente se usa el promedio histórico de esa tienda y familia
        faltan = x["nivel"].isna()
        if faltan.any():
            llaves = list(zip(df.loc[faltan, "tienda_id"], df.loc[faltan, "familia_id"]))
            x.loc[faltan, "nivel"] = [self.nivel_historico.get(k, np.nan) for k in llaves]
        return x.astype(float)

    def fit(self, df: pd.DataFrame):
        df = df.dropna(subset=["ventas", "nivel", "en_promocion"])
        df = df[df["nivel"] > 0]
        self.n_filas = len(df)
        self.entrenado_hasta = pd.to_datetime(df["fecha"]).max().date()
        self.nivel_historico = df.groupby(["tienda_id", "familia_id"])["ventas"].mean().to_dict()

        for fam, g in df.groupby("familia_id"):
            x = g[self.columnas].astype(float)
            y = (g["ventas"] / g["nivel"]).clip(upper=10)
            cat = [self.columnas.index("tienda_id")]
            m = HistGradientBoostingRegressor(loss="poisson", categorical_features=cat, random_state=0, **self.params)
            q = HistGradientBoostingRegressor(
                loss="quantile", quantile=float(self.tau[fam]), categorical_features=cat, random_state=0, **self.params
            )
            self.medio[fam] = m.fit(x, y)
            self.cuantil[fam] = q.fit(x, y)
        return self

    def predict(self, df: pd.DataFrame) -> pd.DataFrame:
        x_all = self._preparar(df)
        esperada = pd.Series(np.nan, index=df.index)
        sugerida = pd.Series(np.nan, index=df.index)
        for fam, idx in df.groupby("familia_id").groups.items():
            x = x_all.loc[idx]
            nivel = x["nivel"].fillna(0).to_numpy()
            esperada.loc[idx] = self.medio[fam].predict(x) * nivel
            sugerida.loc[idx] = self.cuantil[fam].predict(x) * nivel
        esperada = esperada.clip(lower=0)
        sugerida = np.rint(sugerida).clip(lower=0)
        return pd.DataFrame({"venta_esperada": esperada, "cantidad_sugerida": sugerida})
