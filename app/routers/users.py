"""
app/routers/users.py — Endpoints de gestion des utilisateurs.
"""

import json

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import User, get_db
from app.schemas import UserCreate, UserOut
from app.services.garmin_auth import login_and_save_token, upsert_garmin_user, encrypt_password

router = APIRouter(prefix="/users", tags=["users"])


class UserTokenRegister(BaseModel):
    name: str
    email: EmailStr
    token_json: str
    garmin_email:    Optional[str] = None
    garmin_password: Optional[str] = None


def _to_out(u: User) -> UserOut:
    return UserOut(
        id=u.id,
        name=u.name or "",
        email=u.email,
        created_at=u.created_at,
        has_token=bool(u.token_json),
    )


@router.post("/", response_model=UserOut, status_code=201)
async def register_user(payload: UserCreate, db: AsyncSession = Depends(get_db)):
    """
    Enregistre un user et récupère son token Garmin via email + password.
    Le mot de passe n'est JAMAIS stocké en clair.
    """
    existing = (await db.execute(select(User).where(User.name == payload.name))).scalar_one_or_none()
    if existing and existing.token_json:
        raise HTTPException(409, f"'{payload.name}' est déjà enregistré avec un token valide.")

    ok = await login_and_save_token(db, payload.name, payload.email, payload.password)
    if not ok:
        raise HTTPException(401, "Authentification Garmin échouée. Vérifier email/mot de passe.")

    user = (await db.execute(select(User).where(User.email == payload.email))).scalar_one()
    return _to_out(user)


@router.post("/register-token", response_model=UserOut, status_code=201)
async def register_with_token(payload: UserTokenRegister, db: AsyncSession = Depends(get_db)):
    try:
        json.loads(payload.token_json)
    except Exception:
        raise HTTPException(400, "token_json invalide.")

    password_enc = None
    if payload.garmin_password:
        password_enc = encrypt_password(payload.garmin_password)

    user = await upsert_garmin_user(
        db,
        garmin_username=payload.name,
        email=payload.email,
        token_json=payload.token_json,
        garmin_email=payload.garmin_email or payload.email,
        garmin_password_enc=password_enc,
    )
    return _to_out(user)


@router.get("/", response_model=list[UserOut])
async def list_users(db: AsyncSession = Depends(get_db)):
    users = (await db.execute(select(User).order_by(User.created_at))).scalars().all()
    return [_to_out(u) for u in users]


@router.get("/by-email/{email}/status")
async def get_user_status_by_email(email: str, db: AsyncSession = Depends(get_db)):
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    return {
        "registered": bool(user),
        "has_token":  bool(user and user.token_json),
        "name":       user.name if user else None,
        "id":         user.id   if user else None,
    }


@router.get("/by-email/{email}", response_model=UserOut)
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


@router.delete("/{name}", status_code=204)
async def delete_user(name: str, db: AsyncSession = Depends(get_db)):
    user = (await db.execute(select(User).where(User.name == name))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, f"User '{name}' introuvable.")
    await db.delete(user)
    await db.commit()
