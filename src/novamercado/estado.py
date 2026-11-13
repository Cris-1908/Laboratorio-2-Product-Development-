"""Estado vivo de la API: qué modelo está cargado y cómo están las dependencias.

Un hilo de fondo (Vigilante) revisa cada pocos segundos MLflow, Airflow y la base, y si el
alias de producción apunta a otra versión, la descarga y la intercambia. Los endpoints solo
leen el resultado, por eso /health responde al instante y la API sigue sirviendo si MLflow se cae.
"""
import logging
import threading
import time
from dataclasses import dataclass, field

import httpx

from . import registro_modelos as registro
from . import repositorio as repo
from .config import Config

log = logging.getLogger("vigilante")


@dataclass
class ModeloActivo:
    modelo: object
    nombre: str
    alias: str
    version: str
    origen: str  # mlflow | cache


class SinModelo(Exception):
    pass


@dataclass
class EstadoServicio:
    _modelo: ModeloActivo | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    # componente -> (ok, motivo)
    dependencias: dict = field(default_factory=lambda: {"base de datos": (True, ""), "mlflow": (True, ""), "airflow": (True, "")})

    def modelo(self) -> ModeloActivo:
        m = self._modelo
        if m is None:
            raise SinModelo()
        return m

    def hay_modelo(self) -> bool:
        return self._modelo is not None

    def poner_modelo(self, m: ModeloActivo) -> None:
        with self._lock:
            self._modelo = m

    def marcar(self, componente: str, ok: bool, motivo: str = "") -> None:
        with self._lock:
            self.dependencias[componente] = (ok, motivo)

    def problemas(self) -> list[str]:
        with self._lock:
            return [f"{c}: {motivo}" if motivo else f"{c} no disponible" for c, (ok, motivo) in self.dependencias.items() if not ok]


class Vigilante:
    def __init__(self, cfg: Config, estado: EstadoServicio, engine, orquestador):
        self.cfg = cfg
        self.estado = estado
        self.engine = engine
        self.orquestador = orquestador
        self._parar = threading.Event()
        self._hilo: threading.Thread | None = None

    # ---- revisiones individuales
    def _revisar_bd(self):
        try:
            repo.hacer_ping(self.engine)
            self.estado.marcar("base de datos", True)
        except Exception as e:
            self.estado.marcar("base de datos", False, f"no responde ({e.__class__.__name__})")

    def _revisar_mlflow(self) -> bool:
        try:
            httpx.get(f"{self.cfg.mlflow_uri}/health", timeout=3).raise_for_status()
            self.estado.marcar("mlflow", True)
            return True
        except Exception as e:
            self.estado.marcar("mlflow", False, f"no responde ({e.__class__.__name__})")
            return False

    def _revisar_airflow(self):
        ok, motivo = self.orquestador.salud()
        self.estado.marcar("airflow", ok, "" if ok else motivo)

    def _sincronizar_modelo(self, mlflow_ok: bool):
        """Carga el modelo si todavía no hay ninguno, o si el alias cambió de versión."""
        if mlflow_ok:
            try:
                vigente = registro.version_vigente(self.cfg)
            except registro.SinModeloVigente:
                return  # el primer modelo todavía no se entrena
            actual = self.estado._modelo
            if actual is not None and actual.version == vigente.version and actual.origen == "mlflow":
                return
            if actual is not None and actual.version == vigente.version:
                pass  # estaba cargado desde la caché, se vuelve a cargar desde MLflow
            try:
                modelo = registro.cargar_modelo(self.cfg, vigente.version)
            except Exception as e:
                log.warning("no se pudo cargar la versión %s: %s", vigente.version, e)
                return
            self.estado.poner_modelo(ModeloActivo(modelo, vigente.nombre, vigente.alias, vigente.version, "mlflow"))
            try:
                registro.guardar_cache(self.cfg, vigente.version, modelo)
            except Exception as e:
                log.warning("no se pudo guardar la caché del modelo: %s", e)
            log.info("modelo cargado: %s versión %s", vigente.nombre, vigente.version)
        elif not self.estado.hay_modelo():
            # MLflow caído y la API acaba de arrancar: se usa la última copia local
            c = registro.leer_cache(self.cfg)
            if c:
                version, modelo = c
                self.estado.poner_modelo(
                    ModeloActivo(modelo, self.cfg.modelo_nombre, self.cfg.modelo_alias, version, "cache")
                )
                log.info("MLflow no responde; se cargó la versión %s desde la caché local", version)

    # ---- ciclo
    def tick(self):
        self._revisar_bd()
        mlflow_ok = self._revisar_mlflow()
        self._revisar_airflow()
        try:
            self._sincronizar_modelo(mlflow_ok)
        except Exception as e:
            log.warning("falló la sincronización del modelo: %s", e)

    def _correr(self):
        while not self._parar.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("error inesperado en el vigilante")
            self._parar.wait(self.cfg.intervalo_vigilancia)

    def iniciar(self):
        self._hilo = threading.Thread(target=self._correr, name="vigilante", daemon=True)
        self._hilo.start()

    def detener(self):
        self._parar.set()
        if self._hilo:
            self._hilo.join(timeout=5)
