"""Consultas y escrituras sobre la base. Aquí no hay reglas de negocio, solo SQL."""
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from . import db
from .contrato import ObservacionCalidad


def _insert(engine: Engine, tabla):
    return pg_insert(tabla) if engine.dialect.name == "postgresql" else sqlite_insert(tabla)


# ------------------------------------------------------------ solicitudes
def _guardar_solicitud(con, tipo: str, payload, http_status: int, estado_calidad: str | None,
                       rechazo: str | None, correcciones: list[ObservacionCalidad] | None) -> int:
    r = con.execute(
        db.solicitudes.insert().values(
            tipo=tipo, payload=payload, http_status=http_status,
            estado_calidad=estado_calidad, detalle_rechazo=rechazo,
        )
    )
    sid = r.inserted_primary_key[0]
    if correcciones:
        con.execute(
            db.correcciones.insert(),
            [{"solicitud_id": sid, "campo": c.campo, "accion": c.accion, "detalle": c.detalle} for c in correcciones],
        )
    return sid


def registrar_rechazo(engine: Engine, tipo: str, payload, motivo: str) -> None:
    with engine.begin() as con:
        _guardar_solicitud(con, tipo, payload, 422, None, motivo, None)


def registrar_prediccion(engine: Engine, payload, estado: str, correcciones: list[ObservacionCalidad],
                         tienda_id: int, fecha: date, filas: list[dict], version: str) -> int:
    """Guarda solicitud, correcciones y predicciones en una sola transacción."""
    with engine.begin() as con:
        sid = _guardar_solicitud(con, "prediccion", payload, 200, estado, None, correcciones)
        con.execute(
            db.predicciones.insert(),
            [
                {
                    "solicitud_id": sid, "tienda_id": tienda_id, "fecha": fecha,
                    "familia_id": f["familia_id"], "en_promocion": f["en_promocion"],
                    "venta_esperada": f["venta_esperada"], "cantidad_sugerida": f["cantidad_sugerida"],
                    "version_modelo": version,
                }
                for f in filas
            ],
        )
    return sid


def registrar_observacion(engine: Engine, payload, estado: str, correcciones: list[ObservacionCalidad],
                          tienda_id: int, fecha: date, ventas: list[dict]) -> int:
    """Upsert idempotente de las ventas del día. Devuelve cuántas filas fueron nuevas o cambiaron."""
    with engine.begin() as con:
        sid = _guardar_solicitud(con, "observacion", payload, 200, estado, None, correcciones)
        filas = [
            {"tienda_id": tienda_id, "fecha": fecha, "familia_id": v["familia_id"], "ventas": v["ventas"], "solicitud_id": sid}
            for v in ventas
        ]
        ins = _insert(engine, db.observaciones).values(filas)
        stmt = ins.on_conflict_do_update(
            index_elements=["tienda_id", "fecha", "familia_id"],
            set_={"ventas": ins.excluded.ventas, "solicitud_id": ins.excluded.solicitud_id, "actualizado_en": func.now()},
            # si el valor es el mismo no se toca la fila y por eso no cuenta como guardada
            where=db.observaciones.c.ventas.is_distinct_from(ins.excluded.ventas),
        )
        return con.execute(stmt).rowcount


def valores_previos_observacion(engine: Engine, tienda_id: int, fecha: date) -> dict[str, int]:
    with engine.connect() as con:
        r = con.execute(
            select(db.observaciones.c.familia_id, db.observaciones.c.ventas).where(
                db.observaciones.c.tienda_id == tienda_id, db.observaciones.c.fecha == fecha
            )
        )
        return {fila[0]: fila[1] for fila in r}


# ------------------------------------------------------------ lectura de ventas
def _union_ventas(engine: Engine, filtro_hist: str, filtro_obs: str, params: dict) -> pd.DataFrame:
    """Histórico publicado + observaciones; si un día está en ambos manda la observación."""
    h = pd.read_sql(
        text(f"SELECT tienda_id, familia_id, fecha, ventas, en_promocion FROM historico_ventas {filtro_hist}"),
        engine, params=params,
    )
    o = pd.read_sql(
        text(f"SELECT tienda_id, familia_id, fecha, ventas FROM observaciones {filtro_obs}"), engine, params=params
    )
    h["fecha"] = pd.to_datetime(h["fecha"])
    o["fecha"] = pd.to_datetime(o["fecha"])
    o["en_promocion"] = float("nan")
    llaves = ["tienda_id", "familia_id", "fecha"]
    todo = pd.concat([h, o], ignore_index=True)
    todo["ventas"] = pd.to_numeric(todo["ventas"], errors="coerce").astype(float)
    todo["en_promocion"] = pd.to_numeric(todo["en_promocion"], errors="coerce").astype(float)
    return todo.drop_duplicates(llaves, keep="last").reset_index(drop=True)


def leer_ventas_tienda(engine: Engine, tienda_id: int, desde: date, hasta: date) -> pd.DataFrame:
    cond = "WHERE tienda_id = :t AND fecha >= :d AND fecha <= :h"
    p = {"t": tienda_id, "d": desde, "h": hasta}
    return _union_ventas(engine, cond, cond, p)[["tienda_id", "familia_id", "fecha", "ventas"]]


def leer_datos_entrenamiento(engine: Engine, hasta: date | None = None) -> pd.DataFrame:
    """Todo lo que se sabe: ventas y, para cada día, cuántos productos estaban en promoción.

    Para los días del histórico la promoción viene en el archivo. Para los días observados
    por la API viene de la última solicitud de pronóstico de ese día (las observaciones
    no traen promociones). Si nunca hubo solicitud, queda NaN.
    """
    cond = "WHERE fecha <= :h" if hasta else ""
    p = {"h": hasta} if hasta else {}
    datos = _union_ventas(engine, cond, cond, p)

    promos = pd.read_sql(
        text("SELECT id, tienda_id, familia_id, fecha, en_promocion FROM predicciones"), engine
    )
    if len(promos):
        promos["fecha"] = pd.to_datetime(promos["fecha"])
        promos = promos.sort_values("id").drop_duplicates(["tienda_id", "familia_id", "fecha"], keep="last")
        promos = promos.rename(columns={"en_promocion": "promo_solicitud"})[["tienda_id", "familia_id", "fecha", "promo_solicitud"]]
        datos = datos.merge(promos, on=["tienda_id", "familia_id", "fecha"], how="left")
        datos["en_promocion"] = datos["en_promocion"].fillna(datos["promo_solicitud"])
        datos = datos.drop(columns="promo_solicitud")
    return datos


def fecha_minima_historico(engine: Engine) -> date | None:
    with engine.connect() as con:
        return con.execute(select(func.min(db.historico_ventas.c.fecha))).scalar()


def ventas_maximas(engine: Engine) -> dict[tuple[int, str], int]:
    with engine.connect() as con:
        r = con.execute(
            select(db.historico_ventas.c.tienda_id, db.historico_ventas.c.familia_id, func.max(db.historico_ventas.c.ventas)).group_by(
                db.historico_ventas.c.tienda_id, db.historico_ventas.c.familia_id
            )
        )
        return {(a, b): c for a, b, c in r}


# ------------------------------------------------------------ meta y carga inicial
def leer_meta(engine: Engine, clave: str) -> str | None:
    with engine.connect() as con:
        return con.execute(select(db.meta.c.valor).where(db.meta.c.clave == clave)).scalar()


def escribir_meta(engine: Engine, clave: str, valor: str) -> None:
    ins = _insert(engine, db.meta).values(clave=clave, valor=valor)
    with engine.begin() as con:
        con.execute(ins.on_conflict_do_update(index_elements=["clave"], set_={"valor": ins.excluded.valor}))


def cargar_historico(engine: Engine, data_dir: Path) -> int:
    """Carga historico_ventas.csv una sola vez. Si ya se cargó no hace nada y devuelve 0."""
    if leer_meta(engine, "historico_cargado") == "si":
        return 0
    h = pd.read_csv(Path(data_dir) / "historico_ventas.csv")
    h["fecha"] = pd.to_datetime(h["fecha"]).dt.date
    with engine.begin() as con:
        con.execute(db.historico_ventas.delete())  # por si quedó una carga a medias
        for i in range(0, len(h), 5000):
            con.execute(db.historico_ventas.insert(), h.iloc[i : i + 5000].to_dict("records"))
        ins = _insert(engine, db.meta).values(clave="historico_cargado", valor="si")
        con.execute(ins.on_conflict_do_update(index_elements=["clave"], set_={"valor": "si"}))
    return len(h)


# ------------------------------------------------------------ reentrenamientos
def guardar_reentrenamiento(engine: Engine, run_id: str, estado: str, promovido: bool,
                            version: str | None, motivo: str, metricas: dict | None = None) -> None:
    ins = _insert(engine, db.reentrenamientos).values(
        run_id=run_id, estado=estado, promovido=promovido, version=version, motivo=motivo, metricas=metricas
    )
    with engine.begin() as con:
        con.execute(
            ins.on_conflict_do_update(
                index_elements=["run_id"],
                set_={"estado": ins.excluded.estado, "promovido": ins.excluded.promovido, "version": ins.excluded.version,
                      "motivo": ins.excluded.motivo, "metricas": ins.excluded.metricas},
            )
        )


def leer_reentrenamiento(engine: Engine, run_id: str) -> dict | None:
    with engine.connect() as con:
        fila = con.execute(select(db.reentrenamientos).where(db.reentrenamientos.c.run_id == run_id)).mappings().first()
    return dict(fila) if fila else None


def hacer_ping(engine: Engine) -> None:
    with engine.connect() as con:
        con.execute(text("SELECT 1"))
