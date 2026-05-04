"""
app/routers/polar.py — Routes OAuth Polar

GET /auth/polar/login?name=Jean&email=jean@email.com
    → Redirige vers Polar pour autorisation

GET /auth/polar/callback?code=...&state=...
    → Reçoit le code OAuth, échange contre un token, sauvegarde en DB
"""

import base64
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.services.polar_auth import (
    exchange_code_for_token,
    get_polar_auth_url,
    register_polar_user,
    save_polar_token,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth/polar", tags=["polar"])


@router.get("/login")
async def polar_login(name: str, email: str):
    """
    Démarre le flow OAuth Polar.
    Redirige l'utilisateur vers la page d'autorisation Polar.

    Usage : GET /auth/polar/login?name=Jean&email=jean@email.com
    """
    # Encode name + email dans le state pour les récupérer au callback
    state_data = json.dumps({"name": name, "email": email})
    state = base64.urlsafe_b64encode(state_data.encode()).decode()

    auth_url = get_polar_auth_url(state=state)
    log.info(f"Polar OAuth démarré pour {name} ({email})")
    return RedirectResponse(url=auth_url)


@router.get("/callback")
async def polar_callback(
    code: str = None,
    state: str = None,
    error: str = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Callback OAuth Polar.
    Reçoit le code d'autorisation et l'échange contre un token.
    """
    # Erreur d'autorisation
    if error:
        log.error(f"Polar OAuth error: {error}")
        return HTMLResponse(_error_page(f"Autorisation refusée : {error}"))

    if not code or not state:
        return HTMLResponse(_error_page("Paramètres manquants."))

    # Décode le state pour récupérer name + email
    try:
        state_data = json.loads(base64.urlsafe_b64decode(state.encode()).decode())
        name  = state_data["name"]
        email = state_data["email"]
    except Exception:
        return HTMLResponse(_error_page("State invalide."))

    # Échange le code contre un token
    try:
        token_data = await exchange_code_for_token(code)
        polar_user_id = str(token_data.get("x_user_id", ""))
        access_token  = token_data.get("access_token", "")
    except Exception as e:
        log.error(f"Polar token exchange error: {e}")
        return HTMLResponse(_error_page(f"Erreur d'authentification : {e}"))

    # Enregistre l'utilisateur sur l'API Polar (obligatoire une fois)
    try:
        await register_polar_user(access_token, polar_user_id)
    except Exception as e:
        log.warning(f"Polar register user warning: {e}")

    # Sauvegarde le token en DB
    ok = await save_polar_token(db, name, email, token_data)
    if not ok:
        return HTMLResponse(_error_page("Erreur lors de la sauvegarde."))

    log.info(f"✓ Polar connecté pour {name} (user_id: {polar_user_id})")
    return HTMLResponse(_success_page(name))


def _success_page(name: str) -> str:
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Peakflow — Connexion réussie</title>
        <meta charset="utf-8">
        <style>
            body {{ font-family: Arial, sans-serif; background: #0a0a0f; color: #e2e8f0;
                   display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }}
            .card {{ text-align: center; padding: 40px; }}
            .check {{ font-size: 64px; color: #6ee7b7; }}
            h1 {{ color: #6ee7b7; }}
            p {{ color: #94a3b8; }}
        </style>
    </head>
    <body>
        <div class="card">
            <div class="check">✓</div>
            <h1>Compte Polar connecté !</h1>
            <p>Tes données Garmin, {name},<br>vont être collectées automatiquement.</p>
            <p style="margin-top:32px; font-size:12px; color:#64748b;">Tu peux fermer cette fenêtre.</p>
        </div>
    </body>
    </html>
    """


def _error_page(message: str) -> str:
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Peakflow — Erreur</title>
        <meta charset="utf-8">
        <style>
            body {{ font-family: Arial, sans-serif; background: #0a0a0f; color: #e2e8f0;
                   display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }}
            .card {{ text-align: center; padding: 40px; }}
            .icon {{ font-size: 64px; }}
            h1 {{ color: #f87171; }}
            p {{ color: #94a3b8; }}
        </style>
    </head>
    <body>
        <div class="card">
            <div class="icon">✗</div>
            <h1>Erreur de connexion</h1>
            <p>{message}</p>
            <p style="margin-top:32px; font-size:12px; color:#64748b;">Tu peux fermer cette fenêtre et réessayer.</p>
        </div>
    </body>
    </html>
    """