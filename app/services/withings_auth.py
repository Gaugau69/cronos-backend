"""
app/services/withings_auth.py — OAuth Withings et gestion des tokens.
"""

import json
import logging
from urllib.parse import urlencode

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import User

log = logging.getLogger(__name__)

WITHINGS_AUTH_URL  = "https://account.withings.com/oauth2_user/authorize2"
WITHINGS_TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
WITHINGS_API_BASE  = "https://wbsapi.withings.net"


def get_withings_auth_url(state: str) -> str:
    params = {
        "response_type": "code",
        "client_id":     settings.withings_client_id,
        "redirect_uri":  settings.withings_redirect_uri,
        "scope":         "user.activity,user.sleepevents,user.metrics,user.heartrate",
        "state":         state,
    }
    return f"{WITHINGS_AUTH_URL}?{urlencode(params)}"


async def exchange_code_for_token(code: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            WITHINGS_TOKEN_URL,
            data={
                "action":        "requesttoken",
                "grant_type":    "authorization_code",
                "client_id":     settings.withings_client_id,
                "client_secret": settings.withings_client_secret,
                "code":          code,
                "redirect_uri":  settings.withings_redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != 0:
            raise Exception(f"Withings token error: {data}")
        return data.get("body", {})


async def save_withings_token(db: AsyncSession, name: str, email: str, token_data: dict) -> bool:
    try:
        token = {
            "provider":      "withings",
            "access_token":  token_data.get("access_token"),
            "refresh_token": token_data.get("refresh_token"),
            "userid":        str(token_data.get("userid", "")),
        }
        stmt = (
            pg_insert(User)
            .values(name=name, email=email, token_json=json.dumps(token))
            .on_conflict_do_update(
                index_elements=["name"],
                set_={"email": email, "token_json": json.dumps(token)},
            )
        )
        await db.execute(stmt)
        await db.commit()
        log.info(f"✓ Token Withings sauvegardé pour {name}")
        return True
    except Exception as e:
        log.error(f"✗ Erreur sauvegarde token Withings: {e}")
        return False


async def get_withings_headers(user: User) -> dict | None:
    if not user.token_json:
        return None
    try:
        token_data = json.loads(user.token_json)
        if token_data.get("provider") != "withings":
            return None
        return {
            "Authorization": f"Bearer {token_data['access_token']}",
            "Content-Type":  "application/x-www-form-urlencoded",
        }
    except Exception:
        return None


def get_withings_userid(user: User) -> str | None:
    try:
        return json.loads(user.token_json).get("userid")
    except Exception:
        return None
