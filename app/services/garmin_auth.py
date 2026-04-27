"""
app/services/garmin_auth.py — Login Garmin et gestion des tokens OAuth.
Compatible garminconnect >= 0.3.x
"""

import json
import logging
import pickle
import base64

from garminconnect import Garmin, GarminConnectAuthenticationError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import User

log = logging.getLogger(__name__)


def _dump_token(api: Garmin) -> str:
    """Sérialise la session Garmin en JSON string pour stockage DB."""
    # garminconnect 0.3.x utilise api.client pour la session
    try:
        # Tentative garth (ancienne API)
        return json.dumps(api.garth.dump())
    except AttributeError:
        pass
    try:
        # Nouvelle API 0.3.x — sérialise le client via pickle encodé en base64
        token_data = {
            "version": "0.3",
            "client": base64.b64encode(pickle.dumps(api.client)).decode("utf-8"),
            "username": getattr(api, "username", ""),
            "display_name": getattr(api, "display_name", ""),
        }
        return json.dumps(token_data)
    except Exception as e:
        log.warning(f"Impossible de sérialiser le token: {e}")
        return json.dumps({"version": "0.3", "client": "", "username": ""})


def _load_api(token_json: str, email: str) -> Garmin | None:
    """Reconstruit une instance Garmin depuis le token stocké."""
    try:
        token_data = json.loads(token_json)

        # Ancienne API (garth)
        if "version" not in token_data:
            api = Garmin(email, "")
            api.login(token_data)
            return api

        # Nouvelle API 0.3.x
        if token_data.get("version") == "0.3" and token_data.get("client"):
            api = Garmin(email, "")
            api.client = pickle.loads(base64.b64decode(token_data["client"]))
            return api

    except Exception as e:
        log.error(f"Erreur reconstruction API: {e}")
    return None


async def login_and_save_token(db: AsyncSession, name: str, email: str, password: str) -> bool:
    """
    Login Garmin avec email + password.
    Sauvegarde le token en DB. Le mot de passe n'est jamais persisté.
    """
    try:
        api = Garmin(email, password)
        api.login()

        token_json = _dump_token(api)

        stmt = (
            pg_insert(User)
            .values(name=name, email=email, token_json=token_json)
            .on_conflict_do_update(
                index_elements=["name"],
                set_={"email": email, "token_json": token_json},
            )
        )
        await db.execute(stmt)
        await db.commit()
        log.info(f"✓ Token saved for {name}")
        return True

    except GarminConnectAuthenticationError:
        log.error(f"✗ Auth failed for {name}")
        return False
    except Exception as e:
        log.error(f"✗ Garmin login error for {name}: {e}")
        return False


async def get_api(db: AsyncSession, user: User) -> Garmin | None:
    """
    Reconstruit une session Garmin depuis le token stocké en DB.
    """
    if not user.token_json:
        log.error(f"No token for {user.name}")
        return None

    api = _load_api(user.token_json, user.email)
    if api is None:
        log.error(f"Token invalide pour {user.name}")
    return api
