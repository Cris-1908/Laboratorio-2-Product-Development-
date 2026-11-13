"""DAG de reentrenamiento.

No tiene horario: la API lo dispara con POST /admin/reentrenar. Cada ejecución:

  entrenar_candidato -> decidir_promocion -> cerrar

  1. entrenar_candidato: lee histórico + observaciones de la base del producto, entrena el
     baseline y el modelo de ML, los evalúa en los últimos días y registra una versión nueva
     en MLflow (todavía sin alias).
  2. decidir_promocion: compara el candidato contra el vigente con el criterio de costo (ver
     DECISIONES.md) y solo si gana mueve el alias; la API lo recoge sola en menos de 2 minutos.
  3. cerrar: corre pase lo que pase. Si algo falló antes, deja constancia en la base y falla
     la ejecución. El alias nunca se toca fuera de la tarea 2, así que un fallo no afecta al
     modelo vigente.

Las tareas corren con un Python aparte (un venv con las mismas versiones que la API) para no
mezclar las dependencias del servicio con las de Airflow.
"""
import os
from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.utils.trigger_rule import TriggerRule

PYTHON = os.environ.get("PYTHON_ENTRENAMIENTO", "/opt/airflow/venv-train/bin/python")
LIMITE_TAREA = timedelta(minutes=8)


def paso(nombre: str) -> str:
    return f"{PYTHON} -m novamercado.reentrenar {nombre} --run-id '{{{{ run_id }}}}'"


with DAG(
    dag_id="reentrenar_modelo",
    description="Reentrena el modelo de pronóstico con el histórico y las observaciones registradas",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=False,
    dagrun_timeout=timedelta(minutes=10),
    default_args={"retries": 0},
    tags=["novamercado"],
) as dag:
    entrenar = BashOperator(
        task_id="entrenar_candidato",
        bash_command=paso("entrenar"),
        execution_timeout=LIMITE_TAREA,
    )
    decidir = BashOperator(
        task_id="decidir_promocion",
        bash_command=paso("decidir"),
        execution_timeout=LIMITE_TAREA,
    )
    cerrar = BashOperator(
        task_id="cerrar",
        bash_command=paso("cerrar"),
        trigger_rule=TriggerRule.ALL_DONE,
        execution_timeout=timedelta(minutes=2),
    )

    entrenar >> decidir >> cerrar
