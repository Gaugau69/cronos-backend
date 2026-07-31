"""
app/routers/feedback.py — Retour utilisateur sur les séances CRONOS.

Endpoints :
    POST /session-feedback             → enregistre un feedback
    GET  /users/{name}/feedback        → historique des feedbacks
    GET  /users/{name}/feedback/stats  → stats agrégées par session
"""

import json
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import DailyMetric, RecommendationsCache, SessionFeedback, User, get_db
from app.dependencies import get_caller_email, require_owner

router = APIRouter(tags=["feedback"])

VALID_FEEDBACKS = {"facile", "ok", "difficile", "pas_faite"}


async def _get_user(db: AsyncSession, name: str) -> User:
    user = (await db.execute(select(User).where(User.name == name))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, f"User '{name}' introuvable.")
    return user


# ── Schémas ──────────────────────────────────────────────────────────────────

class FeedbackCreate(BaseModel):
    name:         str
    session_id:   int
    session_name: str
    feedback:     str     # 'facile' | 'ok' | 'difficile'
    done_at:      date | None = None

    @field_validator("feedback")
    @classmethod
    def validate_feedback(cls, v: str) -> str:
        if v not in VALID_FEEDBACKS:
            raise ValueError(f"feedback doit être parmi {VALID_FEEDBACKS}")
        return v


class FeedbackOut(BaseModel):
    id:           int
    session_id:   int
    session_name: str
    feedback:     str
    done_at:      date

    class Config:
        from_attributes = True


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/session-feedback", response_model=FeedbackOut)
async def submit_feedback(
    payload: FeedbackCreate,
    db: AsyncSession = Depends(get_db),
    caller_email: str = Depends(get_caller_email),
):
    user = await require_owner(payload.name, db, caller_email)

    entry = SessionFeedback(
        user_id      = user.id,
        session_id   = payload.session_id,
        session_name = payload.session_name,
        feedback     = payload.feedback,
        done_at      = payload.done_at or date.today(),
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return entry


@router.get("/users/{name}/feedback", response_model=list[FeedbackOut])
async def get_feedback(
    name: str,
    days: int = 30,
    db: AsyncSession = Depends(get_db),
    caller_email: str = Depends(get_caller_email),
):
    user = await require_owner(name, db, caller_email)
    since = date.today() - timedelta(days=days)
    rows = (await db.execute(
        select(SessionFeedback)
        .where(SessionFeedback.user_id == user.id)
        .where(SessionFeedback.done_at >= since)
        .order_by(SessionFeedback.done_at.desc())
    )).scalars().all()
    return rows


@router.get("/users/{name}/feedback/stats")
async def get_feedback_stats(
    name: str,
    db: AsyncSession = Depends(get_db),
    caller_email: str = Depends(get_caller_email),
):
    """Statistiques agrégées par session — pour ajuster l'algorithme."""
    user = await require_owner(name, db, caller_email)
    rows = (await db.execute(
        select(SessionFeedback)
        .where(SessionFeedback.user_id == user.id)
        .where(SessionFeedback.done_at >= date.today() - timedelta(days=90))
    )).scalars().all()

    stats: dict[int, dict] = {}
    for fb in rows:
        sid = fb.session_id
        if sid not in stats:
            stats[sid] = {"session_name": fb.session_name, "facile": 0, "ok": 0, "difficile": 0, "pas_faite": 0}
        stats[sid][fb.feedback] = stats[sid].get(fb.feedback, 0) + 1

    result = []
    for sid, s in stats.items():
        rated = s["facile"] + s["ok"] + s["difficile"]
        result.append({
            "session_id":   sid,
            "session_name": s["session_name"],
            "total":        rated + s["pas_faite"],
            "facile":       s["facile"],
            "ok":           s["ok"],
            "difficile":    s["difficile"],
            "pas_faite":    s["pas_faite"],
            "avg_difficulty": round(
                (s["facile"] * -1 + s["ok"] * 0 + s["difficile"] * 1) / rated, 2
            ) if rated else 0,
        })

    result.sort(key=lambda x: x["total"], reverse=True)
    return result


@router.get("/users/{name}/pending-feedback")
async def get_pending_feedback(
    name: str,
    db: AsyncSession = Depends(get_db),
    caller_email: str = Depends(get_caller_email),
):
    """Retourne la première séance d'hier sans feedback, ou {pending: false}."""
    user = await require_owner(name, db, caller_email)
    yesterday = date.today() - timedelta(days=1)

    cache = (await db.execute(
        select(RecommendationsCache)
        .where(RecommendationsCache.user_id == user.id)
        .where(RecommendationsCache.cache_date == yesterday)
    )).scalar_one_or_none()

    if not cache:
        return {"pending": False}

    recs = json.loads(cache.recommendations_json)
    if not recs:
        return {"pending": False}

    top = recs[0]

    existing = (await db.execute(
        select(SessionFeedback)
        .where(SessionFeedback.user_id == user.id)
        .where(SessionFeedback.session_id == top["id"])
        .where(SessionFeedback.done_at == yesterday)
    )).scalar_one_or_none()

    if existing:
        return {"pending": False}

    return {
        "pending":      True,
        "session_id":   top["id"],
        "session_name": top["name"],
        "category":     top.get("category", ""),
        "done_at":      yesterday.isoformat(),
    }


# ── Wellness subjectif ────────────────────────────────────────────────────────

WELLNESS_MAP = {"fatigued": 0.0, "normal": 0.5, "great": 1.0}


class WellnessCreate(BaseModel):
    name:  str
    score: str   # "fatigued" | "normal" | "great"
    day:   date | None = None

    @field_validator("score")
    @classmethod
    def validate_score(cls, v: str) -> str:
        if v not in WELLNESS_MAP:
            raise ValueError(f"score doit être parmi {list(WELLNESS_MAP.keys())}")
        return v


@router.post("/wellness", status_code=200)
async def submit_wellness(
    payload: WellnessCreate,
    db: AsyncSession = Depends(get_db),
    caller_email: str = Depends(get_caller_email),
):
    """Enregistre le bien-être subjectif du jour dans DailyMetric.wellness_score."""
    user = await require_owner(payload.name, db, caller_email)
    day = payload.day or date.today()
    score = WELLNESS_MAP[payload.score]

    existing = (await db.execute(
        select(DailyMetric)
        .where(DailyMetric.user_id == user.id)
        .where(DailyMetric.date == day)
    )).scalar_one_or_none()

    if existing:
        existing.wellness_score = score
    else:
        db.add(DailyMetric(user_id=user.id, date=day, wellness_score=score))

    await db.commit()
    return {"status": "ok", "day": day.isoformat(), "score": score}


@router.get("/users/{name}/wellness")
async def get_wellness(
    name: str,
    db: AsyncSession = Depends(get_db),
    caller_email: str = Depends(get_caller_email),
):
    """Retourne le wellness_score d'aujourd'hui."""
    user = await require_owner(name, db, caller_email)
    today = date.today()
    row = (await db.execute(
        select(DailyMetric.wellness_score)
        .where(DailyMetric.user_id == user.id)
        .where(DailyMetric.date == today)
    )).scalar_one_or_none()
    return {"day": today.isoformat(), "score": row}
