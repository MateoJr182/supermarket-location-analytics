"""ETL v2: grilla espacial y variables por celda para Gran Santiago y Gran Concepción.

Cambios respecto de la v1 (archivo/01_etl_v1.ipynb, tag v1.0):
- Celdas completas alineadas a múltiplos del tamaño (sin gpd.clip): 125, 250, 500 y 1000 m
  quedan anidadas y las celdas del borde ya no son más chicas.
- Puntos asignados a su celda por índice, no con sjoin: más rápido y sin dobles conteos.
- Variable objetivo de conteo (n_supermercados) en vez de binaria.
- Variables nuevas: largo de vías, distancia a vía principal y fracción residencial.
- Salida en Parquet con coordenadas (ix, iy, x, y) para el análisis espacial.
- Controles de calidad que detienen la corrida si algo no cuadra.

Uso:
    python -m src.etl                 # corre todo según config.yaml
    from src import etl; etl.ejecutar(etl.cargar_config())
"""
from __future__ import annotations

import time
import unicodedata
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
import yaml

WGS84 = "EPSG:4326"


# ---------------------------------------------------------------------------
# Configuración y utilidades
# ---------------------------------------------------------------------------

def cargar_config(ruta: str | Path = "config.yaml") -> dict:
    with open(ruta, encoding="utf-8") as f:
        return yaml.safe_load(f)


def normalizar_nombre(nombre: str) -> str:
    return (
        unicodedata.normalize("NFKD", str(nombre))
        .encode("ascii", "ignore")
        .decode("ascii")
        .strip()
        .upper()
    )


def _log(mensaje: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {mensaje}", flush=True)


# ---------------------------------------------------------------------------
# 1. Máscara de cada ciudad
# ---------------------------------------------------------------------------

def crear_mascara(comunas: gpd.GeoDataFrame, nombres: list[str], crs_utm: str):
    """Une las comunas pedidas. Devuelve la máscara en UTM y en WGS84."""
    if "COMUNA" not in comunas.columns:
        raise KeyError("La cartografía comunal no tiene la columna 'COMUNA'.")
    normalizadas = comunas["COMUNA"].map(normalizar_nombre)
    buscadas = {normalizar_nombre(n) for n in nombres}
    seleccion = comunas[normalizadas.isin(buscadas)]
    faltantes = buscadas - set(normalizadas[normalizadas.isin(buscadas)])
    if faltantes:
        raise ValueError(f"No se encontraron estas comunas: {sorted(faltantes)}")
    mascara_utm = seleccion.to_crs(crs_utm).geometry.union_all()
    mascara_wgs84 = gpd.GeoSeries([mascara_utm], crs=crs_utm).to_crs(WGS84).iloc[0]
    return mascara_utm, mascara_wgs84


# ---------------------------------------------------------------------------
# 2. Capas de OpenStreetMap
# ---------------------------------------------------------------------------

def _dentro(gdf: gpd.GeoDataFrame, mascara_utm) -> gpd.GeoDataFrame:
    """Deja solo los puntos que caen en la máscara (la lectura con mask puede traer bordes)."""
    return gdf[gdf.intersects(mascara_utm)].copy()


def extraer_capas(cfg: dict, mascara_utm, mascara_wgs84, crs_utm: str) -> dict:
    """Lee las capas OSM de la ciudad y las devuelve en UTM."""
    ruta = cfg["rutas"]["gpkg_osm"]
    osm = cfg["osm"]

    usos = gpd.read_file(ruta, layer="gis_osm_landuse_a_free", mask=mascara_wgs84)
    residenciales = usos[usos["fclass"] == "residential"].to_crs(crs_utm)

    vias = gpd.read_file(ruta, layer="gis_osm_roads_free", mask=mascara_wgs84)
    vias = vias[vias["fclass"].isin(osm["clases_viales"])].to_crs(crs_utm)

    transporte = gpd.read_file(ruta, layer="gis_osm_transport_free", mask=mascara_wgs84)
    transporte = transporte[transporte["fclass"].isin(osm["clases_transporte"])].to_crs(crs_utm)
    transporte = _dentro(transporte, mascara_utm)

    # Puntos de interés: capa de puntos + polígonos (edificios) convertidos a centroide.
    categorias = osm["categorias_poi"]
    puntos = gpd.read_file(ruta, layer="gis_osm_pois_free", mask=mascara_wgs84)
    puntos = puntos[puntos["fclass"].isin(categorias)].to_crs(crs_utm)
    poligonos = gpd.read_file(ruta, layer="gis_osm_pois_a_free", mask=mascara_wgs84)
    poligonos = poligonos[poligonos["fclass"].isin(categorias)].to_crs(crs_utm)

    # Duplicado = el polígono contiene un punto de la misma categoría o comparte osm_id (igual que v1).
    cruce = gpd.sjoin(poligonos, puntos[["fclass", "geometry"]], predicate="contains")
    duplicados = cruce.index[cruce["fclass_left"] == cruce["fclass_right"]]
    es_duplicado = poligonos.index.isin(duplicados) | poligonos["osm_id"].isin(puntos["osm_id"]).to_numpy()
    poligonos = poligonos[~es_duplicado].copy()
    poligonos["geometry"] = poligonos.geometry.centroid
    poligonos["origen"] = "poligono"
    puntos["origen"] = "punto"

    pois = pd.concat([puntos, poligonos], ignore_index=True)
    pois = _dentro(gpd.GeoDataFrame(pois, geometry="geometry", crs=crs_utm), mascara_utm)

    return {
        "residenciales": residenciales,
        "vias": vias,
        "transporte": transporte,
        "pois": pois,
        "poligonos_descartados": int(es_duplicado.sum()),
    }


def guardar_capas(capas: dict, carpeta: str | Path, prefijo: str) -> None:
    """Guarda las capas intermedias en data/interim (una capa por tabla del GeoPackage)."""
    carpeta = Path(carpeta)
    carpeta.mkdir(parents=True, exist_ok=True)
    ruta = carpeta / f"{prefijo}_capas.gpkg"
    if ruta.exists():
        ruta.unlink()
    for nombre in ["residenciales", "vias", "transporte", "pois"]:
        capa = capas[nombre]
        columnas = [c for c in ["osm_id", "fclass", "name", "origen", "geometry"] if c in capa.columns]
        capa[columnas].to_file(ruta, layer=nombre, driver="GPKG")


# ---------------------------------------------------------------------------
# 3. Censo 2024
# ---------------------------------------------------------------------------

def centroides_censo(manzanas: gpd.GeoDataFrame, mascara_utm, crs_utm: str) -> gpd.GeoDataFrame:
    """Centroide de cada manzana (calculado en UTM antes de filtrar) con su población n_per."""
    if "n_per" not in manzanas.columns:
        raise KeyError("Las manzanas censales no tienen la columna 'n_per'.")
    caja = gpd.GeoSeries([mascara_utm], crs=crs_utm).to_crs(manzanas.crs).total_bounds
    cerca = manzanas.cx[caja[0]:caja[2], caja[1]:caja[3]]
    # Se usa la geometría activa (.geometry): en el parquet del INE la columna no se llama "geometry".
    centroides = gpd.GeoDataFrame(
        {"n_per": pd.to_numeric(cerca["n_per"], errors="raise").to_numpy()},
        geometry=cerca.geometry.to_crs(crs_utm).centroid.to_numpy(),
        crs=crs_utm,
    )
    return _dentro(centroides, mascara_utm)


# ---------------------------------------------------------------------------
# 4. Grilla y variables por celda
# ---------------------------------------------------------------------------

def construir_grilla(mascara_utm, crs_utm: str, tamano: float, desfase: float = 0.0):
    """Celdas completas que tocan la máscara, con origen alineado a múltiplos del tamaño.

    Devuelve la grilla y el origen (x0, y0). Con desfase > 0 la grilla se corre en x e y.
    """
    xmin, ymin, xmax, ymax = mascara_utm.bounds
    x0 = np.floor(xmin / tamano) * tamano - desfase
    y0 = np.floor(ymin / tamano) * tamano - desfase
    nx = int(np.ceil((xmax - x0) / tamano)) + 1
    ny = int(np.ceil((ymax - y0) / tamano)) + 1
    ix, iy = [a.ravel() for a in np.meshgrid(np.arange(nx), np.arange(ny))]
    celdas = shapely.box(x0 + ix * tamano, y0 + iy * tamano,
                         x0 + (ix + 1) * tamano, y0 + (iy + 1) * tamano)
    shapely.prepare(mascara_utm)
    toca = shapely.intersects(celdas, mascara_utm)
    ix, iy, celdas = ix[toca], iy[toca], celdas[toca]

    # Fracción de la celda dentro de la máscara: solo en las celdas de borde, con una
    # muestra de 5 x 5 puntos por celda (rápido y con precisión de 4%).
    frac = np.ones(len(celdas))
    borde = ~shapely.within(celdas, mascara_utm)
    k = 5
    paso = (np.arange(k) + 0.5) / k * tamano
    bx = (x0 + ix[borde] * tamano)[:, None, None] + paso[None, None, :]
    by = (y0 + iy[borde] * tamano)[:, None, None] + paso[None, :, None]
    bx, by = np.broadcast_arrays(bx, by)
    adentro = shapely.contains_xy(mascara_utm, bx.ravel(), by.ravel()).reshape(-1, k * k)
    frac[borde] = adentro.mean(axis=1)

    grilla = gpd.GeoDataFrame(
        {
            "ix": ix.astype("int32"),
            "iy": iy.astype("int32"),
            "x": x0 + (ix + 0.5) * tamano,
            "y": y0 + (iy + 0.5) * tamano,
            "frac_dentro": frac.round(2),
        },
        geometry=celdas,
        crs=crs_utm,
    )
    return grilla, (x0, y0)


def contar_por_celda(grilla, puntos, origen, tamano, nombre, peso=None) -> pd.DataFrame:
    """Cuenta (o suma `peso`) los puntos de cada celda usando su índice (ix, iy)."""
    x0, y0 = origen
    ix = np.floor((puntos.geometry.x.to_numpy() - x0) / tamano).astype(int)
    iy = np.floor((puntos.geometry.y.to_numpy() - y0) / tamano).astype(int)
    valores = np.ones(len(puntos)) if peso is None else puntos[peso].to_numpy(dtype=float)
    conteo = pd.Series(valores).groupby([ix, iy]).sum().rename(nombre)
    conteo.index.names = ["ix", "iy"]
    salida = grilla.join(conteo, on=["ix", "iy"])
    salida[nombre] = salida[nombre].fillna(0)
    return salida


def largo_vias(grilla, vias) -> pd.Series:
    """Metros de vía (primary, secondary, tertiary, trunk) dentro de cada celda."""
    if vias.empty:
        return pd.Series(0.0, index=grilla.index)
    tramos = gpd.overlay(vias[["geometry"]], grilla[["ix", "iy", "geometry"]],
                         how="intersection", keep_geom_type=True)
    largo = tramos.assign(m=tramos.length).groupby(["ix", "iy"])["m"].sum()
    return grilla.join(largo.rename("m"), on=["ix", "iy"])["m"].fillna(0.0)


def distancia_a_vias(grilla, vias) -> np.ndarray:
    """Distancia en metros desde el centro de cada celda a la vía más cercana de `vias`."""
    if vias.empty:
        return np.full(len(grilla), np.nan)
    centros = gpd.GeoDataFrame(geometry=gpd.points_from_xy(grilla["x"], grilla["y"]), crs=grilla.crs)
    cercana = gpd.sjoin_nearest(centros, vias[["geometry"]], distance_col="dist")
    return cercana.groupby(level=0)["dist"].min().reindex(centros.index).to_numpy()


def fraccion_residencial(grilla, residenciales, tamano) -> pd.Series:
    """Fracción del área de la celda cubierta por uso de suelo residencial (OSM)."""
    if residenciales.empty:
        return pd.Series(0.0, index=grilla.index)
    poligonos = gpd.GeoDataFrame(geometry=residenciales.geometry.make_valid(), crs=grilla.crs)
    poligonos = poligonos[poligonos.geom_type.isin(["Polygon", "MultiPolygon"])]
    partes = gpd.overlay(grilla[["ix", "iy", "geometry"]], poligonos, how="intersection", keep_geom_type=True)
    area = partes.assign(a=partes.area).groupby(["ix", "iy"])["a"].sum() / tamano**2
    return grilla.join(area.rename("a"), on=["ix", "iy"])["a"].fillna(0.0).clip(0, 1)


def tabla_celdas(cfg, prefijo, capas, censo, mascara_utm, crs_utm, tamano, desfase=0.0) -> pd.DataFrame:
    """Arma la tabla de una ciudad para un tamaño de celda y verifica que todo cuadre."""
    grilla, origen = construir_grilla(mascara_utm, crs_utm, tamano, desfase)
    pois = capas["pois"]

    grilla = contar_por_celda(grilla, pois[pois["fclass"] == "supermarket"], origen, tamano, "n_supermercados")
    grilla = contar_por_celda(grilla, capas["transporte"], origen, tamano, "conteo_paraderos")
    for categoria in cfg["osm"]["categorias_poi"]:
        if categoria == "supermarket":
            continue
        grilla = contar_por_celda(grilla, pois[pois["fclass"] == categoria], origen, tamano, f"conteo_{categoria}")
    grilla = contar_por_celda(grilla, censo, origen, tamano, "poblacion_celda", peso="n_per")

    vias = capas["vias"]
    principales = vias[vias["fclass"].isin(cfg["osm"]["clases_via_principal"])]
    grilla["largo_vias_m"] = largo_vias(grilla, vias).round(1)
    grilla["dist_via_principal_m"] = np.round(distancia_a_vias(grilla, principales), 1)
    grilla["frac_residencial"] = fraccion_residencial(grilla, capas["residenciales"], tamano).round(4)

    # Controles de calidad: si algo no cuadra, la corrida se detiene.
    n_super = int((pois["fclass"] == "supermarket").sum())
    assert grilla["n_supermercados"].sum() == n_super, "Hay supermercados sin celda."
    assert grilla["conteo_paraderos"].sum() == len(capas["transporte"]), "Hay paraderos sin celda."
    assert np.isclose(grilla["poblacion_celda"].sum(), censo["n_per"].sum()), "Hay población sin celda."
    assert not grilla.duplicated(["ix", "iy"]).any(), "Hay celdas duplicadas."

    conteos = [c for c in grilla.columns if c.startswith(("n_", "conteo_"))]
    grilla[conteos] = grilla[conteos].astype("int32")
    grilla["poblacion_celda"] = grilla["poblacion_celda"].round().astype("int32")
    grilla.insert(0, "id_celda", [f"{prefijo}_{int(tamano)}_{a}_{b}" for a, b in zip(grilla["ix"], grilla["iy"])])
    grilla.insert(1, "ciudad", prefijo)
    grilla.insert(2, "tamano_m", int(tamano))
    grilla.insert(3, "desfase_m", float(desfase))
    grilla["crs_utm"] = crs_utm
    return pd.DataFrame(grilla.drop(columns="geometry"))


def geometria_celdas(tabla: pd.DataFrame) -> gpd.GeoDataFrame:
    """Reconstruye los polígonos de las celdas desde x, y y tamano_m (para mapas)."""
    medio = tabla["tamano_m"].to_numpy() / 2
    geometria = shapely.box(tabla["x"] - medio, tabla["y"] - medio, tabla["x"] + medio, tabla["y"] + medio)
    return gpd.GeoDataFrame(tabla, geometry=geometria, crs=tabla["crs_utm"].iloc[0])


def resumen(tabla: pd.DataFrame) -> dict:
    con_super = tabla["n_supermercados"] > 0
    return {
        "ciudad": tabla["ciudad"].iloc[0],
        "tamano_m": int(tabla["tamano_m"].iloc[0]),
        "desfase_m": float(tabla["desfase_m"].iloc[0]),
        "celdas": len(tabla),
        "celdas_pobladas": int((tabla["poblacion_celda"] > 0).sum()),
        "supermercados": int(tabla["n_supermercados"].sum()),
        "celdas_con_super": int(con_super.sum()),
        "celdas_con_2mas": int((tabla["n_supermercados"] >= 2).sum()),
        "pct_celdas_con_super_que_tienen_2mas": round(100 * (tabla["n_supermercados"] >= 2).sum() / max(con_super.sum(), 1), 1),
        "pct_celdas_con_super_sin_poblacion": round(100 * (con_super & (tabla["poblacion_celda"] == 0)).sum() / max(con_super.sum(), 1), 1),
        "poblacion": int(tabla["poblacion_celda"].sum()),
    }


# ---------------------------------------------------------------------------
# 5. Corrida completa
# ---------------------------------------------------------------------------

def ejecutar(cfg: dict, ciudades: list[str] | None = None, guardar_interim: bool = True) -> pd.DataFrame:
    """Corre el ETL para todas las ciudades y tamaños de celda. Devuelve el resumen."""
    rutas = cfg["rutas"]
    for clave in ["gpkg_osm", "censo_manzanas", "censo_comunas"]:
        if not Path(rutas[clave]).is_file():
            raise FileNotFoundError(f"No se encontró {rutas[clave]}")
    salida = Path(rutas["processed"])
    salida.mkdir(parents=True, exist_ok=True)
    Path(rutas["resultados"]).mkdir(parents=True, exist_ok=True)

    _log("Leyendo cartografía censal...")
    comunas = gpd.read_parquet(rutas["censo_comunas"])
    manzanas = gpd.read_parquet(rutas["censo_manzanas"])

    g = cfg["grilla"]
    corridas = [(t, 0.0) for t in g["tamanos_m"]]
    if g.get("desfase_sensibilidad_m"):
        corridas.append((g["tamano_principal_m"], float(g["desfase_sensibilidad_m"])))

    filas = []
    for prefijo, ciudad in cfg["ciudades"].items():
        if ciudades and prefijo not in ciudades:
            continue
        crs = ciudad["crs_utm"]
        _log(f"{ciudad['nombre']}: máscara de {len(ciudad['comunas'])} comunas")
        mascara_utm, mascara_wgs84 = crear_mascara(comunas, ciudad["comunas"], crs)

        _log(f"{ciudad['nombre']}: extrayendo capas OSM")
        capas = extraer_capas(cfg, mascara_utm, mascara_wgs84, crs)
        if guardar_interim:
            guardar_capas(capas, rutas["interim"], prefijo)
        censo = centroides_censo(manzanas, mascara_utm, crs)
        n_super = int((capas["pois"]["fclass"] == "supermarket").sum())
        _log(f"{ciudad['nombre']}: {n_super} supermercados, {len(capas['transporte'])} paraderos, "
             f"{len(capas['pois'])} POI, {censo['n_per'].sum():,.0f} habitantes")

        for tamano, desfase in corridas:
            tabla = tabla_celdas(cfg, prefijo, capas, censo, mascara_utm, crs, tamano, desfase)
            sufijo = f"_d{int(desfase)}" if desfase else ""
            ruta = salida / f"celdas_{prefijo}_{int(tamano)}m{sufijo}.parquet"
            tabla.to_parquet(ruta, index=False)
            fila = resumen(tabla)
            filas.append(fila)
            _log(f"  {int(tamano)} m{' desplazada' if desfase else ''}: {fila['celdas']:,} celdas, "
                 f"{fila['celdas_con_super']} con supermercado ({fila['celdas_con_2mas']} con 2 o más) -> {ruta}")

    tabla_resumen = pd.DataFrame(filas)
    tabla_resumen.to_csv(Path(rutas["resultados"]) / "resumen_tamanos.csv", index=False)
    _log("Listo.")
    return tabla_resumen


if __name__ == "__main__":
    print(ejecutar(cargar_config()).to_string(index=False))
