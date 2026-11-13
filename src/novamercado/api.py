"""API de NovaMercado (FastAPI). Cumple el contrato del Anexo del laboratorio.

Los endpoints son delgados a propósito: reciben, llaman a limpieza / predictor / repositorio y
responden. Las reglas viven en limpieza.py y las consultas en repositorio.py.
"""
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from . import repositorio as repo
from .catalogos import Catalogos, cargar_catalogos
from .config import Config, cargar_config
from .contrato import (
    EstadoReentrenamiento,
    ModeloVigente,
    ObservacionCalidad,
    RespuestaObservacion,
    RespuestaPrediccion,
    RespuestaReentrenar,
    SolicitudCruda,
    calcular_estado,
)
from .db import crear_engine, crear_tablas
from .estado import EstadoServicio, SinModelo, Vigilante
from .limpieza import Rechazo, limpiar_observacion, limpiar_prediccion
from .orquestador import Orquestador, OrquestadorNoDisponible
from .predictor import Predictor

log = logging.getLogger("api")


@dataclass
class Contexto:
    """Todo lo que los endpoints necesitan. En las pruebas se arma con piezas falsas."""

    cfg: Config
    engine: Engine
    cat: Catalogos
    estado: EstadoServicio
    predictor: Predictor
    orquestador: Orquestador
    vigilante: Vigilante | None = None
    _fecha_min: date | None = None
    _venta_max: dict | None = None

    # estos dos salen de la base, que puede estar vacía en los primeros segundos de un arranque desde cero
    def fecha_min(self) -> date | None:
        if self._fecha_min is None:
            try:
                self._fecha_min = repo.fecha_minima_historico(self.engine)
            except SQLAlchemyError:
                return None
        return self._fecha_min

    def venta_maxima(self) -> dict:
        if not self._venta_max:
            try:
                self._venta_max = repo.ventas_maximas(self.engine)
            except SQLAlchemyError:
                return {}
        return self._venta_max or {}


def construir_contexto(cfg: Config) -> Contexto:
    engine = crear_engine(cfg.database_url)
    try:
        cat = cargar_catalogos(cfg.data_dir)
    except FileNotFoundError as e:
        raise RuntimeError(
            f"No se encontraron los archivos publicados en {cfg.data_dir} ({e.filename}). "
            "Copie ahí los CSV del laboratorio (ver README.md)."
        ) from e
    estado = EstadoServicio()
    orq = Orquestador(cfg.airflow_url, cfg.airflow_usuario, cfg.airflow_clave, cfg.dag_id)
    return Contexto(
        cfg=cfg, engine=engine, cat=cat, estado=estado,
        predictor=Predictor(engine, cat, estado), orquestador=orq,
        vigilante=Vigilante(cfg, estado, engine, orq),
    )


def obtener_contexto(request: Request) -> Contexto:
    return request.app.state.ctx


def crear_app(ctx: Contexto | None = None) -> FastAPI:
    @asynccontextmanager
    async def ciclo_de_vida(app: FastAPI):
        # al arrancar: se arma el contexto (si no lo inyectaron) y se enciende el vigilante
        if ctx is None:
            app.state.ctx = construir_contexto(cargar_config())
            try:
                crear_tablas(app.state.ctx.engine)
            except Exception as e:  # la base puede estar levantándose; el vigilante lo irá reportando
                log.warning("no se pudieron crear las tablas todavía: %s", e)
        if app.state.ctx.vigilante is not None:
            app.state.ctx.vigilante.iniciar()
        yield
        # al apagar
        if app.state.ctx.vigilante is not None:
            app.state.ctx.vigilante.detener()

    app = FastAPI(
        title="NovaMercado - pronóstico diario de ventas",
        description="Pronóstico por familia de producto con defensa del contrato, registro de la operación y reentrenamiento.",
        version="1.0.0",
        lifespan=ciclo_de_vida,
    )
    if ctx is not None:
        app.state.ctx = ctx  # contexto inyectado (pruebas): no hace falta esperar al lifespan

    # ---------------------------------------------------------------- errores
    @app.exception_handler(RequestValidationError)
    async def cuerpo_invalido(request: Request, exc: RequestValidationError):
        """El cuerpo ni siquiera es un objeto JSON. Respuesta 422 con detail en texto, como pide el contrato."""
        motivo = "el cuerpo de la solicitud debe ser un objeto JSON"
        ctx_ = getattr(request.app.state, "ctx", None)
        tipo = {"/v1/prediccion": "prediccion", "/v1/observaciones": "observacion"}.get(request.url.path)
        if ctx_ is not None and tipo:
            try:
                repo.registrar_rechazo(ctx_.engine, tipo, {"cuerpo_crudo": str(exc.body)[:2000]}, motivo)
            except SQLAlchemyError:
                pass
        return JSONResponse(status_code=422, content={"detail": motivo})

    # ---------------------------------------------------------------- pronóstico
    @app.post("/v1/prediccion", response_model=RespuestaPrediccion, tags=["pronóstico"])
    def prediccion(cuerpo: SolicitudCruda, ctx: Contexto = Depends(obtener_contexto)):
        crudo = cuerpo.model_dump()
        try:
            sol, observaciones = limpiar_prediccion(crudo, ctx.cat, ctx.fecha_min())
        except Rechazo as e:
            _guardar_rechazo(ctx, "prediccion", crudo, str(e))
            raise HTTPException(status_code=422, detail=str(e))

        try:
            predicciones, version, avisos = ctx.predictor.predecir(sol)
        except SinModelo:
            raise HTTPException(status_code=503, detail="todavía no hay un modelo cargado")
        except SQLAlchemyError:
            raise HTTPException(status_code=503, detail="la base de datos no está disponible")

        calidad = calcular_estado(observaciones + avisos)
        promos = {f.familia_id: f.en_promocion for f in sol.familias}
        filas = [{**p.model_dump(), "en_promocion": promos[p.familia_id]} for p in predicciones]
        try:
            repo.registrar_prediccion(ctx.engine, crudo, calidad.estado, calidad.observaciones,
                                      sol.tienda_id, sol.fecha, filas, version)
        except SQLAlchemyError:
            raise HTTPException(status_code=503, detail="no se pudo registrar la predicción en la base de datos")

        return RespuestaPrediccion(
            tienda_id=sol.tienda_id, fecha=sol.fecha, version_modelo=version,
            predicciones=predicciones, calidad=calidad,
        )

    @app.post("/v1/observaciones", response_model=RespuestaObservacion, tags=["pronóstico"])
    def observaciones(cuerpo: SolicitudCruda, ctx: Contexto = Depends(obtener_contexto)):
        crudo = cuerpo.model_dump()
        try:
            sol, avisos = limpiar_observacion(crudo, ctx.cat, ctx.fecha_min(), ctx.venta_maxima())
        except Rechazo as e:
            _guardar_rechazo(ctx, "observacion", crudo, str(e))
            raise HTTPException(status_code=422, detail=str(e))

        try:
            previos = repo.valores_previos_observacion(ctx.engine, sol.tienda_id, sol.fecha)
            cambiadas = [v.familia_id for v in sol.ventas if v.familia_id in previos and previos[v.familia_id] != v.ventas]
            if cambiadas:
                avisos.append(ObservacionCalidad(
                    campo="ventas", accion="advertir",
                    detalle=f"ya había ventas registradas con otro valor para: {', '.join(cambiadas)}; se reemplazaron por las nuevas",
                ))
            calidad = calcular_estado(avisos)
            guardados = repo.registrar_observacion(
                ctx.engine, crudo, calidad.estado, calidad.observaciones,
                sol.tienda_id, sol.fecha, [v.model_dump() for v in sol.ventas],
            )
        except SQLAlchemyError:
            raise HTTPException(status_code=503, detail="no se pudo registrar la observación en la base de datos")
        return RespuestaObservacion(registros_guardados=guardados, calidad=calidad)

    # ---------------------------------------------------------------- administración
    @app.post("/admin/reentrenar", status_code=202, response_model=RespuestaReentrenar, tags=["administración"])
    def reentrenar(ctx: Contexto = Depends(obtener_contexto)):
        try:
            return RespuestaReentrenar(id=ctx.orquestador.disparar())
        except OrquestadorNoDisponible as e:
            raise HTTPException(status_code=503, detail=str(e))

    @app.get("/admin/reentrenar/{run_id}", response_model=EstadoReentrenamiento,
             response_model_exclude_none=True, tags=["administración"])
    def estado_reentrenamiento(run_id: str, ctx: Contexto = Depends(obtener_contexto)):
        # 1) si el DAG ya dejó su resultado en la base, esa es la fuente de verdad
        try:
            fila = repo.leer_reentrenamiento(ctx.engine, run_id)
        except SQLAlchemyError:
            raise HTTPException(status_code=503, detail="la base de datos no está disponible")
        if fila:
            return EstadoReentrenamiento(estado=fila["estado"], promovido=fila["promovido"],
                                         version=fila["version"] or _version_actual(ctx), motivo=fila["motivo"])
        # 2) si no, preguntamos a Airflow
        try:
            st = ctx.orquestador.estado(run_id)
        except OrquestadorNoDisponible as e:
            raise HTTPException(status_code=503, detail=str(e))
        if st is None:
            raise HTTPException(status_code=404, detail=f"no existe la ejecución '{run_id}'")
        if st in ("success", "failed"):
            # terminó pero no dejó resultado: algo se rompió fuera de nuestras tareas
            return EstadoReentrenamiento(
                estado="fallido", promovido=False, version=_version_actual(ctx),
                motivo="la ejecución terminó sin registrar un resultado; el modelo vigente no se modificó",
            )
        return EstadoReentrenamiento(estado="en_curso")

    # ---------------------------------------------------------------- estado del servicio
    @app.get("/modelo", response_model=ModeloVigente, tags=["servicio"])
    def modelo(ctx: Contexto = Depends(obtener_contexto)):
        try:
            m = ctx.estado.modelo()
        except SinModelo:
            raise HTTPException(status_code=503, detail="todavía no hay un modelo cargado")
        return ModeloVigente(nombre=m.nombre, alias=m.alias, version=m.version)

    @app.get("/health", tags=["servicio"])
    def health(ctx: Contexto = Depends(obtener_contexto)):
        if not ctx.estado.hay_modelo():
            return JSONResponse(status_code=503, content={"estado": "iniciando", "motivo": "todavía no hay un modelo cargado"})
        problemas = ctx.estado.problemas()
        if problemas:
            return {"estado": "degradado", "motivo": "; ".join(problemas)}
        return {"estado": "ok"}

    return app


def _version_actual(ctx: Contexto) -> str | None:
    try:
        return ctx.estado.modelo().version
    except SinModelo:
        return None


def _guardar_rechazo(ctx: Contexto, tipo: str, payload: dict, motivo: str) -> None:
    try:
        repo.registrar_rechazo(ctx.engine, tipo, payload, motivo)
    except SQLAlchemyError:
        log.warning("no se pudo registrar el rechazo en la base")


app = crear_app()
