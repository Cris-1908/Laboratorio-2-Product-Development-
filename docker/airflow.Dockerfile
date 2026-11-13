# Airflow 2.10 con un entorno de Python aparte (venv-train) donde corre el entrenamiento.
# Ese entorno usa los mismos requirements.txt que la API: el modelo se guarda con pickle y
# tiene que abrirse con exactamente las mismas versiones de scikit-learn, pandas y numpy.
FROM apache/airflow:2.10.5-python3.12

USER airflow
COPY --chown=airflow:root requirements.txt /tmp/build/requirements.txt
COPY --chown=airflow:root pyproject.toml /tmp/build/pyproject.toml
COPY --chown=airflow:root src /tmp/build/src

RUN python -m venv /opt/airflow/venv-train \
    && /opt/airflow/venv-train/bin/pip install --no-cache-dir --disable-pip-version-check -r /tmp/build/requirements.txt \
    && /opt/airflow/venv-train/bin/pip install --no-cache-dir --disable-pip-version-check --no-deps /tmp/build \
    && rm -rf /tmp/build

ENV PYTHON_ENTRENAMIENTO=/opt/airflow/venv-train/bin/python
