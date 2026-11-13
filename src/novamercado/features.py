"""Construcción de variables para el modelo.

La regla que más importa aquí: para pronosticar el día D solo se usan ventas de
días anteriores a D. Las promociones y el calendario de D sí se conocen.
El mismo código se usa para entrenar (todas las fechas) y para predecir
(una fecha con unos 60 días de historia), así no hay dos versiones que se desalineen.
"""
import warnings
from datetime import date, timedelta

import numpy as np
import pandas as pd

from .catalogos import Catalogos

# días de rezago que se miran; todos son >= 1 para no tocar el día a predecir
LAGS = (1, 7, 14, 21, 28)
HISTORIA_MINIMA_DIAS = 60  # lo que necesita la API para armar los rezagos y las medias
# ventana (en días) con la que se mide el "nivel" de una serie; las razones se calculan contra él
VENTANA_NIVEL = 14

FEATURES = [
    "tienda_id", "en_promocion",
    "dow", "dom", "mes", "dia_anio", "dias_a_fin_mes", "edad_tienda",
    "feriado_nacional", "feriado_local", "medio_dia", "comercial", "pago",
    "dias_a_feriado", "dias_desde_feriado",
    "lag1", "lag7", "lag14", "lag21", "lag28", "media7", "media14", "media28", "media56", "media_dow",
    "r_lag1", "r_lag7", "r_media7", "r_media14", "r_media28", "r_media56", "r_media_dow",
]


def _calendario(fechas: pd.DatetimeIndex, tiendas: pd.DataFrame, cat: Catalogos) -> pd.DataFrame:
    """Variables que dependen solo de la fecha y de la tienda (ciudad y apertura)."""
    fer = cat.feriados.copy()
    fer["fecha"] = pd.to_datetime(fer["fecha"])
    filas = []
    for _, t in tiendas.iterrows():
        aplica = fer[(fer["ambito"] == "nacional") | (fer["ambito"] == t["ciudad"])]
        feriados_dias = aplica[aplica["tipo"] == "feriado"]["fecha"].sort_values().to_numpy()

        d = pd.DataFrame({"fecha": fechas})
        d["tienda_id"] = int(t["tienda_id"])
        d["dow"] = d["fecha"].dt.dayofweek
        d["dom"] = d["fecha"].dt.day
        d["mes"] = d["fecha"].dt.month
        d["dia_anio"] = d["fecha"].dt.dayofyear
        d["dias_a_fin_mes"] = d["fecha"].dt.days_in_month - d["dom"]
        apertura = pd.Timestamp(t["fecha_apertura"])
        d["edad_tienda"] = np.clip((d["fecha"] - apertura).dt.days, 0, 365)

        for nombre, tipo in (("medio_dia", "medio_dia"), ("comercial", "comercial"), ("pago", "pago")):
            dias = set(aplica[aplica["tipo"] == tipo]["fecha"])
            d[nombre] = d["fecha"].isin(dias).astype(int)
        d["feriado_nacional"] = d["fecha"].isin(
            set(fer[(fer["ambito"] == "nacional") & (fer["tipo"] == "feriado")]["fecha"])
        ).astype(int)
        d["feriado_local"] = d["fecha"].isin(
            set(fer[(fer["ambito"] == t["ciudad"]) & (fer["tipo"] == "feriado")]["fecha"])
        ).astype(int)

        # distancia al feriado más cercano hacia adelante y hacia atrás (tope de 7)
        v = d["fecha"].to_numpy()
        a_feriado, desde_feriado = [], []
        for dia in v:
            pos = np.searchsorted(feriados_dias, dia, side="left")
            sig = (feriados_dias[pos] - dia).astype("timedelta64[D]").astype(int) if pos < len(feriados_dias) else 99
            pos2 = np.searchsorted(feriados_dias, dia, side="right") - 1
            ant = (dia - feriados_dias[pos2]).astype("timedelta64[D]").astype(int) if pos2 >= 0 else 99
            a_feriado.append(min(sig, 7))
            desde_feriado.append(min(ant, 7))
        d["dias_a_feriado"] = a_feriado
        d["dias_desde_feriado"] = desde_feriado
        filas.append(d)
    return pd.concat(filas, ignore_index=True)


def construir_features(ventas: pd.DataFrame, objetivos: pd.DataFrame, cat: Catalogos) -> pd.DataFrame:
    """Devuelve una fila por objetivo con todas las variables de FEATURES.

    ventas:    tienda_id, familia_id, fecha, ventas (NaN donde no se confía en el dato)
    objetivos: tienda_id, familia_id, fecha, en_promocion
    """
    objetivos = objetivos.copy()
    objetivos["fecha"] = pd.to_datetime(objetivos["fecha"])
    ventas = ventas.copy()
    ventas["fecha"] = pd.to_datetime(ventas["fecha"])
    ventas["ventas"] = pd.to_numeric(ventas["ventas"], errors="coerce").astype(float)

    desde = min(ventas["fecha"].min(), objetivos["fecha"].min()) if len(ventas) else objetivos["fecha"].min()
    hasta = objetivos["fecha"].max()
    rango = pd.date_range(desde, hasta, freq="D")

    # solo calculamos las series que realmente se piden
    pedidas = pd.MultiIndex.from_frame(objetivos[["tienda_id", "familia_id"]].drop_duplicates())
    if ventas.empty:
        ancho = pd.DataFrame(np.nan, index=rango, columns=pedidas)
    else:
        ancho = ventas.pivot_table(index="fecha", columns=["tienda_id", "familia_id"], values="ventas", aggfunc="last")
        ancho = ancho.reindex(index=rango, columns=pedidas)

    piezas = {}
    for k in LAGS:
        piezas[f"lag{k}"] = ancho.shift(k)
    ayer = ancho.shift(1)
    piezas["media7"] = ayer.rolling(7, min_periods=3).mean()
    piezas["media14"] = ayer.rolling(14, min_periods=5).mean()
    piezas["media28"] = ayer.rolling(28, min_periods=7).mean()
    piezas["media56"] = ayer.rolling(56, min_periods=14).mean()
    mismo_dia = np.stack([ancho.shift(k).to_numpy() for k in (7, 14, 21, 28)])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # columnas sin ningún dato dan 'mean of empty slice'
        media_dow = np.nanmean(mismo_dia, axis=0)
    piezas["media_dow"] = pd.DataFrame(media_dow, index=ancho.index, columns=ancho.columns)

    largo = None
    for nombre, tabla in piezas.items():
        t = tabla.stack([0, 1], future_stack=True).rename(nombre).reset_index()
        t.columns = ["fecha", "tienda_id", "familia_id", nombre]
        largo = t if largo is None else largo.merge(t, on=["fecha", "tienda_id", "familia_id"], how="left")

    df = objetivos.merge(largo, on=["fecha", "tienda_id", "familia_id"], how="left")

    # razones respecto al nivel de las últimas 4 semanas: hacen al modelo menos dependiente de la escala
    nivel = df[f"media{VENTANA_NIVEL}"].fillna(df["media28"]).fillna(df["media7"])
    df["nivel"] = nivel
    for nombre, col in (("r_lag1", "lag1"), ("r_lag7", "lag7"), ("r_media7", "media7"), ("r_media14", "media14"),
                        ("r_media28", "media28"), ("r_media56", "media56"), ("r_media_dow", "media_dow")):
        df[nombre] = df[col] / nivel.replace(0, np.nan)

    cal = _calendario(rango, cat.tiendas[cat.tiendas["tienda_id"].isin(objetivos["tienda_id"].unique())], cat)
    df = df.merge(cal, on=["fecha", "tienda_id"], how="left")
    return df


def ventas_para_features(historico: pd.DataFrame) -> pd.DataFrame:
    """Marca como NaN los días en que una tienda reporta cero en todas sus familias.

    Eso es un cierre (o una caída del sistema), no demanda: si se dejara como cero
    el modelo aprendería que a veces no se vende nada.
    """
    h = historico.copy()
    total = h.groupby(["tienda_id", "fecha"])["ventas"].transform("sum")
    h.loc[total == 0, "ventas"] = np.nan
    return h


def ultimo_dia_con_datos(ventas: pd.DataFrame, tienda_id: int, antes_de: date) -> date | None:
    v = ventas[(ventas["tienda_id"] == tienda_id) & (pd.to_datetime(ventas["fecha"]) < pd.Timestamp(antes_de))]
    v = v.dropna(subset=["ventas"])
    if v.empty:
        return None
    return pd.Timestamp(v["fecha"].max()).date()


def ventana_historia(fecha: date, dias: int = HISTORIA_MINIMA_DIAS + 25) -> tuple[date, date]:
    """Rango de fechas que hay que leer de la base para predecir `fecha`."""
    return fecha - timedelta(days=dias), fecha - timedelta(days=1)
