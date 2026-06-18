"""
app/routers/users.py — Endpoints de gestion des utilisateurs.
"""

import asyncio
import json
import logging
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal, DailyMetric, User, get_db
from app.schemas import UserCreate, UserOut
from app.services.garmin_auth import login_and_save_token, upsert_garmin_user, encrypt_password
from app.dependencies import require_admin

log = logging.getLogger(__name__)
router = APIRouter(prefix="/users", tags=["users"])

BACKFILL_YEARS = 2  # années d'historique récupérées automatiquement après connexion


async def _trigger_historical_backfill(user_id: int, user_name: str) -> None:
    """
    Déclenche automatiquement un backfill historique côté serveur (Railway)
    dès qu'un utilisateur connecte son compte Garmin.
    Vérifie d'abord qu'il n'a pas déjà de données — évite les doublons.
    """
    from app.services.collect import collect_user_range

    async with AsyncSessionLocal() as db:
        # Vérifie si des données existent déjà
        existing = (await db.execute(
            select(DailyMetric).where(DailyMetric.user_id == user_id).limit(1)
        )).scalar_one_or_none()

        if existing:
            log.info(f"[BACKFILL] {user_name} — données existantes, pas de backfill nécessaire")
            return

        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if not user or not user.token_json:
            return

        start = date.today() - timedelta(days=365 * BACKFILL_YEARS)
        end   = date.today() - timedelta(days=1)

        log.info(f"[BACKFILL] Démarrage {user_name} : {start} → {end}")
        try:
            result = await collect_user_range(db, user, start, end)
            log.info(f"[BACKFILL] Terminé {user_name} : {result}")
        except Exception as e:
            log.error(f"[BACKFILL] Erreur {user_name} : {e}")


class UserTokenRegister(BaseModel):
    name: str
    email: EmailStr
    token_json: str
    peakflow_email:  Optional[EmailStr] = None
    garmin_email:    Optional[str] = None
    garmin_password: Optional[str] = None


def _to_out(u: User) -> UserOut:
    return UserOut(
        id=u.id,
        name=u.name or "",
        email=u.email,
        display_name=u.firstname or u.name or u.email,
        created_at=u.created_at,
        has_token=bool(u.token_json),
    )


@router.post("/", response_model=UserOut, status_code=201)
async def register_user(payload: UserCreate, db: AsyncSession = Depends(get_db)):
    """
    Enregistre un user et récupère son token Garmin via email + password.
    Le mot de passe n'est JAMAIS stocké en clair.
    """
    existing = (await db.execute(select(User).where(User.name == payload.name))).scalar_one_or_none()
    if existing and existing.token_json:
        raise HTTPException(409, f"'{payload.name}' est déjà enregistré avec un token valide.")

    ok = await login_and_save_token(db, payload.name, payload.email, payload.password)
    if not ok:
        raise HTTPException(401, "Authentification Garmin échouée. Vérifier email/mot de passe.")

    user = (await db.execute(select(User).where(User.email == payload.email))).scalar_one()
    asyncio.create_task(_trigger_historical_backfill(user.id, user.name))
    return _to_out(user)


@router.post("/register-token", response_model=UserOut, status_code=201)
async def register_with_token(payload: UserTokenRegister, db: AsyncSession = Depends(get_db)):
    try:
        json.loads(payload.token_json)
    except Exception:
        raise HTTPException(400, "token_json invalide.")

    password_enc = None
    if payload.garmin_password:
        password_enc = encrypt_password(payload.garmin_password)

    # Utilise peakflow_email pour trouver le bon compte PeakFlow si fourni
    lookup_email = str(payload.peakflow_email) if payload.peakflow_email else str(payload.email)
    # L'email PeakFlow sert d'identifiant unique (garmin_username = email)
    garmin_username = lookup_email

    user = await upsert_garmin_user(
        db,
        garmin_username=garmin_username,
        email=lookup_email,
        token_json=payload.token_json,
        garmin_email=payload.garmin_email or str(payload.email),
        garmin_password_enc=password_enc,
    )
    asyncio.create_task(_trigger_historical_backfill(user.id, user.name))
    return _to_out(user)


@router.get("/", response_model=list[UserOut], dependencies=[Depends(require_admin)])
async def list_users(db: AsyncSession = Depends(get_db)):
    users = (await db.execute(select(User).order_by(User.created_at))).scalars().all()
    return [_to_out(u) for u in users]


@router.get("/by-email/{email}/status", dependencies=[Depends(require_admin)])
async def get_user_status_by_email(email: str, db: AsyncSession = Depends(get_db)):
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    return {
        "registered":   bool(user),
        "has_token":    bool(user and user.token_json),
        "name":         user.name if user else None,
        "id":           user.id   if user else None,
        "display_name": (user.firstname or user.name or email) if user else None,
    }


@router.get("/by-email/{email}", response_model=UserOut, dependencies=[Depends(require_admin)])
async def get_user_by_email(email: str, db: AsyncSession = Depends(get_db)):
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, f"Aucun utilisateur avec l'email '{email}'.")
    return _to_out(user)


@router.get("/{name}", response_model=UserOut)
async def get_user(name: str, db: AsyncSession = Depends(get_db)):
    user = (await db.execute(select(User).where(User.name == name))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, f"User '{name}' introuvable.")
    return _to_out(user)


@router.delete("/{name}", status_code=204, dependencies=[Depends(require_admin)])
async def delete_user(name: str, db: AsyncSession = Depends(get_db)):
    user = (await db.execute(select(User).where(User.name == name))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, f"User '{name}' introuvable.")
    await db.delete(user)
    await db.commit()
