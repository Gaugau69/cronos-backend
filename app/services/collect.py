"""
app/services/collect.py — Orchestration de la collecte et upsert en DB.

Supporte Garmin, Polar et Withings — détecte automatiquement le provider depuis le token.
"""

import json
import logging
import os
from datetime import date, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Activity, DailyMetric, User
from app.services.garmin_auth import get_api
from app.services.garmin_parse import (
    parse_activities, parse_body_battery, parse_heart_rate,
    parse_hrv, parse_sleep, parse_stats, parse_steps, parse_stress,
)
from app.services.polar_auth import get_polar_api_headers
from app.services.polar_parse import collect_activities_polar, collect_day_polar
from app.services.withings_auth import get_withings_headers, get_withings_userid
from app.services.withings_parse import collect_activities_withings, collect_day_withings

log = logging.getLogger(__name__)

_GARMIN_PARSERS = [
    parse_sleep, parse_heart_rate, parse_hrv,
    parse_stress, parse_steps, parse_body_battery, parse_stats,
]


def _get_provider(user: User) -> str:
    if not user.token_json:
        return "unknown"
    try:
        token_data = json.loads(user.token_json)
        return token_data.get("provider", "garmin")
    except Exception:
        return "garmin"


def _safe_upsert_row(row: dict, exclude_keys: tuple) -> dict:
    set_dict = {k: v for k, v in row.items() if k not in exclude_keys}
    return set_dict if set_dict else {"user_id": row.get("user_id")}


async def collect_user_range(db: AsyncSession, user: User, start: date, end: date) -> dict:
    provider = _get_provider(user)
    log.info(f"[{user.name}] provider: {provider}")

    if provider == "polar":
        return await _collect_polar_range(db, user, start, end)
    elif provider == "withings":
        return await _collect_withings_range(db, user, start, end)
    else:
        return await _collect_garmin_range(db, user, start, end)


# ─────────────────────────────────────────────────────────────
# Notification email via Resend
# ─────────────────────────────────────────────────────────────

async def _notify_token_expired(name: str, email: str):
    """
    Envoie un email à l'utilisateur quand son token expire
    et que le re-login automatique a échoué.
    Utilise Resend API.
    """
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        log.warning(f"[{name}] RESEND_API_KEY non défini — email non envoyé")
        return

    from_email = os.environ.get("EMAIL_FROM", "peakflow@peakflow-technologies.com")

    body_html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 520px; margin: 0 auto; padding: 32px; background: #0a0e1a; color: #f5f5f0;">
      <h2 style="color: #10B981; font-size: 1.2rem; margin-bottom: 16px;">Peakflow — Reconnexion requise</h2>
      <p style="color: #94a3b8; line-height: 1.7; margin-bottom: 24px;">
        Bonjour <strong style="color: #f5f5f0;">{name}</strong>,<br><br>
        Ton accès à ta montre a expiré et la reconnexion automatique n'a pas fonctionné.<br>
        Tes données ne sont plus collectées depuis hier.
      </p>
      <p style="margin-bottom: 32px;">
        <a href="https://peakflow-technologies.com/cronos"
           style="background: #10B981; color: #060d0a; padding: 12px 24px; border-radius: 6px; text-decoration: none; font-weight: bold; display: inline-block;">
          Reconnecter ma montre →
        </a>
      </p>
      <p style="color: #64748b; font-size: 0.85rem; line-height: 1.6;">
        Télécharge l'app Peakflow, connecte-toi avec tes identifiants Garmin ou Polar, et tes données reprendront automatiquement cette nuit.<br><br>
        L'équipe Peakflow
      </p>
    </div>
    """

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "from":    from_email,
                    "to":      [email],
                    "subject": "Peakflow — Reconnexion requise",
                    "html":    body_html,
                },
                timeout=10,
            )
            if resp.status_code == 200:
                log.info(f"[{name}] ✓ Email de notification envoyé à {email}")
            else:
                log.error(f"[{name}] Erreur envoi email : {resp.status_code} — {resp.text}")
    except Exception as e:
        log.error(f"[{name}] Exception envoi email : {e}")


async def _notify_watch_not_worn(name: str, email: str):
    """Envoie un email si la montre n'a pas été portée depuis 2 jours."""
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        return
    from_email = os.environ.get("EMAIL_FROM", "peakflow@peakflow-technologies.com")
    body_html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 520px; margin: 0 auto; padding: 32px; background: #0a0e1a; color: #f5f5f0;">
      <h2 style="color: #10B981; font-size: 1.2rem; margin-bottom: 16px;">Peakflow — Données manquantes</h2>
      <p style="color: #94a3b8; line-height: 1.7; margin-bottom: 24px;">
        Bonjour <strong style="color: #f5f5f0;">{name}</strong>,<br><br>
        CRONOS n'a reçu aucune donnée de ta montre depuis 2 jours.<br>
        Pense à la porter la nuit pour que tes recommandations restent précises !
      </p>
      <p style="margin-bottom: 32px;">
        <a href="https://web-production-3668.up.railway.app/cronos"
           style="background: #10B981; color: #060d0a; padding: 12px 24px; border-radius: 6px; text-decoration: none; font-weight: bold; display: inline-block;">
          Voir mes recommandations →
        </a>
      </p>
      <p style="color: #64748b; font-size: 0.85rem;">L'équipe Peakflow</p>
    </div>
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"from": from_email, "to": [email], "subject": "Peakflow — Pense à porter ta montre 🏃", "html": body_html},
                timeout=10,
            )
            if resp.status_code == 200:
                log.info(f"[{name}] ✓ Email montre non portée envoyé")
            else:
                log.error(f"[{name}] Erreur email montre : {resp.status_code}")
    except Exception as e:
        log.error(f"[{name}] Exception email montre : {e}")


# ─────────────────────────────────────────────────────────────
# Garmin
# ─────────────────────────────────────────────────────────────

async def _collect_garmin_range(db: AsyncSession, user: User, start: date, end: date) -> dict:
    api = await get_api(db, user)
    if not api:
        return {"status": "error", "reason": "token invalide"}

    days_ok = 0
    acts_ok = 0
    current = start
    relogin_attempted = False

    while current <= end:
        log.info(f"[{user.name}] collecting {current}")

        try:
            row = {"user_id": user.id, "date": current}
            for parser in _GARMIN_PARSERS:
                row.update(parser(api, current))

            set_dict = _safe_upsert_row(row, ("user_id", "date"))
            await db.execute(
                pg_insert(DailyMetric)
                .values(**row)
                .on_conflict_do_update(
                    constraint="uq_user_date",
                    set_=set_dict,
                )
            )
            days_ok += 1

            for act in parse_activities(api, current):
                act_row = {"user_id": user.id, "date": current, **act}
                act_set = _safe_upsert_row(act_row, ("user_id", "activity_id"))
                await db.execute(
                    pg_insert(Activity)
                    .values(**act_row)
                    .on_conflict_do_update(
                        constraint="uq_user_activity",
                        set_=act_set,
                    )
                )
                acts_ok += 1

            await db.commit()

        except Exception as e:
            if "401" in str(e) and not relogin_attempted:
                log.warning(f"[{user.name}] 401 détecté — tentative de re-login automatique")
                from app.services.garmin_auth import _relogin
                new_api = await _relogin(db, user)
                if new_api:
                    api = new_api
                    relogin_attempted = True
                    log.info(f"[{user.name}] Re-login réussi — reprise de la collecte")
                    continue
                else:
                    log.error(f"[{user.name}] Re-login échoué — notification email envoyée")
                    await _notify_token_expired(user.name, user.email)
                    return {"status": "error", "reason": "401 + re-login échoué"}
            else:
                log.error(f"[{user.name}] Erreur collecte {current}: {e}")

        current += timedelta(days=1)

    return {"status": "ok", "days": days_ok, "activities": acts_ok}


# ─────────────────────────────────────────────────────────────
# Polar
# ─────────────────────────────────────────────────────────────

async def _collect_polar_range(db: AsyncSession, user: User, start: date, end: date) -> dict:
    headers = await get_polar_api_headers(user)
    if not headers:
        return {"status": "error", "reason": "token Polar invalide"}

    try:
        token_data = json.loads(user.token_json)
        polar_user_id = token_data.get("polar_user_id", "")
    except Exception:
        return {"status": "error", "reason": "token Polar mal formé"}

    if not polar_user_id:
        return {"status": "error", "reason": "polar_user_id manquant"}

    days_ok = 0
    acts_ok = 0
    current = start

    while current <= end:
        log.info(f"[{user.name}] collecting Polar {current}")

        metrics = await collect_day_polar(headers, polar_user_id, current)
        row = {"user_id": user.id, "date": current, **metrics}

        set_dict = _safe_upsert_row(row, ("user_id", "date"))
        await db.execute(
            pg_insert(DailyMetric)
            .values(**row)
            .on_conflict_do_update(
                constraint="uq_user_date",
                set_=set_dict,
            )
        )
        days_ok += 1

        activities = await collect_activities_polar(headers, polar_user_id, current)
        for act in activities:
            act_row = {"user_id": user.id, "date": current, **act}
            act_set = _safe_upsert_row(act_row, ("user_id", "activity_id"))
            await db.execute(
                pg_insert(Activity)
                .values(**act_row)
                .on_conflict_do_update(
                    constraint="uq_user_activity",
                    set_=act_set,
                )
            )
            acts_ok += 1

        await db.commit()
        current += timedelta(days=1)

    return {"status": "ok", "days": days_ok, "activities": acts_ok}


# ─────────────────────────────────────────────────────────────
# Withings
# ─────────────────────────────────────────────────────────────

async def _collect_withings_range(db: AsyncSession, user: User, start: date, end: date) -> dict:
    headers = await get_withings_headers(user)
    if not headers:
        return {"status": "error", "reason": "token Withings invalide"}

    days_ok = 0
    acts_ok = 0
    current = start

    while current <= end:
        log.info(f"[{user.name}] collecting Withings {current}")

        metrics = await collect_day_withings(headers, current)
        row = {"user_id": user.id, "date": current, **metrics}

        set_dict = _safe_upsert_row(row, ("user_id", "date"))
        await db.execute(
            pg_insert(DailyMetric)
            .values(**row)
            .on_conflict_do_update(
                constraint="uq_user_date",
                set_=set_dict,
            )
        )
        days_ok += 1

        activities = await collect_activities_withings(headers, current)
        for act in activities:
            act_row = {"user_id": user.id, "date": current, **act}
            act_set = _safe_upsert_row(act_row, ("user_id", "activity_id"))
            await db.execute(
                pg_insert(Activity)
                .values(**act_row)
                .on_conflict_do_update(
                    constraint="uq_user_activity",
                    set_=act_set,
                )
            )
            acts_ok += 1

        await db.commit()
        current += timedelta(days=1)

    return {"status": "ok", "days": days_ok, "activities": acts_ok}


# ─────────────────────────────────────────────────────────────
# Cron job
# ─────────────────────────────────────────────────────────────

async def collect_all_users_yesterday(db: AsyncSession):
    """Cron job — collecte J-1 pour tous les users enregistrés."""
    yesterday = date.today() - timedelta(days=1)
    two_days_ago = date.today() - timedelta(days=2)
    users = (await db.execute(select(User))).scalars().all()
    log.info(f"Cron: {yesterday} — {len(users)} user(s)")
    for user in users:
        try:
            summary = await collect_user_range(db, user, yesterday, yesterday)
            log.info(f"[{user.name}] {summary}")

            # Vérifie si la montre n'a pas été portée depuis 2 jours
            recent = (await db.execute(
                select(DailyMetric)
                .where(DailyMetric.user_id == user.id)
                .where(DailyMetric.date >= two_days_ago)
                .order_by(DailyMetric.date.desc())
            )).scalars().all()

            # Montre non portée = 2 jours consécutifs sans aucun signal
            no_data = all(
                not any([m.sleep_score, m.hrv_last_night, m.body_battery_charged, m.resting_hr])
                for m in recent
            ) if len(recent) >= 2 else False

            if no_data:
                log.warning(f"[{user.name}] Montre non portée depuis 2 jours — notification envoyée")
                await _notify_watch_not_worn(user.name, user.email)

        except Exception as e:
            log.error(f"[{user.name}] Erreur collecte: {e}")


async def backfill_missing_days(db: AsyncSession, lookback_days: int = 14):
    """
    Vérifie les `lookback_days` derniers jours pour chaque user et comble les jours manquants.
    Appelé après collect_all_users_yesterday pour rattraper les jours où le cron aurait échoué.
    """
    today = date.today()
    users = (await db.execute(select(User))).scalars().all()
    log.info(f"Backfill: vérification des {lookback_days} derniers jours pour {len(users)} user(s)")

    for user in users:
        try:
            # Récupère les dates déjà présentes en base pour ce user
            start_window = today - timedelta(days=lookback_days)
            existing = (await db.execute(
                select(DailyMetric.date)
                .where(DailyMetric.user_id == user.id)
                .where(DailyMetric.date >= start_window)
                .where(DailyMetric.date < today)
            )).scalars().all()

            existing_dates = set(existing)
            missing = [
                today - timedelta(days=i)
                for i in range(1, lookback_days + 1)
                if (today - timedelta(days=i)) not in existing_dates
            ]

            if not missing:
                continue

            log.info(f"[{user.name}] {len(missing)} jour(s) manquant(s) — backfill en cours")
            for day in missing:
                try:
                    summary = await collect_user_range(db, user, day, day)
                    log.info(f"[{user.name}] backfill {day}: {summary}")
                except Exception as e:
                    log.error(f"[{user.name}] backfill {day} échoué: {e}")

        except Exception as e:
            log.error(f"[{user.name}] Erreur backfill: {e}")