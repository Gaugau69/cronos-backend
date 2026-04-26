"""
app/routers/users.py — Endpoints de gestion des utilisateurs.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import User, get_db
from app.schemas import UserCreate, UserOut
from app.services.garmin_auth import login_and_save_token

router = APIRouter(prefix="/users", tags=["users"])


def _to_out(u: User) -> UserOut:
    return UserOut(id=u.id, name=u.name, email=u.email,
                   created_at=u.created_at, has_token=bool(u.token_json))


@router.post("/", response_model=UserOut, status_code=201)
async def register_user(payload: UserCreate, db: AsyncSession = Depends(get_db)):
    """
    Enregistre un user et récupère son token Garmin via email + password.
    Le mot de passe n'est JAMAIS stocké.
    """
    existing = (await db.execute(select(User).where(User.name == payload.name))).scalar_one_or_none()
    if existing and existing.token_json:
        raise HTTPException(409, f"'{payload.name}' est déjà enregistré avec un token valide.")

    ok = await login_and_save_token(db, payload.name, payload.email, payload.password)
    if not ok:
        raise HTTPException(401, "Authentification Garmin échouée. Vérifier email/mot de passe.")

    user = (await db.execute(select(User).where(User.name == payload.name))).scalar_one()
    return _to_out(user)


@router.get("/", response_model=list[UserOut])
async def list_users(db: AsyncSession = Depends(get_db)):
    users = (await db.execute(select(User).order_by(User.created_at))).scalars().all()
    return [_to_out(u) for u in users]


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
