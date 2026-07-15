"""
app/services/coros_auth.py — Auth COROS Training Hub API.

Login email + MD5(password). Token valide 24h, re-login automatique.
Stockage dans token_json : {"provider": "coros", "email", "password_enc",
                            "access_token", "user_id", "region", "token_timestamp"}
"""

import hashlib
import json
import logging
import time

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import User
from app.services.garmin_auth import decrypt_password, encrypt_password

log = logging.getLogger(__name__)

BASE_URLS = {
    "eu":   "https://teameuapi.coros.com",
    "us":   "https://teamapi.coros.com",
    "asia": "https://teamcnapi.coros.com",
}
TOKEN_TTL_S = 23 * 3600  # re-login après 23h


def _base_url(region: str) -> str:
    return BASE_URLS.get(region, BASE_URLS["eu"])


def _md5(value: str) -> str:
    return hashlib.md5(value.encode()).hexdigest()


def _auth_headers(access_token: str, user_id: str) -> dict:
    return {
        "Content-Type": "application/json",
        "accessToken": access_token,
        "yfheader": json.dumps({"userId": user_id}),
    }


async def login_coros(email: str, password: str, region: str = "eu") -> dict:
    """
    Login COROS et retourne {"access_token", "user_id", "region"}.
    Lance une exception en cas d'échec.
    """
    url = _base_url(region) + "/account/login"
    payload = {"account": email, "accountType": 2, "pwd": _md5(password)}

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        body = resp.json()

    if body.get("result") != "0000":
        raise ValueError(f"COROS login failed: {body.get('message', 'unknown error')}")

    data = body.get("data", {})
    return {
        "access_token": data["accessToken"],
        "user_id":      str(data["userId"]),
        "region":       region,
    }


async def save_coros_token(
    db: AsyncSession,
    peakflow_email: str,
    coros_email: str,
    password: str,
    auth_data: dict,
) -> bool:
    """Sauvegarde le token COROS dans token_json pour l'utilisateur PeakFlow."""
    try:
        token_json = json.dumps({
            "provider":        "coros",
            "email":           coros_email,
            "password_enc":    encrypt_password(password) or "",
            "access_token":    auth_data["access_token"],
            "user_id":         auth_data["user_id"],
            "region":          auth_data["region"],
            "token_timestamp": int(time.time()),
        })
        await db.execute(
            update(User).where(User.email == peakflow_email).values(token_json=token_json)
        )
        await db.commit()
        log.info(f"✓ Token COROS sauvegardé pour {peakflow_email}")
        return True
    except Exception as e:
        log.error(f"✗ Sauvegarde token COROS: {e}")
        return False


async def get_coros_auth(user: User, db: AsyncSession | None = None) -> tuple[str, str, str]:
    """
    Retourne (access_token, user_id, region) depuis le token_json.
    Re-login automatique si token expiré (> 23h).
    """
    if not user.token_json:
        raise ValueError("Pas de token COROS pour cet utilisateur")

    data = json.loads(user.token_json)
    if data.get("provider") != "coros":
        raise ValueError("Provider n'est pas COROS")

    access_token    = data["access_token"]
    user_id         = data["user_id"]
    region          = data.get("region", "eu")
    token_timestamp = data.get("token_timestamp", 0)
    password_enc    = data.get("password_enc", "")

    # Re-login si token expiré
    if int(time.time()) - token_timestamp > TOKEN_TTL_S and password_enc:
        password = decrypt_password(password_enc)
        if password:
            try:
                auth = await login_coros(data["email"], password, region)
                access_token = auth["access_token"]
                user_id      = auth["user_id"]
                # Mettre à jour le token en DB
                if db:
                    new_token = {**data, "access_token": access_token, "token_timestamp": int(time.time())}
                    await db.execute(
                        update(User).where(User.id == user.id)
                        .values(token_json=json.dumps(new_token))
                    )
                    await db.commit()
                log.info(f"[{user.name}] Token COROS renouvelé")
            except Exception as e:
                log.warning(f"[{user.name}] Re-login COROS échoué: {e}")

    return access_token, user_id, region
