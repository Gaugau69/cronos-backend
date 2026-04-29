"""
app/services/garmin_parse.py — Parsing des réponses brutes de l'API Garmin.
"""

import logging
import time
from datetime import date

from garminconnect import Garmin

log = logging.getLogger(__name__)
_SLEEP = 1.0  # secondes entre appels pour ne pas surcharger Garmin


def _safe(fn, *args, default=None):
    try:
        result = fn(*args)
        time.sleep(_SLEEP)
        log.info(f"✓ {fn.__name__} OK")
        return result
    except Exception as e:
        log.warning(f"{fn.__name__}: {e}")
        return default


def parse_sleep(api: Garmin, d: date) -> dict:
    data = _safe(api.get_sleep_data, d.isoformat())
    if not data:
        return {}
    s = data.get("dailySleepDTO", {})
    return {
        "sleep_start":          s.get("sleepStartTimestampLocal"),
        "sleep_end":            s.get("sleepEndTimestampLocal"),
        "sleep_duration_min":   (s.get("sleepTimeSeconds") or 0) // 60,
        "deep_sleep_min":       (s.get("deepSleepSeconds") or 0) // 60,
        "light_sleep_min":      (s.get("lightSleepSeconds") or 0) // 60,
        "rem_sleep_min":        (s.get("remSleepSeconds") or 0) // 60,
        "awake_min":            (s.get("awakeSleepSeconds") or 0) // 60,
        "sleep_score":          s.get("sleepScores", {}).get("overall", {}).get("value"),
        "avg_spo2":             s.get("averageSpO2Value"),
        "avg_respiration_rate": s.get("averageRespirationValue"),
    }


def parse_heart_rate(api: Garmin, d: date) -> dict:
    data = _safe(api.get_heart_rates, d.isoformat())
    if not data:
        return {}
    return {
        "resting_hr":             data.get("restingHeartRate"),
        "max_hr":                 data.get("maxHeartRate"),
        "min_hr":                 data.get("minHeartRate"),
        "last_7d_avg_resting_hr": data.get("lastSevenDaysAvgRestingHeartRate"),
    }


def parse_hrv(api: Garmin, d: date) -> dict:
    data = _safe(api.get_hrv_data, d.isoformat())
    if not data:
        return {}
    s = data.get("hrvSummary", {})
    return {
        "hrv_weekly_avg": s.get("weeklyAvg"),
        "hrv_last_night": s.get("lastNight"),
        "hrv_5min_high":  s.get("lastNight5MinHigh"),
        "hrv_status":     s.get("status"),
        "hrv_feedback":   s.get("feedbackPhrase"),
    }


def parse_stress(api: Garmin, d: date) -> dict:
    data = _safe(api.get_stress_data, d.isoformat())
    if not data:
        return {}
    return {
        "avg_stress":    data.get("avgStressLevel"),
        "max_stress":    data.get("maxStressLevel"),
        "rest_stress":   data.get("restStressDuration"),
        "low_stress":    data.get("lowStressDuration"),
        "medium_stress": data.get("mediumStressDuration"),
        "high_stress":   data.get("highStressDuration"),
    }


def parse_steps(api: Garmin, d: date) -> dict:
    data = _safe(api.get_steps_data, d.isoformat())
    if not data or not isinstance(data, list):
        return {}
    return {"total_steps": sum(i.get("steps", 0) for i in data)}


def parse_body_battery(api: Garmin, d: date) -> dict:
    data = _safe(api.get_body_battery, d.isoformat(), d.isoformat())
    if not data or not isinstance(data, list):
        return {}
    charged = [i.get("charged") for i in data if i.get("charged") is not None]
    drained = [i.get("drained") for i in data if i.get("drained") is not None]
    return {
        "body_battery_charged": max(charged) if charged else None,
        "body_battery_drained": max(drained) if drained else None,
    }


def parse_stats(api: Garmin, d: date) -> dict:
    data = _safe(api.get_stats, d.isoformat())
    if not data:
        return {}
    return {
        "calories_total":  data.get("totalKilocalories"),
        "calories_active": data.get("activeKilocalories"),
        "distance_m":      data.get("totalDistanceMeters"),
        "active_min":      (data.get("highlyActiveSeconds") or 0) // 60
                         + (data.get("moderateIntensityMinutes") or 0),
        "floors_climbed":  data.get("floorsAscended"),
    }


def parse_activities(api: Garmin, d: date) -> list[dict]:
    acts = _safe(api.get_activities_by_date, d.isoformat(), d.isoformat()) or []
    return [
        {
            "activity_id":       a.get("activityId"),
            "activity_name":     a.get("activityName"),
            "activity_type":     a.get("activityType", {}).get("typeKey"),
            "start_time":        a.get("startTimeLocal"),
            "duration_min":      round((a.get("duration") or 0) / 60, 1),
            "distance_km":       round((a.get("distance") or 0) / 1000, 2),
            "avg_hr":            a.get("averageHR"),
            "max_hr":            a.get("maxHR"),
            "calories":          a.get("calories"),
            "avg_speed_kmh":     round(a["averageSpeed"] * 3.6, 2) if a.get("averageSpeed") else None,
            "elevation_gain_m":  a.get("elevationGain"),
            "training_effect":   a.get("aerobicTrainingEffect"),
            "vo2max":            a.get("vO2MaxValue"),
        }
        for a in acts
    ]
