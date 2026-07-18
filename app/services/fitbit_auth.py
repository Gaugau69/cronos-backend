"""
app/services/fitbit_auth.py — OAuth Fitbit Web API et gestion des tokens.

Tokens: access_token expire en 8h, refresh_token long-lived.
"""

import base64
import json
import logging
from urllib.parse import urlencode

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import User

log = logging.getLogger(__name__)

FITBIT_AUTH_URL  = "https://www.fitbit.com/oauth2/authorize"
FITBIT_TOKEN_URL = "https://api.fitbit.com/oauth2/token"
FITBIT_API_BASE  = "https://api.fitbit.com"


def get_fitbit_auth_url(state: str) -> str:
    params = {
        "response_type": "code",
        "client_id":     settings.fitbit_client_id,
        "redirect_uri":  settings.fitbit_redirect_uri,
        "scope":         "activity heartrate sleep profile oxygen_saturation respiratory_rate",
        "state":         state,
        "code_challenge_method": "none",
    }
    return f"{FITBIT_AUTH_URL}?{urlencode(params)}"


def _fitbit_basic_auth() -> str:
    """Fitbit exige Basic Auth sur l'endpoint token."""
    creds = f"{settings.fitbit_client_id}:{settings.fitbit_client_secret}"
    return "Basic " + base64.b64encode(creds.encode()).decode()


async def exchange_code_for_token(code: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            FITBIT_TOKEN_URL,
            data={
                "grant_type":   "authorization_code",
                "code":         code,
                "redirect_uri": settings.fitbit_redirect_uri,
            },
            headers={
                "Authorization":  _fitbit_basic_auth(),
                "Content-Type":   "application/x-www-form-urlencoded",
            },
        )
        resp.raise_for_status()
        return resp.json()


async def save_fitbit_token(db: AsyncSession, peakflow_email: str, fitbit_email: str, token_data: dict) -> bool:
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    try:
        token = {
            "provider":      "fitbit",
            "access_token":  token_data.get("access_token"),
            "refresh_token": token_data.get("refresh_token"),
            "user_id":       token_data.get("user_id", ""),
        }
        token_json = json.dumps(token)
        existing = (await db.execute(select(User).where(User.email == peakflow_email))).scalar_one_or_none()
        if existing:
            await db.execute(
                update(User).where(User.email == peakflow_email)
                .values(token_json=token_json, watch_email=fitbit_email, name=peakflow_email)
            )
        else:
            await db.execute(
                pg_insert(User)
                .values(name=peakflow_email, email=peakflow_email, token_json=token_json, watch_email=fitbit_email)
                .on_conflict_do_update(index_elements=["email"], set_={"token_json": token_json, "watch_email": fitbit_email})
            )
        await db.commit()
        log.info(f"✓ Token Fitbit sauvegardé pour {peakflow_email}")
        return True
    except Exception as e:
        log.error(f"✗ Erreur sauvegarde token Fitbit pour {peakflow_email}: {e}")
        return False


async def refresh_fitbit_token(db: AsyncSession, user: User) -> dict | None:
    """Rafraîchit l'access_token Fitbit (expire toutes les 8h)."""
    try:
        token_data = json.loads(user.token_json)
        refresh_token = token_data.get("refresh_token")
        if not refresh_token:
            return None
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                FITBIT_TOKEN_URL,
                data={
                    "grant_type":    "refresh_token",
                    "refresh_token": refresh_token,
                },
                headers={
                    "Authorization": _fitbit_basic_auth(),
                    "Content-Type":  "application/x-www-form-urlencoded",
                },
            )
            resp.raise_for_status()
            body = resp.json()
            new_token = {
                **token_data,
                "access_token":  body["access_token"],
                "refresh_token": body.get("refresh_token", refresh_token),
            }
            await db.execute(update(User).where(User.id == user.id).values(token_json=json.dumps(new_token)))
            await db.commit()
            log.info(f"[{user.name}] Token Fitbit rafraîchi ✓")
            return new_token
    except Exception as e:
        log.error(f"[{user.name}] Refresh token Fitbit échoué: {e}")
        return None


async def get_fitbit_headers(user: User, refreshed_token: dict | None = None) -> dict | None:
    if not user.token_json:
        return None
    try:
        token_data = refreshed_token or json.loads(user.token_json)
        if token_data.get("provider") != "fitbit":
            return None
        return {"Authorization": f"Bearer {token_data['access_token']}"}
    except Exception:
        return None
