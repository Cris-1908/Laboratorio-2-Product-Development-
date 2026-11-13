"""Simula la operación diaria de las tiendas contra el servicio y mide el costo de sus sugerencias.

Por cada día:  observaciones del día anterior  ->  solicitudes de pronóstico del día.
Cada cierto número de días dispara un reentrenamiento por el endpoint de administración.

Las ventas "reales" se inventan a partir del histórico publicado, corrido 364 días (para que
coincida el día de la semana), y opcionalmente con un cambio de demanda. No es el programa de
evaluación del equipo docente; sirve para ver el ciclo completo y para generar evidencia.

Uso contra el producto levantado con docker compose:

    python scripts/simular_operacion.py --url http://localhost:8000 --desde 2026-03-01 --dias 28 \
        --reentrenar-cada 7 --defectuosas 0.15 --deriva
"""
import argparse
import copy
import json
import random
import time
from datetime import date, timedelta
from pathlib import Path

import httpx
import pandas as pd

RAIZ = Path(__file__).resolve().parent.parent
TIENDAS_SIM = [1, 2, 3, 4, 5, 6, 7, 8, 9]  # la tienda 10 abrió hace poco y no tiene un año atrás


class Mundo:
    """La 'realidad': ventas y promociones por día, tienda y familia."""

    def __init__(self, data_dir: Path, deriva: bool, inicio_deriva: date, semilla: int = 0):
        h = pd.read_csv(data_dir / "historico_ventas.csv", parse_dates=["fecha"])
        self.tabla = h.set_index(["fecha", "tienda_id", "familia_id"])
        self.deriva = deriva
        self.inicio_deriva = inicio_deriva
        self.rng = random.Random(semilla)

    def dia(self, d: date, tienda: int) -> pd.DataFrame:
        origen = pd.Timestamp(d - timedelta(days=364))
        x = self.tabla.loc[origen].xs(tienda, level="tienda_id").copy()
        x["ventas"] = x["ventas"].astype(float)
        if self.deriva and d >= self.inicio_deriva:
            x.loc["bebidas", "ventas"] *= 1.4  # sube el nivel de una familia
            promo = x["en_promocion"] > 0
            x.loc[(x.index == "snacks") & promo, "ventas"] *= 1.7  # y la promoción rinde más en otra
        x["ventas"] = x["ventas"].round().astype(int)
        return x


# --------------------------------------------------------------- solicitudes defectuosas
def _tienda_texto(p):
    p["tienda_id"] = f"T{p['tienda_id']:02d}"


def _familia_mayusculas(p):
    i = random.randrange(len(p["familias"]))
    p["familias"][i]["familia_id"] = p["familias"][i]["familia_id"].upper()


def _promo_texto(p):
    i = random.randrange(len(p["familias"]))
    p["familias"][i]["en_promocion"] = str(p["familias"][i]["en_promocion"])


def _fecha_dmy(p):
    p["fecha"] = date.fromisoformat(p["fecha"]).strftime("%d/%m/%Y")


def _faltan_familias(p):
    for _ in range(random.randint(1, 3)):
        p["familias"].pop(random.randrange(len(p["familias"])))


def _repetida(p):
    p["familias"].append(dict(random.choice(p["familias"]), en_promocion=random.randint(0, 9)))


def _campo_renombrado(p):
    p["tienda"] = p.pop("tienda_id")


def _promo_negativa(p):
    p["familias"][0]["en_promocion"] = -2


def _promo_nula(p):
    p["familias"][random.randrange(len(p["familias"]))]["en_promocion"] = None


def _familia_inventada(p):
    p["familias"].append({"familia_id": "electrodomesticos", "en_promocion": 1})


def _fecha_imposible(p):  # esta se espera rechazada
    p["fecha"] = "2026-02-31"


def _tienda_inexistente(p):  # esta también
    p["tienda_id"] = 99


DEFECTOS_PREDICCION = [_tienda_texto, _familia_mayusculas, _promo_texto, _fecha_dmy, _faltan_familias,
                       _repetida, _campo_renombrado, _promo_negativa, _promo_nula, _familia_inventada]
RECHAZOS_PREDICCION = [_fecha_imposible, _tienda_inexistente]


def _ventas_negativa(o):
    o["ventas"][random.randrange(len(o["ventas"]))]["ventas"] = -5


def _ventas_texto_miles(o):
    i = random.randrange(len(o["ventas"]))
    o["ventas"][i]["ventas"] = f"{o['ventas'][i]['ventas']:,}"


def _ventas_coma_decimal(o):
    i = random.randrange(len(o["ventas"]))
    o["ventas"][i]["ventas"] = f"{o['ventas'][i]['ventas']},0"


DEFECTOS_OBSERVACION = [_ventas_negativa, _ventas_texto_miles, _ventas_coma_decimal]


# --------------------------------------------------------------- simulación
def simular(cliente, mundo: Mundo, costos: pd.DataFrame, desde: date, dias: int, reentrenar_cada: int,
            defectuosas: float, esperar_reentrenar, imprimir=print) -> pd.DataFrame:
    filas = []
    cuenta_estado = {}
    for k in range(dias):
        d = desde + timedelta(days=k)

        if k > 0:  # primero llegan las ventas reales de ayer
            ayer = d - timedelta(days=1)
            for t in TIENDAS_SIM:
                real = mundo.dia(ayer, t)
                cuerpo = {"tienda_id": t, "fecha": ayer.isoformat(),
                          "ventas": [{"familia_id": f, "ventas": int(v)} for f, v in real["ventas"].items()]}
                if random.random() < defectuosas:
                    random.choice(DEFECTOS_OBSERVACION)(cuerpo)
                r = cliente.post("/v1/observaciones", json=cuerpo)
                assert r.status_code == 200, r.text

        # después las solicitudes de pronóstico de hoy
        for t in TIENDAS_SIM:
            real = mundo.dia(d, t)
            cuerpo = {"tienda_id": t, "fecha": d.isoformat(),
                      "familias": [{"familia_id": f, "en_promocion": int(p)} for f, p in real["en_promocion"].items()]}
            esperado_rechazo = False
            if random.random() < defectuosas:
                defecto = random.choice(DEFECTOS_PREDICCION + RECHAZOS_PREDICCION)
                cuerpo = copy.deepcopy(cuerpo)
                defecto(cuerpo)
                esperado_rechazo = defecto in RECHAZOS_PREDICCION
            r = cliente.post("/v1/prediccion", json=cuerpo)
            if esperado_rechazo:
                assert r.status_code == 422, r.text
                cuenta_estado["rechazada"] = cuenta_estado.get("rechazada", 0) + 1
                continue
            assert r.status_code == 200, (cuerpo, r.text)
            resp = r.json()
            cuenta_estado[resp["calidad"]["estado"]] = cuenta_estado.get(resp["calidad"]["estado"], 0) + 1
            sug = {p["familia_id"]: p["cantidad_sugerida"] for p in resp["predicciones"]}
            esp = {p["familia_id"]: p["venta_esperada"] for p in resp["predicciones"]}
            for fam, v in real["ventas"].items():
                if fam not in sug:
                    continue
                c = costos.loc[fam]
                costo = c.costo_faltante * max(v - sug[fam], 0) + c.costo_sobrante * max(sug[fam] - v, 0)
                filas.append({"fecha": d, "tienda_id": t, "familia_id": fam, "real": v, "esperada": esp[fam],
                              "sugerida": sug[fam], "costo": costo, "version": resp["version_modelo"]})

        if reentrenar_cada and (k + 1) % reentrenar_cada == 0 and k + 1 < dias:
            resultado = esperar_reentrenar()
            imprimir(f"  [{d}] reentrenamiento: {resultado}")

    df = pd.DataFrame(filas)
    imprimir(f"\nCalidad de las respuestas: {cuenta_estado}")
    return df


def resumen(df: pd.DataFrame, imprimir=print):
    imprimir("\nCosto por día (suma de las 9 tiendas) y versión del modelo:")
    por_dia = df.groupby("fecha").agg(costo=("costo", "sum"), version=("version", "first"))
    por_dia["wape"] = df.groupby("fecha").apply(lambda g: (g.real - g.esperada).abs().sum() / g.real.sum(), include_groups=False)
    imprimir(por_dia.round(3).to_string())
    imprimir(f"\nCosto total: {df.costo.sum():,.0f}   costo medio por fila: {df.costo.mean():.2f}")
    imprimir("\nPor versión del modelo:")
    imprimir(df.groupby("version").agg(filas=("costo", "size"), costo_medio=("costo", "mean")).round(2).to_string())


def reentrenar_por_http(cliente, espera_maxima: int = 900):
    def _ejecutar():
        r = cliente.post("/admin/reentrenar")
        assert r.status_code == 202, r.text
        run_id = r.json()["id"]
        limite = time.time() + espera_maxima
        while time.time() < limite:
            e = cliente.get(f"/admin/reentrenar/{run_id}").json()
            if e["estado"] != "en_curso":
                return e
            time.sleep(5)
        raise TimeoutError("el reentrenamiento no terminó a tiempo")
    return _ejecutar


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--data", default=str(RAIZ / "data"))
    ap.add_argument("--desde", default="2026-03-01")
    ap.add_argument("--dias", type=int, default=28)
    ap.add_argument("--reentrenar-cada", type=int, default=7)
    ap.add_argument("--defectuosas", type=float, default=0.1, help="fracción de solicitudes con algún defecto")
    ap.add_argument("--deriva", action="store_true", help="cambia el comportamiento de la demanda a mitad del periodo")
    ap.add_argument("--semilla", type=int, default=1)
    a = ap.parse_args()

    random.seed(a.semilla)
    desde = date.fromisoformat(a.desde)
    mundo = Mundo(Path(a.data), a.deriva, desde + timedelta(days=a.dias // 2), a.semilla)
    costos = pd.read_csv(Path(a.data) / "costos.csv").set_index("familia_id")
    with httpx.Client(base_url=a.url, timeout=60) as cliente:
        print("modelo vigente al iniciar:", cliente.get("/modelo").json())
        df = simular(cliente, mundo, costos, desde, a.dias, a.reentrenar_cada, a.defectuosas, reentrenar_por_http(cliente))
        resumen(df)
        print("\nmodelo vigente al terminar:", cliente.get("/modelo").json())


if __name__ == "__main__":
    main()
