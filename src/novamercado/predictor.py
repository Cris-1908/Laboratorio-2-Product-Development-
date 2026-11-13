"""Del pedido ya limpio a las predicciones por familia."""
from datetime import date

import pandas as pd

from . import repositorio as repo
from .catalogos import Catalogos
from .contrato import ObservacionCalidad, PrediccionFamilia, SolicitudPrediccion
from .estado import EstadoServicio
from .features import construir_features, ultimo_dia_con_datos, ventana_historia, ventas_para_features

# si el último dato de la tienda es más viejo que esto, se avisa que el pronóstico se apoya en información atrasada
DIAS_MAX_SIN_DATOS = 3


class Predictor:
    def __init__(self, engine, cat: Catalogos, estado: EstadoServicio):
        self.engine = engine
        self.cat = cat
        self.estado = estado

    def predecir(self, sol: SolicitudPrediccion) -> tuple[list[PrediccionFamilia], str, list[ObservacionCalidad]]:
        activo = self.estado.modelo()  # levanta SinModelo si todavía no hay
        desde, hasta = ventana_historia(sol.fecha)
        ventas = repo.leer_ventas_tienda(self.engine, sol.tienda_id, desde, hasta)
        ventas = ventas_para_features(ventas)

        objetivos = pd.DataFrame(
            [{"tienda_id": sol.tienda_id, "familia_id": f.familia_id, "fecha": pd.Timestamp(sol.fecha), "en_promocion": f.en_promocion}
             for f in sol.familias]
        )
        df = construir_features(ventas, objetivos, self.cat)
        res = activo.modelo.predict(df)

        salida = [
            PrediccionFamilia(
                familia_id=fam,
                venta_esperada=round(float(esp), 1),
                cantidad_sugerida=int(max(0, sug)),
            )
            for fam, esp, sug in zip(df["familia_id"], res["venta_esperada"], res["cantidad_sugerida"])
        ]
        return salida, activo.version, self._avisos_de_datos(ventas, sol.tienda_id, sol.fecha)

    def _avisos_de_datos(self, ventas: pd.DataFrame, tienda_id: int, fecha: date) -> list[ObservacionCalidad]:
        ultimo = ultimo_dia_con_datos(ventas, tienda_id, fecha)
        if ultimo is None:
            return [ObservacionCalidad(
                campo="fecha", accion="advertir",
                detalle="no hay ventas previas de esta tienda; el pronóstico usa solo el promedio histórico del modelo",
            )]
        atraso = (fecha - ultimo).days - 1
        if atraso > DIAS_MAX_SIN_DATOS:
            return [ObservacionCalidad(
                campo="fecha", accion="advertir",
                detalle=f"las últimas ventas registradas de la tienda son del {ultimo.isoformat()} ({atraso} días sin observaciones); el pronóstico usa información atrasada",
            )]
        return []
