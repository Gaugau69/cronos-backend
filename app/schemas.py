"""
app/schemas.py — Schémas Pydantic (validation entrée / sérialisation sortie).
"""

from datetime import date, datetime
from typing import Optional
from pydantic import BaseModel, EmailStr


# ── Users ──────────────────────────────────────────────────────────────────

class UserCreate(BaseModel):
    name: str
    email: EmailStr
    password: str   # Utilisé uniquement pour le premier login Garmin, jamais stocké


class UserOut(BaseModel):
    id: int
    name: str
    email: str
    created_at: datetime
    has_token: bool

    class Config:
        from_attributes = True


# ── Daily Metrics ──────────────────────────────────────────────────────────

class DailyMetricOut(BaseModel):
    date: date
    user_id: int

    sleep_duration_min: Optional[int] = None
    deep_sleep_min: Optional[int] = None
    light_sleep_min: Optional[int] = None
    rem_sleep_min: Optional[int] = None
    awake_min: Optional[int] = None
    sleep_score: Optional[int] = None
    avg_spo2: Optional[float] = None
    avg_respiration_rate: Optional[float] = None

    resting_hr: Optional[int] = None
    max_hr: Optional[int] = None
    last_7d_avg_resting_hr: Optional[float] = None

    hrv_weekly_avg: Optional[float] = None
    hrv_last_night: Optional[float] = None
    hrv_5min_high: Optional[float] = None
    hrv_status: Optional[str] = None

    avg_stress: Optional[int] = None
    max_stress: Optional[int] = None

    total_steps: Optional[int] = None
    body_battery_charged: Optional[int] = None
    body_battery_drained: Optional[int] = None
    calories_total: Optional[int] = None
    calories_active: Optional[int] = None
    distance_m: Optional[float] = None
    active_min: Optional[int] = None

    class Config:
        from_attributes = True


# ── Activities ─────────────────────────────────────────────────────────────

class ActivityOut(BaseModel):
    date: date
    activity_id: int
    activity_name: Optional[str] = None
    activity_type: Optional[str] = None
    start_time: Optional[str] = None
    duration_min: Optional[float] = None
    distance_km: Optional[float] = None
    avg_hr: Optional[int] = None
    max_hr: Optional[int] = None
    calories: Optional[int] = None
    avg_speed_kmh: Optional[float] = None
    elevation_gain_m: Optional[float] = None
    training_effect: Optional[float] = None
    vo2max: Optional[float] = None

    class Config:
        from_attributes = True


# ── Collect ────────────────────────────────────────────────────────────────

class CollectRequest(BaseModel):
    name: str
    start_date: Optional[date] = None
    end_date: Optional[date] = None
