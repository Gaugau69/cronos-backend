"""
app/routers/session_history.py — Historique des séances effectuées.
"""

from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionHistory, User, get_db
from app.dependencies import get_caller_email, require_owner

router = APIRouter(tags=["history"])


async def _get_user(db: AsyncSession, name: str) -> User:
    user = (await db.execute(select(User).where(User.name == name))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, f"User '{name}' introuvable.")
    return user


class SessionHistoryCreate(BaseModel):
    name: str
    session_id: int
    session_name: str
    category: Optional[str] = None
    duration_min: Optional[int] = None
    distance_km: Optional[float] = None
    done_at: Optional[date] = None


@router.post("/session-history", status_code=201)
async def add_session_history(
    payload: SessionHistoryCreate,
    db: AsyncSession = Depends(get_db),
    caller_email: str = Depends(get_caller_email),
):
    user = await require_owner(payload.name, db, caller_email)
    entry = SessionHistory(
        user_id=user.id,
        session_id=payload.session_id,
        session_name=payload.session_name,
        category=payload.category,
        duration_min=payload.duration_min,
        distance_km=payload.distance_km,
        done_at=payload.done_at or date.today(),
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return {"id": entry.id, "session_name": entry.session_name, "done_at": entry.done_at.isoformat()}


@router.get("/users/{name}/session-history")
async def get_session_history(
    name: str,
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    caller_email: str = Depends(get_caller_email),
):
    user = await require_owner(name, db, caller_email)
    since = date.today() - timedelta(days=days)
    entries = (await db.execute(
        select(SessionHistory)
        .where(SessionHistory.user_id == user.id)
        .where(SessionHistory.done_at >= since)
        .order_by(SessionHistory.done_at.desc())
    )).scalars().all()
    return [
        {
            "id":           e.id,
            "session_id":   e.session_id,
            "session_name": e.session_name,
            "category":     e.category,
            "duration_min": e.duration_min,
            "distance_km":  e.distance_km,
            "done_at":      e.done_at.isoformat(),
        }
        for e in entries
    ]


@router.delete("/session-history/{entry_id}", status_code=204)
async def delete_session_history(
    entry_id: int,
    db: AsyncSession = Depends(get_db),
    caller_email: str = Depends(get_caller_email),
):
    entry = (await db.execute(
        select(SessionHistory).where(SessionHistory.id == entry_id)
    )).scalar_one_or_none()
    if not entry:
        return
    owner = (await db.execute(select(User).where(User.id == entry.user_id))).scalar_one_or_none()
    if not owner or (owner.email or "").lower() != caller_email:
        raise HTTPException(403, "Accès refusé.")
    await db.execute(delete(SessionHistory).where(SessionHistory.id == entry_id))
    await db.commit()
