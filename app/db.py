"""
app/db.py — Définition des tables ORM et connexion PostgreSQL.
"""

from sqlalchemy import (
    BigInteger, Boolean, Column, Date, DateTime, Float, ForeignKey,
    Integer, SmallInteger, String, Text, UniqueConstraint, func,
)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker

from app.config import settings
from datetime import date

# ── Connexion ──────────────────────────────────────────────────────────────

def _async_db_url(url: str) -> str:
    return url.replace("postgresql://", "postgresql+asyncpg://", 1).replace(
        "postgres://", "postgresql+asyncpg://", 1
    )

engine = create_async_engine(
    _async_db_url(settings.database_url), echo=False, pool_size=5, pool_pre_ping=True
)

AsyncSessionLocal = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Migrations manuelles — ajoute les colonnes si elles n'existent pas
        for col, coltype in [
            ("temperature_c",    "FLOAT"),
            ("precipitation_mm", "FLOAT"),
            ("wellness_score",   "FLOAT"),
        ]:
            await conn.execute(
                __import__("sqlalchemy", fromlist=["text"]).text(
                    f"ALTER TABLE cronos_daily_metrics ADD COLUMN IF NOT EXISTS {col} {coltype}"
                )
            )
        # Activer les notifs pour les utilisateurs existants qui avaient opt-out par défaut
        await conn.execute(
            __import__("sqlalchemy", fromlist=["text"]).text(
                "UPDATE cronos_notification_prefs SET email_enabled = TRUE WHERE email_enabled = FALSE"
            )
        )


# ── Tables ─────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id         = Column(Integer,              primary_key=True, autoincrement=True)
    email      = Column(String(255),          unique=True, nullable=False)
    created_at = Column(DateTime,             nullable=True)

    firstname           = Column(String(100),           nullable=True)

    # Watch fields (multi-provider: garmin/polar/withings/coros)
    name               = Column("watch_username",     String(100), unique=True, nullable=True, index=True)
    token_json         = Column("watch_token_json",   Text,        nullable=True)
    watch_email        = Column("watch_email",        String(255), nullable=True)
    watch_password_enc = Column("watch_password_enc", String(500), nullable=True)

    daily_metrics        = relationship("DailyMetric",         back_populates="user", cascade="all, delete-orphan")
    activities           = relationship("Activity",            back_populates="user", cascade="all, delete-orphan")
    athlete_profile      = relationship("AthleteProfile",      back_populates="user", cascade="all, delete-orphan", uselist=False)
    planned_races        = relationship("PlannedRace",         back_populates="user", cascade="all, delete-orphan")
    session_feedback     = relationship("SessionFeedback",     back_populates="user", cascade="all, delete-orphan")
    recommendations_cache= relationship("RecommendationsCache",back_populates="user", cascade="all, delete-orphan")
    activity_tracks      = relationship("ActivityTrack",       back_populates="user", cascade="all, delete-orphan")
    notification_prefs   = relationship("NotificationPrefs",   back_populates="user", cascade="all, delete-orphan", uselist=False)
    injury_logs          = relationship("InjuryLog",           back_populates="user", cascade="all, delete-orphan")


class DailyMetric(Base):
    __tablename__ = "cronos_daily_metrics"
    __table_args__ = (UniqueConstraint("user_id", "date", name="uq_cronos_user_date"),)

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

    # Météo du jour (Open-Meteo, rempli automatiquement à la collecte)
    temperature_c    = Column(Float,   nullable=True)   # température max °C
    precipitation_mm = Column(Float,   nullable=True)   # précipitations mm

    # Bien-être subjectif (0=fatigué, 0.5=normal, 1=en forme — rempli par l'athlète)
    wellness_score   = Column(Float,   nullable=True)

    user = relationship("User", back_populates="daily_metrics")


class Activity(Base):
    __tablename__ = "cronos_activities"
    __table_args__ = (UniqueConstraint("user_id", "activity_id", name="uq_cronos_user_activity"),)

    id      = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    date    = Column(Date, nullable=False, index=True)

    activity_id      = Column(BigInteger,   nullable=False)
    activity_name    = Column(String(255),  nullable=True)
    activity_type    = Column(String(100),  nullable=True)
    start_time       = Column(String(50),   nullable=True)
    duration_min     = Column(Float,        nullable=True)
    distance_km      = Column(Float,        nullable=True)
    avg_hr           = Column(Integer,      nullable=True)
    max_hr           = Column(Integer,      nullable=True)
    calories         = Column(Integer,      nullable=True)
    avg_speed_kmh    = Column(Float,        nullable=True)
    elevation_gain_m = Column(Float,        nullable=True)
    training_effect  = Column(Float,        nullable=True)
    vo2max           = Column(Float,        nullable=True)
    rpe              = Column(SmallInteger, nullable=True)

    user = relationship("User", back_populates="activities")


# ─────────────────────────────────────────────────────────────
# A. Profil athlète
# ─────────────────────────────────────────────────────────────

class AthleteProfile(Base):
    """
    Profil sportif de l'athlète — une ligne par utilisateur.
    Utilisé par CRONOS pour personnaliser les recommandations de séances.
    """
    __tablename__ = "cronos_athlete_profiles"

    id      = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)

    # Niveau et type
    level           = Column(String(50),  nullable=True)   # debutant / intermediaire / avance / elite
    sport_type      = Column(String(50),  nullable=True)   # route / trail / ultra / triathlon / mixte
    years_running   = Column(Integer,     nullable=True)   # années de pratique

    # Volume habituel
    weekly_km       = Column(Float,       nullable=True)   # km/semaine habituels
    weekly_sessions = Column(Integer,     nullable=True)   # séances/semaine habituelles
    long_run_km     = Column(Float,       nullable=True)   # km de la sortie longue habituelle

    # Performances de référence
    vo2max_estimated = Column(Float,      nullable=True)   # VO2max estimé (depuis Garmin)
    best_5k_min      = Column(Float,      nullable=True)   # meilleur 5km en minutes
    best_10k_min     = Column(Float,      nullable=True)   # meilleur 10km en minutes
    best_half_min    = Column(Float,      nullable=True)   # meilleur semi en minutes
    best_marathon_min= Column(Float,      nullable=True)   # meilleur marathon en minutes

    # Objectifs
    primary_goal    = Column(String(100), nullable=True)   # finir / chrono / podium / progression
    target_distance = Column(String(50),  nullable=True)   # 5k / 10k / semi / marathon / ultra

    # Contraintes
    max_weekly_km    = Column(Float,      nullable=True)   # volume max supportable
    injury_history   = Column(Text,       nullable=True)   # historique blessures (texte libre)
    preferred_days   = Column(String(50), nullable=True)   # jours préférés ex: "lun,mer,sam"

    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())

    user = relationship("User", back_populates="athlete_profile")


# ─────────────────────────────────────────────────────────────
# B. Courses planifiées
# ─────────────────────────────────────────────────────────────

class PlannedRace(Base):
    """
    Calendrier des courses à venir — plusieurs par utilisateur.
    Utilisé par CRONOS pour adapter la périodisation.
    """
    __tablename__ = "cronos_planned_races"

    id      = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    # Infos course
    race_name     = Column(String(255), nullable=False)         # ex: "Marathon de Paris"
    race_date     = Column(Date,        nullable=False, index=True)
    distance_km   = Column(Float,       nullable=False)         # ex: 42.195
    race_type     = Column(String(50),  nullable=True)          # route / trail / ultra / triathlon
    elevation_m   = Column(Integer,     nullable=True)          # dénivelé+ pour trail

    # Objectif
    goal_type     = Column(String(50),  nullable=True)          # finir / chrono / podium
    goal_time_min = Column(Float,       nullable=True)          # objectif chrono en minutes
    priority      = Column(String(20),  nullable=True)          # A / B / C (importance de la course)

    # Statut
    is_completed  = Column(Boolean,     default=False)
    actual_time_min = Column(Float,     nullable=True)          # temps réel après la course

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="planned_races")


class SessionHistory(Base):
    __tablename__ = "cronos_session_history"

    id           = Column(Integer, primary_key=True)
    user_id      = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    session_id   = Column(Integer, nullable=False)
    session_name = Column(String(200), nullable=False)
    category     = Column(String(50), nullable=True)
    duration_min = Column(Integer, nullable=True)
    distance_km  = Column(Float, nullable=True)
    done_at      = Column(Date, nullable=False, default=date.today)
    created_at   = Column(DateTime(timezone=True), server_default=func.now())


# ─────────────────────────────────────────────
# C. Feedback sur les séances
# ─────────────────────────────────────────────

class SessionFeedback(Base):
    """Retour utilisateur après une séance — 'facile', 'ok' ou 'difficile'."""
    __tablename__ = "cronos_session_feedback"

    id           = Column(Integer, primary_key=True)
    user_id      = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    session_id   = Column(Integer, nullable=False)
    session_name = Column(String(200), nullable=False)
    feedback     = Column(String(20), nullable=False)   # 'facile' | 'ok' | 'difficile'
    done_at      = Column(Date, nullable=False, default=date.today)
    created_at   = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="session_feedback")


# ─────────────────────────────────────────────
# D. Cache des recommandations (TTL 24h)
# ─────────────────────────────────────────────

class RecommendationsCache(Base):
    """Cache des recommandations du jour — évite de recalculer si le contenu n'a pas changé."""
    __tablename__ = "cronos_recommendations_cache"
    __table_args__ = (UniqueConstraint("user_id", "cache_date", name="uq_cronos_cache_user_date"),)

    id                   = Column(Integer, primary_key=True)
    user_id              = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    cache_date           = Column(Date, nullable=False, index=True)
    recommendations_json = Column(Text, nullable=False)
    athlete_json         = Column(Text, nullable=True)
    recovery_json        = Column(Text, nullable=True)
    load_json            = Column(Text, nullable=True)
    computed_with_ml     = Column(Boolean, default=False)
    created_at           = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="recommendations_cache")


# ─────────────────────────────────────────────
# E. Tracks GPS (zones de chaleur)
# ─────────────────────────────────────────────

class ActivityTrack(Base):
    """Coordonnées GPS d'une activité — alimentent la heatmap Leaflet."""
    __tablename__ = "cronos_activity_tracks"
    __table_args__ = (UniqueConstraint("user_id", "activity_id", name="uq_cronos_track_user_activity"),)

    id               = Column(Integer, primary_key=True)
    user_id          = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    activity_id      = Column(BigInteger, nullable=False)
    coordinates_json = Column(Text, nullable=False)   # JSON [[lat, lng], ...]
    created_at       = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="activity_tracks")


# ─────────────────────────────────────────────
# F. Préférences de notifications
# ─────────────────────────────────────────────

class NotificationPrefs(Base):
    """Préférences d'envoi de l'email quotidien de recommandation."""
    __tablename__ = "cronos_notification_prefs"

    id            = Column(Integer, primary_key=True)
    user_id       = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    email_enabled = Column(Boolean, default=True)
    send_hour     = Column(Integer, default=8)    # heure UTC d'envoi (0-23)

    user = relationship("User", back_populates="notification_prefs")


# ─────────────────────────────────────────────
# G. Journal des blessures
# ─────────────────────────────────────────────

class InjuryLog(Base):
    """Blessure déclarée par l'utilisateur — adapte les recommandations CRONOS."""
    __tablename__ = "cronos_injury_logs"

    id             = Column(Integer, primary_key=True)
    user_id        = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    body_zone      = Column(String(50),  nullable=False)   # ex: "genou_gauche", "dos"
    severity       = Column(String(20),  nullable=False)   # "gene" | "legere" | "arret"
    estimated_days = Column(Integer,     nullable=True)    # durée estimée (None = à réévaluer)
    status         = Column(String(20),  nullable=False, default="active")  # active | recovering | healed
    notes          = Column(Text,        nullable=True)
    created_at     = Column(DateTime(timezone=True), server_default=func.now())
    updated_at     = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="injury_logs")