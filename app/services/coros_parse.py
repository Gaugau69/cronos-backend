"""
app/services/coros_parse.py — Collecte des données COROS Training Hub API.

Même interface que garmin_parse.py / polar_parse.py :
    collect_day_coros(access_token, user_id, target_date, region) -> dict
    collect_activities_coros(access_token, user_id, target_date, region) -> list[dict]

Endpoints utilisés :
    GET /analyse/dayDetail/query  → HRV, RHR, charge
    GET /activity/query           → liste des activités
"""

import logging
from datetime import date, datetime, timezone

import httpx

from app.services.coros_auth import BASE_URLS, _auth_headers

log = logging.getLogger(__name__)

SPORT_NAMES = {
    100: "Running", 102: "Trail Running", 103: "Track Running", 104: "Hiking",
    200: "Road Bike", 201: "Indoor Cycling", 203: "Gravel Bike", 204: "MTB",
    400: "Cardio", 402: "Strength", 403: "Yoga",
    900: "Walking", 9807: "Bike Commute",
}


async def _get(
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    params: dict | None = None,
) -> dict | None:
    try:
        resp = await client.get(url, headers=headers, params=params)
        if resp.status_code == 200:
            body = resp.json()
            if body.get("result") != "0000":
                log.warning(f"COROS API {url}: result={body.get('result')} {body.get('message')}")
                return None
            return body.get("data")
        log.warning(f"COROS API {url}: HTTP {resp.status_code}")
        return None
    except Exception as e:
        log.warning(f"COROS API error {url}: {e}")
        return None


def _coros_ts_to_seconds(ts: int) -> float:
    """Détecte le format du timestamp COROS et retourne des secondes Unix.
    COROS utilise centisecondes sur certains endpoints, secondes sur d'autres."""
    if ts > 1e11:   # centisecondes (ex: 175_000_000_000)
        return ts / 100
    return float(ts)  # secondes


def _coros_ts_to_iso(ts: int | None) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(_coros_ts_to_seconds(ts), tz=timezone.utc).isoformat()
    except Exception:
        return None


async def collect_day_coros(
    access_token: str,
    user_id: str,
    target_date: date,
    region: str = "eu",
) -> dict:
    """
    Collecte les métriques COROS pour un jour donné.
    Retourne un dict compatible avec la table daily_metrics.
    """
    base = BASE_URLS.get(region, BASE_URLS["eu"])
    headers = _auth_headers(access_token, user_id)
    date_str = target_date.strftime("%Y%m%d")

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
        # Daily analysis (HRV, RHR, training load)
        data = await _get(
            client,
            f"{base}/analyse/dayDetail/query",
            headers,
            params={"startDay": date_str, "endDay": date_str},
        )
        if data:
            day_list = data.get("dayList", [])
            if day_list:
                day = day_list[0]
                result["hrv_last_night"] = day.get("avgSleepHrv")
                result["hrv_weekly_avg"] = day.get("sleepHrvBase")
                result["resting_hr"]     = day.get("rhr")

    return result


async def collect_activities_coros(
    access_token: str,
    user_id: str,
    target_date: date,
    region: str = "eu",
) -> list[dict]:
    """
    Collecte les activités COROS pour un jour donné.
    Retourne une liste de dicts compatibles avec la table activities.
    """
    base = BASE_URLS.get(region, BASE_URLS["eu"])
    headers = _auth_headers(access_token, user_id)
    date_str = target_date.strftime("%Y%m%d")
    activities = []

    async with httpx.AsyncClient(timeout=20) as client:
        data = await _get(
            client,
            f"{base}/activity/query",
            headers,
            params={
                "startDay":   date_str,
                "endDay":     date_str,
                "pageNumber": 1,
                "size":       30,
                "modeList":   "",
            },
        )
        if not data:
            return activities

        for item in data.get("dataList", []):
            sport_type = item.get("sportType")
            start_ts   = item.get("startTime")
            duration_s = item.get("totalTime") or 0
            distance_m = item.get("distance") or item.get("totalDistance") or 0
            cal_raw    = item.get("calorie") or 0

            # Vérifier que l'activité est bien du bon jour
            if start_ts:
                act_date = datetime.fromtimestamp(_coros_ts_to_seconds(start_ts), tz=timezone.utc).date()
                if act_date != target_date:
                    continue

            activities.append({
                "activity_id":      int(item.get("labelId", 0)) or abs(hash(str(item.get("labelId", "")))),
                "activity_name":    item.get("name") or item.get("remark") or SPORT_NAMES.get(sport_type, "Activity"),
                "activity_type":    SPORT_NAMES.get(sport_type, f"sport_{sport_type}").lower().replace(" ", "_"),
                "start_time":       _coros_ts_to_iso(start_ts),
                "duration_min":     round(duration_s / 60, 1) if duration_s else 0,
                "distance_km":      round(distance_m / 1000, 3) if distance_m else 0,
                "avg_hr":           item.get("avgHr"),
                "max_hr":           item.get("maxHr"),
                "calories":         round(cal_raw / 1000) if cal_raw else None,
                "avg_speed_kmh":    None,
                "elevation_gain_m": item.get("ascent") or item.get("totalAscent"),
                "training_effect":  item.get("trainingLoad"),
                "vo2max":           None,
            })

    return activities
