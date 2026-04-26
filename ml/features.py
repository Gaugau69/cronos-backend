"""
ml/features.py — Pipeline de feature engineering pour AION.

Construit le tenseur (N_windows, L, F) pour le modèle JEPA :
  - N_windows : nombre de fenêtres glissantes disponibles
  - L = 14    : longueur de la fenêtre (jours)
  - F = 12    : features par jour

Features par jour (ordre fixe) :
  0  hrv_rmssd        — HRV nuit dernière (ms)
  1  hr_rest          — FC au repos (bpm)
  2  sleep_duration   — Durée de sommeil (h)
  3  sleep_quality    — Score de sommeil (0-100)
  4  hr_mean          — FC moyenne séance (0 si repos)
  5  hr_drift         — Dérive FC estimée (0 si repos)
  6  pace_mean        — Allure moyenne m/s (0 si repos)
  7  pace_hr_ratio    — Ratio allure/FC, proxy économie (0 si repos)
  8  duration         — Durée séance min (0 si repos)
  9  elevation_gain   — Dénivelé+ m (0 si repos)
  10 training_load    — Charge estimée = duration * hr_mean / 100 (0 si repos)
  11 is_rest_day      — Masque binaire : 1 = repos, 0 = séance

Usage:
    from ml.features import build_dataset

    X, meta = build_dataset(
        daily_path="data/daily_metrics.csv",
        activities_path="data/activities.csv",
        window=14,
        user="gauthier",
    )
    # X.shape = (N, 14, 12)
    # meta = DataFrame avec date de fin de chaque fenêtre
"""

import warnings
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# ─────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────

WINDOW = 14       # jours par fenêtre
N_FEATURES = 12   # features par jour

FEATURE_NAMES = [
    "hrv_rmssd",
    "hr_rest",
    "sleep_duration",
    "sleep_quality",
    "hr_mean",
    "hr_drift",
    "pace_mean",
    "pace_hr_ratio",
    "duration",
    "elevation_gain",
    "training_load",
    "is_rest_day",
]


# ─────────────────────────────────────────────
# 1. Chargement des données
# ─────────────────────────────────────────────

def load_data(
    daily_path: str | Path,
    activities_path: str | Path,
    user: Optional[str] = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Charge les CSV daily_metrics et activities.
    Filtre par user si fourni.
    """
    daily = pd.read_csv(daily_path, parse_dates=["date"])
    acts  = pd.read_csv(activities_path, parse_dates=["date"])

    if user:
        daily = daily[daily["user"] == user].copy()
        acts  = acts[acts["user"] == user].copy()

    daily = daily.sort_values("date").reset_index(drop=True)
    acts  = acts.sort_values("date").reset_index(drop=True)

    return daily, acts


# ─────────────────────────────────────────────
# 2. Agrégation des activités par jour
# ─────────────────────────────────────────────

def aggregate_activities(acts: pd.DataFrame) -> pd.DataFrame:
    """
    Pour chaque jour, agrège toutes les activités (running principalement).
    Retourne un DataFrame avec une ligne par jour.
    """
    # Filtre activités de course (running, trail, treadmill)
    running_types = {"running", "trail_running", "treadmill_running", "track_running"}
    runs = acts[acts["activity_type"].isin(running_types)].copy()

    if runs.empty:
        # Pas de données running — on prend toutes les activités
        runs = acts.copy()

    # Agrégation par jour
    agg = runs.groupby("date").agg(
        hr_mean        = ("avg_hr",          "mean"),
        hr_max         = ("max_hr",          "max"),
        pace_mean      = ("avg_speed_kmh",   "mean"),   # km/h → converti plus tard
        duration       = ("duration_min",    "sum"),
        elevation_gain = ("elevation_gain_m","sum"),
        training_effect= ("training_effect", "mean"),
    ).reset_index()

    # Convertit km/h → m/s
    agg["pace_mean"] = agg["pace_mean"] / 3.6

    # Dérive FC estimée : proxy = (hr_max - hr_mean) / hr_mean
    # Idéalement calculé intra-séance mais on approche ici
    agg["hr_drift"] = (agg["hr_max"] - agg["hr_mean"]) / agg["hr_mean"].replace(0, np.nan)
    agg["hr_drift"] = agg["hr_drift"].fillna(0)

    # Charge d'entraînement estimée = duration (min) * hr_mean / 100
    agg["training_load"] = agg["duration"] * agg["hr_mean"] / 100

    # Ratio allure/FC (économie de course) — évite division par 0
    agg["pace_hr_ratio"] = agg["pace_mean"] / agg["hr_mean"].replace(0, np.nan)
    agg["pace_hr_ratio"] = agg["pace_hr_ratio"].fillna(0)

    return agg[[
        "date", "hr_mean", "hr_drift", "pace_mean",
        "pace_hr_ratio", "duration", "elevation_gain", "training_load"
    ]]


# ─────────────────────────────────────────────
# 3. Construction du DataFrame journalier complet
# ─────────────────────────────────────────────

def build_daily_features(
    daily: pd.DataFrame,
    acts_agg: pd.DataFrame,
) -> pd.DataFrame:
    """
    Joint les métriques de repos et les agrégats d'activités.
    Remplit les jours de repos avec 0 sur les features séance.
    Ajoute le masque is_rest_day.
    """
    # Colonnes utiles du daily
    rest_cols = {
        "date":           "date",
        "hrv_last_night": "hrv_rmssd",
        "resting_hr":     "hr_rest",
        "sleep_duration_min": "sleep_duration_raw",
        "sleep_score":    "sleep_quality",
    }
    df = daily[list(rest_cols.keys())].rename(columns=rest_cols).copy()

    # Convertit durée de sommeil en heures
    df["sleep_duration"] = df["sleep_duration_raw"] / 60
    df = df.drop(columns=["sleep_duration_raw"])

    # Merge avec activités
    df = df.merge(acts_agg, on="date", how="left")

    # Masque jour de repos
    df["is_rest_day"] = df["duration"].isna().astype(float)

    # Remplace NaN séance par 0
    seance_cols = ["hr_mean", "hr_drift", "pace_mean", "pace_hr_ratio",
                   "duration", "elevation_gain", "training_load"]
    df[seance_cols] = df[seance_cols].fillna(0)

    # Réordonne dans l'ordre FEATURE_NAMES
    df = df[["date"] + FEATURE_NAMES]

    return df.sort_values("date").reset_index(drop=True)


# ─────────────────────────────────────────────
# 4. Normalisation robuste par athlète
# ─────────────────────────────────────────────

def compute_normalization_stats(df: pd.DataFrame) -> dict:
    """
    Calcule médiane et IQR pour chaque feature (sauf is_rest_day).
    Statistiques robustes pour gérer les outliers (compét, maladie).
    """
    stats = {}
    for col in FEATURE_NAMES:
        if col == "is_rest_day":
            stats[col] = {"median": 0.0, "iqr": 1.0}
            continue
        values = df[col].replace(0, np.nan).dropna()
        if len(values) < 5:
            stats[col] = {"median": 0.0, "iqr": 1.0}
        else:
            q25, q75 = np.percentile(values, [25, 75])
            iqr = q75 - q25
            stats[col] = {
                "median": float(np.median(values)),
                "iqr":    float(iqr) if iqr > 0 else 1.0,
            }
    return stats


def normalize(df: pd.DataFrame, stats: dict) -> pd.DataFrame:
    """
    Applique la normalisation robuste (median/IQR) à chaque feature.
    Les jours de repos (valeur 0 sur features séance) restent à 0
    pour ne pas être confondus avec une valeur normalisée.
    """
    df = df.copy()
    for col in FEATURE_NAMES:
        if col == "is_rest_day":
            continue
        median = stats[col]["median"]
        iqr    = stats[col]["iqr"]
        # Normalise uniquement les valeurs non nulles
        mask = df[col] != 0
        df.loc[mask, col] = (df.loc[mask, col] - median) / iqr
    return df


# ─────────────────────────────────────────────
# 5. Fenêtres glissantes → tenseur (N, L, F)
# ─────────────────────────────────────────────

def build_windows(
    df: pd.DataFrame,
    window: int = WINDOW,
    step: int = 1,
) -> tuple[np.ndarray, pd.DataFrame]:
    """
    Découpe le DataFrame en fenêtres glissantes.

    Returns:
        X    : np.ndarray de forme (N, window, F)
        meta : DataFrame avec colonnes [window_start, window_end, n_rest_days, n_training_days]
    """
    feature_matrix = df[FEATURE_NAMES].values  # (T, F)
    dates = df["date"].values

    windows = []
    metas   = []

    for i in range(0, len(df) - window + 1, step):
        w = feature_matrix[i : i + window]   # (window, F)
        if w.shape[0] < window:
            continue

        # Vérifie qu'il n'y a pas trop de NaN résiduels
        nan_ratio = np.isnan(w).mean()
        if nan_ratio > 0.3:
            warnings.warn(f"Fenêtre {i} : {nan_ratio:.0%} de NaN, ignorée")
            continue

        # Remplace NaN résiduels par 0
        w = np.nan_to_num(w, nan=0.0)

        windows.append(w)
        metas.append({
            "window_start":      dates[i],
            "window_end":        dates[i + window - 1],
            "n_rest_days":       int(w[:, FEATURE_NAMES.index("is_rest_day")].sum()),
            "n_training_days":   window - int(w[:, FEATURE_NAMES.index("is_rest_day")].sum()),
        })

    X    = np.stack(windows, axis=0).astype(np.float32)  # (N, L, F)
    meta = pd.DataFrame(metas)

    return X, meta


# ─────────────────────────────────────────────
# 6. Pipeline principal
# ─────────────────────────────────────────────

def build_dataset(
    daily_path: str | Path,
    activities_path: str | Path,
    user: Optional[str] = None,
    window: int = WINDOW,
    step: int = 1,
    save_dir: Optional[str | Path] = None,
) -> tuple[np.ndarray, pd.DataFrame, dict]:
    """
    Pipeline complet :
      1. Charge les données
      2. Agrège les activités par jour
      3. Construit le DataFrame journalier
      4. Normalise par athlète
      5. Découpe en fenêtres glissantes

    Returns:
        X      : np.ndarray (N, window, F) — tenseur pour le modèle JEPA
        meta   : DataFrame avec infos sur chaque fenêtre
        stats  : dict des stats de normalisation (à sauvegarder pour l'inférence)
    """
    print(f"[AION] Chargement des données pour {user or 'tous les users'}...")
    daily, acts = load_data(daily_path, activities_path, user=user)
    print(f"  → {len(daily)} jours, {len(acts)} activités")

    print("[AION] Agrégation des activités...")
    acts_agg = aggregate_activities(acts)

    print("[AION] Construction des features journalières...")
    df = build_daily_features(daily, acts_agg)
    print(f"  → {len(df)} jours avec features")

    print("[AION] Normalisation robuste par athlète...")
    stats = compute_normalization_stats(df)
    df_norm = normalize(df, stats)

    print(f"[AION] Fenêtres glissantes (L={window}, step={step})...")
    X, meta = build_windows(df_norm, window=window, step=step)
    print(f"  → {X.shape[0]} fenêtres — shape finale : {X.shape}")

    # Sauvegarde optionnelle
    if save_dir:
        save_dir = Path(save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)
        np.save(save_dir / "X.npy", X)
        meta.to_csv(save_dir / "meta.csv", index=False)
        import json
        with open(save_dir / "norm_stats.json", "w") as f:
            json.dump(stats, f, indent=2)
        print(f"  → Sauvegardé dans {save_dir}/")

    return X, meta, stats


# ─────────────────────────────────────────────
# 7. Utilitaires
# ─────────────────────────────────────────────

def describe_dataset(X: np.ndarray, meta: pd.DataFrame) -> None:
    """Affiche un résumé du dataset construit."""
    N, L, F = X.shape
    print(f"\n{'='*50}")
    print(f"Dataset AION")
    print(f"{'='*50}")
    print(f"  Fenêtres  : {N}")
    print(f"  Fenêtre   : {L} jours")
    print(f"  Features  : {F}")
    print(f"  Shape     : {X.shape}")
    print(f"  Période   : {meta['window_start'].min().date()} → {meta['window_end'].max().date()}")
    print(f"  Jours repos (moy/fenêtre) : {meta['n_rest_days'].mean():.1f}/{L}")
    print(f"\nFeatures :")
    for i, name in enumerate(FEATURE_NAMES):
        vals = X[:, :, i].flatten()
        vals = vals[vals != 0]
        if len(vals) > 0:
            print(f"  [{i:2d}] {name:20s} — mean={vals.mean():.3f}, std={vals.std():.3f}")
    print(f"{'='*50}\n")


def get_targets(X: np.ndarray, horizon: int = 1) -> tuple[np.ndarray, np.ndarray]:
    """
    Prépare les paires (contexte, cible) pour l'entraînement JEPA.

    Pour chaque fenêtre i :
      - contexte x_t  = X[i]           → (L, F)
      - cible    y_t  = X[i + horizon] → (L, F)  [fenêtre décalée de `horizon` jours]

    Returns:
        X_ctx : (N', L, F) — fenêtres contexte
        X_tgt : (N', L, F) — fenêtres cible
    """
    n = len(X) - horizon
    X_ctx = X[:n]
    X_tgt = X[horizon:]
    return X_ctx, X_tgt


# ─────────────────────────────────────────────
# CLI minimal
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="AION — Feature engineering")
    parser.add_argument("--daily",      required=True, help="Chemin vers daily_metrics.csv")
    parser.add_argument("--activities", required=True, help="Chemin vers activities.csv")
    parser.add_argument("--user",       default=None,  help="Nom de l'utilisateur (optionnel)")
    parser.add_argument("--window",     type=int, default=14, help="Taille de fenêtre (défaut: 14)")
    parser.add_argument("--save",       default=None, help="Dossier de sauvegarde")
    args = parser.parse_args()

    X, meta, stats = build_dataset(
        daily_path=args.daily,
        activities_path=args.activities,
        user=args.user,
        window=args.window,
        save_dir=args.save,
    )
    describe_dataset(X, meta)

    # Exemple de paires JEPA
    X_ctx, X_tgt = get_targets(X, horizon=1)
    print(f"Paires JEPA (horizon=1j) : X_ctx={X_ctx.shape}, X_tgt={X_tgt.shape}")
