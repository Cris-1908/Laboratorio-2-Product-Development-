"""Reentrenamiento: se corre desde el DAG de Airflow (tres tareas) y desde el arranque inicial.

    python -m novamercado.reentrenar entrenar --run-id X   # entrena y registra una versión candidata
    python -m novamercado.reentrenar decidir  --run-id X   # compara contra el vigente y, si gana, mueve el alias
    python -m novamercado.reentrenar cerrar   --run-id X   # deja constancia aunque algo haya fallado

El criterio de reemplazo vive en `decidir()` y está explicado en DECISIONES.md.
"""
import argparse
import json
import sys
from datetime import timedelta

import pandas as pd

from . import registro_modelos as registro
from . import repositorio as repo
from .catalogos import Catalogos, cargar_catalogos
from .config import Config, cargar_config
from .db import crear_engine, crear_tablas
from .features import construir_features, ventas_para_features
from .modelo import PARAMS_DEFECTO, ModeloBaseline, ModeloVentas, evaluar

FILAS_MINIMAS = 5000


# ------------------------------------------------------------ datos
def preparar_dataset(engine, cat: Catalogos, hasta=None) -> pd.DataFrame:
    """Histórico + observaciones, con todas las variables ya calculadas."""
    datos = repo.leer_datos_entrenamiento(engine, hasta)
    ventas = ventas_para_features(datos[["tienda_id", "familia_id", "fecha", "ventas"]])
    # un día sin promoción conocida sirve para construir rezagos, pero no como fila de entrenamiento
    objetivos = datos.dropna(subset=["en_promocion"])[["tienda_id", "familia_id", "fecha", "en_promocion"]]
    df = construir_features(ventas, objetivos, cat)
    return df.merge(ventas, on=["tienda_id", "familia_id", "fecha"], how="left")


def partir_ventana(df: pd.DataFrame, dias_validacion: int):
    """Los últimos `dias_validacion` días son la validación; todo lo anterior es entrenamiento."""
    fin = df["fecha"].max()
    inicio = fin - timedelta(days=dias_validacion - 1)
    entrenamiento = df[df["fecha"] < inicio]
    validacion = df[(df["fecha"] >= inicio)].dropna(subset=["ventas", "nivel"])
    return entrenamiento, validacion, inicio, fin


def _predecir_y_evaluar(modelo, validacion: pd.DataFrame, cat: Catalogos) -> tuple[dict, pd.DataFrame]:
    pred = modelo.predict(validacion)
    tabla = pd.concat([validacion[["familia_id", "ventas"]].reset_index(drop=True), pred.reset_index(drop=True)], axis=1)
    return evaluar(tabla, cat.costos), tabla


# ------------------------------------------------------------ criterio de reemplazo
def decidir(costo_candidato: float, costo_vigente: float | None, costo_baseline: float, mejora_minima: float) -> tuple[bool, str]:
    """Reglas (todas deben cumplirse para promover):
      1. si no hay modelo vigente, el candidato entra;
      2. el costo del candidato en validación es al menos `mejora_minima` menor que el del vigente;
      3. el candidato le gana al baseline.
    """
    f = lambda x: f"{x:,.0f}".replace(",", " ")
    if costo_vigente is None:
        return True, f"No había modelo vigente; el primero entra (costo en validación {f(costo_candidato)} vs {f(costo_baseline)} del baseline)"
    if costo_candidato >= costo_baseline:
        return False, (f"No se promueve: el candidato ({f(costo_candidato)}) no le gana al baseline ({f(costo_baseline)}) "
                       f"en validación; el vigente cuesta {f(costo_vigente)}")
    mejora = 1 - costo_candidato / costo_vigente
    if mejora >= mejora_minima:
        return True, (f"Costo en validación {f(costo_candidato)} vs {f(costo_vigente)} del vigente "
                      f"(mejora de {mejora:.1%}, se exige al menos {mejora_minima:.1%})")
    return False, (f"No se promueve: costo en validación {f(costo_candidato)} vs {f(costo_vigente)} del vigente "
                   f"(mejora de {mejora:.1%}, se exige al menos {mejora_minima:.1%})")


# ------------------------------------------------------------ paso 1: entrenar
def entrenar_candidato(cfg: Config, run_id: str) -> dict:
    import mlflow

    engine = crear_engine(cfg.database_url)
    crear_tablas(engine)
    cat = cargar_catalogos(cfg.data_dir)

    df = preparar_dataset(engine, cat)
    entrenamiento, validacion, inicio_val, fin_val = partir_ventana(df, cfg.dias_validacion)
    if len(entrenamiento.dropna(subset=["ventas"])) < FILAS_MINIMAS or validacion.empty:
        raise RuntimeError(f"datos insuficientes para entrenar ({len(entrenamiento)} filas de entrenamiento, {len(validacion)} de validación)")

    base = ModeloBaseline().fit(entrenamiento.dropna(subset=["ventas"]))
    ml = ModeloVentas(cat.tau(), PARAMS_DEFECTO).fit(entrenamiento)
    m_base, _ = _predecir_y_evaluar(base, validacion, cat)
    m_ml, tabla = _predecir_y_evaluar(ml, validacion, cat)

    por_familia = {
        fam: evaluar(g, cat.costos) for fam, g in tabla.groupby("familia_id")
    }

    mlflow.set_tracking_uri(cfg.mlflow_uri)
    mlflow.set_registry_uri(cfg.mlflow_uri)
    mlflow.set_experiment(cfg.experimento)
    comunes = {
        "dag_run_id": run_id,
        "val_desde": str(inicio_val.date()),
        "val_hasta": str(fin_val.date()),
        "datos_hasta": str(df["fecha"].max().date()),
        "entrenado_hasta": str(ml.entrenado_hasta),
        "filas_entrenamiento": str(ml.n_filas),
    }
    with mlflow.start_run(run_name=f"baseline-{run_id}") as rb:
        mlflow.set_tags({**comunes, "rol": "baseline"})
        mlflow.log_param("modelo", "promedio mismo día de las últimas 4 semanas")
        mlflow.log_metrics({f"val_{k}": v for k, v in m_base.items()})

    with mlflow.start_run(run_name=f"hgb-{run_id}") as r:
        mlflow.set_tags({**comunes, "rol": "candidato", "baseline_run_id": rb.info.run_id})
        mlflow.log_params({**{f"hgb_{k}": v for k, v in PARAMS_DEFECTO.items()}, "dias_validacion": cfg.dias_validacion,
                           "tau_por_familia": json.dumps({k: round(v, 3) for k, v in cat.tau().items()})})
        mlflow.log_metrics({f"val_{k}": v for k, v in m_ml.items()})
        mlflow.log_metrics({
            "val_costo_baseline": m_base["costo_total"],
            "val_mejora_vs_baseline": 1 - m_ml["costo_total"] / m_base["costo_total"],
        })
        mlflow.log_dict(por_familia, "validacion_por_familia.json")
        version = registro.registrar_modelo(cfg, ml)
        mlflow.set_tag("model_version", version)
        run_ml = r.info.run_id

    from mlflow import MlflowClient

    c = MlflowClient(tracking_uri=cfg.mlflow_uri, registry_uri=cfg.mlflow_uri)
    c.set_model_version_tag(cfg.modelo_nombre, version, "dag_run_id", run_id)
    c.set_model_version_tag(cfg.modelo_nombre, version, "entrenado_hasta", comunes["entrenado_hasta"])

    resultado = {"run_id": run_id, "mlflow_run": run_ml, "version": version,
                 "costo_candidato": m_ml["costo_total"], "costo_baseline": m_base["costo_total"],
                 "validacion": [comunes["val_desde"], comunes["val_hasta"]]}
    print(json.dumps(resultado))
    return resultado


# ------------------------------------------------------------ paso 2: decidir
def _buscar_candidato(cfg: Config, run_id: str):
    from mlflow import MlflowClient

    c = MlflowClient(tracking_uri=cfg.mlflow_uri, registry_uri=cfg.mlflow_uri)
    exp = c.get_experiment_by_name(cfg.experimento)
    runs = c.search_runs([exp.experiment_id], f"tags.dag_run_id = '{run_id}' and tags.rol = 'candidato'")
    if not runs:
        raise RuntimeError(f"no hay candidato registrado para la ejecución {run_id}")
    return c, runs[0]


def decidir_promocion(cfg: Config, run_id: str, guardar: bool = True) -> dict:
    engine = crear_engine(cfg.database_url)
    cat = cargar_catalogos(cfg.data_dir)
    cliente, run = _buscar_candidato(cfg, run_id)
    version = run.data.tags["model_version"]
    costo_cand = run.data.metrics["val_costo_total"]
    costo_base = run.data.metrics["val_costo_baseline"]
    desde, hasta = run.data.tags["val_desde"], run.data.tags["val_hasta"]

    try:
        vigente = registro.version_vigente(cfg)
    except registro.SinModeloVigente:
        vigente = None

    costo_vig = None
    if vigente is not None:
        # el vigente se evalúa sobre exactamente la misma ventana que el candidato
        df = preparar_dataset(engine, cat, hasta=pd.Timestamp(hasta).date())
        val = df[(df["fecha"] >= pd.Timestamp(desde)) & (df["fecha"] <= pd.Timestamp(hasta))].dropna(subset=["ventas", "nivel"])
        modelo_vig = registro.cargar_modelo(cfg, vigente.version)
        m_vig, _ = _predecir_y_evaluar(modelo_vig, val, cat)
        costo_vig = m_vig["costo_total"]

    promover, motivo = decidir(costo_cand, costo_vig, costo_base, cfg.mejora_minima)
    if promover:
        registro.poner_alias(cfg, version)
    version_final = version if promover else (vigente.version if vigente else version)

    import mlflow
    with mlflow.start_run(run_id=run.info.run_id):
        if costo_vig is not None:
            mlflow.log_metric("val_costo_vigente", costo_vig)
        mlflow.set_tags({"decision": "promovido" if promover else "rechazado", "decision_motivo": motivo[:500]})
    cliente.set_model_version_tag(cfg.modelo_nombre, version, "decision", "promovido" if promover else "rechazado")

    resultado = {"run_id": run_id, "promovido": promover, "version": version_final, "motivo": motivo,
                 "version_candidata": version, "costo_candidato": costo_cand, "costo_vigente": costo_vig, "costo_baseline": costo_base}
    if guardar:
        repo.guardar_reentrenamiento(engine, run_id, "exitoso", promover, version_final, motivo, resultado)
    print(json.dumps(resultado))
    return resultado


# ------------------------------------------------------------ paso 3: cerrar
def cerrar(cfg: Config, run_id: str, fallo_previo: bool = False) -> int:
    """Si alguna tarea anterior falló, deja constancia (el modelo vigente no se tocó) y devuelve 1."""
    engine = crear_engine(cfg.database_url)
    crear_tablas(engine)
    if repo.leer_reentrenamiento(engine, run_id) is not None:
        return 0
    try:
        version = registro.version_vigente(cfg).version
    except Exception:
        version = None
    repo.guardar_reentrenamiento(
        engine, run_id, "fallido", False, version,
        "El reentrenamiento falló antes de decidir; el modelo vigente no se modificó (ver el log de la ejecución en Airflow)",
    )
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m novamercado.reentrenar")
    ap.add_argument("paso", choices=["entrenar", "decidir", "cerrar"])
    ap.add_argument("--run-id", required=True)
    a = ap.parse_args(argv)
    cfg = cargar_config()
    if a.paso == "entrenar":
        entrenar_candidato(cfg, a.run_id)
    elif a.paso == "decidir":
        decidir_promocion(cfg, a.run_id)
    else:
        return cerrar(cfg, a.run_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
