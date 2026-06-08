"""
app/services/garmin_auth.py — Login Garmin et gestion des tokens OAuth.
Compatible garminconnect >= 0.3.x

Re-login automatique en cas de 401 si credentials chiffrés disponibles.
"""

import json
import logging
import os
import pickle
import base64

from cryptography.fernet import Fernet
from garminconnect import Garmin, GarminConnectAuthenticationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import User

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Chiffrement credentials
# ─────────────────────────────────────────────────────────────

def _get_fernet() -> Fernet | None:
    from app.config import settings
    key = settings.garmin_encryption_key or os.environ.get("GARMIN_ENCRYPTION_KEY", "")
    if not key:
        log.warning("GARMIN_ENCRYPTION_KEY non défini — stockage credentials désactivé")
        return None
    try:
        return Fernet(key.encode())
    except Exception as e:
        log.error(f"Clé de chiffrement invalide : {e}")
        return None


def encrypt_password(password: str) -> str | None:
    f = _get_fernet()
    if not f:
        return None
    return f.encrypt(password.encode()).decode()


def decrypt_password(encrypted: str) -> str | None:
    f = _get_fernet()
    if not f:
        return None
    try:
        return f.decrypt(encrypted.encode()).decode()
    except Exception as e:
        log.error(f"Déchiffrement impossible : {e}")
        return None


# ─────────────────────────────────────────────────────────────
# Helpers token
# ─────────────────────────────────────────────────────────────

def _extract_display_name_from_token(token_data: dict) -> str:
    if token_data.get("display_name"):
        return token_data["display_name"]

    di_token = token_data.get("di_token", "")
    if di_token:
        try:
            payload = di_token.split(".")[1]
            payload += "=" * (4 - len(payload) % 4)
            decoded = json.loads(base64.b64decode(payload))
            uuid = decoded.get("sub") or decoded.get("clientId") or decoded.get("clid", "")
            if uuid:
                log.info(f"display_name extrait du JWT: {uuid}")
                return uuid
        except Exception as e:
            log.warning(f"Impossible d'extraire display_name du JWT: {e}")
    return ""


def _dump_token(api: Garmin) -> str:
    try:
        return json.dumps(api.garth.dump())
    except AttributeError:
        pass
    try:
        token_data = {
            "version":      "0.3",
            "client":       base64.b64encode(pickle.dumps(api.client)).decode("utf-8"),
            "username":     getattr(api, "username", ""),
            "display_name": getattr(api, "display_name", ""),
        }
        return json.dumps(token_data)
    except Exception as e:
        log.warning(f"Impossible de sérialiser le token: {e}")
        return json.dumps({"version": "0.3", "client": "", "username": ""})


def _load_api(token_json: str, email: str) -> Garmin | None:
    try:
        token_data = json.loads(token_json)

        if "version" not in token_data:
            api = Garmin(email, "")
            api.login(token_data)
            return api

        if token_data.get("version") == "0.3" and token_data.get("client"):
            api = Garmin(email, "")
            api.client = pickle.loads(base64.b64decode(token_data["client"]))
            try:
                api.client._refresh_di_token()
                log.info(f"Token pickle rafraîchi pour {email}")
            except Exception as e:
                log.warning(f"Refresh token pickle échoué pour {email}: {e}")
            display_name = _extract_display_name_from_token(token_data)
            if display_name:
                api.display_name = display_name
                log.info(f"display_name restauré : {display_name}")
            return api

        if token_data.get("version") == "0.3" and token_data.get("client_dump"):
            api = Garmin(email, "")
            api.client.loads(token_data["client_dump"])
            display_name = token_data.get("display_name", "")
            if display_name:
                api.display_name = display_name
                log.info(f"display_name restauré (dumps) : {display_name}")
            return api

    except Exception as e:
        log.error(f"Erreur reconstruction API: {e}")
    return None


# ─────────────────────────────────────────────────────────────
# Re-login automatique
# ─────────────────────────────────────────────────────────────

async def _relogin(db: AsyncSession, user: User) -> Garmin | None:
    """
    Re-login automatique depuis les credentials chiffrés stockés.
    Appelé quand un 401 est détecté lors de la collecte.
    """
    if not user.garmin_email or not user.garmin_password_enc:
        log.warning(f"[{user.name}] Pas de credentials stockés — re-login impossible")
        return None

    password = decrypt_password(user.garmin_password_enc)
    if not password:
        log.error(f"[{user.name}] Déchiffrement du mot de passe échoué")
        return None

    log.info(f"[{user.name}] Re-login automatique en cours...")
    try:
        api = Garmin(user.garmin_email, password)
        api.login()
        token_json = _dump_token(api)

        # Met à jour le token en DB
        await db.execute(
            pg_insert(User)
            .values(name=user.name, email=user.email, token_json=token_json)
            .on_conflict_do_update(
                index_elements=["name"],
                set_={"token_json": token_json},
            )
        )
        await db.commit()
        log.info(f"[{user.name}] ✓ Re-login réussi — nouveau token sauvegardé")
        return api

    except GarminConnectAuthenticationError:
        log.error(f"[{user.name}] ✗ Re-login échoué — mauvais identifiants ?")
        return None
    except Exception as e:
        log.error(f"[{user.name}] ✗ Erreur re-login : {e}")
        return None


# ─────────────────────────────────────────────────────────────
# Interface principale
# ─────────────────────────────────────────────────────────────

async def login_and_save_token(
    db: AsyncSession,
    name: str,
    email: str,
    password: str,
    save_credentials: bool = True,
) -> bool:
    """
    Login Garmin avec email + password.
    Sauvegarde le token en DB.
    Si save_credentials=True, stocke aussi les credentials chiffrés
    pour permettre le re-login automatique en cas d'expiration.
    """
    try:
        api = Garmin(email, password)
        api.login()
        token_json = _dump_token(api)

        # Chiffre le mot de passe si demandé
        password_enc = None
        if save_credentials:
            password_enc = encrypt_password(password)
            if password_enc:
                log.info(f"[{name}] Credentials chiffrés sauvegardés")
            else:
                log.warning(f"[{name}] Chiffrement impossible — credentials non stockés")

        values = {
            "name":       name,
            "email":      email,
            "token_json": token_json,
        }
        if password_enc:
            values["garmin_email"]        = email
            values["garmin_password_enc"] = password_enc

        set_dict = {k: v for k, v in values.items() if k != "name"}

        stmt = (
            pg_insert(User)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["name"],
                set_=set_dict,
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


async def get_api(db: AsyncSession, user: User, auto_relogin: bool = True) -> Garmin | None:
    """
    Reconstruit une session Garmin depuis le token stocké en DB.
    Si le token est invalide et que auto_relogin=True,
    tente un re-login automatique depuis les credentials chiffrés.
    """
    if not user.token_json:
        log.error(f"No token for {user.name}")
        return None

    api = _load_api(user.token_json, user.email)
    if api is None:
        log.error(f"Token invalide pour {user.name}")
        if auto_relogin:
            log.info(f"[{user.name}] Tentative de re-login automatique...")
            api = await _relogin(db, user)
    return api


async def get_api_with_relogin(db: AsyncSession, user: User) -> Garmin | None:
    """
    Version avec re-login automatique explicite — à utiliser dans le cron.
    Détecte les 401 et tente un re-login si credentials disponibles.
    """
    return await get_api(db, user, auto_relogin=True)


async def check_and_refresh_tokens(db: AsyncSession) -> None:
    """
    Vérifie tous les tokens au démarrage du cron.
    Si un token est proche de l'expiration, tente un re-login préventif.
    """
    from sqlalchemy import select
    users = (await db.execute(select(User))).scalars().all()

    for user in users:
        if not user.token_json:
            continue
        try:
            token_data = json.loads(user.token_json)
            # Vérifie si le token client_dump est valide en reconstruisant l'API
            api = _load_api(user.token_json, user.email or "")
            if api is None:
                log.warning(f"[{user.name}] Token invalide détecté au démarrage — re-login préventif")
                await _relogin(db, user)
            else:
                # Tente un appel léger pour vérifier si le token est encore actif
                try:
                    api.get_user_summary(date.today().isoformat())
                    log.info(f"[{user.name}] Token valide ✓")
                except Exception as e:
                    if "401" in str(e):
                        log.warning(f"[{user.name}] Token expiré détecté — re-login préventif")
                        await _relogin(db, user)
        except Exception as e:
            log.error(f"[{user.name}] Erreur vérification token : {e}")