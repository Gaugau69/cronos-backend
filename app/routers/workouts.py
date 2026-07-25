"""
app/routers/workouts.py — Export de séances CRONOS vers les montres connectées.
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import User, get_db
from app.dependencies import get_caller_email, require_owner

log = logging.getLogger(__name__)
router = APIRouter(prefix="/workouts", tags=["workouts"])


# ── Schémas ──────────────────────────────────────────────────────────────────

class SessionData(BaseModel):
    session_id: int
    session_name: str
    category: str        # repos / recuperation / endurance / intensite / force / specifique
    intensity: float     # 0.0 – 1.0
    duration_min: int
    distance_km: float
    description: str
    example: str


class WorkoutPushRequest(BaseModel):
    username: str
    session: SessionData


class WorkoutPushResponse(BaseModel):
    ok: bool
    provider: str
    workout_id: Any = None
    message: str = ""


# ── Conversion CRONOS → Garmin ────────────────────────────────────────────────

def _build_garmin_workout(session: SessionData):
    """Convertit une SessionData en RunningWorkout Garmin structuré."""
    from garminconnect.workout import (
        RunningWorkout, WorkoutSegment,
        create_warmup_step, create_interval_step,
        create_recovery_step, create_cooldown_step,
        create_repeat_group,
    )

    total_secs = session.duration_min * 60
    cat = session.category

    if cat in ("repos", "recuperation"):
        warmup_secs   = min(300, total_secs // 4)
        cooldown_secs = min(180, total_secs // 6)
        main_secs     = max(60, total_secs - warmup_secs - cooldown_secs)
        steps = [
            create_warmup_step(warmup_secs, step_order=1),
            create_recovery_step(main_secs, step_order=2),
            create_cooldown_step(cooldown_secs, step_order=3),
        ]

    elif cat in ("endurance", "specifique"):
        warmup_secs   = min(600, total_secs // 6)
        cooldown_secs = min(300, total_secs // 8)
        main_secs     = max(120, total_secs - warmup_secs - cooldown_secs)
        steps = [
            create_warmup_step(warmup_secs, step_order=1),
            create_interval_step(main_secs, step_order=2),
            create_cooldown_step(cooldown_secs, step_order=3),
        ]

    elif cat in ("intensite", "force"):
        warmup_secs   = min(600, total_secs // 5)
        cooldown_secs = min(300, total_secs // 8)
        interval_pool = max(60, total_secs - warmup_secs - cooldown_secs)

        # Durée d'un intervalle selon l'intensité
        if session.intensity >= 0.85:
            interval_secs  = 60
            recovery_secs  = 60
        elif session.intensity >= 0.70:
            interval_secs  = 180
            recovery_secs  = 120
        else:
            interval_secs  = 300
            recovery_secs  = 150

        repeat_cycle  = interval_secs + recovery_secs
        reps          = max(3, min(20, interval_pool // repeat_cycle))

        interval_step  = create_interval_step(interval_secs,  step_order=1)
        recovery_step  = create_recovery_step(recovery_secs,  step_order=2)
        repeat         = create_repeat_group(reps, [interval_step, recovery_step], step_order=2)

        steps = [
            create_warmup_step(warmup_secs, step_order=1),
            repeat,
            create_cooldown_step(cooldown_secs, step_order=3),
        ]

    else:
        # Fallback : séance simple
        steps = [create_interval_step(total_secs, step_order=1)]

    return RunningWorkout(
        workoutName=session.session_name,
        description=f"{session.description} — {session.example}",
        estimatedDurationInSecs=total_secs,
        workoutSegments=[
            WorkoutSegment(
                segmentOrder=1,
                sportType={"sportTypeId": 1, "sportTypeKey": "running"},
                workoutSteps=steps,
            )
        ],
    )


# ── Conversion CRONOS → Coros ─────────────────────────────────────────────────

async def _push_coros_workout(token_data: dict, session: SessionData) -> dict:
    """Pousse une séance vers le COROS Training Hub API."""
    import httpx
    from datetime import date
    from app.services.coros_auth import BASE_URLS, _auth_headers

    access_token = token_data.get("access_token")
    user_id      = token_data.get("user_id")
    region       = token_data.get("region", "eu")

    if not access_token or not user_id:
        raise ValueError("Token COROS invalide — reconnecte ta montre.")

    base    = BASE_URLS.get(region, BASE_URLS["eu"])
    headers = _auth_headers(access_token, user_id)

    cat        = session.category
    total_secs = session.duration_min * 60
    distance_m = int(session.distance_km * 1000)
    today      = date.today().strftime("%Y%m%d")

    payload = {
        "userId":   int(user_id),   # int, pas string
        "name":     session.session_name,
        "mode":     100,
        "planDate": int(today),     # int YYYYMMDD
        "targetTime": session.duration_min,  # en minutes
        "targetDis":  distance_m,
    }

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{base}/training/plan/add",
            headers=headers,
            json=payload,
        )
        body = resp.json()
        log.info(f"COROS /training/plan/add payload={payload} → HTTP {resp.status_code} body={body}")
        if body.get("result") != "0000":
            raise ValueError(
                f"COROS API ({body.get('result', resp.status_code)}): "
                f"{body.get('message', 'Erreur inconnue')}"
            )
        return body.get("data") or {}


# ── Endpoint principal ────────────────────────────────────────────────────────

@router.post("/push", response_model=WorkoutPushResponse)
async def push_workout(
    req: WorkoutPushRequest,
    db: AsyncSession = Depends(get_db),
    caller_email: str = Depends(get_caller_email),
):
    user: User = await require_owner(req.username, db, caller_email)

    token_raw = user.token_json
    if not token_raw:
        raise HTTPException(400, "Aucun token watch trouvé — connectez votre montre d'abord.")

    try:
        token_data = json.loads(token_raw)
    except Exception:
        raise HTTPException(500, "Token watch corrompu.")

    provider = token_data.get("provider", "garmin")

    # ── Garmin ────────────────────────────────────────────────────────────────
    if provider == "garmin":
        from app.services.garmin_auth import _load_api
        watch_email = user.watch_email or user.email or ""
        api = _load_api(token_raw, watch_email)
        if api is None:
            raise HTTPException(503, "Impossible de charger la session Garmin — reconnectez votre montre.")
        try:
            workout = _build_garmin_workout(req.session)
            result  = api.upload_running_workout(workout)
            workout_id = result.get("workoutId") if isinstance(result, dict) else None
            log.info(f"Workout Garmin poussé pour {req.username}: {workout_id}")
            return WorkoutPushResponse(
                ok=True,
                provider="garmin",
                workout_id=workout_id,
                message="Séance ajoutée à Garmin Connect — elle se synchronisera sur ta montre à la prochaine connexion.",
            )
        except Exception as e:
            log.error(f"Erreur push Garmin pour {req.username}: {e}")
            raise HTTPException(502, f"Erreur Garmin Connect: {e}")

    # ── Polar ─────────────────────────────────────────────────────────────────
    elif provider == "polar":
        raise HTTPException(501, "Export Polar coming soon — l'API Polar ne supporte pas encore le push de workout sans accord partenaire.")

    # ── Coros ─────────────────────────────────────────────────────────────────
    elif provider == "coros":
        try:
            result = await _push_coros_workout(token_data, req.session)
            log.info(f"Workout Coros poussé pour {req.username}: {result}")
            return WorkoutPushResponse(
                ok=True,
                provider="coros",
                workout_id=result.get("planId") or result.get("id"),
                message="Séance ajoutée à COROS Training Hub — elle se synchronisera sur ta montre à la prochaine connexion.",
            )
        except Exception as e:
            log.error(f"Erreur push Coros pour {req.username}: {e}")
            raise HTTPException(502, f"Erreur COROS: {e}")

    else:
        raise HTTPException(400, f"Provider '{provider}' non supporté pour l'export de workout.")
