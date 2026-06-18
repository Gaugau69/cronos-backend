"""
app/services/ml_recommender.py — Inférence CRONOS ML.

Pipeline complet :
  1. 14 jours de métriques → tenseur (1, 14, 12)
  2. Encoder JEPA        → z_jepa (1, 64)
  3. Encodeur profil     → x_profile (1, 12)
  4. Encodeur course     → x_race (1, 6)
  5. SessionScorer       → scores (1, 45)
  6. Top-k recommandations avec métadonnées complètes

Fallback : si les checkpoints ne sont pas disponibles ou si les données
           sont insuffisantes (< 7 jours), retourne None → algorithme heuristique.
"""

import json
import logging
import os
import sys
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ── Chemin vers cronos-ml ────────────────────────────────────────────────────

ML_DIR = Path(
    os.environ.get(
        "CRONOS_ML_DIR",
        Path(__file__).parent.parent.parent.parent / "cronos-ml",
    )
)

CHECKPOINT_ENCODER    = ML_DIR / "checkpoints" / "best_model.pt"
CHECKPOINT_RECOMMENDER= ML_DIR / "checkpoints" / "best_recommender.pt"
NORM_STATS_PATH       = ML_DIR / "data" / "processed" / "norm_stats.json"

# ── Constantes features (pipeline.py) ───────────────────────────────────────

FEATURE_NAMES = [
    "hrv_rmssd", "hr_rest", "sleep_duration", "sleep_quality",
    "hr_mean", "hr_drift", "pace_mean", "pace_hr_ratio",
    "duration", "elevation_gain", "training_load", "is_rest_day",
]
WINDOW = 14

# ── Chargement des modèles (lazy, singleton) ─────────────────────────────────

_encoder    = None
_scorer     = None
_ml_ready   = None   # None = pas encore testé, True/False après premier appel
_models_lock = threading.Lock()  # double-checked locking pour la sécurité thread


def _load_models() -> bool:
    global _encoder, _scorer, _ml_ready

    if _ml_ready is not None:   # fast-path sans lock
        return _ml_ready

    with _models_lock:
        if _ml_ready is not None:   # re-vérification dans le lock
            return _ml_ready

    try:
        if not CHECKPOINT_ENCODER.exists() or not CHECKPOINT_RECOMMENDER.exists():
            logger.warning("Checkpoints ML introuvables — mode heuristique")
            _ml_ready = False
            return False

        if str(ML_DIR) not in sys.path:
            sys.path.insert(0, str(ML_DIR))

        import torch
        from models.encoder import Encoder
        from recommendation.recommender import SessionScorer

        enc = Encoder()
        enc.load_state_dict(
            torch.load(CHECKPOINT_ENCODER, map_location="cpu", weights_only=True),
            strict=False,
        )
        enc.eval()

        scr = SessionScorer()
        scr.load_state_dict(
            torch.load(CHECKPOINT_RECOMMENDER, map_location="cpu", weights_only=True),
            strict=False,
        )
        scr.eval()

        _encoder  = enc
        _scorer   = scr
        _ml_ready = True
        logger.info("Modèles ML CRONOS chargés.")
        return True

    except Exception as e:
        logger.error(f"Chargement ML échoué : {e}")
        _ml_ready = False
        return False


# ── Normalisation robuste ────────────────────────────────────────────────────

def _load_norm_stats(user_name: Optional[str]) -> Optional[dict]:
    """Charge les stats de normalisation pour l'utilisateur (ou stats globales)."""
    if not NORM_STATS_PATH.exists():
        return None
    with open(NORM_STATS_PATH) as f:
        all_stats = json.load(f)
    return all_stats.get(user_name) or next(iter(all_stats.values()), None)


def _compute_norm_stats_from_data(rows: list[dict]) -> dict:
    """Calcule les stats de normalisation à partir des données de l'utilisateur."""
    stats = {}
    data_by_feat = {fn: [] for fn in FEATURE_NAMES}

    for row in rows:
        for fn in FEATURE_NAMES:
            v = row.get(fn)
            if v is not None and v != 0:
                data_by_feat[fn].append(float(v))

    for fn in FEATURE_NAMES:
        if fn == "is_rest_day":
            stats[fn] = {"median": 0.0, "iqr": 1.0}
            continue
        vals = data_by_feat[fn]
        if len(vals) < 5:
            stats[fn] = {"median": 0.0, "iqr": 1.0}
        else:
            q25, q75 = np.percentile(vals, [25, 75])
            iqr = q75 - q25
            stats[fn] = {
                "median": float(np.median(vals)),
                "iqr":    float(iqr) if iqr > 0 else 1.0,
            }
    return stats


def _normalize_row(row: dict, stats: dict) -> list[float]:
    out = []
    for fn in FEATURE_NAMES:
        v = float(row.get(fn) or 0.0)
        if fn == "is_rest_day" or v == 0:
            out.append(v)
        else:
            median = stats[fn]["median"]
            iqr    = stats[fn]["iqr"]
            out.append((v - median) / iqr)
    return out


# ── Construction du tenseur features ─────────────────────────────────────────

def _build_feature_rows(metrics: list, activities: list) -> list[dict]:
    """
    Joint métriques journalières + activités sur 14 jours.
    Retourne une liste de dicts avec les 12 features par jour, triée par date.
    """
    # Index activités par date
    acts_by_date: dict[date, dict] = {}
    for a in activities:
        d = a.date if hasattr(a, "date") else a["date"]
        if isinstance(d, str):
            d = date.fromisoformat(d)
        acts_by_date.setdefault(d, {"hr_sum": 0, "hr_max": 0, "pace_sum": 0,
                                     "duration": 0, "elevation": 0, "n": 0})
        e = acts_by_date[d]
        hr  = float(getattr(a, "avg_hr",          0) or 0)
        hrm = float(getattr(a, "max_hr",          0) or 0)
        spd = float(getattr(a, "avg_speed_kmh",   0) or 0)   # km/h
        dur = float(getattr(a, "duration_min",    0) or 0)
        elv = float(getattr(a, "elevation_gain_m",0) or 0)
        e["hr_sum"]   += hr
        e["hr_max"]    = max(e["hr_max"], hrm)
        e["pace_sum"] += spd / 3.6 if spd > 0 else 0   # → m/s
        e["duration"] += dur
        e["elevation"]+= elv
        e["n"]        += 1

    rows = []
    for m in metrics:
        d = m.date if hasattr(m, "date") else date.fromisoformat(m["date"])

        hrv   = float(getattr(m, "hrv_last_night",      0) or 0)
        hr_r  = float(getattr(m, "resting_hr",          0) or 0)
        slp_m = float(getattr(m, "sleep_duration_min",  0) or 0)
        slp_q = float(getattr(m, "sleep_score",         0) or 0)

        act = acts_by_date.get(d)
        if act and act["n"] > 0:
            hr_mean  = act["hr_sum"] / act["n"]
            pace     = act["pace_sum"] / act["n"]
            dur      = act["duration"]
            elv      = act["elevation"]
            hr_drift = (act["hr_max"] - hr_mean) / hr_mean if hr_mean > 0 else 0
            p_hr_r   = pace / hr_mean if hr_mean > 0 else 0
            load     = dur * hr_mean / 100
            is_rest  = 0.0
        else:
            hr_mean = hr_drift = pace = p_hr_r = dur = elv = load = 0.0
            is_rest = 1.0

        rows.append({
            "date":          d,
            "hrv_rmssd":     hrv,
            "hr_rest":       hr_r,
            "sleep_duration":slp_m / 60,
            "sleep_quality": slp_q,
            "hr_mean":       hr_mean,
            "hr_drift":      hr_drift,
            "pace_mean":     pace,
            "pace_hr_ratio": p_hr_r,
            "duration":      dur,
            "elevation_gain":elv,
            "training_load": load,
            "is_rest_day":   is_rest,
        })

    rows.sort(key=lambda r: r["date"])
    return rows


# ── Interface publique ────────────────────────────────────────────────────────

def ml_recommend(
    metrics: list,
    activities: list,
    profile: Optional[object],
    races: list,
    user_name: Optional[str] = None,
    top_k: int = 5,
) -> Optional[list[dict]]:
    """
    Tente une inférence ML. Retourne None si indisponible ou données insuffisantes.
    Sinon retourne une liste de top_k dicts de recommandation (même format que heuristique).
    """
    if not _load_models():
        return None

    # Sans HRV (feature principale), le modèle ne peut pas différencier → heuristique
    hrv_values = [getattr(m, "hrv_last_night", None) for m in metrics if getattr(m, "hrv_last_night", None)]
    if not hrv_values:
        logger.debug("Pas de données HRV — ML ignoré, bascule heuristique")
        return None

    try:
        import torch

        # 1. Features
        rows = _build_feature_rows(metrics, activities)
        if len(rows) < WINDOW:
            logger.debug(f"Pas assez de jours ({len(rows)} < {WINDOW}) — ML ignoré")
            return None

        recent_rows = rows[-WINDOW:]

        # 2. Normalisation
        stats = _load_norm_stats(user_name) or _compute_norm_stats_from_data(rows)
        tensor_rows = [_normalize_row(r, stats) for r in recent_rows]
        x = torch.tensor([tensor_rows], dtype=torch.float32)   # (1, 14, 12)

        with torch.no_grad():
            z_jepa = _encoder(x)   # (1, 64)

        # 3. Profil
        if str(ML_DIR) not in sys.path:
            sys.path.insert(0, str(ML_DIR))
        from recommendation.encoders import encode_athlete_profile, encode_race_context
        from recommendation.session_types_v2 import SESSION_CATALOGUE

        profile_dict = {}
        if profile:
            for attr in ("level", "sport_type", "years_running", "weekly_km",
                         "weekly_sessions", "long_run_km", "vo2max_estimated",
                         "best_5k_min", "best_marathon_min", "max_weekly_km",
                         "primary_goal", "target_distance"):
                v = getattr(profile, attr, None)
                if v is not None:
                    profile_dict[attr] = v

        x_profile = encode_athlete_profile(profile_dict)

        # 4. Course
        race_dict = None
        if races:
            upcoming = [r for r in races if not getattr(r, "is_completed", False)]
            if upcoming:
                next_r = min(upcoming, key=lambda r: r.race_date)
                days_to = (next_r.race_date - date.today()).days
                race_dict = {
                    "days_to_race": days_to,
                    "distance_km":  float(next_r.distance_km),
                    "elevation_m":  int(getattr(next_r, "elevation_m", 0) or 0),
                    "goal_type":    getattr(next_r, "goal_type", "finir"),
                    "priority":     getattr(next_r, "priority", "B"),
                }
        x_race = encode_race_context(race_dict)

        # 5. Scores
        with torch.no_grad():
            scores = _scorer(z_jepa, x_profile, x_race).squeeze(0)   # (45,)

        # 6. Construire recommandations (ids issus de SESSION_CATALOGUE v2)
        top_indices = torch.argsort(scores, descending=True)[:top_k].tolist()

        results = []
        for rank, idx in enumerate(top_indices):
            s = SESSION_CATALOGUE[idx]
            results.append({
                "id":           s.id,
                "rank":         rank + 1,
                "name":         s.name,
                "description":  s.description,
                "category":     s.category,
                "intensity":    s.intensity,
                "duration_min": s.duration_min,
                "distance_km":  s.distance_km,
                "score":        round(float(scores[idx]) * 100, 1),
                "recovery_cost":s.recovery_cost,
                "min_level":    s.min_level,
                "example":      s.example_debutant,
            })

        return results

    except Exception as e:
        logger.error(f"Inférence ML échouée : {e}", exc_info=True)
        return None
