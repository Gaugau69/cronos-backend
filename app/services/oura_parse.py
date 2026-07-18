"""
app/services/oura_parse.py — Collecte des données Oura Ring v2.

API Oura v2 :
    GET /v2/usercollection/daily_sleep      → sommeil + HRV + FC repos
    GET /v2/usercollection/daily_readiness  → readiness score
    GET /v2/usercollection/daily_activity   → activité journalière
    GET /v2/usercollection/daily_spo2       → SpO2
    GET /v2/usercollection/workout          → séances sport
"""

import logging
from datetime import date

import httpx

log = logging.getLogger(__name__)
OURA_API_BASE = "https://api.ouraring.com/v2"


async def _get(client: httpx.AsyncClient, url: str, headers: dict, params: dict | None = None) -> dict | None:
    try:
        resp = await client.get(url, headers=headers, params=params)
        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code == 401:
            raise Exception(f"Oura 401: token expiré ou révoqué ({url})")
        log.warning(f"Oura API {url}: {resp.status_code}")
        return None
    except Exception as e:
        if "401" in str(e):
            raise
        log.warning(f"Oura API error {url}: {e}")
        return None


async def collect_day_oura(headers: dict, target_date: date) -> dict:
    date_str = target_date.strftime("%Y-%m-%d")
    result = {
        "sleep_start": None, "sleep_end": None,
        "sleep_duration_min": 0, "deep_sleep_min": 0,
        "light_sleep_min": 0, "rem_sleep_min": 0,
        "awake_min": 0, "sleep_score": None,
        "hrv_weekly_avg": None, "hrv_last_night": None,
        "hrv_5min_high": None, "hrv_status": None, "hrv_feedback": None,
        "resting_hr": None, "max_hr": None, "min_hr": None,
        "avg_stress": None, "max_stress": None,
        "body_battery_charged": None, "body_battery_drained": None,
        "total_steps": None, "calories_total": None,
        "calories_active": None, "distance_m": None, "active_min": None,
        "avg_spo2": None, "avg_respiration_rate": None,
    }

    async with httpx.AsyncClient(timeout=15) as client:

        # ── Sommeil ──
        sleep_data = await _get(client, f"{OURA_API_BASE}/usercollection/daily_sleep", headers,
                                 params={"start_date": date_str, "end_date": date_str})
        if sleep_data and sleep_data.get("data"):
            s = sleep_data["data"][0]
            result["sleep_duration_min"] = (s.get("total_sleep_duration", 0) or 0) // 60
            result["deep_sleep_min"]     = (s.get("deep_sleep_duration", 0) or 0) // 60
            result["light_sleep_min"]    = (s.get("light_sleep_duration", 0) or 0) // 60
            result["rem_sleep_min"]      = (s.get("rem_sleep_duration", 0) or 0) // 60
            result["awake_min"]          = (s.get("awake_time", 0) or 0) // 60
            result["sleep_score"]        = s.get("sleep_score") or s.get("score")
            result["hrv_last_night"]     = s.get("average_hrv")
            result["resting_hr"]         = s.get("lowest_heart_rate")
            result["avg_respiration_rate"] = s.get("average_breath") or s.get("breathing_average")
            result["sleep_start"]        = s.get("bedtime_start")
            result["sleep_end"]          = s.get("bedtime_end")

        # ── Readiness (HRV status) ──
        readiness_data = await _get(client, f"{OURA_API_BASE}/usercollection/daily_readiness", headers,
                                     params={"start_date": date_str, "end_date": date_str})
        if readiness_data and readiness_data.get("data"):
            r = readiness_data["data"][0]
            score = r.get("score")
            if score is not None:
                result["hrv_status"] = "balanced" if score >= 70 else "compromised"
                result["hrv_feedback"] = f"Readiness {score}"
            if not result["hrv_last_night"]:
                contrib = r.get("contributors", {})
                result["hrv_last_night"] = contrib.get("hrv_balance")

        # ── Activité journalière ──
        activity_data = await _get(client, f"{OURA_API_BASE}/usercollection/daily_activity", headers,
                                    params={"start_date": date_str, "end_date": date_str})
        if activity_data and activity_data.get("data"):
            a = activity_data["data"][0]
            result["total_steps"]     = a.get("steps")
            result["calories_total"]  = a.get("total_calories")
            result["calories_active"] = a.get("active_calories")
            result["distance_m"]      = (a.get("equivalent_walking_distance") or 0)
            active_min = (
                (a.get("high_activity_time", 0) or 0) +
                (a.get("medium_activity_time", 0) or 0)
            )
            result["active_min"] = active_min // 60 if active_min else None

        # ── SpO2 ──
        spo2_data = await _get(client, f"{OURA_API_BASE}/usercollection/daily_spo2", headers,
                                params={"start_date": date_str, "end_date": date_str})
        if spo2_data and spo2_data.get("data"):
            spo2 = spo2_data["data"][0]
            avg = spo2.get("spo2_percentage", {}) or {}
            result["avg_spo2"] = avg.get("average")

    return result


async def collect_activities_oura(headers: dict, target_date: date) -> list[dict]:
    date_str = target_date.strftime("%Y-%m-%d")
    activities = []

    async with httpx.AsyncClient(timeout=15) as client:
        data = await _get(client, f"{OURA_API_BASE}/usercollection/workout", headers,
                          params={"start_date": date_str, "end_date": date_str})
        if not data or not data.get("data"):
            return activities

        for w in data["data"]:
            start = w.get("start_datetime", "")
            end   = w.get("end_datetime", "")
            dur_sec = 0
            if start and end:
                try:
                    from datetime import datetime, timezone
                    s = datetime.fromisoformat(start.replace("Z", "+00:00"))
                    e = datetime.fromisoformat(end.replace("Z", "+00:00"))
                    dur_sec = (e - s).total_seconds()
                except Exception:
                    pass

            activities.append({
                "activity_id":      abs(hash(w.get("id", start))),
                "activity_name":    w.get("label") or w.get("activity", "Workout"),
                "activity_type":    (w.get("activity") or w.get("sport") or "workout").lower().replace(" ", "_"),
                "start_time":       start,
                "duration_min":     round(dur_sec / 60, 1) if dur_sec else 0,
                "distance_km":      (w.get("distance", 0) or 0) / 1000,
                "avg_hr":           w.get("heart_rate_average"),
                "max_hr":           w.get("heart_rate_max"),
                "calories":         w.get("calories"),
                "avg_speed_kmh":    None,
                "elevation_gain_m": None,
                "training_effect":  None,
                "vo2max":           None,
            })

    return activities
