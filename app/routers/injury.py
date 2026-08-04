"""
app/routers/injury.py — Déclaration et suivi des blessures utilisateur.

Endpoints :
    POST /injury                         → déclare une nouvelle blessure
    PATCH /injury/{injury_id}/status     → met à jour le statut (recovering / healed)
    GET  /users/{name}/injuries/active   → blessures actives ou en rémission
"""

from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import InjuryLog, User, get_db
from app.dependencies import get_caller_email, require_owner

router = APIRouter(tags=["injury"])

VALID_ZONES = {
    "genou_gauche", "genou_droite",
    "cheville_gauche", "cheville_droite",
    "jambe_gauche", "jambe_droite",
    "mollet_gauche", "mollet_droite",
    "cuisse_gauche", "cuisse_droite",
    "hanche_gauche", "hanche_droite",
    "pied_gauche", "pied_droite",
    "dos", "epaule_gauche", "epaule_droite",
    "autre",
}
VALID_SEVERITIES = {"gene", "legere", "arret"}
VALID_STATUSES   = {"active", "recovering", "healed"}


async def _get_user(db: AsyncSession, name: str) -> User:
    user = (await db.execute(select(User).where(User.name == name))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, f"User '{name}' introuvable.")
    return user


class InjuryCreate(BaseModel):
    name:          str
    body_zone:     str
    severity:      str
    estimated_days: int | None = None
    notes:         str | None  = None


class StatusUpdate(BaseModel):
    name:   str
    status: str


class InjuryOut(BaseModel):
    id:             int
    body_zone:      str
    severity:       str
    estimated_days: int | None
    status:         str
    notes:          str | None
    created_at:     datetime
    updated_at:     datetime

    class Config:
        from_attributes = True


@router.post("/injury", response_model=InjuryOut)
async def declare_injury(
    payload: InjuryCreate,
    db: AsyncSession = Depends(get_db),
    caller_email: str = Depends(get_caller_email),
):
    user = await require_owner(payload.name, db, caller_email)

    if payload.body_zone not in VALID_ZONES:
        raise HTTPException(400, f"body_zone invalide. Valeurs acceptées : {sorted(VALID_ZONES)}")
    if payload.severity not in VALID_SEVERITIES:
        raise HTTPException(400, f"severity doit être parmi {VALID_SEVERITIES}")

    entry = InjuryLog(
        user_id        = user.id,
        body_zone      = payload.body_zone,
        severity       = payload.severity,
        estimated_days = payload.estimated_days,
        notes          = payload.notes,
        status         = "active",
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return entry


@router.patch("/injury/{injury_id}/status", response_model=InjuryOut)
async def update_injury_status(
    injury_id: int,
    payload: StatusUpdate,
    db: AsyncSession = Depends(get_db),
    caller_email: str = Depends(get_caller_email),
):
    user = await require_owner(payload.name, db, caller_email)

    if payload.status not in VALID_STATUSES:
        raise HTTPException(400, f"status doit être parmi {VALID_STATUSES}")

    injury = (await db.execute(
        select(InjuryLog)
        .where(InjuryLog.id == injury_id)
        .where(InjuryLog.user_id == user.id)
    )).scalar_one_or_none()

    if not injury:
        raise HTTPException(404, "Blessure introuvable.")

    injury.status     = payload.status
    injury.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(injury)
    return injury


@router.get("/users/{name}/injuries/active", response_model=list[InjuryOut])
async def get_active_injuries(
    name: str,
    db: AsyncSession = Depends(get_db),
    caller_email: str = Depends(get_caller_email),
):
    """Retourne les blessures actives ou en rémission (exclut les guéries)."""
    user = await require_owner(name, db, caller_email)

    rows = (await db.execute(
        select(InjuryLog)
        .where(InjuryLog.user_id == user.id)
        .where(InjuryLog.status.in_(["active", "recovering"]))
        .order_by(InjuryLog.created_at.desc())
    )).scalars().all()

    return rows
