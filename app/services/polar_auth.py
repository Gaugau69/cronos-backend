"""
app/services/polar_auth.py — OAuth Polar et gestion des tokens.

Flow OAuth :
  1. GET /auth/polar/login?name=Jean&email=jean@email.com
     → Redirige vers Polar pour autorisation
  2. GET /auth/polar/callback?code=...&state=...
     → Échange le code contre un token
     → Enregistre l'utilisateur en DB
     → Redirige vers page de succès
"""

import json
import logging
from urllib.parse import urlencode

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import User

log = logging.getLogger(__name__)

POLAR_AUTH_URL     = "https://flow.polar.com/oauth2/authorization"
POLAR_TOKEN_URL    = "https://polarremote.com/v2/oauth2/token"
POLAR_API_BASE     = "https://www.polaraccesslink.com/v3"
POLAR_REGISTER_URL = f"{POLAR_API_BASE}/users"


def get_polar_auth_url(state: str) -> str:
    """
    Génère l'URL d'autorisation Polar OAuth2.
    L'utilisateur est redirigé vers cette URL pour autoriser l'accès.
    """
    params = {
        "response_type": "code",
        "client_id":     settings.polar_client_id,
        "redirect_uri":  settings.polar_redirect_uri,
        "scope":         "accesslink.read_all",
        "state":         state,
    }
    return f"{POLAR_AUTH_URL}?{urlencode(params)}"


async def exchange_code_for_token(code: str) -> dict:
    """
    Échange le code OAuth contre un access token Polar.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            POLAR_TOKEN_URL,
            data={
                "grant_type":   "authorization_code",
                "code":         code,
                "redirect_uri": settings.polar_redirect_uri,
            },
            auth=(settings.polar_client_id, settings.polar_client_secret),
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()


async def register_polar_user(access_token: str, polar_user_id: str) -> dict:
    """
    Enregistre l'utilisateur sur l'API Polar AccessLink.
    Doit être fait une seule fois par utilisateur.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            POLAR_REGISTER_URL,
            json={"member-id": polar_user_id},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type":  "application/json",
                "Accept":        "application/json",
            },
        )
        # 409 = déjà enregistré, c'est ok
        if resp.status_code not in (200, 201, 409):
            resp.raise_for_status()
        return resp.json() if resp.status_code != 409 else {}


async def save_polar_token(
    db: AsyncSession,
    peakflow_email: str,
    polar_email: str,
    token_data: dict,
) -> bool:
    """
    Sauvegarde le token Polar en DB, lié au compte PeakFlow via peakflow_email.
    """
    from sqlalchemy import update
    try:
        polar_token = {
            "provider":       "polar",
            "access_token":   token_data.get("access_token"),
            "token_type":     token_data.get("token_type"),
            "polar_user_id":  str(token_data.get("x_user_id", "")),
            "refresh_token":  token_data.get("refresh_token", ""),
        }
        token_json = json.dumps(polar_token)

        existing = (await db.execute(select(User).where(User.email == peakflow_email))).scalar_one_or_none()
        if existing:
            await db.execute(
                update(User).where(User.email == peakflow_email)
                .values(token_json=token_json, watch_email=polar_email, name=peakflow_email)
            )
        else:
            stmt = (
                pg_insert(User)
                .values(name=peakflow_email, email=peakflow_email, token_json=token_json, watch_email=polar_email)
                .on_conflict_do_update(
                    index_elements=["email"],
                    set_={"watch_token_json": token_json, "watch_email": polar_email},
                )
            )
            await db.execute(stmt)
        await db.commit()
        log.info(f"✓ Token Polar sauvegardé pour {peakflow_email}")
        return True
    except Exception as e:
        log.error(f"✗ Erreur sauvegarde token Polar pour {peakflow_email}: {e}")
        return False


async def refresh_polar_token(db: AsyncSession, user: User) -> dict | None:
    """
    Rafraîchit le token Polar via refresh_token OAuth2.
    Retourne le nouveau token_data (merged) ou None si échec/pas de refresh_token.
    """
    if not user.token_json:
        return None
    try:
        token_data = json.loads(user.token_json)
        if token_data.get("provider") != "polar":
            return None
        refresh_token = token_data.get("refresh_token")
        if not refresh_token:
            log.warning(f"[{user.name}] Pas de refresh_token Polar stocké — reconnexion manuelle nécessaire si token expiré")
            return None

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                POLAR_TOKEN_URL,
                data={"grant_type": "refresh_token", "refresh_token": refresh_token},
                auth=(settings.polar_client_id, settings.polar_client_secret),
                headers={"Accept": "application/json"},
            )
            if resp.status_code != 200:
                log.error(f"[{user.name}] Refresh Polar échoué: {resp.status_code} — {resp.text[:200]}")
                return None

            new_token = resp.json()
            updated = {
                **token_data,
                "access_token":  new_token["access_token"],
                "refresh_token": new_token.get("refresh_token", refresh_token),
            }
            await db.execute(
                update(User).where(User.id == user.id).values(token_json=json.dumps(updated))
            )
            await db.commit()
            log.info(f"[{user.name}] ✓ Token Polar rafraîchi")
            return updated

    except Exception as e:
        log.error(f"[{user.name}] Erreur refresh Polar: {e}")
        return None


async def get_polar_api_headers(user: User, refreshed_token: dict | None = None) -> dict | None:
    """
    Retourne les headers d'authentification Polar depuis le token en DB (ou le token rafraîchi).
    """
    try:
        token_data = refreshed_token or (json.loads(user.token_json) if user.token_json else None)
        if not token_data or token_data.get("provider") != "polar":
            return None
        return {
            "Authorization": f"Bearer {token_data['access_token']}",
            "Accept":        "application/json",
        }
    except Exception as e:
        log.error(f"Erreur lecture token Polar pour {user.name}: {e}")
        return None