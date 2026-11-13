"""Pruebas del contrato HTTP del Anexo. Usan un modelo y un Airflow falsos, y SQLite."""
import json
from datetime import date

from sqlalchemy import func, select

from novamercado import db
from novamercado.estado import SinModelo

from .conftest import cargar_ejemplo


# ---------------------------------------------------------------- POST /v1/prediccion
def test_prediccion_valida_cumple_el_contrato(cliente, pred_valida):
    r = cliente.post("/v1/prediccion", json=pred_valida)
    assert r.status_code == 200
    d = r.json()
    assert d["tienda_id"] == 5 and d["fecha"] == "2025-11-17"
    assert d["version_modelo"] == "7" and isinstance(d["version_modelo"], str)
    assert d["calidad"] == {"estado": "ok", "observaciones": []}
    assert len(d["predicciones"]) == 12
    for p in d["predicciones"]:
        assert set(p) == {"familia_id", "venta_esperada", "cantidad_sugerida"}
        assert isinstance(p["venta_esperada"], float)
        assert isinstance(p["cantidad_sugerida"], int) and p["cantidad_sugerida"] >= 0


def test_prediccion_conserva_las_familias_pedidas(cliente, pred_valida):
    d = cliente.post("/v1/prediccion", json=pred_valida).json()
    assert [p["familia_id"] for p in d["predicciones"]] == [f["familia_id"] for f in pred_valida["familias"]]


def test_prediccion_corregida_informa_lo_que_hizo(cliente):
    r = cliente.post("/v1/prediccion", json=cargar_ejemplo("errores/E02_familia_otra_forma.json"))
    assert r.status_code == 200
    calidad = r.json()["calidad"]
    assert calidad["estado"] == "corregido"
    assert {o["accion"] for o in calidad["observaciones"]} == {"reparar"}
    assert all({"campo", "accion", "detalle"} <= set(o) for o in calidad["observaciones"])


def test_cualquier_advertir_vuelve_el_estado_advertencia(cliente):
    r = cliente.post("/v1/prediccion", json=cargar_ejemplo("errores/E05_faltan_familias.json"))
    assert r.json()["calidad"]["estado"] == "advertencia"


def test_cada_ejemplo_del_catalogo_de_predicciones_responde_200(cliente):
    for n in ("E01_tienda_texto", "E02_familia_otra_forma", "E03_promocion_texto", "E04_fecha_formato",
              "E05_faltan_familias", "E06_familia_repetida", "E07_campo_renombrado"):
        r = cliente.post("/v1/prediccion", json=cargar_ejemplo(f"errores/{n}.json"))
        assert r.status_code == 200, (n, r.text)
        assert len(r.json()["predicciones"]) == 12


def test_rechazo_es_422_con_detail_en_texto(cliente, pred_valida):
    pred_valida["tienda_id"] = 99
    r = cliente.post("/v1/prediccion", json=pred_valida)
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str) and "99" in r.json()["detail"]


def test_cuerpo_que_no_es_un_objeto_es_422(cliente):
    for cuerpo in ([1, 2, 3], "hola", 5):
        r = cliente.post("/v1/prediccion", content=json.dumps(cuerpo), headers={"content-type": "application/json"})
        assert r.status_code == 422 and isinstance(r.json()["detail"], str)
    r = cliente.post("/v1/prediccion", content="{no es json", headers={"content-type": "application/json"})
    assert r.status_code == 422


def test_sin_modelo_cargado_responde_503(ctx, cliente, pred_valida):
    ctx.estado._modelo = None
    assert cliente.post("/v1/prediccion", json=pred_valida).status_code == 503


def test_la_misma_fecha_se_puede_pedir_varias_veces(cliente, ctx, pred_valida):
    for _ in range(3):
        assert cliente.post("/v1/prediccion", json=pred_valida).status_code == 200
    with ctx.engine.connect() as con:
        assert con.execute(select(func.count()).select_from(db.predicciones)).scalar() == 36


def test_se_registra_solicitud_correcciones_y_version(cliente, ctx):
    cliente.post("/v1/prediccion", json=cargar_ejemplo("errores/E05_faltan_familias.json"))
    cliente.post("/v1/prediccion", json={**cargar_ejemplo("prediccion_valida.json"), "tienda_id": 99})
    with ctx.engine.connect() as con:
        sol = con.execute(select(db.solicitudes.c.http_status, db.solicitudes.c.estado_calidad)).all()
        assert sorted(sol) == [(200, "advertencia"), (422, None)]
        assert con.execute(select(func.count()).select_from(db.correcciones)).scalar() >= 1
        versiones = {v for (v,) in con.execute(select(db.predicciones.c.version_modelo))}
        assert versiones == {"7"}


# ---------------------------------------------------------------- POST /v1/observaciones
def test_observacion_valida(cliente, obs_valida):
    r = cliente.post("/v1/observaciones", json=obs_valida)
    assert r.status_code == 200
    assert r.json() == {"registros_guardados": 12, "calidad": {"estado": "ok", "observaciones": []}}


def test_observacion_es_idempotente(cliente, ctx, obs_valida):
    assert cliente.post("/v1/observaciones", json=obs_valida).json()["registros_guardados"] == 12
    r = cliente.post("/v1/observaciones", json=obs_valida)
    assert r.status_code == 200
    assert r.json()["registros_guardados"] == 0
    assert r.json()["calidad"]["estado"] == "ok"
    with ctx.engine.connect() as con:
        assert con.execute(select(func.count()).select_from(db.observaciones)).scalar() == 12


def test_observacion_con_un_valor_cambiado_cuenta_solo_ese(cliente, obs_valida):
    cliente.post("/v1/observaciones", json=obs_valida)
    obs_valida["ventas"][0]["ventas"] += 10
    r = cliente.post("/v1/observaciones", json=obs_valida).json()
    assert r["registros_guardados"] == 1
    assert r["calidad"]["estado"] == "advertencia"


def test_observacion_envio_repetido_del_catalogo(cliente):
    e09 = cargar_ejemplo("errores/E09_envio_repetido.json")
    assert cliente.post("/v1/observaciones", json=e09).json()["registros_guardados"] == 12
    assert cliente.post("/v1/observaciones", json=e09).json()["registros_guardados"] == 0


def test_observacion_con_negativas_guarda_el_resto(cliente, ctx):
    r = cliente.post("/v1/observaciones", json=cargar_ejemplo("errores/E08_ventas_negativas.json"))
    assert r.status_code == 200
    assert r.json()["registros_guardados"] == 11
    assert r.json()["calidad"]["estado"] == "advertencia"
    with ctx.engine.connect() as con:
        guardadas = {f for (f,) in con.execute(select(db.observaciones.c.familia_id))}
    assert "frutas_verduras" not in guardadas


def test_observacion_con_numeros_con_formato(cliente):
    r = cliente.post("/v1/observaciones", json=cargar_ejemplo("errores/E10_numero_formato.json"))
    assert r.json()["registros_guardados"] == 12
    assert r.json()["calidad"]["estado"] == "corregido"


def test_observacion_rechazada(cliente, obs_valida):
    obs_valida["fecha"] = "no es fecha"
    r = cliente.post("/v1/observaciones", json=obs_valida)
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)


def test_la_observacion_de_ayer_entra_a_las_features_de_hoy(cliente, ctx):
    """El histórico de la prueba llega al 16 de nov. Se manda lo del 16 y el modelo falso responde igual; lo importante es que la lectura la incluya."""
    from novamercado import repositorio as repo

    cliente.post("/v1/observaciones", json={"tienda_id": 5, "fecha": "2025-11-16",
                                            "ventas": [{"familia_id": "abarrotes", "ventas": 12345}]})
    v = repo.leer_ventas_tienda(ctx.engine, 5, date(2025, 11, 15), date(2025, 11, 16))
    fila = v[(v.familia_id == "abarrotes") & (v.fecha == "2025-11-16")]
    assert fila["ventas"].iloc[0] == 12345  # la observación manda sobre el histórico


# ---------------------------------------------------------------- admin
def test_reentrenar_responde_202_con_id(cliente):
    r = cliente.post("/admin/reentrenar")
    assert r.status_code == 202
    assert isinstance(r.json()["id"], str)


def test_reentrenar_503_si_airflow_no_responde(ctx, cliente):
    ctx.orquestador.caido = True
    assert cliente.post("/admin/reentrenar").status_code == 503


def test_estado_en_curso_no_trae_resultado(cliente):
    run = cliente.post("/admin/reentrenar").json()["id"]
    r = cliente.get(f"/admin/reentrenar/{run}")
    assert r.status_code == 200
    assert r.json() == {"estado": "en_curso"}


def test_estado_desconocido_es_404(cliente):
    assert cliente.get("/admin/reentrenar/no-existe").status_code == 404


def test_estado_terminado_trae_promovido_version_y_motivo(ctx, cliente):
    from novamercado import repositorio as repo

    run = cliente.post("/admin/reentrenar").json()["id"]
    repo.guardar_reentrenamiento(ctx.engine, run, "exitoso", True, "8", "Costo en validación 18 210 vs 19 940 del vigente")
    r = cliente.get(f"/admin/reentrenar/{run}")
    assert r.json() == {"estado": "exitoso", "promovido": True, "version": "8",
                        "motivo": "Costo en validación 18 210 vs 19 940 del vigente"}


def test_ejecucion_fallida_informa_que_el_vigente_no_cambio(ctx, cliente):
    run = cliente.post("/admin/reentrenar").json()["id"]
    ctx.orquestador.runs[run] = "failed"
    d = cliente.get(f"/admin/reentrenar/{run}").json()
    assert d["estado"] == "fallido" and d["promovido"] is False and d["version"] == "7"


# ---------------------------------------------------------------- /modelo y /health
def test_modelo_vigente(cliente):
    assert cliente.get("/modelo").json() == {"nombre": "novamercado-ventas", "alias": "produccion", "version": "7"}


def test_modelo_503_sin_modelo(ctx, cliente):
    ctx.estado._modelo = None
    assert cliente.get("/modelo").status_code == 503


def test_health_ok(cliente):
    r = cliente.get("/health")
    assert r.status_code == 200 and r.json()["estado"] == "ok"


def test_health_degradado_si_cae_una_dependencia_y_la_api_sigue_respondiendo(ctx, cliente, pred_valida):
    ctx.estado.marcar("mlflow", False, "no responde")
    r = cliente.get("/health")
    assert r.status_code == 200
    assert r.json()["estado"] == "degradado" and "mlflow" in r.json()["motivo"]
    assert cliente.post("/v1/prediccion", json=pred_valida).status_code == 200  # sigue con el modelo cargado
    ctx.estado.marcar("mlflow", True)
    assert cliente.get("/health").json()["estado"] == "ok"


def test_health_503_sin_modelo(ctx, cliente):
    ctx.estado._modelo = None
    assert cliente.get("/health").status_code == 503
