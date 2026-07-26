from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from app.config import settings
import httpx
import random
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/route", tags=["route"])

GRAPHHOPPER_API_KEY = settings.graphhopper_api_key
GRAPHHOPPER_URL = "https://graphhopper.com/api/1/route"


class LoopResponse(BaseModel):
    coordinates: list[list[float]]
    distance_km: float
    duration_min: float
    profile: str


@router.get("/loop", response_model=LoopResponse)
async def generate_loop(
    lat: float = Query(...),
    lng: float = Query(...),
    distance_km: float = Query(..., gt=0, le=100),
    profile: str = Query("foot", regex="^(foot|bike)$"),
    seed: int = Query(0),
):
    """Génère une boucle via l'algorithme round_trip de GraphHopper.

    L'algo round_trip prend une distance cible en mètres et gère lui-même
    la topographie — pas de biais selon le terrain (montagne, plaine, etc.).
    """
    if not GRAPHHOPPER_API_KEY:
        raise HTTPException(500, "GRAPHHOPPER_API_KEY non configurée")

    rng = random.Random(seed if seed else random.randint(1, 100000))
    rt_seed = rng.randint(1, 999999)

    params = [
        ("profile", profile),
        ("point", f"{lat},{lng}"),
        ("algorithm", "round_trip"),
        ("round_trip.distance", int(distance_km * 1000)),
        ("round_trip.seed", rt_seed),
        ("points_encoded", "false"),
        ("instructions", "false"),
        ("key", GRAPHHOPPER_API_KEY),
    ]

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(GRAPHHOPPER_URL, params=params)
    except httpx.RequestError as e:
        logger.error(f"GraphHopper request failed: {e}")
        raise HTTPException(503, "Service de routing indisponible")

    if r.status_code != 200:
        logger.warning(f"GraphHopper returned {r.status_code}: {r.text[:200]}")
        raise HTTPException(r.status_code, f"GraphHopper error: {r.text[:200]}")

    data = r.json()
    paths = data.get("paths", [])
    if not paths:
        raise HTTPException(404, "Aucun parcours trouvé")

    path = paths[0]
    coords = [[pt[1], pt[0]] for pt in path["points"]["coordinates"]]

    return LoopResponse(
        coordinates=coords,
        distance_km=round(path["distance"] / 1000, 2),
        duration_min=round(path["time"] / 60000, 1),
        profile=profile,
    )