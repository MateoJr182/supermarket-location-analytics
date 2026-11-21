"""Filtro de celdas inhabitables (distancia X) y variables de entorno.

Lee las tablas del ETL v2 (data/processed/celdas_<ciudad>_<tamaño>m.parquet) y genera, para cada
tamaño de celda, una tabla con ambas ciudades lista para modelar:
data/processed/modelo_<tamaño>m.parquet

Pasos:
1. Distancia de cada celda a la celda poblada más cercana (dist_poblada_m).
2. Filtro de habitabilidad: se conservan las celdas a X metros o menos de una celda poblada.
   X se elige con los datos: el menor candidato que conserva al menos el 99% de los supermercados
   de ambas ciudades en la grilla principal (250 m). El mismo X se usa en todos los tamaños.
3. Variables de entorno: sumas en radios de 500 m y 1 km (población, paraderos, comercio,
   educación, salud, deporte, vías) y fracción residencial promedio.
4. Variables de competencia (n_super_500m, n_super_1000m, dist_super_m): se guardan para la capa
   de decisión, pero NO se usan como predictores porque son la variable objetivo desplazada.

Uso:
    python -m src.features
    from src import features; features.ejecutar(features.cargar_config())
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.ndimage import convolve, distance_transform_edt

from src.etl import cargar_config, _log  # noqa: F401  (cargar_config se reexporta para el notebook)

COLUMNAS_COMPETENCIA = ["n_super_500m", "n_super_1000m", "dist_super_m"]


# ---------------------------------------------------------------------------
# Utilidades de grilla: la tabla se lleva a una matriz (fila = iy, columna = ix)
# ---------------------------------------------------------------------------

def a_matriz(tabla: pd.DataFrame, valores) -> np.ndarray:
    """Pone una columna (o arreglo) de la tabla en una matriz con la forma de la grilla."""
    matriz = np.zeros((tabla["iy"].max() + 1, tabla["ix"].max() + 1))
    matriz[tabla["iy"].to_numpy(), tabla["ix"].to_numpy()] = np.asarray(valores, dtype=float)
    return matriz


def de_matriz(tabla: pd.DataFrame, matriz: np.ndarray) -> np.ndarray:
    """Lee de la matriz el valor de cada celda de la tabla."""
    return matriz[tabla["iy"].to_numpy(), tabla["ix"].to_numpy()]


def kernel_circular(radio_m: float, tamano: float) -> np.ndarray:
    """Celdas cuyo centro está a radio_m o menos del centro de la celda (incluida ella misma)."""
    r = int(radio_m // tamano)
    dy, dx = np.mgrid[-r:r + 1, -r:r + 1]
    return ((dx**2 + dy**2) * tamano**2 <= radio_m**2 + 1e-6).astype(float)


def suma_entorno(tabla, valores, radio_m, tamano) -> np.ndarray:
    matriz = convolve(a_matriz(tabla, valores), kernel_circular(radio_m, tamano), mode="constant", cval=0.0)
    return de_matriz(tabla, matriz)


def promedio_entorno(tabla, valores, radio_m, tamano) -> np.ndarray:
    """Promedio en el radio, contando solo celdas que existen en la grilla de la ciudad."""
    k = kernel_circular(radio_m, tamano)
    suma = convolve(a_matriz(tabla, valores), k, mode="constant", cval=0.0)
    n = convolve(a_matriz(tabla, np.ones(len(tabla))), k, mode="constant", cval=0.0)
    return de_matriz(tabla, suma) / np.maximum(de_matriz(tabla, n), 1)


def distancia_a(tabla, condicion, tamano) -> np.ndarray:
    """Distancia (m) del centro de cada celda al centro de la celda más cercana que cumple la condición."""
    marcadas = a_matriz(tabla, condicion) > 0
    if not marcadas.any():
        return np.full(len(tabla), np.inf)
    return de_matriz(tabla, distance_transform_edt(~marcadas) * tamano)


# ---------------------------------------------------------------------------
# Variables por celda
# ---------------------------------------------------------------------------

def agregar_variables(tabla: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Agrega distancia a celda poblada, variables de entorno y de competencia."""
    f = cfg["features"]
    t = tabla.copy()
    tamano = float(t["tamano_m"].iloc[0])

    t["dist_poblada_m"] = distancia_a(t, t["poblacion_celda"] > 0, tamano).round(1)
    t["dist_paradero_m"] = distancia_a(t, t["conteo_paraderos"] > 0, tamano).round(1)

    for radio in f["radios_m"]:
        sufijo = f"_{int(radio)}m"
        t["pob" + sufijo] = suma_entorno(t, t["poblacion_celda"], radio, tamano).round().astype("int64")
        t["paraderos" + sufijo] = suma_entorno(t, t["conteo_paraderos"], radio, tamano).round().astype("int32")
        for grupo, categorias in f["grupos_poi"].items():
            valores = t[[f"conteo_{c}" for c in categorias]].sum(axis=1)
            t[grupo + sufijo] = suma_entorno(t, valores, radio, tamano).round().astype("int32")
        t["vias_km" + sufijo] = (suma_entorno(t, t["largo_vias_m"], radio, tamano) / 1000).round(2)
        t["residencial" + sufijo] = promedio_entorno(t, t["frac_residencial"], radio, tamano).round(3)

    # Competencia: solo para la capa de decisión, nunca como predictor.
    t["n_super_500m"] = suma_entorno(t, t["n_supermercados"], 500, tamano).round().astype("int32")
    t["n_super_1000m"] = suma_entorno(t, t["n_supermercados"], 1000, tamano).round().astype("int32")
    t["dist_super_m"] = distancia_a(t, t["n_supermercados"] > 0, tamano).round(1)
    return t


def columnas_predictoras(tabla: pd.DataFrame, cfg: dict) -> list[str]:
    """Variables que el modelo puede usar: de la celda y del entorno (sin competencia ni ubicación)."""
    excluir = {"id_celda", "ciudad", "tamano_m", "desfase_m", "ix", "iy", "x", "y", "crs_utm",
               "frac_dentro", "n_supermercados", "habitable", *COLUMNAS_COMPETENCIA}
    return [c for c in tabla.columns if c not in excluir]


# ---------------------------------------------------------------------------
# Filtro de habitabilidad
# ---------------------------------------------------------------------------

def curva_filtro_x(tablas: dict[str, pd.DataFrame], candidatos: list[float]) -> pd.DataFrame:
    """Para cada X candidato: celdas que quedan y % de supermercados y población que se conservan."""
    filas = []
    for ciudad, t in list(tablas.items()) + [("ambas", pd.concat(tablas.values(), ignore_index=True))]:
        for x in candidatos:
            queda = t["dist_poblada_m"] <= x + 1e-6
            filas.append({
                "ciudad": ciudad,
                "x_m": x,
                "celdas_habitables": int(queda.sum()),
                "pct_celdas": round(100 * queda.mean(), 1),
                "supermercados": int(t.loc[queda, "n_supermercados"].sum()),
                "pct_supermercados": round(100 * t.loc[queda, "n_supermercados"].sum() / t["n_supermercados"].sum(), 1),
                "pct_poblacion": round(100 * t.loc[queda, "poblacion_celda"].sum() / t["poblacion_celda"].sum(), 1),
            })
    return pd.DataFrame(filas)


def elegir_x(curva: pd.DataFrame, cobertura: float) -> float:
    """Menor X que conserva al menos `cobertura` de los supermercados de ambas ciudades juntas."""
    ambas = curva[curva["ciudad"] == "ambas"].sort_values("x_m")
    cumplen = ambas[ambas["pct_supermercados"] >= 100 * cobertura]
    if cumplen.empty:
        raise ValueError("Ningún X candidato alcanza la cobertura pedida; agregue candidatos más grandes.")
    return float(cumplen["x_m"].iloc[0])


# ---------------------------------------------------------------------------
# Corrida completa
# ---------------------------------------------------------------------------

def _corridas(cfg) -> list[tuple[int, str]]:
    g = cfg["grilla"]
    corridas = [(int(t), "") for t in g["tamanos_m"]]
    if g.get("desfase_sensibilidad_m"):
        corridas.append((int(g["tamano_principal_m"]), f"_d{int(g['desfase_sensibilidad_m'])}"))
    return corridas


def cargar_tablas(cfg, tamano: int, sufijo: str = "") -> dict[str, pd.DataFrame]:
    carpeta = Path(cfg["rutas"]["processed"])
    return {c: pd.read_parquet(carpeta / f"celdas_{c}_{tamano}m{sufijo}.parquet") for c in cfg["ciudades"]}


def ejecutar(cfg: dict) -> dict:
    """Elige X, filtra y agrega variables para todos los tamaños. Devuelve curva, X y resumen."""
    f = cfg["features"]
    salida = Path(cfg["rutas"]["processed"])
    resultados = Path(cfg["rutas"]["resultados_features"])
    resultados.mkdir(parents=True, exist_ok=True)
    principal = int(cfg["grilla"]["tamano_principal_m"])

    # 1. X se elige en la grilla principal
    base = {c: agregar_variables(t, cfg) for c, t in cargar_tablas(cfg, principal).items()}
    curva = curva_filtro_x(base, f["filtro_x"]["candidatos_m"])
    x_datos = elegir_x(curva, f["filtro_x"]["cobertura_supermercados"])
    x = float(f["filtro_x"]["x_fijo_m"]) if f["filtro_x"].get("x_fijo_m") is not None else x_datos
    curva.to_csv(resultados / "curva_filtro_x.csv", index=False)
    _log(f"X elegido con los datos: {x_datos:.0f} m; X usado: {x:.0f} m")

    # 2. Filtro + variables para cada tamaño
    filas = []
    for tamano, sufijo in _corridas(cfg):
        tablas = base if (tamano == principal and not sufijo) else {
            c: agregar_variables(t, cfg) for c, t in cargar_tablas(cfg, tamano, sufijo).items()}
        partes = []
        for ciudad, t in tablas.items():
            t = t.copy()
            t["habitable"] = t["dist_poblada_m"] <= x + 1e-6
            h = t[t["habitable"]]
            filas.append({
                "ciudad": ciudad, "tamano_m": tamano, "desfase_m": float(t["desfase_m"].iloc[0]),
                "celdas": len(t), "celdas_habitables": len(h),
                "pct_celdas_habitables": round(100 * len(h) / len(t), 1),
                "supermercados": int(t["n_supermercados"].sum()),
                "supermercados_conservados": int(h["n_supermercados"].sum()),
                "pct_supermercados_conservados": round(100 * h["n_supermercados"].sum() / t["n_supermercados"].sum(), 1),
                "pct_celdas_habitables_con_super": round(100 * (h["n_supermercados"] > 0).mean(), 2),
            })
            partes.append(h.drop(columns="habitable"))
        modelo = pd.concat(partes, ignore_index=True)
        ruta = salida / f"modelo_{tamano}m{sufijo}.parquet"
        modelo.to_parquet(ruta, index=False)
        _log(f"{tamano} m{' desplazada' if sufijo else ''}: {len(modelo):,} celdas habitables "
             f"({int(modelo['n_supermercados'].sum())} supermercados) -> {ruta}")

    resumen = pd.DataFrame(filas)
    resumen.to_csv(resultados / "resumen_filtro.csv", index=False)
    with open(resultados / "x_elegido.json", "w", encoding="utf-8") as fh:
        json.dump({"x_datos_m": x_datos, "x_usado_m": x,
                   "cobertura_supermercados": f["filtro_x"]["cobertura_supermercados"],
                   "tamano_referencia_m": principal}, fh, indent=2)
    _log("Listo.")
    return {"curva": curva, "x_m": x, "x_datos_m": x_datos, "resumen": resumen}


if __name__ == "__main__":
    r = ejecutar(cargar_config())
    print(r["resumen"].to_string(index=False))
