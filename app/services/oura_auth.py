"""
app/services/oura_auth.py — OAuth Oura Ring v2 et gestion des tokens.

Tokens: access_token expire en ~1j, refresh_token long-lived.
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

OURA_AUTH_URL  = "https://cloud.ouraring.com/oauth/authorize"
OURA_TOKEN_URL = "https://api.ouraring.com/oauth/token"
OURA_API_BASE  = "https://api.ouraring.com/v2"


def get_oura_auth_url(state: str) -> str:
    params = {
        "response_type": "code",
        "client_id":     settings.oura_client_id,
        "redirect_uri":  settings.oura_redirect_uri,
        "scope":         "daily email heartrate workout personal",
        "state":         state,
    }
    return f"{OURA_AUTH_URL}?{urlencode(params)}"


async def exchange_code_for_token(code: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            OURA_TOKEN_URL,
            data={
                "grant_type":    "authorization_code",
                "code":          code,
                "redirect_uri":  settings.oura_redirect_uri,
                "client_id":     settings.oura_client_id,
                "client_secret": settings.oura_client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        return resp.json()


async def save_oura_token(db: AsyncSession, peakflow_email: str, oura_email: str, token_data: dict) -> bool:
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    try:
        token = {
            "provider":      "oura",
            "access_token":  token_data.get("access_token"),
            "refresh_token": token_data.get("refresh_token"),
            "token_type":    token_data.get("token_type", "Bearer"),
        }
        token_json = json.dumps(token)
        existing = (await db.execute(select(User).where(User.email == peakflow_email))).scalar_one_or_none()
        if existing:
            await db.execute(
                update(User).where(User.email == peakflow_email)
                .values(token_json=token_json, watch_email=oura_email, name=peakflow_email)
            )
        else:
            await db.execute(
                pg_insert(User)
                .values(name=peakflow_email, email=peakflow_email, token_json=token_json, watch_email=oura_email)
                .on_conflict_do_update(index_elements=["email"], set_={"token_json": token_json, "watch_email": oura_email})
            )
        await db.commit()
        log.info(f"✓ Token Oura sauvegardé pour {peakflow_email}")
        return True
    except Exception as e:
        log.error(f"✗ Erreur sauvegarde token Oura pour {peakflow_email}: {e}")
        return False


async def refresh_oura_token(db: AsyncSession, user: User) -> dict | None:
    """Rafraîchit l'access_token Oura via le refresh_token."""
    try:
        token_data = json.loads(user.token_json)
        refresh_token = token_data.get("refresh_token")
        if not refresh_token:
            return None
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                OURA_TOKEN_URL,
                data={
                    "grant_type":    "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id":     settings.oura_client_id,
                    "client_secret": settings.oura_client_secret,
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
            log.info(f"[{user.name}] Token Oura rafraîchi ✓")
            return new_token
    except Exception as e:
        log.error(f"[{user.name}] Refresh token Oura échoué: {e}")
        return None


async def get_oura_headers(user: User, refreshed_token: dict | None = None) -> dict | None:
    if not user.token_json:
        return None
    try:
        token_data = refreshed_token or json.loads(user.token_json)
        if token_data.get("provider") != "oura":
            return None
        return {"Authorization": f"Bearer {token_data['access_token']}"}
    except Exception:
        return None
