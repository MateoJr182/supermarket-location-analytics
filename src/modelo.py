"""Modelo de conteo de supermercados, validación por bloques espaciales y brecha por zona.

Lee data/processed/modelo_<tamaño>m.parquet (ambas ciudades, solo celdas habitables) y sigue cinco pasos:

0. Partición. Antes de cualquier decisión se aparta como PRUEBA FINAL un 20% de los bloques de 3 km,
   sorteado por ciudad y según si el bloque tiene supermercado. Esas celdas no se usan para elegir nada.
1. Desarrollo (80% restante). Con validación cruzada de 5 particiones por bloques de 3 km se prueban
   unas pocas configuraciones de gradient boosting (ajuste ligero) y se comparan tres modelos: nulo
   (promedio por ciudad), regresión de Poisson (GLM) y gradient boosting con pérdida Poisson.
2. Desarrollo: validación aleatoria contra bloques de 1, 2, 3 y 5 km, importancia de variables y
   coeficientes del GLM.
3. Prueba final. Los modelos elegidos se entrenan con todo el desarrollo y se miden UNA sola vez en el
   20% apartado, con intervalos bootstrap por bloques y precisión/recall en el k% de celdas mejor puntuadas.
4. Sensibilidad al tamaño de celda, con las mismas reglas (métricas solo en desarrollo).
5. Mapa final. Con la configuración ya elegida, cada celda se predice con un modelo que no vio su bloque
   (5 particiones sobre todas las celdas). Esas predicciones se calibran por ciudad (total esperado =
   total observado) y alimentan la brecha = esperados − observados en 1 km, las zonas candidatas y el
   dashboard.

Uso:
    python -m src.modelo
    from src import modelo; modelo.ejecutar(modelo.cargar_config())
"""
from __future__ import annotations

import warnings
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, RegressorMixin
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.inspection import permutation_importance
from sklearn.linear_model import PoissonRegressor
from sklearn.metrics import average_precision_score, d2_tweedie_score, roc_auc_score
from sklearn.model_selection import GroupKFold, KFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer, StandardScaler

from src import features
from src.etl import _log, cargar_config  # noqa: F401  (cargar_config se reexporta para el notebook)

NOMBRES_MODELOS = {
    "nulo": "Nulo (promedio por ciudad)",
    "glm": "Poisson (GLM)",
    "gbm": "Gradient boosting (Poisson)",
}


# ---------------------------------------------------------------------------
# Datos
# ---------------------------------------------------------------------------

def cargar_tabla(cfg: dict, tamano: int | None = None, sufijo: str = "") -> pd.DataFrame:
    tamano = tamano or cfg["modelo"]["tamano_m"]
    ruta = Path(cfg["rutas"]["processed"]) / f"modelo_{int(tamano)}m{sufijo}.parquet"
    tabla = pd.read_parquet(ruta).reset_index(drop=True)
    tabla["es_ccp"] = (tabla["ciudad"] == "ccp").astype(int)
    return tabla


def matriz_x(tabla: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Variables predictoras (de la celda y del entorno) más el indicador de ciudad."""
    columnas = features.columnas_predictoras(tabla.drop(columns="es_ccp"), cfg) + ["es_ccp"]
    return tabla[columnas].astype(float)


def pesos_por_ciudad(tabla: pd.DataFrame) -> np.ndarray:
    """Cada ciudad pesa lo mismo en el ajuste (Santiago tiene ~3 veces más celdas)."""
    n = tabla.groupby("ciudad")["ciudad"].transform("size")
    return (len(tabla) / (tabla["ciudad"].nunique() * n)).to_numpy()


def bloques(tabla: pd.DataFrame, lado_m: float) -> pd.Series:
    """Bloque cuadrado de `lado_m` metros al que pertenece cada celda (separado por ciudad)."""
    return (tabla["ciudad"] + "_" + (tabla["x"] // lado_m).astype(int).astype(str)
            + "_" + (tabla["y"] // lado_m).astype(int).astype(str))


# ---------------------------------------------------------------------------
# Modelos
# ---------------------------------------------------------------------------

class PromedioPorCiudad(RegressorMixin, BaseEstimator):
    """Modelo nulo: predice el promedio de supermercados por celda de cada ciudad."""

    def fit(self, X, y, sample_weight=None):
        ccp = X["es_ccp"].to_numpy() == 1
        y = np.asarray(y, dtype=float)
        self.media_ = {True: y[ccp].mean(), False: y[~ccp].mean()}
        return self

    def predict(self, X):
        return np.where(X["es_ccp"].to_numpy() == 1, self.media_[True], self.media_[False])


def crear_modelos(cfg: dict, params_gbm: dict) -> dict:
    m = cfg["modelo"]
    return {
        "nulo": PromedioPorCiudad(),
        # Los conteos y distancias son muy asimétricos: log(1 + x) y luego estandarizar.
        "glm": Pipeline([
            ("log1p", FunctionTransformer(np.log1p, feature_names_out="one-to-one")),
            ("escala", StandardScaler()),
            ("poisson", PoissonRegressor(alpha=m["poisson_alpha"], max_iter=3000)),
        ]),
        "gbm": crear_gbm(cfg, params_gbm),
    }


def crear_gbm(cfg: dict, params: dict) -> HistGradientBoostingRegressor:
    return HistGradientBoostingRegressor(loss="poisson", random_state=cfg["modelo"]["semilla"],
                                         early_stopping=False, **params)


def ajuste_ligero(cfg, tabla, X, y, w, divisiones) -> tuple[dict, pd.DataFrame]:
    """Evalúa las configuraciones de gradient boosting de config.yaml y devuelve la de mayor D².

    Se eligen con la misma validación por bloques que se reporta, así que el D² del modelo elegido es
    levemente optimista; la tabla completa muestra cuánto varía entre configuraciones.
    """
    filas = []
    for params in cfg["modelo"]["gradient_boosting_grilla"]:
        mu = predicciones_fuera_de_muestra(crear_gbm(cfg, params), X, y, w, divisiones)
        por_ciudad = {f["ciudad"]: f for f in metricas(tabla, y, mu, cfg["modelo"]["top_pct_captura"])}
        filas.append({**params, "d2_ambas": por_ciudad["ambas"]["d2_poisson"],
                      **{f"d2_{c}": f["d2_poisson"] for c, f in por_ciudad.items() if c != "ambas"},
                      "pr_auc_ambas": por_ciudad["ambas"]["pr_auc"],
                      "esperados_sobre_observados": mu.sum() / y.sum()})
    grilla = pd.DataFrame(filas).sort_values("d2_ambas", ascending=False).reset_index(drop=True)
    claves = list(cfg["modelo"]["gradient_boosting_grilla"][0])
    mejor = {k: (int(v) if float(v).is_integer() and k != "learning_rate" and k != "l2_regularization" else float(v))
             for k, v in grilla.loc[0, claves].items()}
    return mejor, grilla


def ajustar(modelo, X, y, w=None):
    if w is None:
        return modelo.fit(X, y)
    if isinstance(modelo, Pipeline):
        return modelo.fit(X, y, **{f"{modelo.steps[-1][0]}__sample_weight": w})
    return modelo.fit(X, y, sample_weight=w)


def predicciones_fuera_de_muestra(modelo_base, X, y, w, divisiones) -> np.ndarray:
    """Predice cada celda con un modelo entrenado sin su grupo de validación."""
    from sklearn.base import clone
    mu = np.zeros(len(y))
    for entrena, prueba in divisiones:
        m = ajustar(clone(modelo_base), X.iloc[entrena], y[entrena], None if w is None else w[entrena])
        mu[prueba] = m.predict(X.iloc[prueba])
    return np.clip(mu, 1e-6, None)


def divisiones_espaciales(tabla, lado_m, n_folds):
    return list(GroupKFold(n_splits=n_folds).split(tabla, groups=bloques(tabla, lado_m)))


def divisiones_aleatorias(tabla, n_folds, semilla):
    return list(KFold(n_splits=n_folds, shuffle=True, random_state=semilla).split(tabla))


# ---------------------------------------------------------------------------
# Métricas
# ---------------------------------------------------------------------------

def captura_top(y, mu, pct) -> float:
    """% de los supermercados que caen en el `pct`% de celdas con mayor predicción."""
    n = max(1, int(round(len(mu) * pct / 100)))
    top = np.argsort(-mu, kind="stable")[:n]
    return 100 * y[top].sum() / max(y.sum(), 1)


def metricas(tabla, y, mu, pct_top) -> list[dict]:
    filas = []
    for ciudad, idx in list(tabla.groupby("ciudad").indices.items()) + [("ambas", np.arange(len(y)))]:
        yy, mm = y[idx], mu[idx]
        filas.append({
            "ciudad": ciudad,
            "d2_poisson": d2_tweedie_score(yy, mm, power=1),
            "pr_auc": average_precision_score(yy > 0, mm),
            "roc_auc": roc_auc_score(yy > 0, mm),
            f"captura_top{pct_top}": captura_top(yy, mm, pct_top),
            "esperados": mm.sum(),
            "observados": yy.sum(),
        })
    return filas


# ---------------------------------------------------------------------------
# Diagnóstico
# ---------------------------------------------------------------------------

def moran_residuos(tabla, y, mu, permutaciones=999, semilla=42) -> pd.DataFrame:
    """I de Moran (8 vecinas, 360 m) de los residuos de Pearson y de la variable objetivo."""
    import esda
    import libpysal
    filas = []
    for ciudad, idx in tabla.groupby("ciudad").indices.items():
        xy = tabla.loc[idx, ["x", "y"]].to_numpy()
        w = libpysal.weights.DistanceBand(xy, threshold=360, binary=True, silence_warnings=True)
        w.transform = "r"
        residuo = (y[idx] - mu[idx]) / np.sqrt(mu[idx])
        for nombre, valores in [("n_supermercados", y[idx].astype(float)), ("residuo de Pearson", residuo)]:
            np.random.seed(semilla)
            mi = esda.Moran(valores, w, permutations=permutaciones)
            filas.append({"ciudad": ciudad, "variable": nombre, "I": mi.I, "p_sim": mi.p_sim})
    return pd.DataFrame(filas)


def importancia(modelo_base, X, y, w, divisiones, repeticiones=3, semilla=42) -> pd.DataFrame:
    """Importancia por permutación medida en los bloques de prueba (caída del D² de Poisson)."""
    from sklearn.base import clone
    caidas = []
    for entrena, prueba in divisiones:
        m = ajustar(clone(modelo_base), X.iloc[entrena], y[entrena], w[entrena])
        r = permutation_importance(m, X.iloc[prueba], y[prueba], n_repeats=repeticiones, random_state=semilla,
                                   scoring=lambda est, Xp, yp: d2_tweedie_score(yp, np.clip(est.predict(Xp), 1e-6, None), power=1))
        caidas.append(r.importances_mean)
    caidas = np.array(caidas)
    return (pd.DataFrame({"variable": X.columns, "caida_d2": caidas.mean(axis=0), "desv": caidas.std(axis=0)})
            .sort_values("caida_d2", ascending=False).reset_index(drop=True))


def coeficientes_glm(tabla, X, y) -> tuple[pd.DataFrame, dict]:
    """GLM de Poisson en statsmodels (variables log1p y estandarizadas) para interpretar coeficientes.

    Devuelve la razón de tasas por una desviación estándar con su intervalo del 95% y el parámetro de
    sobredispersión de una Binomial Negativa ajustada con las mismas variables.
    """
    import statsmodels.api as sm
    Z = np.log1p(X.drop(columns="es_ccp"))
    Z = (Z - Z.mean()) / Z.std(ddof=0).replace(0, 1)
    Z["es_ccp"] = X["es_ccp"]
    Z = sm.add_constant(Z)
    glm = sm.GLM(y, Z, family=sm.families.Poisson()).fit()
    ic = glm.conf_int()
    tabla_coef = pd.DataFrame({
        "variable": glm.params.index,
        "coef": glm.params.values,
        "razon_tasas": np.exp(glm.params.values),
        "ic95_inf": np.exp(ic[0].values),
        "ic95_sup": np.exp(ic[1].values),
        "p_valor": glm.pvalues.values,
    })
    tabla_coef = tabla_coef[tabla_coef["variable"] != "const"].sort_values("coef", key=np.abs, ascending=False)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        nb = sm.NegativeBinomial(y, Z).fit(disp=0, maxiter=200)
    disp = {
        "alpha_binomial_negativa": float(nb.params["alpha"]),
        "p_valor_alpha": float(nb.pvalues["alpha"]),
        "aic_poisson": float(glm.aic),
        "aic_binomial_negativa": float(nb.aic),
        "devianza_sobre_gl_poisson": float(glm.deviance / glm.df_resid),
    }
    return tabla_coef.reset_index(drop=True), disp


# ---------------------------------------------------------------------------
# Brecha y zonas candidatas
# ---------------------------------------------------------------------------

def agregar_brecha(tabla: pd.DataFrame, mu: np.ndarray, cfg: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Calibra por ciudad y calcula esperados − observados en el radio de decisión."""
    radio = cfg["decision"]["radio_brecha_m"]
    t = tabla.copy()
    t["esperado_sin_calibrar"] = mu
    partes, calibracion = [], []
    for ciudad, d in t.groupby("ciudad"):
        d = d.copy()
        factor = d["n_supermercados"].sum() / d["esperado_sin_calibrar"].sum()
        d["esperado"] = d["esperado_sin_calibrar"] * factor
        calibracion.append({"ciudad": ciudad, "observados": d["n_supermercados"].sum(),
                            "esperados_sin_calibrar": d["esperado_sin_calibrar"].sum(), "factor": factor})
        tamano = float(d["tamano_m"].iloc[0])
        d[f"esperado_{radio}m"] = features.suma_entorno(d, d["esperado"], radio, tamano)
        col_obs = f"n_super_{radio}m"
        if col_obs not in d:  # el ETL guarda 500 y 1000 m; otro radio se calcula con las celdas habitables
            d[col_obs] = features.suma_entorno(d, d["n_supermercados"], radio, tamano)
        d[f"brecha_{radio}m"] = d[f"esperado_{radio}m"] - d[col_obs]
        d["puntaje_idoneidad"] = (100 * d["esperado"].rank(pct=True)).round(1)
        partes.append(d)
    return pd.concat(partes).sort_index(), pd.DataFrame(calibracion)


def agregar_ubicacion(tabla: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Latitud, longitud y, si están los datos crudos, la comuna de cada celda."""
    t = tabla.copy()
    t["lon"], t["lat"], t["comuna"] = np.nan, np.nan, None
    ruta = Path(cfg["rutas"]["censo_comunas"])
    comunas = gpd.read_parquet(ruta) if ruta.is_file() else None
    for crs, idx in t.groupby("crs_utm").indices.items():
        puntos = gpd.GeoDataFrame(geometry=gpd.points_from_xy(t.loc[idx, "x"], t.loc[idx, "y"]), crs=crs, index=idx)
        wgs = puntos.to_crs("EPSG:4326")
        t.loc[idx, "lon"], t.loc[idx, "lat"] = wgs.geometry.x.round(6), wgs.geometry.y.round(6)
        if comunas is not None:
            limites = comunas[["COMUNA", comunas.geometry.name]].to_crs(crs)
            union = gpd.sjoin(puntos, limites, how="left", predicate="within")
            nombre = union[~union.index.duplicated()]["COMUNA"].reindex(idx)
            if nombre.isna().any():  # centros de celda en el mar o en el borde: comuna más cercana
                cerca = gpd.sjoin_nearest(puntos.loc[nombre.index[nombre.isna()]], limites, how="left")
                nombre.loc[nombre.isna()] = cerca[~cerca.index.duplicated()]["COMUNA"]
            t.loc[idx, "comuna"] = nombre.to_numpy()
    return t


def elegir_candidatas(tabla: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Reglas de decisión sobre la brecha: sin supermercado cerca, demanda suficiente y zonas separadas."""
    dec = cfg["decision"]
    radio = dec["radio_brecha_m"]
    elegidas = []
    for ciudad, d in tabla.groupby("ciudad"):
        umbral_pob = d["pob_1000m"].quantile(dec["percentil_min_poblacion_1km"] / 100)
        pool = d[(d["dist_super_m"] > dec["sin_supermercado_a_m"]) & (d["pob_1000m"] >= umbral_pob)
                 & (d[f"brecha_{radio}m"] > 0)].sort_values(f"brecha_{radio}m", ascending=False)
        xy = []
        for i, fila in pool.iterrows():
            if all((fila["x"] - a) ** 2 + (fila["y"] - b) ** 2 >= dec["separacion_min_m"] ** 2 for a, b in xy):
                xy.append((fila["x"], fila["y"]))
                elegidas.append(i)
            if len(xy) == dec["n_candidatas_por_ciudad"]:
                break
    cand = tabla.loc[elegidas].copy()
    cand["ranking"] = cand.groupby("ciudad")[f"brecha_{radio}m"].rank(ascending=False, method="first").astype(int)
    cand["mapa"] = "https://www.google.com/maps?q=" + cand["lat"].astype(str) + "," + cand["lon"].astype(str)
    return cand.sort_values(["ciudad", "ranking"])


# ---------------------------------------------------------------------------
# Prueba final: un 20% de los bloques apartado antes de todo
# ---------------------------------------------------------------------------

def separar_prueba_final(tabla: pd.DataFrame, cfg: dict) -> tuple[np.ndarray, list[str]]:
    """Aparta una fracción de los bloques de 3 km como prueba final.

    El sorteo se hace por ciudad y por separado para bloques con y sin supermercado, así la prueba
    tiene las dos ciudades y una proporción de supermercados parecida a la del total.
    """
    m = cfg["modelo"]
    b = bloques(tabla, m["bloque_validacion_m"])
    info = (pd.DataFrame({"bloque": b, "ciudad": tabla["ciudad"], "n": tabla["n_supermercados"]})
            .groupby("bloque").agg(ciudad=("ciudad", "first"), supermercados=("n", "sum")))
    info["con_super"] = info["supermercados"] > 0
    rng = np.random.default_rng(m["semilla"])
    prueba = []
    for _, grupo in info.groupby(["ciudad", "con_super"]):
        n = int(round(len(grupo) * m["fraccion_prueba_final"]))
        prueba += list(rng.choice(np.sort(grupo.index.to_numpy()), size=n, replace=False))
    return b.isin(prueba).to_numpy(), sorted(prueba)


def subconjunto(tabla, X, y, mascara):
    t = tabla[mascara].reset_index(drop=True)
    return t, X[mascara].reset_index(drop=True), y[mascara]


def composicion(tabla: pd.DataFrame, es_prueba: np.ndarray, lado_m: float) -> pd.DataFrame:
    b = bloques(tabla, lado_m)
    filas = []
    for nombre, mascara in [("desarrollo (80%)", ~es_prueba), ("prueba final (20%)", es_prueba)]:
        for ciudad, d in list(tabla[mascara].groupby("ciudad")) + [("ambas", tabla[mascara])]:
            filas.append({"conjunto": nombre, "ciudad": ciudad, "bloques": b[d.index].nunique(),
                          "celdas": len(d), "pct_celdas": 100 * len(d) / (tabla["ciudad"] == ciudad).sum()
                          if ciudad != "ambas" else 100 * len(d) / len(tabla),
                          "supermercados": int(d["n_supermercados"].sum())})
    return pd.DataFrame(filas)


def precision_recall_top(tabla, y, mu, pcts) -> pd.DataFrame:
    """Precisión y recall si se marca como 'con supermercado' el k% de celdas con mayor predicción."""
    filas = []
    for ciudad, idx in list(tabla.groupby("ciudad").indices.items()) + [("ambas", np.arange(len(y)))]:
        yy, mm = y[idx], mu[idx]
        con = yy > 0
        for pct in pcts:
            n = max(1, int(round(len(mm) * pct / 100)))
            top = np.argsort(-mm, kind="stable")[:n]
            filas.append({"ciudad": ciudad, "top_pct": pct, "celdas_marcadas": n,
                          "precision": 100 * con[top].mean(),
                          "recall_celdas": 100 * con[top].sum() / max(con.sum(), 1),
                          "recall_supermercados": 100 * yy[top].sum() / max(yy.sum(), 1),
                          "tasa_base": 100 * con.mean()})
    return pd.DataFrame(filas)


def intervalos_bootstrap(tabla, y, mu, lado_m, pct_top, repeticiones, semilla) -> pd.DataFrame:
    """Intervalos del 95% remuestreando bloques de la prueba final (dentro de cada ciudad)."""
    b = bloques(tabla, lado_m).to_numpy()
    rng = np.random.default_rng(semilla)
    por_ciudad = {c: [np.flatnonzero((b == blq)) for blq in np.unique(b[idx])]
                  for c, idx in tabla.groupby("ciudad").indices.items()}
    resultados = {c: [] for c in list(por_ciudad) + ["ambas"]}
    for _ in range(repeticiones):
        muestras = {c: np.concatenate([bl[i] for i in rng.integers(0, len(bl), len(bl))]) for c, bl in por_ciudad.items()}
        muestras["ambas"] = np.concatenate(list(muestras.values()))
        for c, idx in muestras.items():
            yy, mm = y[idx], mu[idx]
            if (yy > 0).sum() == 0:
                continue
            resultados[c].append((d2_tweedie_score(yy, mm, power=1), average_precision_score(yy > 0, mm),
                                  captura_top(yy, mm, pct_top)))
    filas = []
    for c, valores in resultados.items():
        v = np.array(valores)
        for j, nombre in enumerate(["d2_poisson", "pr_auc", f"captura_top{pct_top}"]):
            filas.append({"ciudad": c, "metrica": nombre, "ic95_inf": np.percentile(v[:, j], 2.5),
                          "ic95_sup": np.percentile(v[:, j], 97.5)})
    return pd.DataFrame(filas)


# ---------------------------------------------------------------------------
# Corrida completa
# ---------------------------------------------------------------------------

def _corridas(cfg):
    g = cfg["grilla"]
    corridas = [(int(t), "") for t in g["tamanos_m"]]
    if g.get("desfase_sensibilidad_m"):
        corridas.append((int(g["tamano_principal_m"]), f"_d{int(g['desfase_sensibilidad_m'])}"))
    return corridas


def _pesos(tabla, cfg):
    return pesos_por_ciudad(tabla) if cfg["modelo"]["pesos_por_ciudad"] else None


def ejecutar(cfg: dict) -> dict:
    m = cfg["modelo"]
    lado, k, pct = m["bloque_validacion_m"], m["n_folds"], m["top_pct_captura"]
    salida = Path(cfg["rutas"]["resultados_modelo"])
    salida.mkdir(parents=True, exist_ok=True)

    tabla = cargar_tabla(cfg)
    X, y = matriz_x(tabla, cfg), tabla["n_supermercados"].to_numpy(dtype=float)
    _log(f"Tabla de {m['tamano_m']} m: {len(tabla):,} celdas habitables, {X.shape[1]} variables, {int(y.sum())} supermercados")

    # 0. Partición: la prueba final se aparta antes de cualquier decisión
    es_prueba, bloques_prueba = separar_prueba_final(tabla, cfg)
    particion = composicion(tabla, es_prueba, lado)
    particion.to_csv(salida / "particion.csv", index=False)
    pd.Series(bloques_prueba, name="bloque").to_csv(salida / "bloques_prueba_final.csv", index=False)
    td, Xd, yd = subconjunto(tabla, X, y, ~es_prueba)
    tp, Xp, yp = subconjunto(tabla, X, y, es_prueba)
    wd = _pesos(td, cfg)
    esp_d = divisiones_espaciales(td, lado, k)
    _log(f"Prueba final: {len(bloques_prueba)} bloques, {len(tp):,} celdas, {int(yp.sum())} supermercados "
         f"(no se usan hasta el paso 3)")

    # 1. Desarrollo (80%): ajuste ligero y comparación de modelos con validación cruzada por bloques
    params_gbm, grilla = ajuste_ligero(cfg, td, Xd, yd, wd, esp_d)
    grilla.to_csv(salida / "ajuste_gradient_boosting.csv", index=False)
    _log(f"Gradient boosting elegido en desarrollo: {params_gbm}")
    modelos = crear_modelos(cfg, params_gbm)
    filas, oof_d = [], {}
    for clave, modelo in modelos.items():
        oof_d[clave] = predicciones_fuera_de_muestra(modelo, Xd, yd, wd, esp_d)
        for f in metricas(td, yd, oof_d[clave], pct):
            filas.append({"conjunto": "validación cruzada (80%)", "modelo": NOMBRES_MODELOS[clave], "clave": clave, **f})
    desarrollo = pd.DataFrame(filas)
    ambas = desarrollo[desarrollo["ciudad"] == "ambas"].set_index("clave")
    mejor = ambas.loc[["glm", "gbm"], "d2_poisson"].idxmax()
    _log(f"Mejor modelo en desarrollo: {NOMBRES_MODELOS[mejor]} (D² = {ambas.loc[mejor, 'd2_poisson']:.3f})")

    # 2. Desarrollo: validación aleatoria y otros tamaños de bloque, importancia y GLM
    filas = []
    esquemas = [("aleatoria", divisiones_aleatorias(td, k, m["semilla"]))]
    esquemas += [(f"bloques de {int(l / 1000)} km", divisiones_espaciales(td, l, k)) for l in m["bloques_sensibilidad_m"]]
    for nombre, divisiones in esquemas:
        for clave in ["glm", "gbm"]:
            mu = oof_d[clave] if nombre == f"bloques de {int(lado / 1000)} km" else \
                predicciones_fuera_de_muestra(modelos[clave], Xd, yd, wd, divisiones)
            for f in metricas(td, yd, mu, pct):
                filas.append({"validacion": nombre, "modelo": NOMBRES_MODELOS[clave], **f})
    validaciones = pd.DataFrame(filas)
    validaciones.to_csv(salida / "validacion_bloques.csv", index=False)
    imp = importancia(modelos[mejor], Xd, yd, wd if wd is not None else np.ones(len(yd)), esp_d, semilla=m["semilla"])
    imp.to_csv(salida / "importancia_variables.csv", index=False)
    coef, dispersion = coeficientes_glm(td, Xd, yd)
    coef.to_csv(salida / "coeficientes_glm.csv", index=False)
    pd.Series(dispersion).rename_axis("medida").to_csv(salida / "sobredispersion.csv", header=["valor"])
    _log("Desarrollo listo (validaciones, importancia, coeficientes)")

    # 3. Prueba final (20%): se entrena con todo el desarrollo y se mide una sola vez
    filas, mu_p = [], {}
    for clave, modelo in modelos.items():
        from sklearn.base import clone
        ajustado = ajustar(clone(modelo), Xd, yd, wd)
        mu_p[clave] = np.clip(ajustado.predict(Xp), 1e-6, None)
        for f in metricas(tp, yp, mu_p[clave], pct):
            filas.append({"conjunto": "prueba final (20%)", "modelo": NOMBRES_MODELOS[clave], "clave": clave, **f})
    comparacion = pd.concat([desarrollo, pd.DataFrame(filas)], ignore_index=True)
    comparacion.to_csv(salida / "comparacion_modelos.csv", index=False)
    ic = intervalos_bootstrap(tp, yp, mu_p[mejor], lado, pct, m["repeticiones_bootstrap"], m["semilla"])
    ic.to_csv(salida / "prueba_final_intervalos.csv", index=False)
    pr_top = precision_recall_top(tp, yp, mu_p[mejor], m["pcts_precision"])
    pr_top.to_csv(salida / "precision_recall_top.csv", index=False)
    f_mejor = comparacion[(comparacion["conjunto"] == "prueba final (20%)") & (comparacion["clave"] == mejor)
                          & (comparacion["ciudad"] == "ambas")].iloc[0]
    _log(f"Prueba final ({NOMBRES_MODELOS[mejor]}): D² = {f_mejor['d2_poisson']:.3f}, PR-AUC = {f_mejor['pr_auc']:.3f}")

    # 4. Sensibilidad al tamaño de celda: métricas en desarrollo; zonas con predicciones de todas las celdas
    filas, zonas = [], {}
    for tamano, sufijo in _corridas(cfg):
        t = cargar_tabla(cfg, tamano, sufijo)
        Xt, yt = matriz_x(t, cfg), t["n_supermercados"].to_numpy(dtype=float)
        dev = ~bloques(t, lado).isin(bloques_prueba).to_numpy()
        tdt, Xdt, ydt = subconjunto(t, Xt, yt, dev)
        mu_dev = predicciones_fuera_de_muestra(modelos[mejor], Xdt, ydt, _pesos(tdt, cfg), divisiones_espaciales(tdt, lado, k))
        etiqueta = f"{tamano} m{' desplazada' if sufijo else ''}"
        for f in metricas(tdt, ydt, mu_dev, pct):
            filas.append({"celda": etiqueta, "tamano_m": tamano, "celdas_desarrollo": len(tdt), **f})
        mu_all = predicciones_fuera_de_muestra(modelos[mejor], Xt, yt, _pesos(t, cfg), divisiones_espaciales(t, lado, k))
        km = t.assign(esperado=mu_all, bx=(t["x"] // 1000).astype(int), by=(t["y"] // 1000).astype(int))
        agg = km.groupby(["ciudad", "bx", "by"]).agg(esp=("esperado", "sum"), obs=("n_supermercados", "sum"))
        zonas[etiqueta] = agg["esp"] - agg["obs"]
        _log(f"Sensibilidad {etiqueta}: D² en desarrollo = {filas[-1]['d2_poisson']:.3f}")
    sensibilidad = pd.DataFrame(filas)
    ref, n_top = zonas[f"{m['tamano_m']} m"], m["n_zonas_estabilidad"]
    coincidencia = []
    for etiqueta, brecha in zonas.items():
        for ciudad in ref.index.get_level_values(0).unique():
            a = set(ref.loc[ciudad].nlargest(n_top).index)
            b = set(brecha.loc[ciudad].nlargest(n_top).index)
            comun = ref.loc[ciudad].index.intersection(brecha.loc[ciudad].index)
            coincidencia.append({"celda": etiqueta, "ciudad": ciudad,
                                 f"coincidencia_top{n_top}_km2": 100 * len(a & b) / n_top,
                                 "spearman_brecha_km2": ref.loc[ciudad].loc[comun].corr(brecha.loc[ciudad].loc[comun], method="spearman")})
    sensibilidad = sensibilidad.merge(pd.DataFrame(coincidencia), on=["celda", "ciudad"], how="left")
    sensibilidad.to_csv(salida / "sensibilidad_tamano.csv", index=False)

    # 5. Mapa final: predicciones fuera de muestra para TODAS las celdas (5 particiones por bloques),
    #    con la configuración ya elegida. Alimentan la brecha, las zonas candidatas y el dashboard.
    w = _pesos(tabla, cfg)
    esp = divisiones_espaciales(tabla, lado, k)
    oof = {clave: predicciones_fuera_de_muestra(modelos[clave], X, y, w, esp) for clave in ["glm", "gbm"]}
    moran = moran_residuos(tabla, y, oof[mejor], semilla=m["semilla"])
    moran.to_csv(salida / "moran_residuos.csv", index=False)
    pred, calibracion = agregar_brecha(tabla, oof[mejor], cfg)
    calibracion.to_csv(salida / "calibracion.csv", index=False)
    pred = agregar_ubicacion(pred, cfg)
    pred["esperado_glm"] = oof["glm"]
    pred["en_prueba_final"] = es_prueba
    candidatas = elegir_candidatas(pred, cfg)
    pred["candidata"] = pred.index.isin(candidatas.index)
    radio = cfg["decision"]["radio_brecha_m"]
    columnas = ["id_celda", "ciudad", "comuna", "ix", "iy", "x", "y", "lon", "lat", "crs_utm", "n_supermercados",
                "esperado", "esperado_sin_calibrar", "esperado_glm", "puntaje_idoneidad", f"esperado_{radio}m",
                f"n_super_{radio}m", f"brecha_{radio}m", "n_super_500m", "dist_super_m", "pob_1000m", "paraderos_500m",
                "comercio_500m", "vias_km_500m", "poblacion_celda", "en_prueba_final", "candidata"]
    pred[columnas].to_parquet(Path(cfg["rutas"]["processed"]) / f"predicciones_{m['tamano_m']}m.parquet", index=False)
    cols_cand = ["ciudad", "ranking", "comuna", "lat", "lon", "mapa", f"brecha_{radio}m", f"esperado_{radio}m",
                 f"n_super_{radio}m", "dist_super_m", "pob_1000m", "paraderos_500m", "comercio_500m", "puntaje_idoneidad", "id_celda"]
    candidatas[cols_cand].round({f"brecha_{radio}m": 2, f"esperado_{radio}m": 2}).to_csv(salida / "zonas_candidatas.csv", index=False)
    _log(f"{len(candidatas)} zonas candidatas -> {salida / 'zonas_candidatas.csv'}")
    _log("Listo.")
    return {"tabla": tabla, "X": X, "y": y, "es_prueba": es_prueba, "particion": particion, "mejor": mejor,
            "params_gbm": params_gbm, "ajuste_gbm": grilla, "comparacion": comparacion, "validaciones": validaciones,
            "intervalos": ic, "precision_recall": pr_top, "importancia": imp, "coeficientes": coef,
            "dispersion": dispersion, "sensibilidad": sensibilidad, "moran": moran, "calibracion": calibracion,
            "oof": oof, "predicciones": pred, "candidatas": candidatas[cols_cand]}


if __name__ == "__main__":
    r = ejecutar(cargar_config())
    print(r["comparacion"].to_string(index=False))
