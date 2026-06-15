"""
app/main.py — Application FastAPI, lifespan, scheduler cron.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db import AsyncSessionLocal, init_db
from app.routers import data, users, polar, withings, profile
from app.routers import feedback as feedback_router
from app.routers import notifications as notifications_router
from app.services.collect import collect_all_users_yesterday
from app.logging_config import setup_logging
setup_logging()

from app.routers.session_history_router import router as session_history_router
from app.api import routes as routes_router
from app.api import pacing as pacing_router

log = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()

STATIC_DIR = Path(__file__).parent / "static"


async def _daily_job():
    """
    Cron job quotidien — 03:00 UTC
    1. Vérifie et rafraîchit les tokens expirés (re-login préventif)
    2. Collecte les métriques J-1 pour tous les utilisateurs
    """
    log.info("=== CRON START ===")
    async with AsyncSessionLocal() as db:
        try:
            from app.services.garmin_auth import check_and_refresh_tokens
            await check_and_refresh_tokens(db)
        except Exception as e:
            log.error(f"Erreur vérification tokens : {e}")

        from app.services.collect import collect_all_users_yesterday, backfill_missing_days
        await collect_all_users_yesterday(db)
        await backfill_missing_days(db, lookback_days=14)
    log.info("=== CRON END ===")


async def _weekly_retrain_job():
    """
    Cron job hebdomadaire — lundi 04:00 UTC.
    Appelle le service cronos-ml pour déclencher le réentraînement des modèles.
    """
    import os
    import httpx

    ml_url = os.environ.get("CRONOS_ML_URL", "").rstrip("/")
    ml_secret = os.environ.get("CRONOS_ML_SECRET", "")
    if not ml_url:
        log.warning("[RETRAIN] CRONOS_ML_URL non défini — réentraînement ignoré")
        return

    log.info("[RETRAIN] Déclenchement du réentraînement hebdomadaire")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{ml_url}/retrain",
                headers={"X-Secret": ml_secret},
            )
        if r.status_code == 200:
            log.info("[RETRAIN] Réentraînement lancé avec succès")
        else:
            log.error(f"[RETRAIN] Erreur {r.status_code}: {r.text}")
    except Exception as e:
        log.error(f"[RETRAIN] Impossible de contacter cronos-ml: {e}")


async def _notification_job():
    """
    Cron job d'emails — tourne toutes les heures, envoie selon la préférence de chaque user.
    """
    from datetime import datetime, timezone
    from sqlalchemy import select
    from app.db import User, NotificationPrefs
    from app.services.email_service import send_daily_recommendation

    current_hour = datetime.now(timezone.utc).hour
    log.info(f"[NOTIF] Vérification des notifications pour l'heure UTC {current_hour}")

    async with AsyncSessionLocal() as db:
        prefs_list = (await db.execute(
            select(NotificationPrefs, User)
            .join(User, NotificationPrefs.user_id == User.id)
            .where(NotificationPrefs.email_enabled == True)
            .where(NotificationPrefs.send_hour == current_hour)
        )).all()

        import os, httpx
        railway_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
        base_url = f"https://{railway_domain}" if railway_domain else "http://localhost:8001"

        for prefs, user in prefs_list:
            if not user.email:
                continue
            try:
                async with httpx.AsyncClient() as client:
                    r = await client.get(
                        f"{base_url}/users/{user.name}/recommend",
                        headers={"x-cronos-email": user.email or user.name},
                        timeout=20,
                    )
                payload = r.json()
                sent = send_daily_recommendation(user.email, user.name, payload)
                log.info(f"[NOTIF] Email {'envoyé' if sent else 'échoué'} → {user.email}")
            except Exception as e:
                log.error(f"[NOTIF] Erreur pour {user.name}: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    await init_db()
    log.info("✓ DB prête")

    scheduler.add_job(
        _daily_job,
        CronTrigger(hour=settings.collect_hour, minute=settings.collect_minute, timezone="UTC"),
        id="daily_collect",
        replace_existing=True,
    )
    scheduler.add_job(
        _notification_job,
        CronTrigger(minute=0, timezone="UTC"),
        id="hourly_notifications",
        replace_existing=True,
    )
    scheduler.add_job(
        _weekly_retrain_job,
        CronTrigger(day_of_week="mon", hour=4, minute=0, timezone="UTC"),
        id="weekly_retrain",
        replace_existing=True,
    )
    scheduler.start()
    log.info(f"✓ Cron démarré — collecte à {settings.collect_hour:02d}:{settings.collect_minute:02d} UTC | notifications toutes les heures | réentraînement ML lundi 04:00 UTC")

    yield

    scheduler.shutdown(wait=False)


app = FastAPI(title="CRONOS Backend", version="0.1.0", lifespan=lifespan)

app.include_router(users.router)
app.include_router(data.router)
app.include_router(polar.router)
app.include_router(withings.router)
app.include_router(profile.router)
app.include_router(session_history_router)
app.include_router(routes_router.router)
app.include_router(pacing_router.router)
app.include_router(feedback_router.router)
app.include_router(notifications_router.router)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/", include_in_schema=False)
async def serve_landing():
    return FileResponse(STATIC_DIR / "landing.html")


@app.get("/connect", include_in_schema=False)
async def serve_connect():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health", tags=["health"])
async def health():
    return {"status": "ok"}

@app.get("/cronos", include_in_schema=False)
async def serve_cronos():
    return FileResponse(STATIC_DIR / "cronos.html")