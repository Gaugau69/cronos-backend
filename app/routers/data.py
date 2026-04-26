"""
app/routers/data.py — Collecte manuelle et lecture des données stockées.
"""

from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Activity, DailyMetric, User, get_db
from app.schemas import ActivityOut, CollectRequest, DailyMetricOut
from app.services.collect import collect_user_range

router = APIRouter(tags=["data"])


async def _get_user(db: AsyncSession, name: str) -> User:
    user = (await db.execute(select(User).where(User.name == name))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, f"User '{name}' introuvable.")
    return user


@router.post("/collect")
async def collect(payload: CollectRequest, db: AsyncSession = Depends(get_db)):
    """Lance une collecte manuelle. Par défaut : hier seulement."""
    user = await _get_user(db, payload.name)
    if not user.token_json:
        raise HTTPException(400, "Pas de token Garmin pour cet utilisateur.")

    start = payload.start_date or (date.today() - timedelta(days=1))
    end   = payload.end_date   or start
    summary = await collect_user_range(db, user, start, end)
    return {"user": payload.name, "start": start, "end": end, **summary}


@router.get("/users/{name}/daily", response_model=list[DailyMetricOut])
async def get_daily(
    name: str,
    start: Optional[date] = Query(None),
    end:   Optional[date] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user(db, name)
    q = select(DailyMetric).where(DailyMetric.user_id == user.id)
    if start:
        q = q.where(DailyMetric.date >= start)
    if end:
        q = q.where(DailyMetric.date <= end)
    return (await db.execute(q.order_by(DailyMetric.date))).scalars().all()


@router.get("/users/{name}/activities", response_model=list[ActivityOut])
async def get_activities(
    name: str,
    start:         Optional[date] = Query(None),
    end:           Optional[date] = Query(None),
    activity_type: Optional[str]  = Query(None, description="running, cycling, swimming..."),
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user(db, name)
    q = select(Activity).where(Activity.user_id == user.id)
    if start:
        q = q.where(Activity.date >= start)
    if end:
        q = q.where(Activity.date <= end)
    if activity_type:
        q = q.where(Activity.activity_type == activity_type)
    return (await db.execute(q.order_by(Activity.date.desc()))).scalars().all()
