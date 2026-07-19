"""
app/routers/notifications.py — Gestion des préférences de notifications email.

Endpoints :
    GET  /users/{name}/notifications          → préférences actuelles
    PUT  /users/{name}/notifications          → activer / désactiver / changer l'heure
    POST /users/{name}/notifications/test     → envoie un email de test
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import NotificationPrefs, User, get_db
from app.dependencies import get_caller_email, require_owner

router = APIRouter(tags=["notifications"])


async def _get_user(db: AsyncSession, name: str) -> User:
    user = (await db.execute(select(User).where(User.name == name))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, f"User '{name}' introuvable.")
    return user


class NotifPrefsIn(BaseModel):
    email_enabled: bool
    send_hour: int = 8   # 0-23 UTC


@router.get("/users/{name}/notifications")
async def get_notifications(
    name: str,
    db: AsyncSession = Depends(get_db),
    caller_email: str = Depends(get_caller_email),
):
    user = await require_owner(name, db, caller_email)
    prefs = (await db.execute(
        select(NotificationPrefs).where(NotificationPrefs.user_id == user.id)
    )).scalar_one_or_none()

    return {
        "email_enabled": prefs.email_enabled if prefs else False,
        "send_hour":     prefs.send_hour     if prefs else 8,
        "email":         user.email,
    }


@router.put("/users/{name}/notifications")
async def update_notifications(
    name: str,
    payload: NotifPrefsIn,
    db: AsyncSession = Depends(get_db),
    caller_email: str = Depends(get_caller_email),
):
    user = await require_owner(name, db, caller_email)
    prefs = (await db.execute(
        select(NotificationPrefs).where(NotificationPrefs.user_id == user.id)
    )).scalar_one_or_none()

    if prefs:
        prefs.email_enabled = payload.email_enabled
        prefs.send_hour     = max(0, min(23, payload.send_hour))
    else:
        db.add(NotificationPrefs(
            user_id       = user.id,
            email_enabled = payload.email_enabled,
            send_hour     = max(0, min(23, payload.send_hour)),
        ))

    await db.commit()
    return {"status": "ok", "email_enabled": payload.email_enabled}


@router.post("/users/{name}/notifications/test")
async def send_test_notification(
    name: str,
    db: AsyncSession = Depends(get_db),
    caller_email: str = Depends(get_caller_email),
):
    """Envoie un email de test avec les recommandations actuelles."""
    from app.services.email_service import send_daily_recommendation

    user = await require_owner(name, db, caller_email)
    if not user.email:
        raise HTTPException(400, "Pas d'email enregistré pour cet utilisateur.")

    # Génère les recommandations fraîches avec une session DB indépendante
    try:
        from app.db import AsyncSessionLocal
        from app.routers.data import recommend_sessions
        async with AsyncSessionLocal() as fresh_db:
            payload = await recommend_sessions(
                name=name, top_k=5, refresh=False, db=fresh_db, caller_email=user.email
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(503, f"Impossible de récupérer les recommandations: {e}")

    try:
        send_daily_recommendation(user.email, name, payload)
    except RuntimeError as e:
        raise HTTPException(500, f"Échec SMTP: {e}")

    return {"status": "sent", "to": user.email}
