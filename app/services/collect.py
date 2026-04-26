"""
app/services/collect.py — Orchestration de la collecte et upsert en DB.
"""

import logging
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Activity, DailyMetric, User
from app.services.garmin_auth import get_api
from app.services.garmin_parse import (
    parse_activities, parse_body_battery, parse_heart_rate,
    parse_hrv, parse_sleep, parse_stats, parse_steps, parse_stress,
)

log = logging.getLogger(__name__)

_PARSERS = [parse_sleep, parse_heart_rate, parse_hrv,
            parse_stress, parse_steps, parse_body_battery, parse_stats]


async def collect_user_range(db: AsyncSession, user: User, start: date, end: date) -> dict:
    """Collecte toutes les métriques d'un user entre start et end, upsert en DB."""
    api = await get_api(db, user)
    if not api:
        return {"status": "error", "reason": "token invalide"}

    days_ok = 0
    acts_ok = 0
    current = start

    while current <= end:
        log.info(f"[{user.name}] collecting {current}")

        # ── Daily metrics ──
        row = {"user_id": user.id, "date": current}
        for parser in _PARSERS:
            row.update(parser(api, current))

        await db.execute(
            pg_insert(DailyMetric)
            .values(**row)
            .on_conflict_do_update(
                constraint="uq_user_date",
                set_={k: row[k] for k in row if k not in ("user_id", "date")},
            )
        )
        days_ok += 1

        # ── Activities ──
        for act in parse_activities(api, current):
            act_row = {"user_id": user.id, "date": current, **act}
            await db.execute(
                pg_insert(Activity)
                .values(**act_row)
                .on_conflict_do_update(
                    constraint="uq_user_activity",
                    set_={k: act_row[k] for k in act_row if k not in ("user_id", "activity_id")},
                )
            )
            acts_ok += 1

        await db.commit()
        current += timedelta(days=1)

    return {"status": "ok", "days": days_ok, "activities": acts_ok}


async def collect_all_users_yesterday(db: AsyncSession):
    """Cron job — collecte J-1 pour tous les users enregistrés."""
    yesterday = date.today() - timedelta(days=1)
    users = (await db.execute(select(User))).scalars().all()
    log.info(f"Cron: {yesterday} — {len(users)} user(s)")
    for user in users:
        summary = await collect_user_range(db, user, yesterday, yesterday)
        log.info(f"[{user.name}] {summary}")
