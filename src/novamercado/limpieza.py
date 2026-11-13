"""Limpieza y validación de lo que mandan las tiendas.

La idea general (se explica con más calma en DECISIONES.md):

  REPARAR   cuando solo cambió la forma y el significado es uno solo.
            'T05' -> 5, 'LÁCTEOS' -> 'lacteos', '17/11/2025' -> 2025-11-17, '1,234' -> 1234.
  ADVERTIR  cuando para seguir hay que suponer algo. Se responde igual, pero se avisa
            qué se supuso (faltan familias, valores en conflicto, fecha ambigua...).
  RECHAZAR  cuando no hay forma honesta de saber de qué tienda, de qué día o de qué
            producto se habla. Se responde 422 con el motivo.

Nada de esto toca la base ni el modelo; son funciones puras para poder probarlas solas.
"""
import math
import re
from datetime import date, datetime
from difflib import get_close_matches

from .catalogos import Catalogos, normalizar_texto
from .contrato import (
    FamiliaPromocion,
    ObservacionCalidad,
    SolicitudObservacion,
    SolicitudPrediccion,
    VentaFamilia,
)

PROMO_MAXIMA_PLAUSIBLE = 500
# una venta diaria mayor a esto veces el máximo histórico de esa tienda y familia se considera un error de captura
FACTOR_VENTA_ABSURDA = 20

ALIAS = {
    "tienda_id": {"tienda", "idtienda", "codigotienda", "codtienda", "store", "storeid", "sucursal", "idsucursal", "numerotienda"},
    "fecha": {"date", "dia", "fechapronostico", "fechaprediccion", "fechaventa", "fechaobservacion"},
    "familias": {"familia", "productos", "categorias", "lineas", "items", "detalle", "families"},
    "ventas": {"venta", "ventasreales", "reales", "unidades", "detalle", "items", "familias", "lineas"},
    "familia_id": {"familia", "idfamilia", "family", "familyid", "categoria", "linea", "nombrefamilia", "producto"},
    "en_promocion": {"promocion", "promociones", "promo", "enpromo", "promos", "productosenpromocion", "numpromociones", "onpromotion"},
    "ventas_item": {"venta", "unidades", "cantidad", "vendido", "real", "total", "sales"},
}

MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}


class Rechazo(Exception):
    """Se levanta cuando la solicitud no se puede atender. El texto va en el campo detail del 422."""


class Registro:
    """Junta las observaciones de calidad que se van generando."""

    def __init__(self):
        self.items: list[ObservacionCalidad] = []

    def reparar(self, campo: str, detalle: str):
        self.items.append(ObservacionCalidad(campo=campo, accion="reparar", detalle=detalle))

    def advertir(self, campo: str, detalle: str):
        self.items.append(ObservacionCalidad(campo=campo, accion="advertir", detalle=detalle))


def _faltan(n: int, cosa: str = "familia") -> str:
    """'falta 1 familia' / 'faltan 3 familias'."""
    return f"falta 1 {cosa}" if n == 1 else f"faltan {n} {cosa}s"


def _compacto(nombre: str) -> str:
    return normalizar_texto(nombre).replace("_", "")


def _buscar_campo(obj: dict, canonico: str, alias: set[str], ruta: str, reg: Registro):
    """Devuelve (existe, valor). Acepta el nombre exacto, o uno equivalente (E07)."""
    if canonico in obj:
        return True, obj[canonico]
    meta = _compacto(canonico)
    candidatos = [k for k in obj if isinstance(k, str) and (_compacto(k) == meta or _compacto(k) in alias)]
    if not candidatos:
        return False, None
    elegido = candidatos[0]
    reg.reparar(f"{ruta}{canonico}" if ruta else canonico, f"el campo '{elegido}' se interpretó como '{canonico}'")
    return True, obj[elegido]


# ---------------------------------------------------------------- tienda
def _parsear_tienda(valor, reg: Registro) -> int:
    if isinstance(valor, bool) or valor is None:
        raise Rechazo("tienda_id: falta el identificador de la tienda o no es válido")
    if isinstance(valor, int):
        return valor
    if isinstance(valor, float):
        if math.isfinite(valor) and valor == int(valor):
            reg.reparar("tienda_id", f"{valor} se interpretó como {int(valor)}")
            return int(valor)
        raise Rechazo(f"tienda_id: '{valor}' no es un identificador de tienda")
    if isinstance(valor, str):
        m = re.fullmatch(r"\s*[A-Za-z_\-# ]*0*(\d+)\s*\.?0*\s*", valor)
        if m:
            n = int(m.group(1))
            reg.reparar("tienda_id", f"'{valor}' se interpretó como {n}")
            return n
    raise Rechazo(f"tienda_id: no se pudo interpretar {valor!r} como un identificador de tienda")


# ---------------------------------------------------------------- fecha
def _parsear_fecha(valor, reg: Registro, campo: str = "fecha") -> date:
    if valor is None or isinstance(valor, bool) or not isinstance(valor, str):
        raise Rechazo(f"{campo}: falta la fecha o no tiene un formato que se pueda leer (se espera AAAA-MM-DD)")
    s = valor.strip()

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return _fecha_o_rechazo(*map(int, s.split("-")), original=valor, campo=campo)

    # ISO con hora: 2025-11-17T00:00:00, 2025-11-17 08:30:00, con zona o sin ella
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?", s)
    if m:
        f = _fecha_o_rechazo(int(m.group(1)), int(m.group(2)), int(m.group(3)), original=valor, campo=campo)
        reg.reparar(campo, f"'{valor}' se interpretó como {f.isoformat()} (se descartó la hora)")
        return f

    m = re.fullmatch(r"(\d{4})[/.](\d{1,2})[/.](\d{1,2})", s) or re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s)
    if m:
        f = _fecha_o_rechazo(int(m.group(1)), int(m.group(2)), int(m.group(3)), original=valor, campo=campo)
        reg.reparar(campo, f"'{valor}' se interpretó como {f.isoformat()}")
        return f

    m = re.fullmatch(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})", s)
    if m:
        a, b, anio = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if a > 12 and b <= 12:
            dia, mes, ambiguo = a, b, False
        elif b > 12 and a <= 12:
            dia, mes, ambiguo = b, a, False  # vino como mes/día
        else:
            dia, mes, ambiguo = a, b, a != b and a <= 12 and b <= 12
        f = _fecha_o_rechazo(anio, mes, dia, original=valor, campo=campo)
        if ambiguo:
            reg.advertir(campo, f"'{valor}' puede ser día/mes o mes/día; se asumió día/mes y se leyó como {f.isoformat()}")
        else:
            reg.reparar(campo, f"'{valor}' se interpretó como {f.isoformat()}")
        return f

    m = re.fullmatch(r"(\d{1,2})\s+de\s+([a-záéíóú]+)\s+(?:de|del)\s+(\d{4})", s.lower())
    if m and normalizar_texto(m.group(2)) in MESES:
        f = _fecha_o_rechazo(int(m.group(3)), MESES[normalizar_texto(m.group(2))], int(m.group(1)), original=valor, campo=campo)
        reg.reparar(campo, f"'{valor}' se interpretó como {f.isoformat()}")
        return f

    raise Rechazo(f"{campo}: no se pudo interpretar {valor!r} como fecha (se espera AAAA-MM-DD)")


def _fecha_o_rechazo(anio: int, mes: int, dia: int, original, campo: str) -> date:
    try:
        return date(anio, mes, dia)
    except ValueError:
        raise Rechazo(f"{campo}: {original!r} no es una fecha del calendario")


# ---------------------------------------------------------------- números
def parsear_numero(texto: str) -> float | None:
    """Lee '1,234' (miles), '812,0' (decimal), '1.234,5', '1,234.5', '-4'. None si no se puede.

    Regla para un único separador: si le siguen exactamente tres dígitos y antes hay
    algo distinto de cero, es separador de miles ('1,234' -> 1234); si no, es decimal.
    """
    s = re.sub(r"[\s ']", "", texto)
    if not s:
        return None
    signo = 1
    if s[0] in "+-":
        signo = -1 if s[0] == "-" else 1
        s = s[1:]
    if not re.fullmatch(r"[\d.,]+", s) or not re.search(r"\d", s):
        return None

    if "." in s and "," in s:
        decimal = "." if s.rfind(".") > s.rfind(",") else ","
        miles = "," if decimal == "." else "."
        if s.count(decimal) != 1:
            return None
        s = s.replace(miles, "").replace(decimal, ".")
    elif "." in s or "," in s:
        sep = "." if "." in s else ","
        partes = s.split(sep)
        if len(partes) > 2:
            if not all(len(p) == 3 for p in partes[1:]) or not partes[0]:
                return None
            s = "".join(partes)
        else:
            antes, despues = partes
            if len(despues) == 3 and antes not in ("", "0"):
                s = antes + despues
            else:
                s = antes + "." + despues
    try:
        return signo * float(s)
    except ValueError:
        return None


def _numero(valor) -> tuple[float | None, bool]:
    """(número, vino_como_texto_o_decimal). None si no es un número."""
    if isinstance(valor, bool) or valor is None:
        return None, False
    if isinstance(valor, int):
        return float(valor), False
    if isinstance(valor, float):
        return (valor, True) if math.isfinite(valor) else (None, False)
    if isinstance(valor, str):
        n = parsear_numero(valor)
        return (n, True) if n is not None and math.isfinite(n) else (None, False)
    return None, False


# ---------------------------------------------------------------- familias
class _Resolutor:
    def __init__(self, cat: Catalogos):
        self.ids = {normalizar_texto(f): f for f in cat.familias}
        self.nombres = {normalizar_texto(n): f for f, n in cat.nombres_familia.items()}
        self.todas = {**self.nombres, **self.ids}

    def resolver(self, valor) -> tuple[str | None, str]:
        """Devuelve (familia_id o None, tipo) con tipo en: exacto, reparado, aproximado, desconocido."""
        if not isinstance(valor, str) or not valor.strip():
            return None, "desconocido"
        if valor in self.ids.values():
            return valor, "exacto"
        n = normalizar_texto(valor)
        if n in self.todas:
            return self.todas[n], "reparado"
        # singular/plural y errores de dedo
        cercanos = get_close_matches(n, list(self.todas), n=2, cutoff=0.84)
        if cercanos:
            elegidos = {self.todas[c] for c in cercanos}
            if len(elegidos) == 1:
                return elegidos.pop(), "aproximado"
        return None, "desconocido"


def _resolver_familias(items: list, cat: Catalogos, reg: Registro, ruta: str, clave_valor) -> list[tuple[int, str, object]]:
    """Recorre la lista de la solicitud y devuelve (posición, familia_id, valor_crudo) de lo que sí se reconoció."""
    res = _Resolutor(cat)
    salida = []
    for i, it in enumerate(items):
        pref = f"{ruta}[{i}]."
        if not isinstance(it, dict):
            reg.advertir(f"{ruta}[{i}]", f"el elemento {it!r} no es un objeto con familia; se ignoró")
            continue
        existe, bruto = _buscar_campo(it, "familia_id", ALIAS["familia_id"], pref, reg)
        if not existe:
            reg.advertir(f"{pref}familia_id", "el elemento no trae familia; se ignoró")
            continue
        fam, tipo = res.resolver(bruto)
        if tipo == "desconocido":
            reg.advertir(f"{pref}familia_id", f"la familia {bruto!r} no existe en el catálogo; se ignoró")
            continue
        if tipo == "reparado":
            reg.reparar(f"{pref}familia_id", f"'{bruto}' se interpretó como '{fam}'")
        elif tipo == "aproximado":
            reg.advertir(f"{pref}familia_id", f"'{bruto}' no coincide exactamente con ninguna familia; se asumió '{fam}'")
        salida.append((i, fam, it))
    return salida


def _lista_de_familias(obj: dict, canonico: str, cat: Catalogos, reg: Registro) -> list | None:
    existe, items = _buscar_campo(obj, canonico, ALIAS[canonico], "", reg)
    if not existe or items is None:
        return None
    if isinstance(items, dict):
        # {"abarrotes": 0, "bebidas": 8}: se convierte a lista de objetos
        reg.reparar(canonico, "la lista llegó como objeto {familia: valor}; se convirtió a lista")
        clave = "en_promocion" if canonico == "familias" else "ventas"
        return [{"familia_id": k, clave: v} for k, v in items.items()]
    if not isinstance(items, list):
        raise Rechazo(f"{canonico}: se esperaba una lista y llegó {type(items).__name__}")
    return items


# ---------------------------------------------------------------- tienda y fecha en contexto
def _validar_tienda_y_fecha(tienda_id: int, fecha: date, cat: Catalogos, fecha_min: date | None):
    if tienda_id not in cat.ids_tienda():
        raise Rechazo(f"tienda_id: la tienda {tienda_id} no existe en el catálogo")
    t = cat.tienda(tienda_id)
    if fecha < t["fecha_apertura"]:
        raise Rechazo(f"fecha: la tienda {tienda_id} abrió el {t['fecha_apertura'].isoformat()}, no hay operación el {fecha.isoformat()}")
    if fecha_min is not None and fecha < fecha_min:
        raise Rechazo(f"fecha: {fecha.isoformat()} es anterior al inicio de los datos ({fecha_min.isoformat()})")


# ================================================================= predicción
def limpiar_prediccion(
    crudo: dict, cat: Catalogos, fecha_min: date | None = None
) -> tuple[SolicitudPrediccion, list[ObservacionCalidad]]:
    reg = Registro()

    ok, t_raw = _buscar_campo(crudo, "tienda_id", ALIAS["tienda_id"], "", reg)
    if not ok:
        raise Rechazo("tienda_id: falta el identificador de la tienda")
    tienda_id = _parsear_tienda(t_raw, reg)

    ok, f_raw = _buscar_campo(crudo, "fecha", ALIAS["fecha"], "", reg)
    if not ok:
        raise Rechazo("fecha: falta la fecha del pronóstico")
    fecha = _parsear_fecha(f_raw, reg)

    _validar_tienda_y_fecha(tienda_id, fecha, cat, fecha_min)
    if fecha > cat.fin_calendario:
        reg.advertir(
            "fecha",
            f"el calendario de feriados llega hasta {cat.fin_calendario.isoformat()}; para {fecha.isoformat()} se asumió un día sin feriados ni fechas especiales",
        )

    items = _lista_de_familias(crudo, "familias", cat, reg)
    if items is None or len(items) == 0:
        reg.advertir("familias", "la solicitud no trae familias; se pronosticaron todas con en_promocion=0")
        items = []

    reconocidas = _resolver_familias(items, cat, reg, "familias", None)
    if items and not reconocidas:
        raise Rechazo("familias: ninguna de las familias enviadas existe en el catálogo")

    # una familia puede venir repetida
    por_familia: dict[str, list[tuple[int, object]]] = {}
    for i, fam, it in reconocidas:
        por_familia.setdefault(fam, []).append((i, it))

    familias_out: list[FamiliaPromocion] = []
    for fam, apariciones in por_familia.items():
        valores = []
        for i, it in apariciones:
            pref = f"familias[{i}].en_promocion"
            valores.append((i, _parsear_promocion(it, pref, reg)))
        if len(apariciones) == 1:
            promo = valores[0][1]
        else:
            distintos = {v for _, v in valores}
            ult_i, ult_v = valores[-1]
            if len(distintos) == 1:
                reg.reparar(f"familias[{ult_i}].familia_id", f"'{fam}' venía repetida con el mismo valor; se dejó una sola vez")
            else:
                lista = ", ".join(str(v) for _, v in valores)
                reg.advertir(
                    f"familias[{ult_i}].familia_id",
                    f"'{fam}' venía repetida con valores distintos de en_promocion ({lista}); se usó el último ({ult_v})",
                )
            promo = ult_v
        familias_out.append(FamiliaPromocion(familia_id=fam, en_promocion=promo))

    faltan = [f for f in cat.familias if f not in por_familia]
    if faltan and items:
        reg.advertir(
            "familias",
            f"{_faltan(len(faltan))} ({', '.join(faltan)}); se pronosticaron con en_promocion=0",
        )
    for f in faltan:
        familias_out.append(FamiliaPromocion(familia_id=f, en_promocion=0))

    return SolicitudPrediccion(tienda_id=tienda_id, fecha=fecha, familias=familias_out), reg.items


def _parsear_promocion(item: dict, ruta: str, reg: Registro) -> int:
    existe, bruto = _buscar_campo(item, "en_promocion", ALIAS["en_promocion"], ruta.rsplit(".", 1)[0] + ".", reg)
    if not existe or bruto is None:
        reg.advertir(ruta, "no viene la cantidad de productos en promoción; se asumió 0")
        return 0
    n, transformado = _numero(bruto)
    if n is None:
        reg.advertir(ruta, f"{bruto!r} no es un número; se asumió 0")
        return 0
    if n < 0:
        reg.advertir(ruta, f"la cantidad en promoción no puede ser negativa ({bruto!r}); se asumió 0")
        return 0
    if n > PROMO_MAXIMA_PLAUSIBLE:
        reg.advertir(ruta, f"{bruto!r} no es una cantidad de productos en promoción creíble; se asumió 0")
        return 0
    if n != int(n):
        reg.advertir(ruta, f"{bruto!r} no es un número entero; se redondeó a {int(round(n))}")
        return int(round(n))
    if transformado:
        reg.reparar(ruta, f"{bruto!r} se interpretó como el entero {int(n)}")
    return int(n)


# ================================================================= observaciones
def limpiar_observacion(
    crudo: dict, cat: Catalogos, fecha_min: date | None = None, venta_maxima: dict | None = None
) -> tuple[SolicitudObservacion, list[ObservacionCalidad]]:
    """venta_maxima: {(tienda_id, familia_id): máximo histórico}, para detectar capturas absurdas."""
    reg = Registro()
    venta_maxima = venta_maxima or {}

    ok, t_raw = _buscar_campo(crudo, "tienda_id", ALIAS["tienda_id"], "", reg)
    if not ok:
        raise Rechazo("tienda_id: falta el identificador de la tienda")
    tienda_id = _parsear_tienda(t_raw, reg)

    ok, f_raw = _buscar_campo(crudo, "fecha", ALIAS["fecha"], "", reg)
    if not ok:
        raise Rechazo("fecha: falta la fecha de la observación")
    fecha = _parsear_fecha(f_raw, reg)
    _validar_tienda_y_fecha(tienda_id, fecha, cat, fecha_min)

    items = _lista_de_familias(crudo, "ventas", cat, reg)
    if not items:
        raise Rechazo("ventas: la observación no trae ventas")

    reconocidas = _resolver_familias(items, cat, reg, "ventas", None)
    if not reconocidas:
        raise Rechazo("ventas: ninguna de las familias enviadas existe en el catálogo")

    por_familia: dict[str, list[tuple[int, object]]] = {}
    for i, fam, it in reconocidas:
        por_familia.setdefault(fam, []).append((i, it))

    salida: list[VentaFamilia] = []
    for fam, apariciones in por_familia.items():
        valores = []
        for i, it in apariciones:
            v = _parsear_venta(it, f"ventas[{i}].ventas", reg, venta_maxima.get((tienda_id, fam)))
            if v is not None:
                valores.append((i, v))
        if not valores:
            continue  # todas las apariciones eran inservibles; ya quedó advertido
        if len(apariciones) > 1:
            distintos = {v for _, v in valores}
            ult_i, ult_v = valores[-1]
            if len(distintos) == 1:
                reg.reparar(f"ventas[{ult_i}].familia_id", f"'{fam}' venía repetida con el mismo valor; se dejó una sola vez")
            else:
                lista = ", ".join(str(v) for _, v in valores)
                reg.advertir(
                    f"ventas[{ult_i}].familia_id",
                    f"'{fam}' venía repetida con valores distintos ({lista}); se usó el último ({ult_v})",
                )
        salida.append(VentaFamilia(familia_id=fam, ventas=valores[-1][1]))

    if not salida:
        raise Rechazo("ventas: ninguna venta es utilizable (ver valores negativos o ilegibles)")

    faltan = [f for f in cat.familias if f not in {s.familia_id for s in salida}]
    if faltan:
        reg.advertir("ventas", f"no vienen ventas de {len(faltan)} {'familia' if len(faltan) == 1 else 'familias'} ({', '.join(faltan)}); no se registraron")

    return SolicitudObservacion(tienda_id=tienda_id, fecha=fecha, ventas=salida), reg.items


def _parsear_venta(item: dict, ruta: str, reg: Registro, maximo: float | None) -> int | None:
    existe, bruto = _buscar_campo(item, "ventas", ALIAS["ventas_item"], ruta.rsplit(".", 1)[0] + ".", reg)
    if not existe or bruto is None:
        reg.advertir(ruta, "no viene el valor de ventas; no se registró esa familia")
        return None
    n, transformado = _numero(bruto)
    if n is None:
        reg.advertir(ruta, f"{bruto!r} no es un número; no se registró esa familia")
        return None
    if n < 0:
        reg.advertir(ruta, f"venta negativa ({bruto!r}), probablemente una devolución; no se registró esa familia")
        return None
    if maximo is not None and n > FACTOR_VENTA_ABSURDA * max(maximo, 1):
        reg.advertir(ruta, f"{bruto!r} es mucho mayor que cualquier venta histórica de esta tienda y familia (máx. {int(maximo)}); no se registró")
        return None
    if n != int(n):
        reg.advertir(ruta, f"{bruto!r} no es un número entero de unidades; se redondeó a {int(round(n))}")
        return int(round(n))
    if transformado:
        reg.reparar(ruta, f"{bruto!r} se interpretó como {int(n)}")
    return int(n)
