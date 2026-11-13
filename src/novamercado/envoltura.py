"""Envoltura pyfunc para guardar nuestro modelo en MLflow.

Vive en su propio módulo (y no dentro de una función) para que MLflow la guarde por
referencia al paquete en vez de copiar su bytecode; así el modelo se abre igual en cualquier
contenedor que tenga instalado novamercado.
"""
import mlflow.pyfunc


class EnvolturaModelo(mlflow.pyfunc.PythonModel):
    def __init__(self, modelo):
        self.modelo = modelo

    def predict(self, context, model_input, params=None):
        return self.modelo.predict(model_input)
