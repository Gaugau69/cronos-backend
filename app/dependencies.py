"""
app/dependencies.py — Dépendances FastAPI partagées.
"""

from fastapi import Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db import User
from app.config import settings


async def get_caller_email(x_cronos_email: str = Header(...)) -> str:
    """Extrait l'email appelant depuis le header X-Cronos-Email."""
    return x_cronos_email.strip().lower()


async def require_owner(name: str, db: AsyncSession, caller_email: str) -> User:
    """
    Vérifie que l'email du header correspond bien à l'utilisateur {name}.
    Lève 404 si l'utilisateur n'existe pas, 403 si l'email ne correspond pas.

    On compare caller_email à la fois à user.email ET user.name car le champ
    utilisé comme identifiant PeakFlow varie selon le provider :
    - Garmin : user.name = garmin_username, user.email = peakflow_email
    - COROS  : user.name = peakflow_email,  user.email = coros_email
    """
    user = (await db.execute(select(User).where(User.name == name))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, f"User '{name}' introuvable.")
    owner_ids = {(user.email or "").lower(), (user.name or "").lower()}
    if caller_email not in owner_ids:
        raise HTTPException(403, "Accès refusé.")
    return user


async def require_admin(x_admin_secret: str = Header(...)) -> None:
    """Vérifie que le header X-Admin-Secret correspond au secret configuré."""
    if not settings.admin_secret:
        raise HTTPException(503, "Endpoints d'administration non configurés.")
    if x_admin_secret != settings.admin_secret:
        raise HTTPException(403, "Accès refusé.")
