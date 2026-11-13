"""Tablas de la base de datos del producto (PostgreSQL en Docker; SQLite en pruebas)."""
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine

metadata = MetaData()

_id_grande = BigInteger().with_variant(Integer, "sqlite")
_json = JSON().with_variant(JSONB(), "postgresql")

# todo lo que llega por POST, tal como llegó, con lo que se respondió
solicitudes = Table(
    "solicitudes", metadata,
    Column("id", _id_grande, primary_key=True, autoincrement=True),
    Column("tipo", String(20), nullable=False),  # prediccion | observacion
    Column("recibida_en", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("payload", _json),
    Column("http_status", Integer, nullable=False),
    Column("estado_calidad", String(20)),
    Column("detalle_rechazo", Text),
)

correcciones = Table(
    "correcciones", metadata,
    Column("id", _id_grande, primary_key=True, autoincrement=True),
    Column("solicitud_id", _id_grande, ForeignKey("solicitudes.id"), nullable=False, index=True),
    Column("campo", Text, nullable=False),
    Column("accion", String(10), nullable=False),
    Column("detalle", Text, nullable=False),
)

predicciones = Table(
    "predicciones", metadata,
    Column("id", _id_grande, primary_key=True, autoincrement=True),
    Column("solicitud_id", _id_grande, ForeignKey("solicitudes.id"), nullable=False),
    Column("tienda_id", Integer, nullable=False),
    Column("fecha", Date, nullable=False),
    Column("familia_id", String(40), nullable=False),
    Column("en_promocion", Integer, nullable=False),
    Column("venta_esperada", Float, nullable=False),
    Column("cantidad_sugerida", Integer, nullable=False),
    Column("version_modelo", String(40), nullable=False),
    Column("creada_en", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Index("ix_predicciones_tienda_fecha", "tienda_id", "fecha"),
)

# una fila por tienda, día y familia: la llave primaria es lo que vuelve idempotente el registro
observaciones = Table(
    "observaciones", metadata,
    Column("tienda_id", Integer, primary_key=True),
    Column("fecha", Date, primary_key=True),
    Column("familia_id", String(40), primary_key=True),
    Column("ventas", Integer, nullable=False),
    Column("solicitud_id", _id_grande, ForeignKey("solicitudes.id")),
    Column("actualizado_en", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

# lo que publicó el equipo docente (se carga una sola vez al arrancar)
historico_ventas = Table(
    "historico_ventas", metadata,
    Column("tienda_id", Integer, primary_key=True),
    Column("fecha", Date, primary_key=True),
    Column("familia_id", String(40), primary_key=True),
    Column("ventas", Integer, nullable=False),
    Column("en_promocion", Integer, nullable=False),
)

meta = Table(
    "meta", metadata,
    Column("clave", String(80), primary_key=True),
    Column("valor", Text, nullable=False),
)

# resultado de cada ejecución del DAG; la API lo lee para responder GET /admin/reentrenar/{id}
reentrenamientos = Table(
    "reentrenamientos", metadata,
    Column("run_id", String(200), primary_key=True),
    Column("estado", String(20), nullable=False),  # exitoso | fallido
    Column("promovido", Boolean, nullable=False),
    Column("version", String(40)),
    Column("motivo", Text),
    Column("metricas", _json),
    Column("terminado_en", DateTime(timezone=True), server_default=func.now(), nullable=False),
)


def crear_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)


def crear_tablas(engine: Engine) -> None:
    metadata.create_all(engine)
