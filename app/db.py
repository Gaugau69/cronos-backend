"""
app/db.py — Définition des tables ORM et connexion PostgreSQL.
"""

from sqlalchemy import (
    BigInteger, Column, Date, DateTime, Float, ForeignKey,
    Integer, String, Text, UniqueConstraint, func,
)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker

from app.config import settings

# ── Connexion ──────────────────────────────────────────────────────────────

engine = create_async_engine(settings.database_url, echo=False, pool_size=5)

AsyncSessionLocal = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ── Tables ─────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    name       = Column(String(100), unique=True, nullable=False, index=True)
    email      = Column(String(255), unique=True, nullable=False)
    token_json = Column(Text, nullable=True)   # Token OAuth Garmin — jamais le mdp
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    daily_metrics = relationship("DailyMetric", back_populates="user", cascade="all, delete-orphan")
    activities    = relationship("Activity",    back_populates="user", cascade="all, delete-orphan")


class DailyMetric(Base):
    __tablename__ = "daily_metrics"
    __table_args__ = (UniqueConstraint("user_id", "date", name="uq_user_date"),)

    id      = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    date    = Column(Date, nullable=False, index=True)

    # Sommeil
    sleep_start          = Column(BigInteger, nullable=True)
    sleep_end            = Column(BigInteger, nullable=True)
    sleep_duration_min   = Column(Integer,   nullable=True)
    deep_sleep_min       = Column(Integer,   nullable=True)
    light_sleep_min      = Column(Integer,   nullable=True)
    rem_sleep_min        = Column(Integer,   nullable=True)
    awake_min            = Column(Integer,   nullable=True)
    sleep_score          = Column(Integer,   nullable=True)
    avg_spo2             = Column(Float,     nullable=True)
    avg_respiration_rate = Column(Float,     nullable=True)

    # Fréquence cardiaque
    resting_hr             = Column(Integer, nullable=True)
    max_hr                 = Column(Integer, nullable=True)
    min_hr                 = Column(Integer, nullable=True)
    last_7d_avg_resting_hr = Column(Float,   nullable=True)

    # HRV
    hrv_weekly_avg = Column(Float,       nullable=True)
    hrv_last_night = Column(Float,       nullable=True)
    hrv_5min_high  = Column(Float,       nullable=True)
    hrv_status     = Column(String(50),  nullable=True)
    hrv_feedback   = Column(String(255), nullable=True)

    # Stress
    avg_stress    = Column(Integer, nullable=True)
    max_stress    = Column(Integer, nullable=True)
    rest_stress   = Column(Integer, nullable=True)
    low_stress    = Column(Integer, nullable=True)
    medium_stress = Column(Integer, nullable=True)
    high_stress   = Column(Integer, nullable=True)

    # Activité générale
    total_steps          = Column(Integer, nullable=True)
    body_battery_charged = Column(Integer, nullable=True)
    body_battery_drained = Column(Integer, nullable=True)
    calories_total       = Column(Integer, nullable=True)
    calories_active      = Column(Integer, nullable=True)
    distance_m           = Column(Float,   nullable=True)
    active_min           = Column(Integer, nullable=True)
    floors_climbed       = Column(Integer, nullable=True)

    user = relationship("User", back_populates="daily_metrics")


class Activity(Base):
    __tablename__ = "activities"
    __table_args__ = (UniqueConstraint("user_id", "activity_id", name="uq_user_activity"),)

    id      = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    date    = Column(Date, nullable=False, index=True)

    activity_id      = Column(BigInteger,  nullable=False)
    activity_name    = Column(String(255), nullable=True)
    activity_type    = Column(String(100), nullable=True)
    start_time       = Column(String(50),  nullable=True)
    duration_min     = Column(Float,       nullable=True)
    distance_km      = Column(Float,       nullable=True)
    avg_hr           = Column(Integer,     nullable=True)
    max_hr           = Column(Integer,     nullable=True)
    calories         = Column(Integer,     nullable=True)
    avg_speed_kmh    = Column(Float,       nullable=True)
    elevation_gain_m = Column(Float,       nullable=True)
    training_effect  = Column(Float,       nullable=True)
    vo2max           = Column(Float,       nullable=True)

    user = relationship("User", back_populates="activities")
