"""Arranque inicial: crea tablas, carga el histórico y entrena el primer modelo si hace falta.

Es idempotente: si ya hay datos cargados y un modelo vigente, no repite nada.
Corre en su propio contenedor (`init`) antes de que se necesite el modelo.
"""
import logging
import sys
import time
import uuid

import httpx

from . import registro_modelos as registro
from . import repositorio as repo
from .config import cargar_config
from .db import crear_engine, crear_tablas
from .reentrenar import decidir_promocion, entrenar_candidato

log = logging.getLogger("bootstrap")


def esperar(nombre: str, comprobar, segundos: int = 300):
    limite = time.time() + segundos
    while True:
        try:
            comprobar()
            log.info("%s listo", nombre)
            return
        except Exception as e:
            if time.time() > limite:
                raise RuntimeError(f"{nombre} no estuvo disponible en {segundos}s: {e}") from e
            time.sleep(3)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = cargar_config()
    engine = crear_engine(cfg.database_url)

    esperar("base de datos", lambda: repo.hacer_ping(engine))
    crear_tablas(engine)

    n = repo.cargar_historico(engine, cfg.data_dir)
    log.info("histórico: %s", f"{n} filas cargadas" if n else "ya estaba cargado")

    esperar("mlflow", lambda: httpx.get(f"{cfg.mlflow_uri}/health", timeout=3).raise_for_status())
    try:
        v = registro.version_vigente(cfg)
        log.info("ya existe un modelo vigente (versión %s); no se entrena", v.version)
        return 0
    except registro.SinModeloVigente:
        pass

    run_id = f"inicial__{uuid.uuid4().hex[:8]}"
    log.info("no hay modelo vigente: entrenando el primero (%s)", run_id)
    entrenar_candidato(cfg, run_id)
    r = decidir_promocion(cfg, run_id)
    log.info("listo: %s", r["motivo"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
