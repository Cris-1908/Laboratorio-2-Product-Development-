"""Pruebas de las políticas de limpieza. Son funciones puras: no hay servidor ni base."""
from datetime import date

import pytest

from novamercado.limpieza import Rechazo, limpiar_observacion, limpiar_prediccion, parsear_numero

from .conftest import cargar_ejemplo


def acciones(obs):
    return {o.accion for o in obs}


def campos(obs):
    return [o.campo for o in obs]


# ---------------------------------------------------------------- predicción válida y catálogo E01-E07
def test_solicitud_valida_no_genera_observaciones(cat, pred_valida):
    sol, obs = limpiar_prediccion(pred_valida, cat)
    assert obs == []
    assert sol.tienda_id == 5 and sol.fecha == date(2025, 11, 17)
    assert len(sol.familias) == 12


@pytest.mark.parametrize("valor", ["T05", "5", " 05 ", "tienda-5", 5.0, "5.0"])
def test_tienda_con_otro_formato_se_repara(cat, pred_valida, valor):
    pred_valida["tienda_id"] = valor
    sol, obs = limpiar_prediccion(pred_valida, cat)
    assert sol.tienda_id == 5
    assert acciones(obs) == {"reparar"}


@pytest.mark.parametrize("valor", ["T99", "abc", None, True, 5.5, 99, ""])
def test_tienda_inexistente_o_ilegible_se_rechaza(cat, pred_valida, valor):
    pred_valida["tienda_id"] = valor
    with pytest.raises(Rechazo):
        limpiar_prediccion(pred_valida, cat)


@pytest.mark.parametrize("valor", ["LÁCTEOS", "lacteos ", "Lácteos", "LACTEOS"])
def test_familia_con_otra_forma_se_repara(cat, pred_valida, valor):
    pred_valida["familias"][6]["familia_id"] = valor
    sol, obs = limpiar_prediccion(pred_valida, cat)
    assert "lacteos" in [f.familia_id for f in sol.familias]
    assert acciones(obs) == {"reparar"}
    assert campos(obs) == ["familias[6].familia_id"]


def test_familia_por_su_nombre_largo(cat, pred_valida):
    pred_valida["familias"][5]["familia_id"] = "Frutas y Verduras"
    sol, obs = limpiar_prediccion(pred_valida, cat)
    assert "frutas_verduras" in [f.familia_id for f in sol.familias]
    assert acciones(obs) == {"reparar"}


def test_familia_con_error_de_dedo_se_acepta_con_advertencia(cat, pred_valida):
    pred_valida["familias"][6]["familia_id"] = "lacteoz"
    sol, obs = limpiar_prediccion(pred_valida, cat)
    assert "lacteos" in [f.familia_id for f in sol.familias]
    assert acciones(obs) == {"advertir"}


def test_familia_desconocida_se_ignora_con_advertencia(cat, pred_valida):
    pred_valida["familias"].append({"familia_id": "electronica", "en_promocion": 3})
    sol, obs = limpiar_prediccion(pred_valida, cat)
    assert len(sol.familias) == 12
    assert acciones(obs) == {"advertir"}


def test_si_ninguna_familia_se_reconoce_se_rechaza(cat, pred_valida):
    pred_valida["familias"] = [{"familia_id": "xyz", "en_promocion": 1}]
    with pytest.raises(Rechazo):
        limpiar_prediccion(pred_valida, cat)


@pytest.mark.parametrize("valor,esperado", [("3", 3), (2.0, 2), ("3.0", 3), (" 4 ", 4), ("5,0", 5)])
def test_promocion_como_texto_o_decimal_se_repara(cat, pred_valida, valor, esperado):
    pred_valida["familias"][2]["en_promocion"] = valor
    sol, obs = limpiar_prediccion(pred_valida, cat)
    assert next(f for f in sol.familias if f.familia_id == "bebidas").en_promocion == esperado
    assert acciones(obs) == {"reparar"}


@pytest.mark.parametrize("valor", [-3, "mucho", None, 2.5, 10**9])
def test_promocion_inservible_se_asume_cero_con_advertencia(cat, pred_valida, valor):
    pred_valida["familias"][2]["en_promocion"] = valor
    sol, obs = limpiar_prediccion(pred_valida, cat)
    assert "advertir" in acciones(obs)


def test_promocion_decimal_no_entero_se_redondea_con_advertencia(cat, pred_valida):
    pred_valida["familias"][2]["en_promocion"] = 2.6
    sol, obs = limpiar_prediccion(pred_valida, cat)
    assert next(f for f in sol.familias if f.familia_id == "bebidas").en_promocion == 3
    assert acciones(obs) == {"advertir"}


@pytest.mark.parametrize("texto", ["2025-11-17T00:00:00", "17/11/2025", "2025-11-17 08:30:00", "17-11-2025",
                                   "2025/11/17", "20251117", "17 de noviembre de 2025", "11/17/2025",
                                   "2025-11-17T00:00:00Z"])
def test_fechas_en_otro_formato_se_reparan(cat, pred_valida, texto):
    pred_valida["fecha"] = texto
    sol, obs = limpiar_prediccion(pred_valida, cat)
    assert sol.fecha == date(2025, 11, 17)
    assert acciones(obs) == {"reparar"}


def test_fecha_ambigua_se_lee_dia_mes_con_advertencia(cat, pred_valida):
    pred_valida["fecha"] = "03/04/2025"
    sol, obs = limpiar_prediccion(pred_valida, cat)
    assert sol.fecha == date(2025, 4, 3)
    assert acciones(obs) == {"advertir"}


def test_fecha_con_dia_y_mes_iguales_no_es_ambigua(cat, pred_valida):
    pred_valida["fecha"] = "05/05/2025"
    sol, obs = limpiar_prediccion(pred_valida, cat)
    assert acciones(obs) == {"reparar"}


@pytest.mark.parametrize("texto", ["2025-02-30", "ayer", "2025-13-01", "", None, 20251117, "17/11/25", "31/04/2025"])
def test_fecha_ilegible_se_rechaza(cat, pred_valida, texto):
    pred_valida["fecha"] = texto
    with pytest.raises(Rechazo):
        limpiar_prediccion(pred_valida, cat)


def test_fecha_anterior_a_los_datos_se_rechaza(cat, pred_valida):
    pred_valida["fecha"] = "2019-05-01"
    with pytest.raises(Rechazo):
        limpiar_prediccion(pred_valida, cat, fecha_min=date(2024, 3, 1))


def test_tienda_no_puede_pedirse_antes_de_abrir(cat, pred_valida):
    pred_valida["tienda_id"] = 10  # abrió el 2025-03-15
    pred_valida["fecha"] = "2025-01-10"
    with pytest.raises(Rechazo, match="abrió"):
        limpiar_prediccion(pred_valida, cat)


def test_fecha_sin_calendario_se_acepta_con_advertencia(cat, pred_valida):
    pred_valida["fecha"] = "2027-03-10"
    sol, obs = limpiar_prediccion(pred_valida, cat)
    assert acciones(obs) == {"advertir"}


def test_faltan_familias_se_completan_con_advertencia(cat):
    cruda = cargar_ejemplo("errores/E05_faltan_familias.json")
    sol, obs = limpiar_prediccion(cruda, cat)
    assert len(sol.familias) == 12
    completadas = [f for f in sol.familias if f.familia_id in ("bebe", "mascotas")]
    assert all(f.en_promocion == 0 for f in completadas)
    assert acciones(obs) == {"advertir"}
    assert "bebe" in obs[0].detalle and "mascotas" in obs[0].detalle


def test_familia_repetida_con_valores_distintos_usa_la_ultima_y_advierte(cat):
    cruda = cargar_ejemplo("errores/E06_familia_repetida.json")
    sol, obs = limpiar_prediccion(cruda, cat)
    assert len(sol.familias) == 12
    assert next(f for f in sol.familias if f.familia_id == "abarrotes").en_promocion == 3
    assert acciones(obs) == {"advertir"}


def test_familia_repetida_con_el_mismo_valor_se_repara(cat, pred_valida):
    pred_valida["familias"].append({"familia_id": "abarrotes", "en_promocion": 0})
    sol, obs = limpiar_prediccion(pred_valida, cat)
    assert len(sol.familias) == 12
    assert acciones(obs) == {"reparar"}


def test_campo_renombrado_se_repara(cat):
    cruda = cargar_ejemplo("errores/E07_campo_renombrado.json")
    sol, obs = limpiar_prediccion(cruda, cat)
    assert sol.tienda_id == 5
    assert acciones(obs) == {"reparar"}


@pytest.mark.parametrize("nombre", ["tiendaId", "TIENDA_ID", "Tienda", "store_id"])
def test_variantes_del_nombre_del_campo(cat, pred_valida, nombre):
    pred_valida[nombre] = pred_valida.pop("tienda_id")
    sol, _ = limpiar_prediccion(pred_valida, cat)
    assert sol.tienda_id == 5


def test_falta_tienda_o_fecha_se_rechaza(cat, pred_valida):
    sin_tienda = {k: v for k, v in pred_valida.items() if k != "tienda_id"}
    sin_fecha = {k: v for k, v in pred_valida.items() if k != "fecha"}
    with pytest.raises(Rechazo):
        limpiar_prediccion(sin_tienda, cat)
    with pytest.raises(Rechazo):
        limpiar_prediccion(sin_fecha, cat)


def test_sin_lista_de_familias_se_pronostican_todas_con_advertencia(cat, pred_valida):
    del pred_valida["familias"]
    sol, obs = limpiar_prediccion(pred_valida, cat)
    assert len(sol.familias) == 12 and all(f.en_promocion == 0 for f in sol.familias)
    assert acciones(obs) == {"advertir"}


def test_campos_de_mas_se_ignoran(cat, pred_valida):
    pred_valida["comentario"] = "hola"
    _, obs = limpiar_prediccion(pred_valida, cat)
    assert obs == []


def test_familias_como_objeto(cat):
    cruda = {"tienda_id": 5, "fecha": "2025-11-17", "familias": {"abarrotes": 0, "bebidas": 8}}
    sol, obs = limpiar_prediccion(cruda, cat)
    assert len(sol.familias) == 12
    assert next(f for f in sol.familias if f.familia_id == "bebidas").en_promocion == 8


# ---------------------------------------------------------------- observaciones
def test_observacion_valida(cat, obs_valida):
    sol, obs = limpiar_observacion(obs_valida, cat)
    assert obs == [] and len(sol.ventas) == 12


def test_ventas_negativas_no_se_registran(cat):
    cruda = cargar_ejemplo("errores/E08_ventas_negativas.json")
    sol, obs = limpiar_observacion(cruda, cat)
    assert "frutas_verduras" not in [v.familia_id for v in sol.ventas]
    assert len(sol.ventas) == 11
    assert "advertir" in acciones(obs)


def test_numeros_con_separadores(cat):
    cruda = cargar_ejemplo("errores/E10_numero_formato.json")
    sol, obs = limpiar_observacion(cruda, cat)
    ventas = {v.familia_id: v.ventas for v in sol.ventas}
    assert ventas["bebe"] == 50 and ventas["bebidas"] == 1292
    assert acciones(obs) == {"reparar"}


@pytest.mark.parametrize("texto,esperado", [
    ("1,234", 1234), ("1.234", 1234), ("812,0", 812.0), ("12,5", 12.5), ("1.234,5", 1234.5), ("1,234.5", 1234.5),
    ("1,234,567", 1234567), ("1 234", 1234), ("-4", -4), ("0,500", 0.5), ("1.5", 1.5), ("994", 994),
])
def test_parsear_numero(texto, esperado):
    assert parsear_numero(texto) == pytest.approx(esperado)


@pytest.mark.parametrize("texto", ["", "abc", "1,2,3", "1..2", "12a", "--4"])
def test_parsear_numero_ilegible(texto):
    assert parsear_numero(texto) is None


def test_observacion_con_valor_absurdo_no_se_registra(cat, obs_valida):
    obs_valida["ventas"][0]["ventas"] = 10**7
    sol, obs = limpiar_observacion(obs_valida, cat, venta_maxima={(5, "abarrotes"): 3000})
    assert "abarrotes" not in [v.familia_id for v in sol.ventas]
    assert "advertir" in acciones(obs)


def test_observacion_sin_ventas_utilizables_se_rechaza(cat):
    cruda = {"tienda_id": 5, "fecha": "2025-11-17", "ventas": [{"familia_id": "abarrotes", "ventas": -1}]}
    with pytest.raises(Rechazo):
        limpiar_observacion(cruda, cat)


def test_observacion_con_familias_faltantes_advierte(cat, obs_valida):
    obs_valida["ventas"] = obs_valida["ventas"][:10]
    sol, obs = limpiar_observacion(obs_valida, cat)
    assert len(sol.ventas) == 10
    assert acciones(obs) == {"advertir"}


def test_observacion_repetida_con_valores_distintos_usa_la_ultima(cat, obs_valida):
    obs_valida["ventas"].append({"familia_id": "abarrotes", "ventas": 1000})
    sol, obs = limpiar_observacion(obs_valida, cat)
    assert next(v for v in sol.ventas if v.familia_id == "abarrotes").ventas == 1000
    assert "advertir" in acciones(obs)
