"""
app/services/fitbit_parse.py — Collecte des données Fitbit Web API.

Endpoints :
    GET /1/user/-/sleep/date/{date}.json             → sommeil
    GET /1/user/-/activities/heart/date/{date}/1d.json → FC repos
    GET /1/user/-/activities/date/{date}.json          → activité
    GET /1.2/user/-/hrv/date/{date}/all.json           → HRV
    GET /1/user/-/spo2/date/{date}/all.json            → SpO2
    GET /1/user/-/activities/list.json                 → séances sport
"""

import logging
from datetime import date

import httpx

log = logging.getLogger(__name__)
FITBIT_API_BASE = "https://api.fitbit.com"


async def _get(client: httpx.AsyncClient, url: str, headers: dict, params: dict | None = None) -> dict | None:
    try:
        resp = await client.get(url, headers=headers, params=params)
        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code == 401:
            raise Exception(f"Fitbit 401: token expiré ou révoqué ({url})")
        elif resp.status_code == 429:
            log.warning(f"Fitbit rate-limited: {url}")
            return None
        log.warning(f"Fitbit API {url}: {resp.status_code}")
        return None
    except Exception as e:
        if "401" in str(e):
            raise
        log.warning(f"Fitbit API error {url}: {e}")
        return None


async def collect_day_fitbit(headers: dict, target_date: date) -> dict:
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
        sleep = await _get(client, f"{FITBIT_API_BASE}/1/user/-/sleep/date/{date_str}.json", headers)
        if sleep and sleep.get("sleep"):
            main = sleep["sleep"][0]
            levels = (main.get("levels") or {}).get("summary", {}) or {}
            result["sleep_duration_min"] = main.get("minutesAsleep", 0)
            result["awake_min"]          = main.get("minutesAwake", 0)
            result["deep_sleep_min"]     = levels.get("deep", {}).get("minutes", 0)
            result["light_sleep_min"]    = levels.get("light", {}).get("minutes", 0)
            result["rem_sleep_min"]      = levels.get("rem", {}).get("minutes", 0)
            result["sleep_score"]        = main.get("efficiency")
            result["sleep_start"]        = main.get("startTime")
            result["sleep_end"]          = main.get("endTime")
            # Score sommeil Fitbit (dans summary si disponible)
            summary = sleep.get("summary", {})
            if summary.get("stages"):
                pass  # summary.stages has deep/light/rem/wake counts

        # ── FC repos ──
        hr = await _get(client, f"{FITBIT_API_BASE}/1/user/-/activities/heart/date/{date_str}/1d.json", headers)
        if hr:
            hr_list = hr.get("activities-heart", [])
            if hr_list:
                val = hr_list[0].get("value", {})
                result["resting_hr"] = val.get("restingHeartRate")

        # ── Activité ──
        activity = await _get(client, f"{FITBIT_API_BASE}/1/user/-/activities/date/{date_str}.json", headers)
        if activity and activity.get("summary"):
            s = activity["summary"]
            result["total_steps"]     = s.get("steps")
            result["calories_total"]  = s.get("caloriesOut")
            result["calories_active"] = s.get("activityCalories")
            result["active_min"]      = (
                (s.get("veryActiveMinutes", 0) or 0) +
                (s.get("fairlyActiveMinutes", 0) or 0)
            )
            for d in s.get("distances", []):
                if d.get("activity") == "total":
                    result["distance_m"] = (d.get("distance", 0) or 0) * 1000
                    break

        # ── HRV ──
        hrv = await _get(client, f"{FITBIT_API_BASE}/1.2/user/-/hrv/date/{date_str}/all.json", headers)
        if hrv and hrv.get("hrv"):
            vals = [m.get("value", {}).get("rmssd") for m in hrv["hrv"] if m.get("value", {}).get("rmssd")]
            if vals:
                result["hrv_last_night"] = sum(vals) / len(vals)

        # ── SpO2 ──
        spo2 = await _get(client, f"{FITBIT_API_BASE}/1/user/-/spo2/date/{date_str}/all.json", headers)
        if spo2 and spo2.get("minutes"):
            vals = [m.get("value") for m in spo2["minutes"] if m.get("value")]
            if vals:
                result["avg_spo2"] = sum(vals) / len(vals)

    return result


async def collect_activities_fitbit(headers: dict, target_date: date) -> list[dict]:
    date_str = target_date.strftime("%Y-%m-%d")
    activities = []

    async with httpx.AsyncClient(timeout=15) as client:
        data = await _get(client, f"{FITBIT_API_BASE}/1/user/-/activities/list.json", headers,
                          params={"afterDate": date_str, "sort": "asc", "limit": 20, "offset": 0})
        if not data or not data.get("activities"):
            return activities

        for a in data["activities"]:
            act_date = (a.get("startDate") or a.get("startTime", "")[:10])
            if act_date != date_str:
                continue
            activities.append({
                "activity_id":      a.get("logId", abs(hash(a.get("startTime", "")))),
                "activity_name":    a.get("activityName", "Activity"),
                "activity_type":    (a.get("activityName") or "activity").lower().replace(" ", "_"),
                "start_time":       a.get("startTime"),
                "duration_min":     round((a.get("activeDuration", 0) or 0) / 60000, 1),
                "distance_km":      a.get("distance", 0),
                "avg_hr":           a.get("averageHeartRate"),
                "max_hr":           None,
                "calories":         a.get("calories"),
                "avg_speed_kmh":    a.get("speed"),
                "elevation_gain_m": a.get("elevationGain"),
                "training_effect":  None,
                "vo2max":           None,
            })

    return activities
