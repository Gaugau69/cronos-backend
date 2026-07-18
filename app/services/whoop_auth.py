"""
app/services/whoop_auth.py — OAuth WHOOP v1 et gestion des tokens.

Tokens: access_token expire en 1h, refresh_token expire en 30j.
"""

import json
import logging
from urllib.parse import urlencode

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import User

log = logging.getLogger(__name__)

WHOOP_AUTH_URL  = "https://api.prod.whoop.com/oauth/oauth2/auth"
WHOOP_TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
WHOOP_API_BASE  = "https://api.prod.whoop.com/developer/v1"


def get_whoop_auth_url(state: str) -> str:
    params = {
        "response_type": "code",
        "client_id":     settings.whoop_client_id,
        "redirect_uri":  settings.whoop_redirect_uri,
        "scope":         "read:sleep read:recovery read:cycles read:workout offline",
        "state":         state,
    }
    return f"{WHOOP_AUTH_URL}?{urlencode(params)}"


async def exchange_code_for_token(code: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            WHOOP_TOKEN_URL,
            data={
                "grant_type":    "authorization_code",
                "code":          code,
                "redirect_uri":  settings.whoop_redirect_uri,
                "client_id":     settings.whoop_client_id,
                "client_secret": settings.whoop_client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        return resp.json()


async def save_whoop_token(db: AsyncSession, peakflow_email: str, whoop_email: str, token_data: dict) -> bool:
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    try:
        token = {
            "provider":      "whoop",
            "access_token":  token_data.get("access_token"),
            "refresh_token": token_data.get("refresh_token"),
            "token_type":    token_data.get("token_type", "Bearer"),
        }
        token_json = json.dumps(token)
        existing = (await db.execute(select(User).where(User.email == peakflow_email))).scalar_one_or_none()
        if existing:
            await db.execute(
                update(User).where(User.email == peakflow_email)
                .values(token_json=token_json, watch_email=whoop_email, name=peakflow_email)
            )
        else:
            await db.execute(
                pg_insert(User)
                .values(name=peakflow_email, email=peakflow_email, token_json=token_json, watch_email=whoop_email)
                .on_conflict_do_update(index_elements=["email"], set_={"token_json": token_json, "watch_email": whoop_email})
            )
        await db.commit()
        log.info(f"✓ Token WHOOP sauvegardé pour {peakflow_email}")
        return True
    except Exception as e:
        log.error(f"✗ Erreur sauvegarde token WHOOP pour {peakflow_email}: {e}")
        return False


async def refresh_whoop_token(db: AsyncSession, user: User) -> dict | None:
    """Rafraîchit l'access_token WHOOP (expire toutes les heures)."""
    try:
        token_data = json.loads(user.token_json)
        refresh_token = token_data.get("refresh_token")
        if not refresh_token:
            return None
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                WHOOP_TOKEN_URL,
                data={
                    "grant_type":    "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id":     settings.whoop_client_id,
                    "client_secret": settings.whoop_client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
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
            log.info(f"[{user.name}] Token WHOOP rafraîchi ✓")
            return new_token
    except Exception as e:
        log.error(f"[{user.name}] Refresh token WHOOP échoué: {e}")
        return None


async def get_whoop_headers(user: User, refreshed_token: dict | None = None) -> dict | None:
    if not user.token_json:
        return None
    try:
        token_data = refreshed_token or json.loads(user.token_json)
        if token_data.get("provider") != "whoop":
            return None
        return {"Authorization": f"Bearer {token_data['access_token']}"}
    except Exception:
        return None
