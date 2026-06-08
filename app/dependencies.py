"""
app/dependencies.py — Dépendances FastAPI partagées.
"""

from fastapi import Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db import User


async def get_caller_email(x_cronos_email: str = Header(...)) -> str:
    """Extrait l'email appelant depuis le header X-Cronos-Email."""
    return x_cronos_email.strip().lower()


async def require_owner(name: str, db: AsyncSession, caller_email: str) -> User:
    """
    Vérifie que l'email du header correspond bien à l'utilisateur {name}.
    Lève 404 si l'utilisateur n'existe pas, 403 si l'email ne correspond pas.
    """
    user = (await db.execute(select(User).where(User.name == name))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, f"User '{name}' introuvable.")
    if (user.email or "").lower() != caller_email:
        raise HTTPException(403, "Accès refusé.")
    return user
