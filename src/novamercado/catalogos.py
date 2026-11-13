"""Carga de los catálogos publicados (tiendas, familias, costos y feriados)."""
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd


def normalizar_texto(texto: str) -> str:
    """minúsculas, sin acentos y con guion bajo: 'Frutas y Verduras ' -> 'frutas_y_verduras'."""
    s = unicodedata.normalize("NFKD", str(texto))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.strip().lower()
    for ch in (" ", "-", "/", "."):
        s = s.replace(ch, "_")
    while "__" in s:
        s = s.replace("__", "_")
    return s.strip("_")


@dataclass
class Catalogos:
    tiendas: pd.DataFrame  # tienda_id, nombre, ciudad, tipo, fecha_apertura (date)
    familias: list[str]
    nombres_familia: dict[str, str]
    costos: pd.DataFrame  # indexado por familia_id
    feriados: pd.DataFrame  # fecha (date), nombre, tipo, ambito
    fin_calendario: date

    def ids_tienda(self) -> set[int]:
        return set(int(t) for t in self.tiendas["tienda_id"])

    def tienda(self, tienda_id: int) -> pd.Series:
        return self.tiendas.set_index("tienda_id").loc[tienda_id]

    def tau(self) -> dict[str, float]:
        """Cuantil óptimo por familia: c_faltante / (c_faltante + c_sobrante)."""
        c = self.costos
        return (c["costo_faltante"] / (c["costo_faltante"] + c["costo_sobrante"])).to_dict()


def cargar_catalogos(data_dir: Path) -> Catalogos:
    data_dir = Path(data_dir)
    tiendas = pd.read_csv(data_dir / "tiendas.csv")
    tiendas["fecha_apertura"] = pd.to_datetime(tiendas["fecha_apertura"]).dt.date
    fam = pd.read_csv(data_dir / "familias.csv")
    costos = pd.read_csv(data_dir / "costos.csv").set_index("familia_id")
    feriados = pd.read_csv(data_dir / "feriados.csv")
    feriados["fecha"] = pd.to_datetime(feriados["fecha"]).dt.date

    return Catalogos(
        tiendas=tiendas,
        familias=list(fam["familia_id"]),
        nombres_familia=dict(zip(fam["familia_id"], fam["nombre"])),
        costos=costos,
        feriados=feriados,
        fin_calendario=max(feriados["fecha"]),
    )
