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

from app.config import settings
from app.db import AsyncSessionLocal, init_db
from app.routers import data, users
from app.services.collect import collect_all_users_yesterday

log = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()

STATIC_DIR = Path(__file__).parent / "static"


async def _daily_job():
    log.info("⏰ Cron : collecte quotidienne")
    async with AsyncSessionLocal() as db:
        await collect_all_users_yesterday(db)


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
    scheduler.start()
    log.info(f"✓ Cron démarré — collecte à {settings.collect_hour:02d}:{settings.collect_minute:02d} UTC")

    yield

    scheduler.shutdown(wait=False)


app = FastAPI(title="AION Backend", version="0.1.0")

app.include_router(users.router)
app.include_router(data.router)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def serve_landing():
    """Page de présentation AION."""
    return FileResponse(STATIC_DIR / "landing.html")


@app.get("/connect", include_in_schema=False)
async def serve_connect():
    """Formulaire de connexion Garmin."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health", tags=["health"])
async def health():
    return {"status": "ok"}
