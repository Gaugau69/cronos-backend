"""
app/services/withings_parse.py — Collecte des données Withings.
"""

import logging
from datetime import date, datetime

import httpx

log = logging.getLogger(__name__)
WITHINGS_API = "https://wbsapi.withings.net"


async def _post(client, url, headers, data) -> dict | None:
    try:
        resp = await client.post(url, headers=headers, data=data)
        if resp.status_code == 200:
            body = resp.json()
            if body.get("status") == 0:
                return body.get("body", {})
        log.warning(f"Withings {url}: {resp.status_code}")
        return None
    except Exception as e:
        log.warning(f"Withings error: {e}")
        return None


async def collect_day_withings(headers: dict, target_date: date) -> dict:
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
        "calories_active": None, "distance_m": None,
        "active_min": None, "avg_spo2": None, "avg_respiration_rate": None,
    }

    start_ts = int(datetime.combine(target_date, datetime.min.time()).timestamp())
    end_ts   = start_ts + 86400

    async with httpx.AsyncClient(timeout=15) as client:

        # ── Sommeil ──
        sleep = await _post(client, f"{WITHINGS_API}/v2/sleep", headers, {
            "action": "getsummary",
            "startdateymd": target_date.strftime("%Y-%m-%d"),
            "enddateymd":   target_date.strftime("%Y-%m-%d"),
            "data_fields":  "nb_rem_episodes,sleep_score,sleep_efficiency,sleep_latency,"
                           "total_sleep_time,total_timeinbed,wakeup_duration,"
                           "light_sleep_duration,deep_sleep_duration,rem_sleep_duration,"
                           "hr_average,hr_min,rr_average,breathing_disturbances_intensity",
        })
        if sleep and sleep.get("series"):
            s = sleep["series"][0]
            data = s.get("data", {})
            result["sleep_duration_min"] = (data.get("total_sleep_time", 0) or 0) // 60
            result["deep_sleep_min"]     = (data.get("deep_sleep_duration", 0) or 0) // 60
            result["light_sleep_min"]    = (data.get("light_sleep_duration", 0) or 0) // 60
            result["rem_sleep_min"]      = (data.get("rem_sleep_duration", 0) or 0) // 60
            result["awake_min"]          = (data.get("wakeup_duration", 0) or 0) // 60
            result["sleep_score"]        = data.get("sleep_score")
            result["resting_hr"]         = data.get("hr_min")
            result["avg_respiration_rate"] = data.get("rr_average")
            result["sleep_start"]        = s.get("startdate")
            result["sleep_end"]          = s.get("enddate")

        # ── Activité journalière ──
        activity = await _post(client, f"{WITHINGS_API}/v2/measure", headers, {
            "action":     "getactivity",
            "startdateymd": target_date.strftime("%Y-%m-%d"),
            "enddateymd":   target_date.strftime("%Y-%m-%d"),
            "data_fields": "steps,distance,calories,totalcalories,active_calories,"
                          "elevation,soft,moderate,intense,hr_average,hr_min,hr_max",
        })
        if activity and activity.get("activities"):
            a = activity["activities"][0]
            result["total_steps"]    = a.get("steps")
            result["distance_m"]     = (a.get("distance", 0) or 0) * 1000
            result["calories_total"] = a.get("totalcalories")
            result["calories_active"]= a.get("active_calories")
            result["active_min"]     = (
                (a.get("soft", 0) or 0) +
                (a.get("moderate", 0) or 0) +
                (a.get("intense", 0) or 0)
            ) // 60
            result["max_hr"]         = a.get("hr_max")
            result["min_hr"]         = a.get("hr_min")

        # ── SpO2 ──
        spo2 = await _post(client, f"{WITHINGS_API}/v2/measure", headers, {
            "action":    "getmeas",
            "meastype":  "54",  # SpO2
            "startdate": start_ts,
            "enddate":   end_ts,
        })
        if spo2 and spo2.get("measuregrps"):
            vals = [
                m["value"] * (10 ** m["unit"])
                for grp in spo2["measuregrps"]
                for m in grp.get("measures", [])
            ]
            if vals:
                result["avg_spo2"] = sum(vals) / len(vals)

    return result


async def collect_activities_withings(headers: dict, target_date: date) -> list[dict]:
    activities = []
    start_ts = int(datetime.combine(target_date, datetime.min.time()).timestamp())
    end_ts   = start_ts + 86400

    async with httpx.AsyncClient(timeout=15) as client:
        data = await _post(client, f"{WITHINGS_API}/v2/measure", headers, {
            "action":    "getworkouts",
            "startdate": start_ts,
            "enddate":   end_ts,
            "data_fields": "calories,intensity,manual_distance,manual_calories,"
                          "hr_average,hr_min,hr_max,pause_duration,algo_pause_duration,"
                          "spo2_average,steps,distance,elevation,laps_duration,total_calories",
        })
        if not data or not data.get("series"):
            return activities

        for w in data["series"]:
            d = w.get("data", {})
            activities.append({
                "activity_id":      w.get("id", hash(str(w.get("startdate", "")))),
                "activity_name":    f"Withings workout {w.get('category', '')}",
                "activity_type":    str(w.get("category", "unknown")),
                "start_time":       str(w.get("startdate", "")),
                "duration_min":     ((w.get("enddate", 0) - w.get("startdate", 0)) / 60),
                "distance_km":      (d.get("distance", 0) or 0) / 1000,
                "avg_hr":           d.get("hr_average"),
                "max_hr":           d.get("hr_max"),
                "calories":         d.get("total_calories"),
                "avg_speed_kmh":    None,
                "elevation_gain_m": d.get("elevation"),
                "training_effect":  None,
                "vo2max":           None,
            })

    return activities
