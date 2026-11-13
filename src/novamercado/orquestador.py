"""Cliente mínimo de la API REST de Airflow (v1, autenticación básica)."""
import uuid
from datetime import datetime, timezone

import httpx


class OrquestadorNoDisponible(Exception):
    pass


class Orquestador:
    def __init__(self, url: str, usuario: str, clave: str, dag_id: str, timeout: float = 5.0):
        self.url = url.rstrip("/")
        self.auth = (usuario, clave)
        self.dag_id = dag_id
        self.timeout = timeout

    def disparar(self) -> str:
        run_id = f"api__{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:6]}"
        try:
            r = httpx.post(
                f"{self.url}/api/v1/dags/{self.dag_id}/dagRuns",
                json={"dag_run_id": run_id, "conf": {}},
                auth=self.auth, timeout=self.timeout,
            )
        except httpx.HTTPError as e:
            raise OrquestadorNoDisponible(f"no se pudo contactar a Airflow: {e.__class__.__name__}") from e
        if r.status_code not in (200, 201):
            raise OrquestadorNoDisponible(f"Airflow respondió {r.status_code} al crear la ejecución: {r.text[:200]}")
        return run_id

    def estado(self, run_id: str) -> str | None:
        """queued | running | success | failed, o None si Airflow no conoce esa ejecución."""
        try:
            r = httpx.get(f"{self.url}/api/v1/dags/{self.dag_id}/dagRuns/{run_id}", auth=self.auth, timeout=self.timeout)
        except httpx.HTTPError as e:
            raise OrquestadorNoDisponible(f"no se pudo contactar a Airflow: {e.__class__.__name__}") from e
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise OrquestadorNoDisponible(f"Airflow respondió {r.status_code}")
        return r.json().get("state")

    def salud(self) -> tuple[bool, str]:
        try:
            r = httpx.get(f"{self.url}/health", timeout=self.timeout)
            datos = r.json()
        except Exception as e:
            return False, f"Airflow no responde ({e.__class__.__name__})"
        for parte in ("metadatabase", "scheduler"):
            if datos.get(parte, {}).get("status") != "healthy":
                return False, f"Airflow: {parte} no está saludable"
        return True, "ok"
