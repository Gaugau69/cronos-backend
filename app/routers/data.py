"""
app/routers/data.py — Collecte manuelle, métriques et recommandations CRONOS.
"""

import asyncio
import json
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import (
    Activity, AthleteProfile, DailyMetric, PlannedRace,
    RecommendationsCache, SessionFeedback, SessionHistory, User, get_db,
    AsyncSessionLocal,
)
from app.schemas import ActivityOut, CollectRequest, DailyMetricOut
from app.services.collect import collect_user_range
from app.services.ml_recommender import ml_recommend
from app.dependencies import get_caller_email, require_owner

router = APIRouter(tags=["data"])

# ─── Suivi des jobs de collecte en mémoire ────────────────────
_collect_jobs: dict[str, dict] = {}


async def _run_collect_bg(job_id: str, user_id: int, start: date, end: date) -> None:
    """Lance collect_user_range en arrière-plan et met à jour _collect_jobs."""
    total_days = (end - start).days + 1
    _collect_jobs[job_id] = {"status": "running", "days_done": 0, "days_total": total_days}
    try:
        async with AsyncSessionLocal() as db:
            user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
            if not user:
                _collect_jobs[job_id] = {"status": "error", "reason": "user not found"}
                return
            result = await collect_user_range(db, user, start, end)
            _collect_jobs[job_id] = {"status": "done", "days_total": total_days, **result}
    except Exception as e:
        _collect_jobs[job_id] = {"status": "error", "reason": str(e)}


async def _get_user(db: AsyncSession, name: str) -> User:
    user = (await db.execute(select(User).where(User.name == name))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, f"User '{name}' introuvable.")
    return user


@router.post("/collect")
async def collect(payload: CollectRequest, db: AsyncSession = Depends(get_db)):
    """Lance la collecte Garmin en arrière-plan. Retourne immédiatement un job_id."""
    user = await _get_user(db, payload.name)
    if not user.token_json:
        raise HTTPException(400, "Pas de token Garmin pour cet utilisateur.")
    start = payload.start_date or (date.today() - timedelta(days=1))
    end   = payload.end_date   or start
    job_id = f"{payload.name}_{start.isoformat()}_{end.isoformat()}"
    asyncio.create_task(_run_collect_bg(job_id, user.id, start, end))
    return {"job_id": job_id, "status": "started", "days_total": (end - start).days + 1}


@router.get("/collect/status/{job_id}")
async def collect_status(job_id: str):
    """Retourne l'état d'un job de collecte."""
    return _collect_jobs.get(job_id, {"status": "unknown"})


@router.get("/users/{name}/metrics/count")
async def get_metrics_count(name: str, db: AsyncSession = Depends(get_db)):
    """Retourne le nombre de jours de métriques collectés — pour la barre de progression."""
    from sqlalchemy import func
    user = await _get_user(db, name)
    count = (await db.execute(
        select(func.count()).where(DailyMetric.user_id == user.id)
    )).scalar_one()
    return {"days_collected": count, "days_target": 730}


@router.get("/users/{name}/daily", response_model=list[DailyMetricOut])
async def get_daily(
    name: str,
    start: Optional[date] = Query(None),
    end:   Optional[date] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user(db, name)
    q = select(DailyMetric).where(DailyMetric.user_id == user.id)
    if start: q = q.where(DailyMetric.date >= start)
    if end:   q = q.where(DailyMetric.date <= end)
    return (await db.execute(q.order_by(DailyMetric.date))).scalars().all()


@router.get("/users/{name}/activities", response_model=list[ActivityOut])
async def get_activities(
    name: str,
    start:         Optional[date] = Query(None),
    end:           Optional[date] = Query(None),
    activity_type: Optional[str]  = Query(None),
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user(db, name)
    q = select(Activity).where(Activity.user_id == user.id)
    if start: q = q.where(Activity.date >= start)
    if end:   q = q.where(Activity.date <= end)
    if activity_type: q = q.where(Activity.activity_type == activity_type)
    return (await db.execute(q.order_by(Activity.date.desc()))).scalars().all()


# ─────────────────────────────────────────────────────────────
# RPE
# ─────────────────────────────────────────────────────────────

class RPEItem(BaseModel):
    activity_id: int
    rpe: int


class RPEPayload(BaseModel):
    name: str
    ratings: list[RPEItem]


@router.get("/users/{name}/activities/recent", response_model=list[ActivityOut])
async def get_recent_activities(
    name: str,
    days: int = Query(3),
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user(db, name)
    since = date.today() - timedelta(days=days)
    q = select(Activity).where(Activity.user_id == user.id).where(Activity.date >= since).order_by(Activity.date.desc())
    return (await db.execute(q)).scalars().all()


@router.post("/rpe")
async def submit_rpe(payload: RPEPayload, db: AsyncSession = Depends(get_db)):
    user = await _get_user(db, payload.name)
    updated = 0
    for item in payload.ratings:
        if not (1 <= item.rpe <= 10):
            raise HTTPException(400, f"RPE doit être entre 1 et 10, reçu: {item.rpe}")
        result = await db.execute(
            update(Activity)
            .where(Activity.user_id == user.id)
            .where(Activity.activity_id == item.activity_id)
            .values(rpe=item.rpe)
        )
        updated += result.rowcount
    await db.commit()
    return {"status": "ok", "updated": updated}


# ─────────────────────────────────────────────────────────────
# Catalogue 45 séances CRONOS v2
# ─────────────────────────────────────────────────────────────

SESSIONS_V2 = [
    {"id":0, "name":"Repos complet", "category":"repos", "intensity":0.0, "duration_min":0, "distance_km":0, "recovery_cost":0.0, "min_level":0, "description":"Journée sans activité — récupération maximale.", "example":"Marche légère si besoin, pas de course"},
    {"id":1, "name":"Récupération passive", "category":"repos", "intensity":0.05, "duration_min":20, "distance_km":0, "recovery_cost":0.0, "min_level":0, "description":"Yoga, étirements, mobilité — aucun effort cardio.", "example":"20min yoga ou étirements doux"},
    {"id":2, "name":"Footing de récupération", "category":"recuperation", "intensity":0.2, "duration_min":30, "distance_km":5, "recovery_cost":0.1, "min_level":0, "description":"Course très lente, on peut tenir une conversation.", "example":"30min très lentement"},
    {"id":3, "name":"Sortie plaisir", "category":"recuperation", "intensity":0.25, "duration_min":40, "distance_km":6, "recovery_cost":0.15, "min_level":0, "description":"Sans chrono ni objectif — juste profiter.", "example":"40min à l'aise, sans regarder la vitesse"},
    {"id":4, "name":"Cross-training vélo", "category":"recuperation", "intensity":0.25, "duration_min":45, "distance_km":0, "recovery_cost":0.1, "min_level":0, "description":"Vélo, elliptique ou natation — entretient la forme sans impact.", "example":"45min vélo ou elliptique tranquille"},
    {"id":5, "name":"Natation récupération", "category":"recuperation", "intensity":0.2, "duration_min":40, "distance_km":0, "recovery_cost":0.05, "min_level":0, "description":"Natation lente — parfait après une semaine chargée.", "example":"40min nage libre tranquille"},
    {"id":6, "name":"Sortie courte endurance", "category":"endurance", "intensity":0.35, "duration_min":30, "distance_km":5, "recovery_cost":0.2, "min_level":0, "description":"Course courte à allure facile — idéale semaines chargées.", "example":"30min à allure confortable"},
    {"id":7, "name":"Endurance fondamentale", "category":"endurance", "intensity":0.45, "duration_min":60, "distance_km":10, "recovery_cost":0.3, "min_level":0, "description":"La base de tout plan — allure modérée Z2.", "example":"60min à allure confort"},
    {"id":8, "name":"Sortie longue débutant", "category":"endurance", "intensity":0.35, "duration_min":60, "distance_km":8, "recovery_cost":0.35, "min_level":0, "description":"Sortie longue accessible — marche/course si besoin.", "example":"60min marche/course alternée"},
    {"id":9, "name":"Sortie longue Z2", "category":"endurance", "intensity":0.4, "duration_min":90, "distance_km":15, "recovery_cost":0.4, "min_level":1, "description":"Course longue aérobie — construit l'endurance de fond.", "example":"90min à allure endurance"},
    {"id":10, "name":"Sortie longue progressive", "category":"endurance", "intensity":0.55, "duration_min":90, "distance_km":16, "recovery_cost":0.5, "min_level":1, "description":"Commence lentement, finit plus vite.", "example":"90min : 60min facile + 30min plus soutenu"},
    {"id":11, "name":"Sortie très longue", "category":"endurance", "intensity":0.4, "duration_min":150, "distance_km":25, "recovery_cost":0.65, "min_level":2, "description":"Sortie longue pour coureurs expérimentés.", "example":"2h30 à allure endurance"},
    {"id":12, "name":"Trail découverte", "category":"endurance", "intensity":0.4, "duration_min":60, "distance_km":10, "recovery_cost":0.35, "min_level":0, "description":"Première sortie en nature — marche dans les montées.", "example":"60min trail avec marche dans les côtes"},
    {"id":13, "name":"Trail technique", "category":"specifique", "intensity":0.55, "duration_min":90, "distance_km":14, "recovery_cost":0.5, "min_level":1, "description":"Technique en terrain varié — descentes, sentiers.", "example":"90min trail avec dénivelé modéré"},
    {"id":14, "name":"Sortie longue trail", "category":"endurance", "intensity":0.4, "duration_min":150, "distance_km":22, "recovery_cost":0.55, "min_level":1, "description":"Sortie longue en montagne — marche/course selon dénivelé.", "example":"2h30 avec dénivelé"},
    {"id":15, "name":"Ultra endurance", "category":"endurance", "intensity":0.35, "duration_min":240, "distance_km":40, "recovery_cost":0.85, "min_level":2, "description":"Sortie très longue préparation ultra.", "example":"4h avec dénivelé important"},
    {"id":16, "name":"Power hiking", "category":"force", "intensity":0.45, "duration_min":60, "distance_km":8, "recovery_cost":0.4, "min_level":0, "description":"Marche rapide en côte — technique ultra, préserve les jambes.", "example":"60min randonnée rapide en montée"},
    {"id":17, "name":"Tempo court", "category":"intensite", "intensity":0.65, "duration_min":40, "distance_km":8, "recovery_cost":0.5, "min_level":1, "description":"Course soutenue courte au seuil.", "example":"40min : échauffement + 20min tempo + retour"},
    {"id":18, "name":"Tempo continu", "category":"intensite", "intensity":0.7, "duration_min":55, "distance_km":10, "recovery_cost":0.6, "min_level":1, "description":"Course soutenue au seuil anaérobie.", "example":"55min à allure semi-marathon"},
    {"id":19, "name":"Tempo long", "category":"intensite", "intensity":0.68, "duration_min":70, "distance_km":13, "recovery_cost":0.65, "min_level":2, "description":"Tempo étendu — résistance à l'allure.", "example":"70min : échauffement + 45min tempo + retour"},
    {"id":20, "name":"Progression", "category":"endurance", "intensity":0.55, "duration_min":60, "distance_km":11, "recovery_cost":0.45, "min_level":1, "description":"Allure croissante — finit fort.", "example":"60min en accélérant progressivement"},
    {"id":21, "name":"Fractionné court débutant", "category":"intensite", "intensity":0.7, "duration_min":40, "distance_km":6, "recovery_cost":0.5, "min_level":0, "description":"Introduction au fractionné — courtes accélérations.", "example":"40min : 8x200m rapide avec 2min marche"},
    {"id":22, "name":"Fractionné court", "category":"intensite", "intensity":0.85, "duration_min":50, "distance_km":9, "recovery_cost":0.65, "min_level":1, "description":"Répétitions courtes haute intensité.", "example":"50min : 10x400m avec récupération"},
    {"id":23, "name":"Fractionné long", "category":"intensite", "intensity":0.78, "duration_min":60, "distance_km":11, "recovery_cost":0.65, "min_level":1, "description":"Répétitions longues au seuil.", "example":"60min : 5x1000m avec récupération"},
    {"id":24, "name":"VO2max", "category":"intensite", "intensity":0.95, "duration_min":50, "distance_km":9, "recovery_cost":0.8, "min_level":2, "description":"Intervalles à intensité maximale aérobie.", "example":"50min : 6x3min à intensité maximale"},
    {"id":25, "name":"30-30", "category":"intensite", "intensity":0.85, "duration_min":40, "distance_km":7, "recovery_cost":0.6, "min_level":1, "description":"30s vite / 30s lent — VO2max accessible.", "example":"40min : 20x(30s rapide + 30s trot)"},
    {"id":26, "name":"Pyramide", "category":"intensite", "intensity":0.8, "duration_min":55, "distance_km":10, "recovery_cost":0.65, "min_level":1, "description":"Fractionné pyramidal.", "example":"200-400-600-800-600-400-200m"},
    {"id":27, "name":"Côtes courtes", "category":"force", "intensity":0.85, "duration_min":45, "distance_km":7, "recovery_cost":0.65, "min_level":0, "description":"Répétitions en montée courte — puissance et force.", "example":"45min : 8-10x100m de côte raide"},
    {"id":28, "name":"Côtes longues", "category":"force", "intensity":0.78, "duration_min":55, "distance_km":9, "recovery_cost":0.7, "min_level":1, "description":"Répétitions en montée longue — endurance musculaire.", "example":"55min : 6x400m côte"},
    {"id":29, "name":"Fartlek", "category":"intensite", "intensity":0.65, "duration_min":50, "distance_km":9, "recovery_cost":0.5, "min_level":0, "description":"Jeu de vitesse libre — accélérations spontanées.", "example":"50min avec accélérations quand tu veux"},
    {"id":30, "name":"Spécifique 5km", "category":"specifique", "intensity":0.85, "duration_min":45, "distance_km":9, "recovery_cost":0.6, "min_level":1, "description":"Travail à allure 5km.", "example":"45min : 3-4x1600m à allure cible"},
    {"id":31, "name":"Spécifique 10km", "category":"specifique", "intensity":0.78, "duration_min":55, "distance_km":11, "recovery_cost":0.6, "min_level":1, "description":"Travail à allure 10km.", "example":"55min : 3x2km à allure cible"},
    {"id":32, "name":"Spécifique semi-marathon", "category":"specifique", "intensity":0.7, "duration_min":75, "distance_km":14, "recovery_cost":0.6, "min_level":1, "description":"Allure semi en condition de course.", "example":"75min dont 40min à allure cible"},
    {"id":33, "name":"Spécifique marathon", "category":"specifique", "intensity":0.65, "duration_min":100, "distance_km":20, "recovery_cost":0.65, "min_level":2, "description":"La séance clé de la préparation marathon.", "example":"100min dont 60min à allure cible"},
    {"id":34, "name":"Allure compétition débutant", "category":"specifique", "intensity":0.6, "duration_min":45, "distance_km":7, "recovery_cost":0.4, "min_level":0, "description":"Courir à l'allure de ta prochaine course — sans s'épuiser.", "example":"45min à l'allure envisagée pour ta course"},
    {"id":35, "name":"Gainage running", "category":"force", "intensity":0.35, "duration_min":30, "distance_km":0, "recovery_cost":0.2, "min_level":0, "description":"Renforcement spécifique — gainage, fessiers, ischios.", "example":"30min : gainage, squats, fentes, pont fessier"},
    {"id":36, "name":"Renforcement musculaire", "category":"force", "intensity":0.4, "duration_min":45, "distance_km":0, "recovery_cost":0.25, "min_level":0, "description":"Gym ou poids de corps — prévention blessures.", "example":"45min circuit fonctionnel"},
    {"id":37, "name":"Mobilité et souplesse", "category":"recuperation", "intensity":0.1, "duration_min":30, "distance_km":0, "recovery_cost":0.0, "min_level":0, "description":"Étirements, foam roller, mobilité articulaire.", "example":"30min yoga running ou étirements"},
    {"id":38, "name":"ABC de course", "category":"force", "intensity":0.4, "duration_min":30, "distance_km":4, "recovery_cost":0.2, "min_level":0, "description":"Exercices techniques — améliore la foulée et l'économie.", "example":"Échauffement + ABC + 15min facile"},
    {"id":39, "name":"Seuil lactique court", "category":"intensite", "intensity":0.72, "duration_min":45, "distance_km":8, "recovery_cost":0.55, "min_level":1, "description":"Travail au seuil lactique.", "example":"45min : 2x15min seuil avec récupération"},
    {"id":40, "name":"Seuil lactique long", "category":"intensite", "intensity":0.75, "duration_min":65, "distance_km":12, "recovery_cost":0.65, "min_level":2, "description":"Effort prolongé au seuil.", "example":"65min : 3x15min seuil"},
    {"id":41, "name":"Pré-compétition activation", "category":"specifique", "intensity":0.45, "duration_min":30, "distance_km":5, "recovery_cost":0.2, "min_level":0, "description":"Sortie légère J-2 ou J-1 — réveille les jambes.", "example":"30min très facile + quelques accélérations légères"},
    {"id":42, "name":"Récupération post-compétition", "category":"recuperation", "intensity":0.15, "duration_min":25, "distance_km":3, "recovery_cost":0.0, "min_level":0, "description":"Footing très lent le lendemain d'une course.", "example":"25min marche ou trot très lent"},
    {"id":43, "name":"Séance sur piste", "category":"intensite", "intensity":0.82, "duration_min":50, "distance_km":10, "recovery_cost":0.65, "min_level":1, "description":"Entraînement sur piste — allures précises.", "example":"50min : 8x400m sur piste"},
    {"id":44, "name":"Course en nature", "category":"endurance", "intensity":0.4, "duration_min":60, "distance_km":10, "recovery_cost":0.3, "min_level":0, "description":"Sortie trail décontractée — profiter sans objectif.", "example":"60min en forêt à allure plaisir"},
]

CAT_LABELS = {
    "endurance": "Endurance", "intensite": "Intensité",
    "recuperation": "Récupération", "force": "Force",
    "specifique": "Spécifique", "repos": "Repos",
}

LEVEL_MAP = {"debutant": 0, "intermediaire": 1, "avance": 2, "elite": 2}

DIST_SESSION_MAP = {
    # IDs alignés sur session_types_v2.py (cronos-ml) — source de vérité
    "5k":      [30, 21, 22, 29],   # Spécifique 5km, Frac. court débutant, Frac. court, Fartlek
    "10k":     [31, 22, 23, 39],   # Spécifique 10km, Frac. court, Frac. long, Seuil court
    "semi":    [32, 18, 23, 20],   # Spécifique semi, Tempo continu, Frac. long, Progression
    "marathon":[33, 9, 18, 10],    # Spécifique marathon, Sortie longue Z2, Tempo continu, Sortie longue progressive
    "ultra":   [14, 15, 16, 11],   # Sortie longue trail, Ultra endurance, Power hiking, Sortie très longue
}


def _get_user_level(avg_pace_kmh: float | None) -> int:
    if not avg_pace_kmh or avg_pace_kmh <= 0:
        return 0
    min_per_km = 60 / avg_pace_kmh
    if min_per_km > 7:   return 0
    if min_per_km > 5.5: return 1
    return 2


def _days_to_next_race_sync(races: list) -> int | None:
    if not races:
        return None
    upcoming = [r for r in races if not getattr(r, 'is_completed', False)]
    if not upcoming:
        return None
    next_race = min(upcoming, key=lambda r: r.race_date)
    try:
        return (next_race.race_date - date.today()).days
    except Exception:
        return None


# Sessions nécessitant du dénivelé (trail, côtes)
_NEEDS_HILLS_IDS = {14, 15, 16, 17, 18, 30, 31}


def _compute_terrain_profile(activities: list) -> dict:
    """
    Analyse les activités pour caractériser le terrain habituel de l'athlète.
    Retourne : typical_distance_km, elev_per_km, terrain ('flat'|'moderate'|'hilly').
    """
    import numpy as np
    distances = [float(a.distance_km) for a in activities if a.distance_km and a.distance_km > 0]
    elevs_per_km = [
        float(a.elevation_gain_m) / float(a.distance_km)
        for a in activities
        if a.elevation_gain_m and a.distance_km and a.distance_km > 0
    ]

    typical_dist = float(np.median(distances)) if distances else None
    elev_per_km  = float(np.median(elevs_per_km)) if elevs_per_km else None

    terrain = "unknown"
    if elev_per_km is not None:
        if elev_per_km >= 20:
            terrain = "hilly"
        elif elev_per_km >= 8:
            terrain = "moderate"
        else:
            terrain = "flat"

    return {
        "typical_distance_km": round(typical_dist, 1) if typical_dist else None,
        "elev_per_km":         round(elev_per_km, 1)  if elev_per_km  else None,
        "terrain":             terrain,
    }


def _compute_training_load(activities: list) -> dict:
    """
    ATL/CTL/TSB selon le modèle PMC (Performance Management Chart).

    ATL (Acute Training Load)     — EMA τ=7 jours,  α=1-e^(-1/7)  ≈ 0.133
    CTL (Chronic Training Load)   — EMA τ=42 jours, α=1-e^(-1/42) ≈ 0.024
    TSB (Training Stress Balance) = CTL - ATL
        TSB > 0 → fraîcheur/forme disponible
        TSB < 0 → fatigue accumulée
    """
    import math
    today = date.today()

    # Agrège la charge par jour (plusieurs activités possibles le même jour)
    daily_loads: dict = {}
    km_7, sess_7 = 0.0, 0

    for a in activities:
        adate = a.date
        days_ago = (today - adate).days
        if days_ago > 42:
            continue
        load = float(a.duration_min or 0) * float(a.avg_hr or 0) / 100
        daily_loads[adate] = daily_loads.get(adate, 0.0) + load
        if days_ago <= 7:
            km_7   += float(a.distance_km or 0)
            sess_7 += 1

    # Coefficients EMA standards (déclin exponentiel, compatibles TrainingPeaks/GC)
    alpha_atl = 1.0 - math.exp(-1.0 / 7.0)    # ≈ 0.133
    alpha_ctl = 1.0 - math.exp(-1.0 / 42.0)   # ≈ 0.024

    # Calcul EMA du plus ancien (J-41) au plus récent (J-0)
    atl, ctl = 0.0, 0.0
    for offset in range(41, -1, -1):
        day  = today - timedelta(days=offset)
        load = daily_loads.get(day, 0.0)
        atl  = atl + alpha_atl * (load - atl)
        ctl  = ctl + alpha_ctl * (load - ctl)

    tsb = round(ctl - atl, 1)

    return {
        "atl":             round(atl, 1),
        "ctl":             round(ctl, 1),
        "tsb":             tsb,
        "weekly_km":       round(km_7, 1),
        "weekly_sessions": sess_7,
        "load_trend": (
            "surcharge" if tsb < -20 else
            "charge"    if tsb < -5  else
            "équilibré" if tsb < 10  else
            "fraîcheur"
        ),
    }


def _compute_recovery(metrics: list, load_info: Optional[dict] = None) -> tuple[float, dict]:
    """
    Recovery score 0-1.
    Signaux prioritaires : HRV (50%), sommeil (30%), body battery (20%).
    Fallback sans HRV/sommeil : FC repos vs baseline (40%) + charge (40%) + body battery (20%).
    """
    import numpy as np
    if not metrics:
        return 0.5, {}
    latest = metrics[0]

    hrv_values  = [float(m.hrv_last_night) for m in metrics if m.hrv_last_night]
    hrv_mean    = float(np.median(hrv_values)) if hrv_values else None
    hrv_today   = float(latest.hrv_last_night) if latest.hrv_last_night else None
    sleep_today = float(latest.sleep_score)    if latest.sleep_score    else None
    bb_today    = float(latest.body_battery_charged) if latest.body_battery_charged else None

    # FC repos : baseline = médiane 14 jours, déviation → fatigue
    hr_rest_values = [float(m.resting_hr) for m in metrics if m.resting_hr]
    hr_rest_today  = float(latest.resting_hr) if latest.resting_hr else None
    hr_rest_median = float(np.median(hr_rest_values)) if len(hr_rest_values) >= 3 else None

    recovery  = 0.5
    n_signals = 0

    has_hrv   = hrv_today and hrv_mean and hrv_mean > 0
    has_sleep = bool(sleep_today)

    if has_hrv:
        hrv_score = min(hrv_today / hrv_mean, 1.5) / 1.5
        recovery += (hrv_score - 0.5) * 0.5
        n_signals += 1
    if has_sleep:
        recovery += (sleep_today / 100 - 0.5) * 0.3
        n_signals += 1
    if bb_today:
        recovery += (bb_today / 100 - 0.5) * (0.2 if (has_hrv or has_sleep) else 0.35)
        n_signals += 1

    # Fallback FC repos quand HRV absent : +1bpm au-dessus baseline → -3% récupération
    if not has_hrv and hr_rest_today and hr_rest_median:
        delta_bpm = hr_rest_today - hr_rest_median
        # +5bpm → -15%, -5bpm → +15% (plafonné à ±0.20)
        hr_adj = max(-0.20, min(0.20, -delta_bpm * 0.03))
        recovery += hr_adj * (0.40 if not has_sleep else 0.20)
        n_signals += 1

    # Charge d'entraînement — poids renforcé si peu de signaux bio
    if load_info:
        tsb = load_info.get("tsb", 0)
        load_weight = 0.30 if n_signals == 0 else (0.15 if n_signals == 1 else 0.06)
        if tsb < -25:
            recovery = max(0.05, recovery - load_weight * 1.0)
        elif tsb < -10:
            recovery = max(0.05, recovery - load_weight * 0.5)
        elif tsb > 15:
            recovery = min(0.95, recovery + load_weight * 0.3)
        if load_info.get("weekly_sessions", 0) == 0:
            recovery = min(0.95, recovery + 0.05)
        n_signals += 1

    recovery = max(0.05, min(0.95, recovery))
    return recovery, {
        "hrv_today":    round(hrv_today, 1)    if hrv_today    else None,
        "hrv_mean":     round(hrv_mean, 1)     if hrv_mean     else None,
        "sleep_score":  int(sleep_today)       if sleep_today  else None,
        "body_battery": int(bb_today)          if bb_today     else None,
        "hr_rest_today":int(hr_rest_today)     if hr_rest_today     else None,
        "hr_rest_median":int(hr_rest_median)   if hr_rest_median    else None,
        "n_signals":    n_signals,
    }


def _rank_sessions(
    recovery: float,
    user_level: int = 0,
    days_to_race: int | None = None,
    top_k: int = 5,
    goal: str | None = None,
    target_dist: str | None = None,
    feedback_adjustments: dict | None = None,
    recent_session_ids: list[int] | None = None,
    terrain_profile: dict | None = None,
) -> list[dict]:

    # J-1 ou J0 : activation uniquement
    if days_to_race is not None and days_to_race <= 1:
        priority_ids = [39, 1, 6, 2, 3]
        results = []
        for sid in priority_ids:
            s = next((x for x in SESSIONS_V2 if x["id"] == sid), None)
            if s and len(results) < top_k:
                results.append({**s, "rank": len(results)+1, "score": 95 - len(results)*5,
                                 "note": "J-1 avant ta course — préserve tes jambes !"})
        return results

    # Récupération très faible → repos
    if recovery < 0.25:
        rest_ids = [0, 1, 6, 2, 5]
        results = []
        for sid in rest_ids[:top_k]:
            s = next((x for x in SESSIONS_V2 if x["id"] == sid), None)
            if s:
                results.append({**s, "rank": len(results)+1, "score": 90 - len(results)*5,
                                 "note": "Ton corps a besoin de récupérer — repose-toi !"})
        return results

    # J-7 : limite les séances intenses
    effective_recovery = recovery
    if days_to_race is not None and days_to_race <= 7:
        effective_recovery = min(recovery, 0.55)

    scored = []
    for s in SESSIONS_V2:
        if s["min_level"] > user_level:
            continue

        intensity = s["intensity"]
        cost = s["recovery_cost"]
        cat = s["category"]

        # Score de base selon récupération
        if effective_recovery >= 0.75:
            score = 1.0 - abs(intensity - 0.75) * 0.5
        elif effective_recovery >= 0.55:
            score = 1.0 - abs(intensity - 0.55) * 0.7
        elif effective_recovery >= 0.4:
            score = 1.0 - abs(intensity - 0.35) * 0.9
        else:
            score = 1.0 - abs(intensity - 0.2) * 1.1

        # Pénalités fatigue
        if effective_recovery < 0.45 and cost > 0.6:
            score *= 0.35
        if effective_recovery < 0.35 and cost > 0.4:
            score *= 0.5
        if effective_recovery < 0.4 and cat in ("recuperation", "repos"):
            score = min(0.97, score * 1.4)
        if cat == "force" and effective_recovery >= 0.4:
            score = min(0.95, score * 1.1)
        if effective_recovery >= 0.65 and cat == "repos":
            score *= 0.3

        # Logique course à venir J-7
        if days_to_race is not None and days_to_race <= 7:
            if cat == "specifique" and intensity < 0.6:
                score = min(0.95, score * 1.2)
            if cat in ("repos", "recuperation"):
                score = min(0.95, score * 1.15)

        # ── Bonus profil athlète ──

        # Objectif déclaré
        if goal == "plaisir" and cat in ("recuperation", "endurance"):
            score = min(0.97, score * 1.1)
        if goal == "plaisir" and cat == "intensite":
            score *= 0.85  # pénalise légèrement les séances intenses
        if goal == "competition" and cat in ("intensite", "specifique") and effective_recovery >= 0.55:
            score = min(0.97, score * 1.15)
        if goal == "progression" and cat == "endurance":
            score = min(0.97, score * 1.1)
        if goal == "chrono" and cat in ("intensite", "specifique"):
            score = min(0.97, score * 1.08)

        # Distance cible → booste les séances spécifiques
        if target_dist and s["id"] in DIST_SESSION_MAP.get(target_dist, []):
            score = min(0.97, score * 1.2)

        # Ajustement feedback (bonus facile, malus difficile)
        if feedback_adjustments and s["id"] in feedback_adjustments:
            score = max(0.03, min(0.97, score + feedback_adjustments[s["id"]]))

        # Anti-répétition : ×0.62 (moins brutal que ×0.45)
        if recent_session_ids and s["id"] in recent_session_ids:
            score = max(0.03, score * 0.62)

        # ── Terrain : adapte les séances au profil réel de l'athlète ──
        if terrain_profile:
            terrain = terrain_profile.get("terrain", "unknown")
            typical_dist = terrain_profile.get("typical_distance_km")

            # Séances nécessitant du dénivelé : bonus si hilly, malus si flat
            if s["id"] in _NEEDS_HILLS_IDS:
                if terrain == "flat":
                    score *= 0.45   # peu réaliste pour cet athlète
                elif terrain == "moderate":
                    score = min(0.97, score * 1.05)
                elif terrain == "hilly":
                    score = min(0.97, score * 1.20)

            # Distance proposée >> distance typique → légère pénalité
            if typical_dist and s["distance_km"] > 0:
                ratio = s["distance_km"] / typical_dist
                if ratio > 2.5:
                    score *= 0.80
                elif ratio > 1.8:
                    score *= 0.90

        scored.append((max(0.03, min(0.97, score)), s))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:top_k]

    # Calibration sur le top_k uniquement :
    # 1. Top-1 toujours ≥ 80/100
    # 2. Écart minimum de 20 pts entre top-1 et dernière session (lisibilité)
    if top:
        max_raw = top[0][0]
        if max_raw < 0.80:
            boost = 0.80 / max_raw
            top = [(min(0.97, sc * boost), s) for sc, s in top]

        if len(top) >= 2:
            top_sc = top[0][0]
            bot_sc = top[-1][0]
            if top_sc - bot_sc < 0.20:
                n = len(top)
                top = [
                    (max(0.03, top_sc - 0.20 * i / (n - 1)), s)
                    for i, (_, s) in enumerate(top)
                ]

    results = []
    for i, (sc, s) in enumerate(top):
        item = {**s, "rank": i+1, "score": round(sc * 100, 1)}
        if days_to_race is not None and days_to_race <= 7:
            item["note"] = f"J-{days_to_race} avant ta course — semaine de récupération"
        results.append(item)
    return results


# ─────────────────────────────────────────────────────────────
# RECOMMEND
# ─────────────────────────────────────────────────────────────

@router.get("/users/{name}/recommend")
async def recommend_sessions(
    name: str,
    top_k: int = Query(5, ge=1, le=10),
    refresh: bool = Query(False, description="Forcer le recalcul même si le cache existe"),
    db: AsyncSession = Depends(get_db),
    caller_email: str = Depends(get_caller_email),
):
    import numpy as np

    user = await require_owner(name, db, caller_email)

    # ── Métriques 90 derniers jours ──────────────────────────────────────────
    metrics = (await db.execute(
        select(DailyMetric)
        .where(DailyMetric.user_id == user.id)
        .where(DailyMetric.date >= date.today() - timedelta(days=90))
        .order_by(DailyMetric.date.desc())
    )).scalars().all()

    METRICS_NEEDED    = 14
    ACTIVITIES_NEEDED = 3

    metrics_days = len({
        m.date for m in metrics
        if any([
            m.resting_hr, m.sleep_duration_min, m.hrv_last_night,
            m.total_steps, m.avg_stress, m.body_battery_charged,
        ])
    })

    activities_42 = (await db.execute(
        select(Activity)
        .where(Activity.user_id == user.id)
        .where(Activity.date >= date.today() - timedelta(days=90))
        .where(Activity.activity_type.in_(["running", "trail_running", "treadmill_running"]))
        .order_by(Activity.date.desc())
    )).scalars().all()

    # Vérification des données insuffisantes AVANT le cache serveur,
    # pour ne jamais servir un cache périmé à un utilisateur sans données.
    if metrics_days < METRICS_NEEDED or len(activities_42) < ACTIVITIES_NEEDED:
        return {
            "status":             "insufficient_data",
            "metrics_days":       metrics_days,
            "metrics_needed":     METRICS_NEEDED,
            "activities_count":   len(activities_42),
            "activities_needed":  ACTIVITIES_NEEDED,
        }

    # ── Cache du jour ────────────────────────────────────────────────────────
    if not refresh:
        cached = (await db.execute(
            select(RecommendationsCache)
            .where(RecommendationsCache.user_id == user.id)
            .where(RecommendationsCache.cache_date == date.today())
        )).scalar_one_or_none()

        if cached:
            return {
                "user":            name,
                "date":            date.today().isoformat(),
                "cached":          True,
                "computed_with_ml":cached.computed_with_ml,
                "recovery":        json.loads(cached.recovery_json or "{}"),
                "athlete":         json.loads(cached.athlete_json  or "{}"),
                "load":            json.loads(cached.load_json     or "{}"),
                "recommendations": json.loads(cached.recommendations_json),
            }

    # ── Charge 7j glissants ──────────────────────────────────────────────────
    load_info = _compute_training_load(activities_42)

    # ── Profil terrain (distance typique + dénivelé habituel) ────────────────
    terrain_profile = _compute_terrain_profile(list(activities_42))

    # ── Recovery score (avec charge) ─────────────────────────────────────────
    recovery, signals = _compute_recovery(metrics, load_info)

    # ── Allure + niveau ───────────────────────────────────────────────────────
    speeds = [float(a.avg_speed_kmh) for a in activities_42 if a.avg_speed_kmh and a.avg_speed_kmh > 0]
    avg_pace_kmh = float(np.mean(speeds)) if speeds else None
    user_level = _get_user_level(avg_pace_kmh)

    # ── Profil déclaré ────────────────────────────────────────────────────────
    profile = (await db.execute(
        select(AthleteProfile).where(AthleteProfile.user_id == user.id)
    )).scalar_one_or_none()

    goal = target_dist = None
    if profile:
        if profile.level:
            user_level = max(user_level, LEVEL_MAP.get(profile.level, 0))
        goal        = profile.primary_goal
        target_dist = profile.target_distance

    # ── Prochaine course ──────────────────────────────────────────────────────
    races = (await db.execute(
        select(PlannedRace)
        .where(PlannedRace.user_id == user.id)
        .where(PlannedRace.is_completed == False)
        .where(PlannedRace.race_date >= date.today())
        .order_by(PlannedRace.race_date)
    )).scalars().all()

    days_to_race = _days_to_next_race_sync(races)

    # ── Historique 3j — anti-répétition ──────────────────────────────────────
    recent_history = (await db.execute(
        select(SessionHistory)
        .where(SessionHistory.user_id == user.id)
        .where(SessionHistory.done_at >= date.today() - timedelta(days=3))
    )).scalars().all()
    recent_session_ids = [h.session_id for h in recent_history]

    # ── Feedback utilisateur — ajustements de score ───────────────────────────
    feedback_rows = (await db.execute(
        select(SessionFeedback)
        .where(SessionFeedback.user_id == user.id)
        .where(SessionFeedback.done_at >= date.today() - timedelta(days=30))
    )).scalars().all()

    # Score d'ajustement par session_id basé sur le feedback
    feedback_adjustments: dict[int, float] = {}
    for fb in feedback_rows:
        adj = {"facile": 0.08, "ok": 0.0, "difficile": -0.10}.get(fb.feedback, 0.0)
        # Décroissance temporelle : feedback récent a plus de poids
        days_ago = (date.today() - fb.done_at).days
        weight   = max(0.2, 1.0 - days_ago / 30)
        feedback_adjustments[fb.session_id] = (
            feedback_adjustments.get(fb.session_id, 0.0) + adj * weight
        )

    # ── Recommandations — ML d'abord, heuristique en fallback ────────────────
    ml_results   = ml_recommend(
        metrics=list(metrics),
        activities=list(activities_42),
        profile=profile,
        races=list(races),
        user_name=name,
        top_k=top_k,
    )
    used_ml = ml_results is not None

    if ml_results:
        for r in ml_results:
            adj = feedback_adjustments.get(r["id"], 0.0)
            score = r["score"] + adj * 100
            if r["id"] in recent_session_ids:
                score *= 0.62
            r["score"] = round(max(1.0, min(99.0, score)), 1)
        ml_results.sort(key=lambda r: r["score"], reverse=True)
        # Même calibration affichage que l'heuristique
        if ml_results and ml_results[0]["score"] < 80:
            boost = 80 / ml_results[0]["score"]
            for r in ml_results:
                r["score"] = round(min(97, r["score"] * boost), 1)
        for i, r in enumerate(ml_results):
            r["rank"] = i + 1
        recommendations = ml_results
    else:
        recommendations = _rank_sessions(
            recovery, user_level, days_to_race, top_k, goal, target_dist,
            feedback_adjustments=feedback_adjustments,
            recent_session_ids=recent_session_ids,
            terrain_profile=terrain_profile,
        )

    # ── Assemblage réponse ────────────────────────────────────────────────────
    recovery_out = {
        "score": round(recovery * 100, 1),
        "level": (
            "Excellente" if recovery >= 0.75 else
            "Bonne"      if recovery >= 0.55 else
            "Moyenne"    if recovery >= 0.4  else
            "Faible"
        ),
        **signals,
    }
    athlete_out = {
        "level":             ["Débutant", "Intermédiaire", "Avancé"][user_level],
        "avg_pace_kmh":      round(avg_pace_kmh, 1) if avg_pace_kmh else None,
        "days_to_next_race": days_to_race,
        "goal":              goal,
        "target_distance":   target_dist,
    }

    # ── Mise en cache ─────────────────────────────────────────────────────────
    await db.execute(
        delete(RecommendationsCache)
        .where(RecommendationsCache.user_id == user.id)
        .where(RecommendationsCache.cache_date == date.today())
    )
    db.add(RecommendationsCache(
        user_id              = user.id,
        cache_date           = date.today(),
        recommendations_json = json.dumps(recommendations, ensure_ascii=False),
        athlete_json         = json.dumps(athlete_out,     ensure_ascii=False),
        recovery_json        = json.dumps(recovery_out,    ensure_ascii=False),
        load_json            = json.dumps(load_info,       ensure_ascii=False),
        computed_with_ml     = used_ml,
    ))
    await db.commit()

    return {
        "user":            name,
        "date":            date.today().isoformat(),
        "cached":          False,
        "computed_with_ml":used_ml,
        "recovery":        recovery_out,
        "athlete":         athlete_out,
        "load":            load_info,
        "terrain":         terrain_profile,
        "recommendations": recommendations,
    }


# ─────────────────────────────────────────────────────────────
# RECOVERY SCORE
# ─────────────────────────────────────────────────────────────

@router.get("/users/{name}/recovery-score")
async def get_recovery_score(
    name: str,
    db: AsyncSession = Depends(get_db),
    caller_email: str = Depends(get_caller_email),
):
    user = await require_owner(name, db, caller_email)
    since = date.today() - timedelta(days=15)
    metrics = (await db.execute(
        select(DailyMetric)
        .where(DailyMetric.user_id == user.id)
        .where(DailyMetric.date >= since)
        .order_by(DailyMetric.date.desc())
    )).scalars().all()

    if not metrics:
        return {"user": name, "score": None, "level": None, "message": "Pas de données disponibles"}

    recovery, signals = _compute_recovery(metrics)
    return {
        "user":  name,
        "date":  date.today().isoformat(),
        "score": round(recovery * 100, 1),
        "level": (
            "Excellente" if recovery >= 0.75 else
            "Bonne"      if recovery >= 0.55 else
            "Moyenne"    if recovery >= 0.4  else
            "Faible"
        ),
        **signals,
    }


# ─────────────────────────────────────────────────────────────
# TRENDS
# ─────────────────────────────────────────────────────────────

@router.get("/users/{name}/trends")
async def get_trends(
    name: str,
    days: int = Query(30, ge=2, le=90),
    db: AsyncSession = Depends(get_db),
    caller_email: str = Depends(get_caller_email),
):
    user = await require_owner(name, db, caller_email)
    since = date.today() - timedelta(days=days)
    metrics = (await db.execute(
        select(DailyMetric)
        .where(DailyMetric.user_id == user.id)
        .where(DailyMetric.date >= since)
        .order_by(DailyMetric.date.asc())
    )).scalars().all()

    if not metrics:
        raise HTTPException(404, "Pas de données disponibles.")

    series = []
    for m in metrics:
        series.append({
            "date":         m.date.isoformat(),
            "hrv":          float(m.hrv_last_night)     if m.hrv_last_night      else None,
            "resting_hr":   int(m.resting_hr)           if m.resting_hr          else None,
            "sleep_score":  int(m.sleep_score)          if m.sleep_score         else None,
            "sleep_min":    int(m.sleep_duration_min)   if m.sleep_duration_min  else None,
            "body_battery": int(m.body_battery_charged) if m.body_battery_charged else None,
            "steps":        int(m.total_steps)          if m.total_steps         else None,
            "stress":       int(m.avg_stress)           if m.avg_stress          else None,
        })

    import numpy as np

    def trend(vals):
        if len(vals) < 5: return "stable"
        h = len(vals) // 2
        diff = (np.mean(vals[h:]) - np.mean(vals[:h])) / (np.mean(vals[:h]) + 1e-6)
        return "hausse" if diff > 0.05 else "baisse" if diff < -0.05 else "stable"

    hrv_v   = [s["hrv"]         for s in series if s["hrv"]]
    sleep_v = [s["sleep_score"] for s in series if s["sleep_score"]]
    hr_v    = [s["resting_hr"]  for s in series if s["resting_hr"]]

    summary = {
        "hrv":         {"mean": round(float(np.mean(hrv_v)),   1) if hrv_v   else None, "trend": trend(hrv_v)},
        "sleep_score": {"mean": round(float(np.mean(sleep_v)), 1) if sleep_v else None, "trend": trend(sleep_v)},
        "resting_hr":  {"mean": round(float(np.mean(hr_v)),    1) if hr_v    else None, "trend": trend(hr_v)},
    }

    return {
        "user":    name,
        "period":  f"{since.isoformat()} → {date.today().isoformat()}",
        "n_days":  len(series),
        "summary": summary,
        "series":  series,
    }


# ─────────────────────────────────────────────────────────────
# HEATMAP
# ─────────────────────────────────────────────────────────────

@router.get("/users/{name}/heatmap")
async def get_heatmap(
    name: str,
    days: int = Query(90, ge=7, le=365),
    sync: bool = Query(False, description="Télécharger les tracks manquants depuis Garmin"),
    db: AsyncSession = Depends(get_db),
    caller_email: str = Depends(get_caller_email),
):
    """
    Retourne les points GPS des activités récentes pour la heatmap Leaflet.
    Si sync=true, télécharge d'abord les tracks manquants depuis Garmin.
    """
    from app.services.heatmap import fetch_and_store_tracks, get_heatmap_points
    user = await require_owner(name, db, caller_email)

    new_tracks = 0
    if sync:
        new_tracks = await fetch_and_store_tracks(db, user, days=days)

    points, tracks = await get_heatmap_points(db, user, days=days)
    return {
        "user":       name,
        "n_points":   len(points),
        "new_tracks": new_tracks,
        "points":     points,
        "tracks":     tracks,
    }


# ─────────────────────────────────────────────────────────────
# LOAD HISTORY (graphe ATL/CTL/TSB)
# ─────────────────────────────────────────────────────────────

@router.get("/users/{name}/load-history")
async def get_load_history(
    name: str,
    days: int = Query(56, ge=14, le=90),
    db: AsyncSession = Depends(get_db),
    caller_email: str = Depends(get_caller_email),
):
    """
    Retourne l'évolution ATL/CTL/TSB jour par jour sur `days` jours.
    Utilisé pour le graphe de charge dans le frontend.
    """
    user = await require_owner(name, db, caller_email)

    # On charge les activités avec assez de recul pour calculer le CTL (42j)
    activities = (await db.execute(
        select(Activity)
        .where(Activity.user_id == user.id)
        .where(Activity.date >= date.today() - timedelta(days=days + 42))
        .where(Activity.activity_type.in_(["running", "trail_running", "treadmill_running"]))
    )).scalars().all()

    # Charge par jour : duration_min × avg_hr / 100
    daily_load: dict[date, float] = {}
    for a in activities:
        load = float(a.duration_min or 0) * float(a.avg_hr or 0) / 100
        daily_load[a.date] = daily_load.get(a.date, 0.0) + load

    today = date.today()
    history = []
    for d in range(days - 1, -1, -1):
        target = today - timedelta(days=d)
        atl = sum(daily_load.get(target - timedelta(days=j), 0.0) for j in range(7))  / 7
        ctl = sum(daily_load.get(target - timedelta(days=j), 0.0) for j in range(42)) / 42
        history.append({
            "date": target.isoformat(),
            "atl":  round(atl, 1),
            "ctl":  round(ctl, 1),
            "tsb":  round(ctl - atl, 1),
        })

    return {"user": name, "history": history}