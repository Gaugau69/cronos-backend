"""
app/services/heatmap.py — Récupération des tracks GPS Garmin pour la heatmap.

Le service :
  1. Récupère les activités récentes dont on n'a pas encore le track
  2. Télécharge le GPX depuis Garmin Connect
  3. Parse les coordonnées (lat, lng) du GPX
  4. Stocke en base (activity_tracks) avec dédoublonnage
"""

import json
import logging
from datetime import date, timedelta
from xml.etree import ElementTree as ET

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Activity, ActivityTrack, User

logger = logging.getLogger(__name__)

GPX_NS = "{http://www.topografix.com/GPX/1/1}"


def _parse_gpx_coordinates(gpx_xml: str) -> list[list[float]]:
    """Extrait les [lat, lng] d'un fichier GPX (string)."""
    try:
        root = ET.fromstring(gpx_xml)
        coords = []
        for trkpt in root.iter(f"{GPX_NS}trkpt"):
            lat = trkpt.get("lat")
            lon = trkpt.get("lon")
            if lat and lon:
                coords.append([round(float(lat), 5), round(float(lon), 5)])
        # Échantillonnage : max 200 points pour limiter la taille stockée
        if len(coords) > 200:
            step = len(coords) // 200
            coords = coords[::step]
        return coords
    except Exception as e:
        logger.warning(f"Erreur parsing GPX : {e}")
        return []


async def fetch_and_store_tracks(
    db: AsyncSession,
    user: User,
    days: int = 90,
) -> int:
    """
    Récupère et stocke les tracks GPS des activités récentes.
    Retourne le nombre de nouveaux tracks enregistrés.
    """
    if not user.token_json:
        return 0

    # Activités running récentes
    since = date.today() - timedelta(days=days)
    activities = (await db.execute(
        select(Activity)
        .where(Activity.user_id == user.id)
        .where(Activity.date >= since)
        .where(Activity.activity_type.in_(["running", "trail_running", "treadmill_running"]))
        .order_by(Activity.date.desc())
    )).scalars().all()

    if not activities:
        return 0

    # Tracks déjà stockés
    existing_ids = set(
        row[0] for row in (await db.execute(
            select(ActivityTrack.activity_id)
            .where(ActivityTrack.user_id == user.id)
        )).all()
    )

    missing = [a for a in activities if a.activity_id not in existing_ids]
    if not missing:
        return 0

    # Connexion Garmin
    try:
        import garth
        client = garth.Client()
        client.loads(user.token_json)
    except Exception as e:
        logger.error(f"Garmin auth failed for {user.name}: {e}")
        return 0

    stored = 0
    for act in missing[:20]:   # max 20 par appel pour éviter le rate-limiting
        try:
            gpx_data = client.download(
                f"/proxy/download-service/files/activity/{act.activity_id}"
            )
            if isinstance(gpx_data, bytes):
                gpx_str = gpx_data.decode("utf-8", errors="replace")
            else:
                gpx_str = str(gpx_data)

            coords = _parse_gpx_coordinates(gpx_str)
            if not coords:
                continue

            db.add(ActivityTrack(
                user_id          = user.id,
                activity_id      = act.activity_id,
                coordinates_json = json.dumps(coords),
            ))
            stored += 1

        except Exception as e:
            logger.debug(f"Track fetch failed for activity {act.activity_id}: {e}")
            continue

    if stored:
        await db.commit()
        logger.info(f"{stored} nouveaux tracks stockés pour {user.name}")

    return stored


async def get_heatmap_points(
    db: AsyncSession,
    user: User,
    days: int = 90,
) -> list[list[float]]:
    """
    Retourne tous les points [lat, lng] des tracks récents pour la heatmap.
    """
    since = date.today() - timedelta(days=days)

    # Activités dans la fenêtre
    act_ids = set(
        row[0] for row in (await db.execute(
            select(Activity.activity_id)
            .where(Activity.user_id == user.id)
            .where(Activity.date >= since)
        )).all()
    )

    if not act_ids:
        return []

    tracks = (await db.execute(
        select(ActivityTrack)
        .where(ActivityTrack.user_id == user.id)
        .where(ActivityTrack.activity_id.in_(act_ids))
    )).scalars().all()

    all_points: list[list[float]] = []
    for t in tracks:
        try:
            all_points.extend(json.loads(t.coordinates_json))
        except Exception:
            pass

    return all_points
