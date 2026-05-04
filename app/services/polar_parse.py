"""
app/services/polar_parse.py — Collecte des données Polar AccessLink.

Même interface que garmin_parse.py :
    collect_day(headers, date) -> dict avec les mêmes clés

API Polar AccessLink v3 :
    - /v3/users/{user_id}/sleep         → sommeil + HRV
    - /v3/users/{user_id}/recharge      → nightly recharge (HRV status)
    - /v3/users/{user_id}/activity      → activité journalière
    - /v3/exercises                     → séances sport
"""

import logging
from datetime import date, timedelta

import httpx

log = logging.getLogger(__name__)

POLAR_API_BASE = "https://www.polaraccesslink.com/v3"


async def _get(client: httpx.AsyncClient, url: str, headers: dict) -> dict | None:
    """Requête GET avec gestion d'erreurs."""
    try:
        resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code == 204:
            return {}  # No content — pas de données pour ce jour
        else:
            log.warning(f"Polar API {url}: {resp.status_code}")
            return None
    except Exception as e:
        log.warning(f"Polar API error {url}: {e}")
        return None


async def collect_day_polar(
    headers: dict,
    polar_user_id: str,
    target_date: date,
) -> dict:
    """
    Collecte toutes les métriques Polar pour un jour donné.
    Retourne un dict avec les mêmes clés que garmin_parse.

    Args:
        headers       : headers d'authentification Polar
        polar_user_id : ID utilisateur Polar
        target_date   : date à collecter
    """
    date_str = target_date.strftime("%Y-%m-%d")
    result = {
        # Sommeil
        "sleep_start":         None,
        "sleep_end":           None,
        "sleep_duration_min":  0,
        "deep_sleep_min":      0,
        "light_sleep_min":     0,
        "rem_sleep_min":       0,
        "awake_min":           0,
        "sleep_score":         None,
        # HRV
        "hrv_weekly_avg":      None,
        "hrv_last_night":      None,
        "hrv_5min_high":       None,
        "hrv_status":          None,
        "hrv_feedback":        None,
        # FC
        "resting_hr":          None,
        "max_hr":              None,
        "min_hr":              None,
        # Stress / Body battery
        "avg_stress":          None,
        "max_stress":          None,
        "body_battery_charged": None,
        "body_battery_drained": None,
        # Activité
        "total_steps":         None,
        "calories_total":      None,
        "calories_active":     None,
        "distance_m":          None,
        "active_min":          None,
        # Respiration / SpO2
        "avg_spo2":            None,
        "avg_respiration_rate": None,
    }

    async with httpx.AsyncClient(timeout=15) as client:

        # ── 1. Sommeil ──
        sleep_data = await _get(
            client,
            f"{POLAR_API_BASE}/users/{polar_user_id}/sleep/{date_str}",
            headers,
        )
        if sleep_data:
            result["sleep_duration_min"] = sleep_data.get("total_sleep_minutes", 0) or 0
            result["deep_sleep_min"]     = sleep_data.get("deep_sleep_minutes", 0) or 0
            result["light_sleep_min"]    = sleep_data.get("light_sleep_minutes", 0) or 0
            result["rem_sleep_min"]      = sleep_data.get("rem_sleep_minutes", 0) or 0
            result["awake_min"]          = sleep_data.get("awake_minutes_during_sleep", 0) or 0
            result["sleep_score"]        = sleep_data.get("sleep_score")

            # HRV depuis le sommeil
            hrv_data = sleep_data.get("hrv_avg_ms")
            if hrv_data:
                result["hrv_last_night"] = hrv_data

            # FC repos depuis le sommeil
            result["resting_hr"] = sleep_data.get("heart_rate_avg")

        # ── 2. Nightly Recharge (HRV status + ANS) ──
        recharge_data = await _get(
            client,
            f"{POLAR_API_BASE}/users/{polar_user_id}/recharge/{date_str}",
            headers,
        )
        if recharge_data:
            ans = recharge_data.get("ans_charge")
            if ans is not None:
                result["hrv_status"]   = "balanced" if ans >= 0 else "compromised"
                result["hrv_feedback"] = recharge_data.get("recharge_status", "")

            # HRV depuis nightly recharge si pas déjà récupéré
            if not result["hrv_last_night"]:
                result["hrv_last_night"] = recharge_data.get("hrv_avg_ms")

        # ── 3. Activité journalière ──
        activity_data = await _get(
            client,
            f"{POLAR_API_BASE}/users/{polar_user_id}/activity-transactions",
            headers,
        )
        # Note : Polar AccessLink v3 utilise des transactions pour les activités
        # On récupère via continuous activity summary
        daily_activity = await _get(
            client,
            f"{POLAR_API_BASE}/users/{polar_user_id}/activity/{date_str}",
            headers,
        )
        if daily_activity:
            result["total_steps"]    = daily_activity.get("steps")
            result["calories_total"] = daily_activity.get("calories", 0)
            result["active_min"]     = daily_activity.get("active_minutes")
            result["distance_m"]     = (daily_activity.get("distance_km", 0) or 0) * 1000

    return result


async def collect_activities_polar(
    headers: dict,
    polar_user_id: str,
    target_date: date,
) -> list[dict]:
    """
    Collecte les séances sportives Polar pour un jour donné.
    Retourne une liste de dicts compatibles avec la table activities.
    """
    activities = []

    async with httpx.AsyncClient(timeout=15) as client:
        # Liste les exercices disponibles
        exercises = await _get(
            client,
            f"{POLAR_API_BASE}/exercises",
            headers,
        )

        if not exercises or "items" not in exercises:
            return activities

        for exercise in exercises.get("items", []):
            # Filtre par date
            ex_date = exercise.get("start_time", "")[:10]
            if ex_date != target_date.strftime("%Y-%m-%d"):
                continue

            # Récupère les détails
            exercise_id = exercise.get("id", "")
            detail = await _get(
                client,
                f"{POLAR_API_BASE}/exercises/{exercise_id}",
                headers,
            )
            if not detail:
                continue

            duration_sec = detail.get("duration", "PT0S")
            # Parse ISO 8601 duration (PT1H30M → 90 min)
            duration_min = _parse_iso_duration(duration_sec)

            activities.append({
                "activity_id":     int(exercise_id) if exercise_id.isdigit() else hash(exercise_id),
                "activity_name":   detail.get("sport", "Unknown"),
                "activity_type":   detail.get("sport", "").lower().replace(" ", "_"),
                "start_time":      detail.get("start_time", ""),
                "duration_min":    duration_min,
                "distance_km":     (detail.get("distance", 0) or 0) / 1000,
                "avg_hr":          detail.get("heart_rate", {}).get("average"),
                "max_hr":          detail.get("heart_rate", {}).get("maximum"),
                "calories":        detail.get("calories"),
                "avg_speed_kmh":   (detail.get("speed", {}).get("avg", 0) or 0) * 3.6,
                "elevation_gain_m": detail.get("ascent", 0),
                "training_effect": detail.get("training_load", {}).get("cardio_load"),
                "vo2max":          detail.get("vo2max"),
            })

    return activities


def _parse_iso_duration(duration_str: str) -> float:
    """Parse ISO 8601 duration string (ex: PT1H30M45S) → minutes."""
    if not duration_str or not duration_str.startswith("PT"):
        return 0.0
    duration_str = duration_str[2:]  # Retire "PT"
    hours = minutes = seconds = 0
    current = ""
    for char in duration_str:
        if char.isdigit() or char == ".":
            current += char
        elif char == "H":
            hours = float(current) if current else 0
            current = ""
        elif char == "M":
            minutes = float(current) if current else 0
            current = ""
        elif char == "S":
            seconds = float(current) if current else 0
            current = ""
    return hours * 60 + minutes + seconds / 60