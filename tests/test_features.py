"""Que el modelo no use el futuro, y que los cálculos de costo sean los del enunciado."""
import numpy as np
import pandas as pd
import pytest

from novamercado.features import construir_features, ventas_para_features
from novamercado.modelo import costo_negocio, evaluar


@pytest.fixture(scope="module")
def historico():
    h = pd.read_csv("data/historico_ventas.csv", parse_dates=["fecha"])
    return h[(h.fecha >= "2025-06-01") & (h.fecha <= "2025-09-30") & (h.tienda_id.isin([1, 5]))].copy()


def _features(ventas, fecha, cat):
    obj = pd.DataFrame({"tienda_id": [5, 5], "familia_id": ["abarrotes", "bebidas"], "fecha": pd.Timestamp(fecha), "en_promocion": [0, 3]})
    return construir_features(ventas_para_features(ventas), obj, cat)


def test_las_features_de_un_dia_no_dependen_de_ventas_de_ese_dia_ni_posteriores(historico, cat):
    fecha = pd.Timestamp("2025-09-15")
    base = _features(historico[historico.fecha < fecha], fecha, cat)
    # el mismo cálculo, pero con el futuro presente y alterado: no debe cambiar nada
    alterado = historico.copy()
    alterado.loc[alterado.fecha >= fecha, "ventas"] = 99999
    con_futuro = _features(alterado, fecha, cat)
    cols = [c for c in base.columns if c not in ("ventas",)]
    pd.testing.assert_frame_equal(base[cols].reset_index(drop=True), con_futuro[cols].reset_index(drop=True))


def test_los_rezagos_son_de_dias_anteriores(historico, cat):
    fecha = pd.Timestamp("2025-09-15")
    f = _features(historico[historico.fecha < fecha], fecha, cat)
    real = historico[(historico.tienda_id == 5) & (historico.familia_id == "abarrotes")].set_index("fecha")["ventas"]
    fila = f[f.familia_id == "abarrotes"].iloc[0]
    assert fila["lag1"] == real[fecha - pd.Timedelta(days=1)]
    assert fila["lag7"] == real[fecha - pd.Timedelta(days=7)]
    assert fila["lag28"] == real[fecha - pd.Timedelta(days=28)]


def test_el_calendario_marca_feriados_locales_solo_en_su_ciudad(cat):
    obj = pd.DataFrame({"tienda_id": [1, 8], "familia_id": "abarrotes", "fecha": pd.Timestamp("2025-08-15"), "en_promocion": 0})
    f = construir_features(pd.DataFrame(columns=["tienda_id", "familia_id", "fecha", "ventas"]), obj, cat).set_index("tienda_id")
    assert f.loc[1, "feriado_local"] == 1  # Virgen de la Asunción, Guatemala
    assert f.loc[8, "feriado_local"] == 0  # Cobán


def test_un_cierre_de_tienda_no_se_toma_como_demanda():
    h = pd.DataFrame({"fecha": pd.to_datetime(["2024-09-10"] * 2 + ["2024-09-11"] * 2), "tienda_id": 7,
                      "familia_id": ["a", "b"] * 2, "ventas": [0, 0, 5, 0]})
    v = ventas_para_features(h)
    assert v[v.fecha == "2024-09-10"]["ventas"].isna().all()
    assert v[v.fecha == "2024-09-11"]["ventas"].tolist() == [5, 0]  # un cero suelto sí es venta real


def test_costo_asimetrico_del_enunciado():
    # faltaron 10 unidades a 1.5 cada una + sobraron 4 a 2.0 cada una
    assert costo_negocio([110, 96], [100, 100], 1.5, 2.0).tolist() == [15.0, 8.0]


def test_tau_es_el_cuantil_del_vendedor_de_periodicos(cat):
    tau = cat.tau()
    assert tau["panaderia"] == pytest.approx(1 / 3.5)  # sobrar cuesta mucho más que faltar
    assert tau["licores"] == pytest.approx(2 / 2.1)  # quedarse sin licor cuesta mucho más
    assert all(0 < t < 1 for t in tau.values())


def test_evaluar_suma_el_costo_por_fila(cat):
    df = pd.DataFrame({"familia_id": ["carnes", "carnes"], "ventas": [100, 100],
                       "venta_esperada": [100.0, 100.0], "cantidad_sugerida": [90, 110]})
    r = evaluar(df, cat.costos)
    assert r["costo_total"] == pytest.approx(1.5 * 10 + 2.0 * 10)
