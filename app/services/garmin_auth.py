"""
app/services/garmin_auth.py — Login Garmin et gestion des tokens OAuth.
"""

import json
import logging

from garminconnect import Garmin, GarminConnectAuthenticationError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import User

log = logging.getLogger(__name__)


async def login_and_save_token(db: AsyncSession, name: str, email: str, password: str) -> bool:
    """
    Login Garmin avec email + password.
    Sauvegarde le token en DB. Le mot de passe n'est jamais persisté.
    """
    try:
        api = Garmin(email, password)
        api.login()

        stmt = (
            pg_insert(User)
            .values(name=name, email=email, token_json=json.dumps(api.garth.dump()))
            .on_conflict_do_update(
                index_elements=["name"],
                set_={"email": email, "token_json": json.dumps(api.garth.dump())},
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
    Rafraîchit et re-sauvegarde le token si nécessaire (~1 an de validité).
    """
    if not user.token_json:
        log.error(f"No token for {user.name}")
        return None
    try:
        token_data = json.loads(user.token_json)
        api = Garmin(user.email, "")
        api.login(token_data)

        new_token = api.garth.dump()
        if new_token != token_data:
            user.token_json = json.dumps(new_token)
            await db.commit()
            log.info(f"Token refreshed for {user.name}")

        return api
    except Exception as e:
        log.error(f"Token reconnect failed for {user.name}: {e}")
        return None
