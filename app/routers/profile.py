"""
app/routers/profile.py — Profil athlète et courses planifiées.
"""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AthleteProfile, PlannedRace, User, get_db
from app.dependencies import get_caller_email, require_owner

router = APIRouter(prefix="/users/{name}", tags=["profile"])


async def _get_user(db: AsyncSession, name: str) -> User:
    user = (await db.execute(select(User).where(User.name == name))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, f"User '{name}' introuvable.")
    return user


# ─────────────────────────────────────────────────────────────
# Schémas
# ─────────────────────────────────────────────────────────────

class AthleteProfileIn(BaseModel):
    level:            Optional[str]   = None  # debutant/intermediaire/avance/elite
    sport_type:       Optional[str]   = None  # route/trail/ultra/triathlon/mixte
    years_running:    Optional[int]   = None
    weekly_km:        Optional[float] = None
    weekly_sessions:  Optional[int]   = None
    long_run_km:      Optional[float] = None
    vo2max_estimated: Optional[float] = None
    best_5k_min:      Optional[float] = None
    best_10k_min:     Optional[float] = None
    best_half_min:    Optional[float] = None
    best_marathon_min:Optional[float] = None
    primary_goal:     Optional[str]   = None  # finir/chrono/podium/progression
    target_distance:  Optional[str]   = None  # 5k/10k/semi/marathon/ultra
    max_weekly_km:    Optional[float] = None
    injury_history:   Optional[str]   = None
    preferred_days:   Optional[str]   = None  # ex: "lun,mer,sam"


class AthleteProfileOut(AthleteProfileIn):
    id:      int
    user_id: int

    class Config:
        from_attributes = True


class PlannedRaceIn(BaseModel):
    race_name:     str
    race_date:     date
    distance_km:   float
    race_type:     Optional[str]   = None   # route/trail/ultra/triathlon
    elevation_m:   Optional[int]   = None
    goal_type:     Optional[str]   = None   # finir/chrono/podium
    goal_time_min: Optional[float] = None
    priority:      Optional[str]   = "B"    # A/B/C


class PlannedRaceOut(PlannedRaceIn):
    id:               int
    user_id:          int
    is_completed:     bool
    actual_time_min:  Optional[float] = None

    class Config:
        from_attributes = True


# ─────────────────────────────────────────────────────────────
# Routes profil athlète
# ─────────────────────────────────────────────────────────────

@router.get("/profile", response_model=AthleteProfileOut)
async def get_profile(
    name: str,
    db: AsyncSession = Depends(get_db),
    caller_email: str = Depends(get_caller_email),
):
    """Retourne le profil athlète."""
    user = await require_owner(name, db, caller_email)
    profile = (
        await db.execute(select(AthleteProfile).where(AthleteProfile.user_id == user.id))
    ).scalar_one_or_none()
    if not profile:
        raise HTTPException(404, "Profil introuvable — créez-le d'abord.")
    return profile


@router.post("/profile", response_model=AthleteProfileOut)
async def upsert_profile(
    name: str,
    payload: AthleteProfileIn,
    db: AsyncSession = Depends(get_db),
    caller_email: str = Depends(get_caller_email),
):
    """Crée ou met à jour le profil athlète."""
    user = await require_owner(name, db, caller_email)
    data = {k: v for k, v in payload.model_dump().items() if v is not None}
    data["user_id"] = user.id

    stmt = (
        pg_insert(AthleteProfile)
        .values(**data)
        .on_conflict_do_update(
            index_elements=["user_id"],
            set_={k: v for k, v in data.items() if k != "user_id"},
        )
        .returning(AthleteProfile)
    )
    result = (await db.execute(stmt)).scalar_one()
    await db.commit()
    await db.refresh(result)
    return result


# ─────────────────────────────────────────────────────────────
# Routes courses planifiées
# ─────────────────────────────────────────────────────────────

@router.get("/races", response_model=list[PlannedRaceOut])
async def get_races(
    name: str,
    upcoming_only: bool = True,
    db: AsyncSession = Depends(get_db),
    caller_email: str = Depends(get_caller_email),
):
    """Retourne les courses planifiées."""
    user = await require_owner(name, db, caller_email)
    q = select(PlannedRace).where(PlannedRace.user_id == user.id)
    if upcoming_only:
        q = q.where(PlannedRace.race_date >= date.today())
    q = q.order_by(PlannedRace.race_date)
    return (await db.execute(q)).scalars().all()


@router.post("/races", response_model=PlannedRaceOut)
async def add_race(
    name: str,
    payload: PlannedRaceIn,
    db: AsyncSession = Depends(get_db),
    caller_email: str = Depends(get_caller_email),
):
    """Ajoute une course au calendrier."""
    user = await require_owner(name, db, caller_email)
    race = PlannedRace(user_id=user.id, **payload.model_dump())
    db.add(race)
    await db.commit()
    await db.refresh(race)
    return race


@router.put("/races/{race_id}", response_model=PlannedRaceOut)
async def update_race(
    name: str, race_id: int,
    payload: PlannedRaceIn,
    db: AsyncSession = Depends(get_db),
    caller_email: str = Depends(get_caller_email),
):
    """Met à jour une course."""
    user = await require_owner(name, db, caller_email)
    race = (
        await db.execute(
            select(PlannedRace)
            .where(PlannedRace.id == race_id)
            .where(PlannedRace.user_id == user.id)
        )
    ).scalar_one_or_none()
    if not race:
        raise HTTPException(404, "Course introuvable.")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(race, k, v)
    await db.commit()
    await db.refresh(race)
    return race


@router.delete("/races/{race_id}")
async def delete_race(
    name: str,
    race_id: int,
    db: AsyncSession = Depends(get_db),
    caller_email: str = Depends(get_caller_email),
):
    """Supprime une course."""
    user = await require_owner(name, db, caller_email)
    await db.execute(
        delete(PlannedRace)
        .where(PlannedRace.id == race_id)
        .where(PlannedRace.user_id == user.id)
    )
    await db.commit()
    return {"status": "deleted"}


@router.post("/races/{race_id}/complete")
async def complete_race(
    name: str, race_id: int,
    actual_time_min: Optional[float] = None,
    db: AsyncSession = Depends(get_db),
    caller_email: str = Depends(get_caller_email),
):
    """Marque une course comme terminée avec le temps réel."""
    user = await require_owner(name, db, caller_email)
    race = (
        await db.execute(
            select(PlannedRace)
            .where(PlannedRace.id == race_id)
            .where(PlannedRace.user_id == user.id)
        )
    ).scalar_one_or_none()
    if not race:
        raise HTTPException(404, "Course introuvable.")
    race.is_completed = True
    race.actual_time_min = actual_time_min
    await db.commit()
    return {"status": "completed", "race": race.race_name}