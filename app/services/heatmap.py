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
        # Échantillonnage : max 500 points par parcours
        if len(coords) > 500:
            step = len(coords) // 500
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
        .where(Activity.activity_type.ilike("%running%"))
        .order_by(Activity.date.desc())
    )).scalars().all()

    logger.info(f"[{user.name}] {len(activities)} activités running trouvées depuis {since}")
    if not activities:
        return 0

    # Tracks déjà stockés
    existing_ids = set(
        row[0] for row in (await db.execute(
            select(ActivityTrack.activity_id)
            .where(ActivityTrack.user_id == user.id)
        )).all()
    )
    logger.info(f"[{user.name}] {len(existing_ids)} tracks déjà en DB, {len(activities)} activités")

    missing = [a for a in activities if a.activity_id not in existing_ids]
    logger.info(f"[{user.name}] {len(missing)} tracks manquants à télécharger")
    if not missing:
        return 0

    # Connexion Garmin — force un re-login frais si credentials disponibles
    from app.services.garmin_auth import get_api, _relogin
    from garminconnect import Garmin

    logger.info(f"[{user.name}] garmin_email={bool(user.garmin_email)} password_enc={bool(user.garmin_password_enc)}")
    api = None
    if user.garmin_email and user.garmin_password_enc:
        api = await _relogin(db, user)
        logger.info(f"[{user.name}] _relogin → api={'OK' if api else 'None'}")
    if api is None:
        api = await get_api(db, user, auto_relogin=False)
        logger.info(f"[{user.name}] get_api → api={'OK' if api else 'None'}")
    if api is None:
        logger.error(f"Garmin auth failed for {user.name}")
        return 0

    stored = 0
    for act in missing[:20]:   # max 20 par appel pour éviter le rate-limiting
        try:
            gpx_data = api.download_activity(
                act.activity_id, dl_fmt=Garmin.ActivityDownloadFormat.GPX
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
) -> tuple[list[list[float]], list[list[list[float]]]]:
    """
    Retourne (points_flat, tracks) où tracks est la liste des parcours séparés.
    points_flat conservé pour compatibilité ; tracks permet de tracer des polylines précises.
    """
    since = date.today() - timedelta(days=days)

    act_ids = set(
        row[0] for row in (await db.execute(
            select(Activity.activity_id)
            .where(Activity.user_id == user.id)
            .where(Activity.date >= since)
        )).all()
    )

    if not act_ids:
        return [], []

    db_tracks = (await db.execute(
        select(ActivityTrack)
        .where(ActivityTrack.user_id == user.id)
        .where(ActivityTrack.activity_id.in_(act_ids))
    )).scalars().all()

    all_points: list[list[float]] = []
    all_tracks: list[list[list[float]]] = []
    for t in db_tracks:
        try:
            coords = json.loads(t.coordinates_json)
            if coords:
                all_points.extend(coords)
                all_tracks.append(coords)
        except Exception:
            pass

    return all_points, all_tracks
