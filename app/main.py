"""
app/main.py — Application FastAPI, lifespan, scheduler cron.
"""

import logging
import traceback
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
from app.routers import data, users, polar, withings, profile, oura, whoop, fitbit
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
    try:
        async with AsyncSessionLocal() as db:
            try:
                from app.services.garmin_auth import check_and_refresh_tokens
                await check_and_refresh_tokens(db)
            except Exception as e:
                log.error(f"Erreur vérification tokens : {e}\n{traceback.format_exc()}")

            try:
                from app.services.collect import collect_all_users_yesterday, backfill_missing_days
                await collect_all_users_yesterday(db)
            except Exception as e:
                log.error(f"[CRON] collect_all_users_yesterday a échoué : {e}\n{traceback.format_exc()}")

            try:
                from app.services.collect import backfill_missing_days
                await backfill_missing_days(db, lookback_days=14)
            except Exception as e:
                log.error(f"[CRON] backfill_missing_days a échoué : {e}\n{traceback.format_exc()}")

    except BaseException as e:
        log.error(f"[CRON] Exception fatale dans _daily_job : {e}\n{traceback.format_exc()}")
        raise
    log.info("=== CRON END ===")



async def _notification_job():
    """
    Cron job d'emails — tourne toutes les heures, envoie selon la préférence de chaque user.
    """
    try:
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

            from app.routers.data import recommend_sessions

            for prefs, user in prefs_list:
                if not user.email:
                    continue
                try:
                    async with AsyncSessionLocal() as fresh_db:
                        payload = await recommend_sessions(
                            name=user.name, top_k=5, refresh=False,
                            db=fresh_db, caller_email=user.email,
                        )
                    # Ne pas envoyer si pas de données (score None = montre non portée)
                    if payload.get("recovery", {}).get("score") is None:
                        log.info(f"[NOTIF] Skip {user.email} — pas de données (email alerte déjà envoyé)")
                        continue
                    sent = send_daily_recommendation(user.email, user.name, payload)
                    log.info(f"[NOTIF] Email {'envoyé' if sent else 'échoué'} → {user.email}")
                except Exception as e:
                    log.error(f"[NOTIF] Erreur pour {user.name}: {e}")

    except Exception as e:
        import traceback
        log.error(f"[NOTIF] Exception dans _notification_job: {e}\n{traceback.format_exc()}")


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
    scheduler.start()
    log.info(f"✓ Cron démarré — collecte à {settings.collect_hour:02d}:{settings.collect_minute:02d} UTC | notifications toutes les heures")

    yield

    scheduler.shutdown(wait=False)


app = FastAPI(title="CRONOS Backend", version="0.1.0", lifespan=lifespan)

app.include_router(users.router)
app.include_router(data.router)
app.include_router(polar.router)
from app.routers import coros as coros_router
app.include_router(coros_router.router)
app.include_router(withings.router)
app.include_router(oura.router)
app.include_router(whoop.router)
app.include_router(fitbit.router)
app.include_router(profile.router)
app.include_router(session_history_router)
app.include_router(routes_router.router)
app.include_router(pacing_router.router)
app.include_router(feedback_router.router)
app.include_router(notifications_router.router)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://peakflow-technologies.com",
        "https://peakflow-technologies-dev.up.railway.app",
        "http://localhost:5173",
        "http://localhost:3000",
        "http://localhost:8080",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
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


@app.post("/admin/run-daily-job", tags=["admin"])
async def run_daily_job_now(x_admin_secret: str = Header(...)):
    """Déclenche immédiatement le job de collecte quotidienne (sans attendre 3h UTC)."""
    from app.dependencies import require_admin
    from fastapi import Header as _Header
    if not settings.admin_secret or x_admin_secret != settings.admin_secret:
        from fastapi import HTTPException as _HTTPException
        raise _HTTPException(403, "Accès refusé.")
    asyncio.create_task(_daily_job())
    return {"status": "started", "message": "Job de collecte lancé en arrière-plan — voir les logs Railway"}

@app.get("/cronos", include_in_schema=False)
async def serve_cronos():
    return FileResponse(STATIC_DIR / "cronos.html")