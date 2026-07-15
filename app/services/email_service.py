"""
app/services/email_service.py — Envoi des emails de recommandation quotidiens.

Utilise SMTP standard (Gmail, Infomaniak, etc.).
Variables d'env : SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM
"""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.config import settings

logger = logging.getLogger(__name__)

CAT_LABELS = {
    "endurance":   "Endurance",
    "intensite":   "Intensité",
    "recuperation":"Récupération",
    "force":       "Force",
    "specifique":  "Spécifique",
    "repos":       "Repos",
}

RECOVERY_COLORS = {
    "Excellente": "#10B981",
    "Bonne":      "#34D399",
    "Moyenne":    "#FBBF24",
    "Faible":     "#F87171",
}


def _build_html(user_name: str, payload: dict) -> str:
    recovery = payload.get("recovery", {})
    athlete  = payload.get("athlete", {})
    load     = payload.get("load", {})
    recs     = payload.get("recommendations", [])[:5]

    rec_level = recovery.get("level", "—")
    color     = RECOVERY_COLORS.get(rec_level, "#6B7280")

    sessions_html = ""
    for r in recs:
        cat = CAT_LABELS.get(r.get("category", ""), r.get("category", ""))
        sessions_html += f"""
        <tr>
          <td style="padding:10px 0;border-bottom:1px solid #1e2040">
            <strong style="color:#fff;font-size:15px">#{r['rank']} {r['name']}</strong><br>
            <span style="color:#9CA3AF;font-size:12px">{cat} · {r.get('duration_min',0)} min · {r.get('distance_km',0)} km</span><br>
            <span style="color:#6B7280;font-size:11px">{r.get('example','')}</span>
          </td>
          <td style="padding:10px 0;border-bottom:1px solid #1e2040;text-align:right;vertical-align:top">
            <span style="background:#10B981;color:#000;padding:3px 8px;border-radius:4px;font-size:12px;font-weight:bold">{r.get('score',0)}/100</span>
          </td>
        </tr>"""

    load_html = ""
    if load:
        trend = load.get("load_trend", "")
        tsb   = load.get("tsb", 0)
        tsb_color = "#10B981" if tsb >= 0 else "#F87171"
        load_html = f"""
        <p style="color:#9CA3AF;font-size:13px;margin:4px 0">
          Charge 7j · ATL <strong style="color:#fff">{load.get('atl',0)}</strong>
          · CTL <strong style="color:#fff">{load.get('ctl',0)}</strong>
          · TSB <strong style="color:{tsb_color}">{tsb:+.0f}</strong>
          · <em>{trend}</em>
        </p>"""

    return f"""<!DOCTYPE html>
<html lang="fr">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="background:#0a0a1a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:0;padding:0">
  <div style="max-width:560px;margin:0 auto;padding:32px 16px">

    <!-- Header -->
    <div style="text-align:center;margin-bottom:32px">
      <h1 style="color:#10B981;font-size:28px;letter-spacing:0.3em;font-weight:800;margin:0">CRONOS</h1>
      <p style="color:#6B7280;font-size:12px;letter-spacing:0.15em;margin:4px 0">PEAKFLOW TECHNOLOGIES</p>
    </div>

    <!-- Bonjour -->
    <p style="color:#D1D5DB;font-size:16px;margin:0 0 24px">
      Bonjour <strong style="color:#fff">{user_name}</strong>, voici tes séances du jour.
    </p>

    <!-- Recovery card -->
    <div style="background:#0f0f2e;border:1px solid #1e2040;border-left:4px solid {color};border-radius:8px;padding:16px;margin-bottom:24px">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div>
          <p style="color:#9CA3AF;font-size:11px;text-transform:uppercase;letter-spacing:0.1em;margin:0 0 4px">Récupération</p>
          <p style="color:{color};font-size:28px;font-weight:800;margin:0">{recovery.get('score','—')}<span style="font-size:14px;color:#6B7280">/100</span></p>
          <p style="color:{color};font-size:13px;font-weight:600;margin:4px 0">{rec_level}</p>
        </div>
        <div style="text-align:right">
          {'<p style="color:#9CA3AF;font-size:12px;margin:2px 0">HRV ' + str(recovery.get('hrv_today','—')) + ' ms</p>' if recovery.get('hrv_today') else ''}
          {'<p style="color:#9CA3AF;font-size:12px;margin:2px 0">Sommeil ' + str(recovery.get('sleep_score','—')) + '/100</p>' if recovery.get('sleep_score') else ''}
        </div>
      </div>
      {load_html}
    </div>

    <!-- Séances -->
    <div style="background:#0f0f2e;border:1px solid #1e2040;border-radius:8px;padding:16px;margin-bottom:24px">
      <p style="color:#9CA3AF;font-size:11px;text-transform:uppercase;letter-spacing:0.1em;margin:0 0 12px">Séances recommandées</p>
      <table style="width:100%;border-collapse:collapse">{sessions_html}</table>
    </div>

    <!-- CTA -->
    <div style="text-align:center;margin-bottom:32px">
      <a href="https://peakflow.ai/cronos" style="display:inline-block;background:#10B981;color:#000;padding:12px 32px;border-radius:6px;font-weight:700;font-size:14px;text-decoration:none;letter-spacing:0.05em">
        VOIR MA SÉANCE →
      </a>
    </div>

    <!-- Footer -->
    <p style="color:#4B5563;font-size:11px;text-align:center;margin:0">
      CRONOS · Peakflow Technologies · <a href="https://peakflow.ai/settings" style="color:#6B7280">Se désabonner</a>
    </p>
  </div>
</body>
</html>"""


def send_daily_recommendation(
    to_email: str,
    user_name: str,
    payload: dict,
) -> bool:
    """
    Envoie l'email quotidien de recommandation.
    Retourne True si envoyé, False en cas d'erreur.
    """
    if not settings.smtp_user or not settings.smtp_password:
        logger.debug("SMTP non configuré — email ignoré")
        return False

    try:
        _date = payload.get("date", "aujourd'hui")
        _level = payload.get("recovery", {}).get("level", "—")
        subject = f"CRONOS · Tes séances du {_date} · {_level}"

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = settings.smtp_from
        msg["To"]      = to_email

        html = _build_html(user_name, payload)
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
            server.starttls()
            server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(settings.smtp_from, [to_email], msg.as_string())

        logger.info(f"Email envoyé à {to_email}")
        return True

    except Exception as e:
        logger.error(f"Échec envoi email à {to_email}: {e}")
        return False
