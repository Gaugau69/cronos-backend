"""
app/routers/coros.py — Connexion COROS Training Hub.

POST /users/connect-coros
    → Login COROS, sauvegarde token, déclenche backfill historique

GET /users/coros-status?name=<email>
    → Polling : connecté ?
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal, DailyMetric, User, get_db
from app.services.coros_auth import login_coros, save_coros_token

log = logging.getLogger(__name__)
router = APIRouter(tags=["coros"])


class ConnectCorosIn(BaseModel):
    name:           str    # email PeakFlow (identifiant cronos-backend)
    coros_email:    str
    coros_password: str
    region:         str = "eu"


@router.post("/users/connect-coros", status_code=201)
async def connect_coros(
    payload: ConnectCorosIn,
    db: AsyncSession = Depends(get_db),
):
    """
    Vérifie les credentials COROS, sauvegarde le token, déclenche le backfill.
    """
    # Vérifie que l'utilisateur PeakFlow existe
    user = (await db.execute(select(User).where(User.email == payload.name))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, f"Utilisateur '{payload.name}' introuvable. Connecte-toi d'abord à PeakFlow.")

    # Login COROS
    try:
        auth_data = await login_coros(payload.coros_email, payload.coros_password, payload.region)
    except Exception as e:
        msg = str(e)
        if "0001" in msg or "password" in msg.lower() or "account" in msg.lower():
            raise HTTPException(401, "Email ou mot de passe COROS incorrect.")
        raise HTTPException(502, f"Impossible de joindre COROS: {msg}")

    # Sauvegarde
    ok = await save_coros_token(db, payload.name, payload.coros_email, payload.coros_password, auth_data)
    if not ok:
        raise HTTPException(500, "Erreur lors de la sauvegarde des credentials COROS.")

    # Backfill historique en tâche de fond
    import asyncio
    asyncio.create_task(_trigger_backfill(user.id, user.name or payload.name))

    log.info(f"✓ COROS connecté pour {payload.name} ({payload.coros_email}, région: {payload.region})")
    return JSONResponse(status_code=201, content={"ok": True, "name": payload.name})


@router.get("/users/coros-status")
async def coros_status(name: str, db: AsyncSession = Depends(get_db)):
    """Polling endpoint — retourne {"connected": true} si COROS est connecté."""
    user = (await db.execute(select(User).where(User.email == name))).scalar_one_or_none()
    if not user or not user.token_json:
        return JSONResponse({"connected": False})
    try:
        import json
        data = json.loads(user.token_json)
        return JSONResponse({"connected": data.get("provider") == "coros"})
    except Exception:
        return JSONResponse({"connected": False})


async def _trigger_backfill(user_id: int, user_name: str) -> None:
    """Backfill 2 ans d'historique COROS en tâche de fond."""
    from datetime import date, timedelta
    from app.services.collect import collect_user_range

    async with AsyncSessionLocal() as db:
        existing = (await db.execute(
            select(DailyMetric).where(DailyMetric.user_id == user_id).limit(1)
        )).scalar_one_or_none()
        if existing:
            log.info(f"[BACKFILL COROS] {user_name} — données existantes, skip")
            return

        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if not user:
            return

        start = date.today() - timedelta(days=365 * 2)
        end   = date.today() - timedelta(days=1)
        log.info(f"[BACKFILL COROS] {user_name}: {start} → {end}")
        try:
            result = await collect_user_range(db, user, start, end)
            log.info(f"[BACKFILL COROS] {user_name} terminé: {result}")
        except Exception as e:
            log.error(f"[BACKFILL COROS] {user_name} erreur: {e}")
