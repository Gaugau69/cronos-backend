"""
app/routers/whoop.py — Routes OAuth WHOOP v1
"""

import base64
import json
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import User, get_db
from app.services.whoop_auth import exchange_code_for_token, get_whoop_auth_url, save_whoop_token

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth/whoop", tags=["whoop"])


@router.get("/login")
async def whoop_login(email: str, peakflow_email: str = ""):
    state_data = json.dumps({"email": email, "peakflow_email": peakflow_email or email})
    state = base64.urlsafe_b64encode(state_data.encode()).decode()
    return RedirectResponse(url=get_whoop_auth_url(state=state))


@router.get("/callback")
async def whoop_callback(
    code: str = None, state: str = None, error: str = None,
    db: AsyncSession = Depends(get_db),
):
    if error:
        return HTMLResponse(_error_page(f"Autorisation refusée : {error}"))
    if not code or not state:
        return HTMLResponse(_error_page("Paramètres manquants."))

    try:
        state_data = json.loads(base64.urlsafe_b64decode(state.encode()).decode())
        email          = state_data["email"]
        peakflow_email = state_data.get("peakflow_email", email)
    except Exception:
        return HTMLResponse(_error_page("State invalide."))

    try:
        token_data = await exchange_code_for_token(code)
    except Exception as e:
        return HTMLResponse(_error_page(f"Erreur d'authentification : {e}"))

    ok = await save_whoop_token(db, peakflow_email, email, token_data)
    if not ok:
        return HTMLResponse(_error_page("Erreur lors de la sauvegarde."))

    log.info(f"✓ WHOOP connecté (peakflow: {peakflow_email})")

    user = (await db.execute(select(User).where(User.email == peakflow_email))).scalar_one_or_none()
    if user:
        import asyncio
        from app.routers.users import _trigger_historical_backfill
        asyncio.create_task(_trigger_historical_backfill(user.id, user.name))

    return HTMLResponse(_success_page(peakflow_email))


@router.get("/status")
async def whoop_status(name: str, db: AsyncSession = Depends(get_db)):
    user = (await db.execute(select(User).where(User.email == name))).scalar_one_or_none()
    if not user or not user.token_json:
        return JSONResponse({"connected": False})
    try:
        return JSONResponse({"connected": json.loads(user.token_json).get("provider") == "whoop"})
    except Exception:
        return JSONResponse({"connected": False})


def _success_page(name: str) -> str:
    return f"""<!DOCTYPE html><html><head><title>Peakflow</title><meta charset="utf-8">
    <style>body{{font-family:Arial,sans-serif;background:#0a0a0f;color:#e2e8f0;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
    .card{{text-align:center;padding:40px}}.check{{font-size:64px;color:#6ee7b7}}h1{{color:#6ee7b7}}p{{color:#94a3b8}}</style></head>
    <body><div class="card"><div class="check">✓</div><h1>WHOOP connecté !</h1>
    <p>Tes données, {name},<br>vont être collectées automatiquement.</p>
    <p style="margin-top:32px;font-size:12px;color:#64748b">Cette fenêtre se ferme dans 3 secondes...</p></div>
    <script>setTimeout(function(){{window.close()}},3000)</script></body></html>"""


def _error_page(message: str) -> str:
    return f"""<!DOCTYPE html><html><head><title>Peakflow</title><meta charset="utf-8">
    <style>body{{font-family:Arial,sans-serif;background:#0a0a0f;color:#e2e8f0;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
    .card{{text-align:center;padding:40px}}h1{{color:#f87171}}p{{color:#94a3b8}}</style></head>
    <body><div class="card"><div style="font-size:64px">✗</div><h1>Erreur</h1>
    <p>{message}</p></div></body></html>"""
