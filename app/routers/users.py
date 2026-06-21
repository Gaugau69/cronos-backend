"""
app/routers/users.py — Endpoints de gestion des utilisateurs.
"""

import asyncio
import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal, DailyMetric, User, get_db
from app.schemas import UserCreate, UserOut
from app.services.garmin_auth import login_and_save_token, upsert_garmin_user, encrypt_password
from app.dependencies import require_admin

log = logging.getLogger(__name__)
router = APIRouter(prefix="/users", tags=["users"])


# ── Sessions MFA en attente ──────────────────────────────────────────────────

@dataclass
class _MFASession:
    name: str
    email: str
    garmin_email: str
    garmin_password: str
    otp_event: threading.Event = field(default_factory=threading.Event)
    otp_holder: list = field(default_factory=list)
    state: str = "logging_in"   # logging_in | mfa_required | completed | failed
    token_json: str = ""
    error: str = ""

    def get_mfa_code(self) -> str:
        """Appelé par garth mid-login — bloque le thread jusqu'à réception de l'OTP."""
        self.state = "mfa_required"
        if not self.otp_event.wait(timeout=300):
            self.state = "failed"
            raise Exception("Timeout MFA — aucun code reçu dans les 5 minutes")
        if not self.otp_holder:
            self.state = "failed"
            raise Exception("Aucun code OTP fourni")
        return self.otp_holder[0]

_mfa_sessions: dict[str, _MFASession] = {}


class MFAVerify(BaseModel):
    session_id: str
    otp_code: str

BACKFILL_YEARS = 2  # années d'historique récupérées automatiquement après connexion


async def _trigger_historical_backfill(user_id: int, user_name: str) -> None:
    """
    Déclenche automatiquement un backfill historique côté serveur (Railway)
    dès qu'un utilisateur connecte son compte Garmin.
    Vérifie d'abord qu'il n'a pas déjà de données — évite les doublons.
    """
    from app.services.collect import collect_user_range

    async with AsyncSessionLocal() as db:
        # Vérifie si des données existent déjà
        existing = (await db.execute(
            select(DailyMetric).where(DailyMetric.user_id == user_id).limit(1)
        )).scalar_one_or_none()

        if existing:
            log.info(f"[BACKFILL] {user_name} — données existantes, pas de backfill nécessaire")
            return

        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if not user or not user.token_json:
            return

        start = date.today() - timedelta(days=365 * BACKFILL_YEARS)
        end   = date.today() - timedelta(days=1)

        log.info(f"[BACKFILL] Démarrage {user_name} : {start} → {end}")
        try:
            result = await collect_user_range(db, user, start, end)
            log.info(f"[BACKFILL] Terminé {user_name} : {result}")
        except Exception as e:
            log.error(f"[BACKFILL] Erreur {user_name} : {e}")


class UserTokenRegister(BaseModel):
    name: str
    email: EmailStr
    token_json: str
    peakflow_email:  Optional[EmailStr] = None
    garmin_email:    Optional[str] = None
    garmin_password: Optional[str] = None


def _to_out(u: User) -> UserOut:
    return UserOut(
        id=u.id,
        name=u.name or "",
        email=u.email,
        display_name=u.firstname or u.name or u.email,
        created_at=u.created_at,
        has_token=bool(u.token_json),
    )


@router.post("/", status_code=201)
async def register_user(payload: UserCreate, db: AsyncSession = Depends(get_db)):
    """
    Enregistre un user et récupère son token Garmin via email + password.
    Retourne 201 UserOut si succès, 202 {mfa_required, session_id} si A2F requise.
    """
    existing = (await db.execute(select(User).where(User.name == payload.name))).scalar_one_or_none()
    if existing and existing.token_json:
        raise HTTPException(409, f"'{payload.name}' est déjà enregistré avec un token valide.")

    session_id = str(uuid.uuid4())
    session = _MFASession(
        name=payload.name,
        email=payload.email,
        garmin_email=payload.email,
        garmin_password=payload.password,
    )
    _mfa_sessions[session_id] = session

    def _sync_login():
        """Tourne dans un thread — blocking OK, n'affecte pas l'event loop."""
        try:
            from garminconnect import Garmin
            import garth
            # On appelle client.login() directement pour éviter le chargement
            # de profil (_load_user_data) qui rallonge le délai après MFA
            api = Garmin(payload.email, payload.password)
            api.client.login(payload.email, payload.password, prompt_mfa=session.get_mfa_code)
            session.token_json = json.dumps(api.client.dump())
            session.state = "completed"
        except Exception as e:
            if session.state != "completed":
                session.state = "failed"
                session.error = str(e)

    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _sync_login)

    # Attend jusqu'à 10s pour détecter : succès, MFA, ou erreur
    for _ in range(20):
        await asyncio.sleep(0.5)
        if session.state in ("mfa_required", "completed", "failed"):
            break

    if session.state == "completed":
        password_enc = encrypt_password(payload.password)
        user = await upsert_garmin_user(
            db, garmin_username=payload.name, email=payload.email,
            token_json=session.token_json, garmin_email=payload.email,
            garmin_password_enc=password_enc,
        )
        asyncio.create_task(_trigger_historical_backfill(user.id, user.name))
        _mfa_sessions.pop(session_id, None)
        return JSONResponse(status_code=201, content=_to_out(user).model_dump())

    if session.state == "mfa_required":
        return JSONResponse(status_code=202, content={"mfa_required": True, "session_id": session_id})

    _mfa_sessions.pop(session_id, None)
    raise HTTPException(401, session.error or "Authentification Garmin échouée. Vérifier email/mot de passe.")


@router.post("/verify-mfa", status_code=201)
async def verify_mfa(payload: MFAVerify, db: AsyncSession = Depends(get_db)):
    """Fournit l'OTP pour compléter un login Garmin avec A2F."""
    session = _mfa_sessions.get(payload.session_id)
    if not session:
        raise HTTPException(404, "Session expirée ou introuvable — recommence la connexion.")
    if session.state != "mfa_required":
        raise HTTPException(400, "Cette session n'attend pas de code MFA.")

    session.otp_holder.append(payload.otp_code.strip())
    session.otp_event.set()

    # Attend la fin du login (max 90s)
    for _ in range(180):
        await asyncio.sleep(0.5)
        if session.state == "completed":
            password_enc = encrypt_password(session.garmin_password)
            user = await upsert_garmin_user(
                db, garmin_username=session.name, email=session.email,
                token_json=session.token_json, garmin_email=session.garmin_email,
                garmin_password_enc=password_enc,
            )
            asyncio.create_task(_trigger_historical_backfill(user.id, user.name))
            _mfa_sessions.pop(payload.session_id, None)
            return JSONResponse(status_code=201, content=_to_out(user).model_dump())
        if session.state == "failed":
            _mfa_sessions.pop(payload.session_id, None)
            raise HTTPException(401, session.error or "Code MFA incorrect.")

    raise HTTPException(408, "Timeout — le login n'a pas abouti dans les 30 secondes.")


@router.post("/register-token", response_model=UserOut, status_code=201)
async def register_with_token(payload: UserTokenRegister, db: AsyncSession = Depends(get_db)):
    try:
        json.loads(payload.token_json)
    except Exception:
        raise HTTPException(400, "token_json invalide.")

    password_enc = None
    if payload.garmin_password:
        password_enc = encrypt_password(payload.garmin_password)

    # Utilise peakflow_email pour trouver le bon compte PeakFlow si fourni
    lookup_email = str(payload.peakflow_email) if payload.peakflow_email else str(payload.email)
    # L'email PeakFlow sert d'identifiant unique (garmin_username = email)
    garmin_username = lookup_email

    user = await upsert_garmin_user(
        db,
        garmin_username=garmin_username,
        email=lookup_email,
        token_json=payload.token_json,
        garmin_email=payload.garmin_email or str(payload.email),
        garmin_password_enc=password_enc,
    )
    asyncio.create_task(_trigger_historical_backfill(user.id, user.name))
    return _to_out(user)


@router.post("/{name}/backfill", dependencies=[Depends(require_admin)])
async def trigger_backfill(name: str, db: AsyncSession = Depends(get_db)):
    """Déclenche manuellement le backfill 2 ans pour un utilisateur existant."""
    user = (await db.execute(select(User).where(User.name == name))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, f"Utilisateur '{name}' introuvable.")
    asyncio.create_task(_trigger_historical_backfill(user.id, user.name))
    return {"status": "started", "user": name, "message": "Backfill 2 ans lancé en arrière-plan"}


@router.get("/", response_model=list[UserOut], dependencies=[Depends(require_admin)])
async def list_users(db: AsyncSession = Depends(get_db)):
    users = (await db.execute(select(User).order_by(User.created_at))).scalars().all()
    return [_to_out(u) for u in users]


@router.get("/by-email/{email}/status", dependencies=[Depends(require_admin)])
async def get_user_status_by_email(email: str, db: AsyncSession = Depends(get_db)):
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    return {
        "registered":   bool(user),
        "has_token":    bool(user and user.token_json),
        "name":         user.name if user else None,
        "id":           user.id   if user else None,
        "display_name": (user.firstname or user.name or email) if user else None,
    }


@router.get("/by-email/{email}", response_model=UserOut, dependencies=[Depends(require_admin)])
async def get_user_by_email(email: str, db: AsyncSession = Depends(get_db)):
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, f"Aucun utilisateur avec l'email '{email}'.")
    return _to_out(user)


@router.get("/{name}", response_model=UserOut)
async def get_user(name: str, db: AsyncSession = Depends(get_db)):
    user = (await db.execute(select(User).where(User.name == name))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, f"User '{name}' introuvable.")
    return _to_out(user)


@router.delete("/{name}", status_code=204, dependencies=[Depends(require_admin)])
async def delete_user(name: str, db: AsyncSession = Depends(get_db)):
    user = (await db.execute(select(User).where(User.name == name))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, f"User '{name}' introuvable.")
    await db.delete(user)
    await db.commit()
