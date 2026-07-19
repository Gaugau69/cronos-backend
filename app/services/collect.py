"""
app/services/collect.py — Orchestration de la collecte et upsert en DB.

Supporte Garmin, Polar, Withings, COROS, Oura, WHOOP, Fitbit — détecte automatiquement le provider.
"""

import asyncio
import json
import logging
import os
import traceback
from concurrent.futures import ThreadPoolExecutor
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
from app.services.withings_auth import get_withings_headers, get_withings_userid, refresh_withings_token
from app.services.withings_parse import collect_activities_withings, collect_day_withings
from app.services.coros_auth import get_coros_auth
from app.services.coros_parse import collect_activities_coros, collect_day_coros
from app.services.oura_auth import get_oura_headers, refresh_oura_token
from app.services.oura_parse import collect_activities_oura, collect_day_oura
from app.services.whoop_auth import get_whoop_headers, refresh_whoop_token
from app.services.whoop_parse import collect_activities_whoop, collect_day_whoop
from app.services.fitbit_auth import get_fitbit_headers, refresh_fitbit_token
from app.services.fitbit_parse import collect_activities_fitbit, collect_day_fitbit

log = logging.getLogger(__name__)

# Tracker de progression du backfill — { user_name: { days_scanned, days_target, running } }
_backfill_progress: dict = {}

def get_backfill_progress(user_name: str) -> dict:
    return _backfill_progress.get(user_name, {"running": False, "days_scanned": 0, "days_target": 90})

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
    elif provider == "coros":
        return await _collect_coros_range(db, user, start, end)
    elif provider == "oura":
        return await _collect_oura_range(db, user, start, end)
    elif provider == "whoop":
        return await _collect_whoop_range(db, user, start, end)
    elif provider == "fitbit":
        return await _collect_fitbit_range(db, user, start, end)
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
        Reconnecte ta montre depuis l'onglet CRONOS sur peakflow-technologies.com, et tes données reprendront automatiquement cette nuit.<br><br>
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
        <a href="https://peakflow-technologies.com/cronos"
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

_PARSER_TIMEOUT = 25  # secondes max par parser Garmin avant abandon
_executor = ThreadPoolExecutor(max_workers=4)


async def _fetch_weather(lat: float, lon: float, day: date) -> dict:
    """Météo journalière via Open-Meteo (gratuit, sans clé API, données historiques)."""
    url = (
        f"https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={lat:.4f}&longitude={lon:.4f}"
        f"&start_date={day.isoformat()}&end_date={day.isoformat()}"
        f"&daily=temperature_2m_max,precipitation_sum"
        f"&timezone=auto"
    )
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(url)
            if r.status_code == 200:
                d = r.json().get("daily", {})
                temps  = d.get("temperature_2m_max", [None])
                precip = d.get("precipitation_sum",  [None])
                return {
                    "temperature_c":    temps[0]  if temps  else None,
                    "precipitation_mm": precip[0] if precip else None,
                }
    except Exception:
        pass
    return {}


async def _run_parser(parser, api, day: date, timeout: float = _PARSER_TIMEOUT) -> dict:
    """Exécute un parser Garmin synchrone dans un thread avec timeout."""
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(_executor, parser, api, day),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        log.warning(f"Timeout parser {parser.__name__} pour {day} — données ignorées")
        return {}
    except Exception as e:
        if "401" in str(e):
            raise
        log.debug(f"Erreur parser {parser.__name__} pour {day}: {e}")
        return {}


async def _get_user_location(db: AsyncSession, user_id: int) -> tuple[float, float] | None:
    """Retourne (lat, lng) depuis le track GPS le plus récent de l'utilisateur."""
    from app.db import ActivityTrack
    track = (await db.execute(
        select(ActivityTrack)
        .where(ActivityTrack.user_id == user_id)
        .order_by(ActivityTrack.id.desc())
        .limit(1)
    )).scalar_one_or_none()
    if not track or not track.coordinates_json:
        return None
    try:
        coords = json.loads(track.coordinates_json)
        if coords and len(coords) > 0:
            return float(coords[0][0]), float(coords[0][1])
    except Exception:
        pass
    return None


async def _collect_garmin_range(db: AsyncSession, user: User, start: date, end: date) -> dict:
    api = await get_api(db, user)
    if not api:
        return {"status": "error", "reason": "token invalide"}

    # Récupère la localisation pour la météo (silencieux si indisponible)
    user_location = await _get_user_location(db, user.id)

    days_ok = 0
    acts_ok = 0
    current = end  # Part du plus récent → les données des 90 derniers jours arrivent en premier
    relogin_attempted = False
    days_target = max(1, (end - start).days + 1)
    days_scanned = 0
    _backfill_progress[user.name] = {"running": True, "days_scanned": 0, "days_target": days_target}

    while current >= start:
        log.info(f"[{user.name}] collecting {current}")

        try:
            row = {"user_id": user.id, "date": current}
            for parser in _GARMIN_PARSERS:
                row.update(await _run_parser(parser, api, current))

            # Météo Open-Meteo (silencieux si pas de localisation)
            if user_location:
                weather = await _fetch_weather(user_location[0], user_location[1], current)
                row.update(weather)

            set_dict = _safe_upsert_row(row, ("user_id", "date"))
            await db.execute(
                pg_insert(DailyMetric)
                .values(**row)
                .on_conflict_do_update(
                    constraint="uq_cronos_user_date",
                    set_=set_dict,
                )
            )
            days_ok += 1

            acts_raw = await _run_parser(parse_activities, api, current)
            acts_list = acts_raw if isinstance(acts_raw, list) else []
            for act in acts_list:
                act_row = {"user_id": user.id, "date": current, **act}
                act_set = _safe_upsert_row(act_row, ("user_id", "activity_id"))
                await db.execute(
                    pg_insert(Activity)
                    .values(**act_row)
                    .on_conflict_do_update(
                        constraint="uq_cronos_user_activity",
                        set_=act_set,
                    )
                )
                acts_ok += 1

            await db.commit()

        except Exception as e:
            # Rollback obligatoire — sans ça, la transaction reste abortée et
            # tous les jours suivants échouent avec InFailedSQLTransactionError
            try:
                await db.rollback()
            except Exception:
                pass
            # Après rollback SQLAlchemy marque l'objet user comme expiré ;
            # refresh() le recharge pour éviter MissingGreenlet sur user.name/email
            try:
                await db.refresh(user)
            except Exception:
                pass

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
                log.debug(f"[{user.name}] Jour {current} ignoré: {type(e).__name__}")

        days_scanned += 1
        _backfill_progress[user.name] = {"running": True, "days_scanned": days_scanned, "days_target": days_target}
        current -= timedelta(days=1)

    _backfill_progress[user.name] = {"running": False, "days_scanned": days_scanned, "days_target": days_target}
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
    current = end
    days_target = max(1, (end - start).days + 1)
    days_scanned = 0
    _backfill_progress[user.name] = {"running": True, "days_scanned": 0, "days_target": days_target}

    while current >= start:
        log.info(f"[{user.name}] collecting Polar {current}")

        try:
            metrics = await collect_day_polar(headers, polar_user_id, current)
            row = {"user_id": user.id, "date": current, **metrics}

            set_dict = _safe_upsert_row(row, ("user_id", "date"))
            await db.execute(
                pg_insert(DailyMetric)
                .values(**row)
                .on_conflict_do_update(
                    index_elements=["user_id", "date"],
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
                        constraint="uq_cronos_user_activity",
                        set_=act_set,
                    )
                )
                acts_ok += 1

            await db.commit()

        except Exception as e:
            try:
                await db.rollback()
            except Exception:
                pass
            try:
                await db.refresh(user)
            except Exception:
                pass
            if "401" in str(e):
                log.error(f"[{user.name}] Polar 401 — accès révoqué, notification envoyée")
                await _notify_token_expired(user.name, user.email)
                return {"status": "error", "reason": "401 Polar — accès révoqué"}
            log.debug(f"[{user.name}] Polar jour {current} ignoré: {e}")

        days_scanned += 1
        _backfill_progress[user.name] = {"running": True, "days_scanned": days_scanned, "days_target": days_target}
        current -= timedelta(days=1)

    _backfill_progress[user.name] = {"running": False, "days_scanned": days_scanned, "days_target": days_target}
    return {"status": "ok", "days": days_ok, "activities": acts_ok}


# ─────────────────────────────────────────────────────────────
# COROS
# ─────────────────────────────────────────────────────────────

async def _collect_coros_range(db: AsyncSession, user: User, start: date, end: date) -> dict:
    try:
        access_token, user_id, region = await get_coros_auth(user, db)
    except Exception as e:
        return {"status": "error", "reason": f"auth COROS: {e}"}

    days_ok = 0
    acts_ok = 0
    days_target = max(1, (end - start).days + 1)
    days_scanned = 0
    current = end

    _backfill_progress[user.name] = {"running": True, "days_scanned": 0, "days_target": days_target}

    while current >= start:
        log.info(f"[{user.name}] collecting COROS {current}")

        try:
            metrics = await collect_day_coros(access_token, user_id, current, region)
            row = {"user_id": user.id, "date": current, **metrics}
            set_dict = _safe_upsert_row(row, ("user_id", "date"))
            await db.execute(
                pg_insert(DailyMetric)
                .values(**row)
                .on_conflict_do_update(index_elements=["user_id", "date"], set_=set_dict)
            )
            days_ok += 1

            activities = await collect_activities_coros(access_token, user_id, current, region)
            for act in activities:
                act_row = {"user_id": user.id, "date": current, **act}
                act_set = _safe_upsert_row(act_row, ("user_id", "activity_id"))
                await db.execute(
                    pg_insert(Activity)
                    .values(**act_row)
                    .on_conflict_do_update(index_elements=["user_id", "activity_id"], set_=act_set)
                )
                acts_ok += 1

            await db.commit()

        except Exception as e:
            try:
                await db.rollback()
            except Exception:
                pass
            try:
                await db.refresh(user)
            except Exception:
                pass
            if "401" in str(e):
                log.error(f"[{user.name}] COROS 401 — token expiré, notification envoyée")
                await _notify_token_expired(user.name, user.email)
                return {"status": "error", "reason": "401 COROS — token expiré"}
            log.debug(f"[{user.name}] COROS jour {current} ignoré: {e}")

        days_scanned += 1
        _backfill_progress[user.name] = {"running": True, "days_scanned": days_scanned, "days_target": days_target}
        current -= timedelta(days=1)

    _backfill_progress[user.name] = {"running": False, "days_scanned": days_scanned, "days_target": days_target}
    return {"status": "ok", "days": days_ok, "activities": acts_ok}


# ─────────────────────────────────────────────────────────────
# Withings
# ─────────────────────────────────────────────────────────────

async def _collect_withings_range(db: AsyncSession, user: User, start: date, end: date) -> dict:
    # Withings access tokens expire every 3h — always refresh before collection
    refreshed = await refresh_withings_token(db, user)
    if refreshed is None:
        log.warning(f"[{user.name}] Refresh token Withings échoué — tentative avec le token existant")
    headers = await get_withings_headers(user, refreshed_token=refreshed)
    if not headers:
        log.error(f"[{user.name}] Token Withings invalide — notification envoyée")
        await _notify_token_expired(user.name, user.email)
        return {"status": "error", "reason": "401 Withings — token invalide"}

    days_ok = 0
    acts_ok = 0
    current = end
    days_target = max(1, (end - start).days + 1)
    days_scanned = 0
    _backfill_progress[user.name] = {"running": True, "days_scanned": 0, "days_target": days_target}

    while current >= start:
        log.info(f"[{user.name}] collecting Withings {current}")

        try:
            metrics = await collect_day_withings(headers, current)
            row = {"user_id": user.id, "date": current, **metrics}

            set_dict = _safe_upsert_row(row, ("user_id", "date"))
            await db.execute(
                pg_insert(DailyMetric)
                .values(**row)
                .on_conflict_do_update(
                    index_elements=["user_id", "date"],
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
                        constraint="uq_cronos_user_activity",
                        set_=act_set,
                    )
                )
                acts_ok += 1

            await db.commit()

        except Exception as e:
            try:
                await db.rollback()
            except Exception:
                pass
            try:
                await db.refresh(user)
            except Exception:
                pass
            if "401" in str(e):
                log.error(f"[{user.name}] Withings 401 — token révoqué, notification envoyée")
                await _notify_token_expired(user.name, user.email)
                return {"status": "error", "reason": "401 Withings — token révoqué"}
            log.debug(f"[{user.name}] Withings jour {current} ignoré: {e}")

        days_scanned += 1
        _backfill_progress[user.name] = {"running": True, "days_scanned": days_scanned, "days_target": days_target}
        current -= timedelta(days=1)

    _backfill_progress[user.name] = {"running": False, "days_scanned": days_scanned, "days_target": days_target}
    return {"status": "ok", "days": days_ok, "activities": acts_ok}


# ─────────────────────────────────────────────────────────────
# Oura
# ─────────────────────────────────────────────────────────────

async def _collect_oura_range(db: AsyncSession, user: User, start: date, end: date) -> dict:
    refreshed = await refresh_oura_token(db, user)
    headers = await get_oura_headers(user, refreshed_token=refreshed)
    if not headers:
        log.error(f"[{user.name}] Token Oura invalide — notification envoyée")
        await _notify_token_expired(user.name, user.email)
        return {"status": "error", "reason": "401 Oura — token invalide"}

    days_ok = 0
    acts_ok = 0
    current = end
    days_target = max(1, (end - start).days + 1)
    days_scanned = 0
    _backfill_progress[user.name] = {"running": True, "days_scanned": 0, "days_target": days_target}

    while current >= start:
        log.info(f"[{user.name}] collecting Oura {current}")
        try:
            metrics = await collect_day_oura(headers, current)
            row = {"user_id": user.id, "date": current, **metrics}
            set_dict = _safe_upsert_row(row, ("user_id", "date"))
            await db.execute(
                pg_insert(DailyMetric).values(**row)
                .on_conflict_do_update(index_elements=["user_id", "date"], set_=set_dict)
            )
            days_ok += 1

            activities = await collect_activities_oura(headers, current)
            for act in activities:
                act_row = {"user_id": user.id, "date": current, **act}
                act_set = _safe_upsert_row(act_row, ("user_id", "activity_id"))
                await db.execute(
                    pg_insert(Activity).values(**act_row)
                    .on_conflict_do_update(constraint="uq_cronos_user_activity", set_=act_set)
                )
                acts_ok += 1
            await db.commit()

        except Exception as e:
            try:
                await db.rollback()
            except Exception:
                pass
            try:
                await db.refresh(user)
            except Exception:
                pass
            if "401" in str(e):
                log.error(f"[{user.name}] Oura 401 — token révoqué, notification envoyée")
                await _notify_token_expired(user.name, user.email)
                return {"status": "error", "reason": "401 Oura — token révoqué"}
            log.debug(f"[{user.name}] Oura jour {current} ignoré: {e}")

        days_scanned += 1
        _backfill_progress[user.name] = {"running": True, "days_scanned": days_scanned, "days_target": days_target}
        current -= timedelta(days=1)

    _backfill_progress[user.name] = {"running": False, "days_scanned": days_scanned, "days_target": days_target}
    return {"status": "ok", "days": days_ok, "activities": acts_ok}


# ─────────────────────────────────────────────────────────────
# WHOOP
# ─────────────────────────────────────────────────────────────

async def _collect_whoop_range(db: AsyncSession, user: User, start: date, end: date) -> dict:
    refreshed = await refresh_whoop_token(db, user)
    headers = await get_whoop_headers(user, refreshed_token=refreshed)
    if not headers:
        log.error(f"[{user.name}] Token WHOOP invalide — notification envoyée")
        await _notify_token_expired(user.name, user.email)
        return {"status": "error", "reason": "401 WHOOP — token invalide"}

    days_ok = 0
    acts_ok = 0
    current = end
    days_target = max(1, (end - start).days + 1)
    days_scanned = 0
    _backfill_progress[user.name] = {"running": True, "days_scanned": 0, "days_target": days_target}

    while current >= start:
        log.info(f"[{user.name}] collecting WHOOP {current}")
        try:
            metrics = await collect_day_whoop(headers, current)
            row = {"user_id": user.id, "date": current, **metrics}
            set_dict = _safe_upsert_row(row, ("user_id", "date"))
            await db.execute(
                pg_insert(DailyMetric).values(**row)
                .on_conflict_do_update(index_elements=["user_id", "date"], set_=set_dict)
            )
            days_ok += 1

            activities = await collect_activities_whoop(headers, current)
            for act in activities:
                act_row = {"user_id": user.id, "date": current, **act}
                act_set = _safe_upsert_row(act_row, ("user_id", "activity_id"))
                await db.execute(
                    pg_insert(Activity).values(**act_row)
                    .on_conflict_do_update(constraint="uq_cronos_user_activity", set_=act_set)
                )
                acts_ok += 1
            await db.commit()

        except Exception as e:
            try:
                await db.rollback()
            except Exception:
                pass
            try:
                await db.refresh(user)
            except Exception:
                pass
            if "401" in str(e):
                log.error(f"[{user.name}] WHOOP 401 — token révoqué, notification envoyée")
                await _notify_token_expired(user.name, user.email)
                return {"status": "error", "reason": "401 WHOOP — token révoqué"}
            log.debug(f"[{user.name}] WHOOP jour {current} ignoré: {e}")

        days_scanned += 1
        _backfill_progress[user.name] = {"running": True, "days_scanned": days_scanned, "days_target": days_target}
        current -= timedelta(days=1)

    _backfill_progress[user.name] = {"running": False, "days_scanned": days_scanned, "days_target": days_target}
    return {"status": "ok", "days": days_ok, "activities": acts_ok}


# ─────────────────────────────────────────────────────────────
# Fitbit
# ─────────────────────────────────────────────────────────────

async def _collect_fitbit_range(db: AsyncSession, user: User, start: date, end: date) -> dict:
    refreshed = await refresh_fitbit_token(db, user)
    headers = await get_fitbit_headers(user, refreshed_token=refreshed)
    if not headers:
        log.error(f"[{user.name}] Token Fitbit invalide — notification envoyée")
        await _notify_token_expired(user.name, user.email)
        return {"status": "error", "reason": "401 Fitbit — token invalide"}

    days_ok = 0
    acts_ok = 0
    current = end
    days_target = max(1, (end - start).days + 1)
    days_scanned = 0
    _backfill_progress[user.name] = {"running": True, "days_scanned": 0, "days_target": days_target}

    while current >= start:
        log.info(f"[{user.name}] collecting Fitbit {current}")
        try:
            metrics = await collect_day_fitbit(headers, current)
            row = {"user_id": user.id, "date": current, **metrics}
            set_dict = _safe_upsert_row(row, ("user_id", "date"))
            await db.execute(
                pg_insert(DailyMetric).values(**row)
                .on_conflict_do_update(index_elements=["user_id", "date"], set_=set_dict)
            )
            days_ok += 1

            activities = await collect_activities_fitbit(headers, current)
            for act in activities:
                act_row = {"user_id": user.id, "date": current, **act}
                act_set = _safe_upsert_row(act_row, ("user_id", "activity_id"))
                await db.execute(
                    pg_insert(Activity).values(**act_row)
                    .on_conflict_do_update(constraint="uq_cronos_user_activity", set_=act_set)
                )
                acts_ok += 1
            await db.commit()

        except Exception as e:
            try:
                await db.rollback()
            except Exception:
                pass
            try:
                await db.refresh(user)
            except Exception:
                pass
            if "401" in str(e):
                log.error(f"[{user.name}] Fitbit 401 — token révoqué, notification envoyée")
                await _notify_token_expired(user.name, user.email)
                return {"status": "error", "reason": "401 Fitbit — token révoqué"}
            log.debug(f"[{user.name}] Fitbit jour {current} ignoré: {e}")

        days_scanned += 1
        _backfill_progress[user.name] = {"running": True, "days_scanned": days_scanned, "days_target": days_target}
        current -= timedelta(days=1)

    _backfill_progress[user.name] = {"running": False, "days_scanned": days_scanned, "days_target": days_target}
    return {"status": "ok", "days": days_ok, "activities": acts_ok}


# ─────────────────────────────────────────────────────────────
# Cron job
# ─────────────────────────────────────────────────────────────

async def collect_all_users_yesterday(db: AsyncSession):
    """Cron job — collecte J-1 pour tous les users ayant un token Garmin/Polar/Withings."""
    yesterday = date.today() - timedelta(days=1)
    two_days_ago = date.today() - timedelta(days=2)
    users = (await db.execute(
        select(User).where(User.token_json.isnot(None))
    )).scalars().all()
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
            log.error(f"[{user.name}] Erreur collecte: {e}\n{traceback.format_exc()}")


async def backfill_missing_days(db: AsyncSession, lookback_days: int = 14):
    """
    Vérifie les `lookback_days` derniers jours pour chaque user et comble les jours manquants.
    Appelé après collect_all_users_yesterday pour rattraper les jours où le cron aurait échoué.
    """
    today = date.today()
    users = (await db.execute(
        select(User).where(User.token_json.isnot(None))
    )).scalars().all()
    log.info(f"Backfill: vérification des {lookback_days} derniers jours pour {len(users)} user(s) avec token")

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
                    # Token expiré sans credentials → notification déjà envoyée, inutile de continuer
                    if isinstance(summary, dict) and "401" in summary.get("reason", ""):
                        log.warning(f"[{user.name}] Token invalide — backfill interrompu pour cet utilisateur")
                        break
                except Exception as e:
                    log.error(f"[{user.name}] backfill {day} échoué: {e}\n{traceback.format_exc()}")

        except Exception as e:
            log.error(f"[{user.name}] Erreur backfill: {e}\n{traceback.format_exc()}")