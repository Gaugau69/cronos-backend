"""
app/services/whoop_parse.py — Collecte des données WHOOP v1.

API WHOOP Developer v1 :
    GET /v1/recovery         → recovery score + HRV + FC repos
    GET /v1/activity/sleep   → sommeil
    GET /v1/activity/workout → séances sport
    GET /v1/cycle            → cycles journaliers (strain + recovery)

WHOOP utilise des plages datetime (pas juste une date).
"""

import logging
from datetime import date, datetime, timedelta, timezone

import httpx

log = logging.getLogger(__name__)
WHOOP_API_BASE = "https://api.prod.whoop.com/developer/v1"


async def _get(client: httpx.AsyncClient, url: str, headers: dict, params: dict | None = None) -> dict | None:
    try:
        resp = await client.get(url, headers=headers, params=params)
        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code == 401:
            raise Exception(f"WHOOP 401: token expiré ou révoqué ({url})")
        log.warning(f"WHOOP API {url}: {resp.status_code}")
        return None
    except Exception as e:
        if "401" in str(e):
            raise
        log.warning(f"WHOOP API error {url}: {e}")
        return None


def _day_window(target_date: date) -> tuple[str, str]:
    """Retourne start/end ISO 8601 UTC pour une journée (00:00 → 00:00 lendemain)."""
    start_dt = datetime(target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc)
    end_dt   = start_dt + timedelta(days=1)
    return start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"), end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


async def collect_day_whoop(headers: dict, target_date: date) -> dict:
    start, end = _day_window(target_date)
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

        # ── Recovery ──
        recovery = await _get(client, f"{WHOOP_API_BASE}/recovery", headers,
                               params={"start": start, "end": end, "limit": 25})
        if recovery and recovery.get("records"):
            r = recovery["records"][0]
            score = (r.get("score") or {})
            hrv = score.get("hrv_rmssd_milli")
            result["hrv_last_night"] = hrv
            result["resting_hr"]     = score.get("resting_heart_rate")
            result["avg_spo2"]       = score.get("spo2_percentage")
            rec_score = score.get("recovery_score")
            if rec_score is not None:
                result["hrv_status"]  = "balanced" if rec_score >= 67 else "compromised"
                result["hrv_feedback"] = f"Recovery {rec_score}%"

        # ── Sommeil ──
        sleep = await _get(client, f"{WHOOP_API_BASE}/activity/sleep", headers,
                            params={"start": start, "end": end, "limit": 25})
        if sleep and sleep.get("records"):
            s = sleep["records"][0]
            score = (s.get("score") or {})
            stages = score.get("stage_summary", {}) or {}

            def ms_to_min(ms):
                return round((ms or 0) / 60000, 1)

            result["sleep_duration_min"] = ms_to_min(stages.get("total_in_bed_time_milli", 0) -
                                                      stages.get("total_awake_time_milli", 0))
            result["deep_sleep_min"]  = ms_to_min(stages.get("total_slow_wave_sleep_time_milli"))
            result["light_sleep_min"] = ms_to_min(stages.get("total_light_sleep_time_milli"))
            result["rem_sleep_min"]   = ms_to_min(stages.get("total_rem_sleep_time_milli"))
            result["awake_min"]       = ms_to_min(stages.get("total_awake_time_milli"))
            result["sleep_score"]     = score.get("sleep_performance_percentage")
            result["avg_respiration_rate"] = score.get("respiratory_rate")
            result["sleep_start"]     = s.get("start")
            result["sleep_end"]       = s.get("end")

        # ── Strain (cycles) — donne les calories actives et la charge ──
        cycle = await _get(client, f"{WHOOP_API_BASE}/cycle", headers,
                            params={"start": start, "end": end, "limit": 25})
        if cycle and cycle.get("records"):
            c = cycle["records"][0]
            score = (c.get("score") or {})
            result["calories_active"] = score.get("kilojoule_active")
            if result["calories_active"]:
                result["calories_active"] = round(result["calories_active"] / 4.184)  # kJ → kcal

    return result


async def collect_activities_whoop(headers: dict, target_date: date) -> list[dict]:
    start, end = _day_window(target_date)
    activities = []

    async with httpx.AsyncClient(timeout=15) as client:
        data = await _get(client, f"{WHOOP_API_BASE}/activity/workout", headers,
                          params={"start": start, "end": end, "limit": 25})
        if not data or not data.get("records"):
            return activities

        for w in data["records"]:
            score = (w.get("score") or {})
            start_time = w.get("start", "")
            end_time   = w.get("end", "")
            dur_min = 0
            if start_time and end_time:
                try:
                    s = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                    e = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
                    dur_min = round((e - s).total_seconds() / 60, 1)
                except Exception:
                    pass

            sport_id = w.get("sport_id", 0)
            activities.append({
                "activity_id":      w.get("id", abs(hash(start_time))),
                "activity_name":    f"WHOOP workout {sport_id}",
                "activity_type":    f"sport_{sport_id}",
                "start_time":       start_time,
                "duration_min":     dur_min,
                "distance_km":      (score.get("distance_meter", 0) or 0) / 1000,
                "avg_hr":           score.get("average_heart_rate"),
                "max_hr":           score.get("max_heart_rate"),
                "calories":         round(score.get("kilojoule", 0) / 4.184) if score.get("kilojoule") else None,
                "avg_speed_kmh":    None,
                "elevation_gain_m": score.get("altitude_gain_meter"),
                "training_effect":  score.get("strain"),
                "vo2max":           None,
            })

    return activities
