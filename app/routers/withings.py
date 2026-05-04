"""
app/routers/withings.py — Routes OAuth Withings
"""

import base64
import json
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import User, get_db
from app.services.withings_auth import (
    exchange_code_for_token,
    get_withings_auth_url,
    save_withings_token,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth/withings", tags=["withings"])


@router.get("/login")
async def withings_login(name: str, email: str):
    state_data = json.dumps({"name": name, "email": email})
    state = base64.urlsafe_b64encode(state_data.encode()).decode()
    return RedirectResponse(url=get_withings_auth_url(state=state))


@router.get("/callback")
async def withings_callback(
    code: str = None, state: str = None, error: str = None,
    db: AsyncSession = Depends(get_db),
):
    if error:
        return HTMLResponse(_error_page(f"Autorisation refusée : {error}"))
    if not code or not state:
        return HTMLResponse(_error_page("Paramètres manquants."))

    try:
        state_data = json.loads(base64.urlsafe_b64decode(state.encode()).decode())
        name, email = state_data["name"], state_data["email"]
    except Exception:
        return HTMLResponse(_error_page("State invalide."))

    try:
        token_data = await exchange_code_for_token(code)
    except Exception as e:
        return HTMLResponse(_error_page(f"Erreur : {e}"))

    ok = await save_withings_token(db, name, email, token_data)
    if not ok:
        return HTMLResponse(_error_page("Erreur sauvegarde."))

    log.info(f"✓ Withings connecté pour {name}")
    return HTMLResponse(_success_page(name))


@router.get("/status")
async def withings_status(name: str, db: AsyncSession = Depends(get_db)):
    user = (await db.execute(select(User).where(User.name == name))).scalar_one_or_none()
    if not user or not user.token_json:
        return JSONResponse({"connected": False})
    try:
        return JSONResponse({"connected": json.loads(user.token_json).get("provider") == "withings"})
    except Exception:
        return JSONResponse({"connected": False})


def _success_page(name):
    return f"""<!DOCTYPE html><html><head><title>Peakflow</title><meta charset="utf-8">
    <style>body{{font-family:Arial,sans-serif;background:#0a0a0f;color:#e2e8f0;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
    .card{{text-align:center;padding:40px}}.check{{font-size:64px;color:#6ee7b7}}h1{{color:#6ee7b7}}p{{color:#94a3b8}}</style></head>
    <body><div class="card"><div class="check">✓</div><h1>Compte Withings connecté !</h1>
    <p>Tes données, {name},<br>vont être collectées automatiquement.</p>
    <p style="margin-top:32px;font-size:12px;color:#64748b">Cette fenêtre se ferme dans 3 secondes...</p></div>
    <script>setTimeout(function(){{window.close()}},3000)</script></body></html>"""


def _error_page(message):
    return f"""<!DOCTYPE html><html><head><title>Peakflow</title><meta charset="utf-8">
    <style>body{{font-family:Arial,sans-serif;background:#0a0a0f;color:#e2e8f0;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
    .card{{text-align:center;padding:40px}}h1{{color:#f87171}}p{{color:#94a3b8}}</style></head>
    <body><div class="card"><div style="font-size:64px">✗</div><h1>Erreur</h1>
    <p>{message}</p></div></body></html>"""
