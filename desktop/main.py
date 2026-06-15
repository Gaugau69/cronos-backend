"""
desktop/main.py — Application desktop Peakflow Garmin/Polar Connector

Flow Garmin :
  1. Sélectionne "Garmin"
  2. Rentre prénom + email + mdp
  3. Login local → token envoyé à Railway

Flow Polar :
  1. Sélectionne "Polar"
  2. Rentre prénom + email
  3. Ouvre le navigateur → OAuth Polar → Railway
"""

import json
import pickle
import base64
import threading
import tkinter as tk
import tkinter.ttk as ttk
import webbrowser
from urllib.parse import urlencode

import requests
from garminconnect import Garmin, GarminConnectAuthenticationError

BACKEND_URL = "https://cronos-backend-production-4976.up.railway.app"

WATCHES = [
    ("Garmin",   "garmin",   True),
    ("Polar",    "polar",    True),
    ("Withings", "withings", False),   # Bientôt
    ("Fitbit",   "fitbit",   False),   # Bientôt
    ("Whoop",    "whoop",    False),   # Bientôt
    ("Suunto",   "suunto",   False),   # Bientôt
]


def dump_token(api: Garmin) -> str:
    try:
        dumped = api.client.dumps()
        return json.dumps({
            "version":      "0.3",
            "client_dump":  dumped,
            "username":     getattr(api, "username", ""),
            "display_name": getattr(api, "display_name", ""),
        })
    except Exception:
        try:
            return json.dumps({
                "version":      "0.3",
                "client":       base64.b64encode(pickle.dumps(api.client)).decode("utf-8"),
                "username":     getattr(api, "username", ""),
                "display_name": getattr(api, "display_name", ""),
            })
        except Exception as e:
            raise Exception(f"Impossible de sérialiser le token: {e}")


class PeakflowApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Peakflow — Connexion montre")
        self.geometry("420x580")
        self.resizable(False, True)
        self.configure(bg="#0a0a0f")

        self._api           = None
        self._name          = None
        self._email         = None
        self._mfa_pending   = False
        self._client_state  = None
        self._provider      = "garmin"

        self._build_ui()

    # ─────────────────────────────────────────────────────────
    # UI
    # ─────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Scrollable container ──
        canvas = tk.Canvas(self, bg="#0a0a0f", highlightthickness=0)
        scrollbar = tk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self._scroll_frame = tk.Frame(canvas, bg="#0a0a0f")
        self._scroll_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=self._scroll_frame, anchor="nw", width=420)

        # Scroll avec molette
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        # Utilise self._scroll_frame comme parent au lieu de self
        parent = self._scroll_frame

        # ── Header ──
        header = tk.Frame(parent, bg="#0a0a0f")
        header.pack(pady=(28, 0))

        tk.Label(header, text="Peakflow", font=("Arial", 26, "bold"),
                 fg="#6ee7b7", bg="#0a0a0f").pack()
        tk.Label(header, text="Connecte ta montre",
                 font=("Arial", 13), fg="#e2e8f0", bg="#0a0a0f").pack(pady=(4, 0))
        tk.Label(header,
                 text="Tes données restent privées.\nTon mot de passe n'est jamais stocké.",
                 font=("Arial", 10), fg="#64748b", bg="#0a0a0f", justify="center").pack(pady=(6, 0))

        # ── Sélecteur de montre ──
        watch_frame = tk.Frame(parent, bg="#0a0a0f")
        watch_frame.pack(padx=32, pady=(16, 0), fill="x")

        tk.Label(watch_frame, text="TA MONTRE", font=("Arial", 9, "bold"),
                 fg="#64748b", bg="#0a0a0f").pack(anchor="w", pady=(0, 6))

        # Style ttk
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Custom.TCombobox",
                        fieldbackground="#080810",
                        background="#080810",
                        foreground="#e2e8f0",
                        selectbackground="#080810",
                        selectforeground="#6ee7b7",
                        bordercolor="#1e1e2e",
                        arrowcolor="#6ee7b7",
                        font=("Arial", 11))

        # Options du menu déroulant
        watch_labels = []
        self._watch_map = {}
        for label, provider, available in WATCHES:
            display = label if available else f"{label}  (bientôt)"
            watch_labels.append(display)
            self._watch_map[display] = (provider, available)

        self.watch_var = tk.StringVar(value="— Choisir ma montre —")
        self.watch_combo = ttk.Combobox(
            watch_frame,
            textvariable=self.watch_var,
            values=["— Choisir ma montre —"] + watch_labels,
            state="readonly",
            style="Custom.TCombobox",
            font=("Arial", 11),
        )
        self.watch_combo.pack(fill="x")
        self.watch_combo.bind("<<ComboboxSelected>>", self._on_watch_change)

        # ── Formulaire (caché au départ) ──
        self.form = tk.Frame(parent, bg="#13131a", bd=0, highlightthickness=1,
                             highlightbackground="#1e1e2e")

        # ── Bouton (caché au départ) ──
        self.btn = tk.Button(
            parent, text="Connecter mon compte",
            font=("Arial", 12, "bold"),
            bg="#6ee7b7", fg="#0a0a0f",
            relief="flat", cursor="hand2",
            activebackground="#4ade80", activeforeground="#0a0a0f",
            command=self._on_submit, pady=12
        )

        self.status_var = tk.StringVar()
        self.status_label = tk.Label(
            parent, textvariable=self.status_var,
            font=("Arial", 10), bg="#0a0a0f", fg="#f87171",
            wraplength=360, justify="center"
        )
        self.status_label.pack(pady=10)
        self._form_visible = False
        self._parent = parent

    def _build_garmin_form(self):
        """Formulaire Garmin : email PeakFlow + email Garmin + mdp."""
        for w in self.form.winfo_children():
            w.destroy()

        tk.Label(self.form, text="EMAIL PEAKFLOW", font=("Arial", 9, "bold"),
                 fg="#64748b", bg="#13131a").pack(anchor="w", padx=20, pady=(20, 4))
        self.peakflow_email_var = tk.StringVar()
        self._entry(self.form, self.peakflow_email_var, "ton@email-peakflow.com").pack(padx=20, fill="x")

        tk.Label(self.form, text="EMAIL GARMIN CONNECT", font=("Arial", 9, "bold"),
                 fg="#64748b", bg="#13131a").pack(anchor="w", padx=20, pady=(14, 4))
        self.email_var = tk.StringVar()
        self._entry(self.form, self.email_var, "ton@email-garmin.com").pack(padx=20, fill="x")

        tk.Label(self.form, text="MOT DE PASSE GARMIN CONNECT", font=("Arial", 9, "bold"),
                 fg="#64748b", bg="#13131a").pack(anchor="w", padx=20, pady=(14, 4))
        self.pwd_var = tk.StringVar()
        self._entry(self.form, self.pwd_var, "••••••••", show="•").pack(padx=20, fill="x")

        tk.Label(self.form,
                 text="🔒  Ton mot de passe est utilisé une seule fois\npour générer un token sécurisé.",
                 font=("Arial", 9), fg="#6ee7b7", bg="#13131a", justify="left"
                 ).pack(padx=20, pady=(14, 20), anchor="w")

    def _build_polar_form(self, label: str = "Polar", color: str = "#38bdf8"):
        """Formulaire Polar/Withings : email PeakFlow + email provider."""
        for w in self.form.winfo_children():
            w.destroy()

        tk.Label(self.form, text="EMAIL PEAKFLOW", font=("Arial", 9, "bold"),
                 fg="#64748b", bg="#13131a").pack(anchor="w", padx=20, pady=(20, 4))
        self.peakflow_email_var = tk.StringVar()
        self._entry(self.form, self.peakflow_email_var, "ton@email-peakflow.com").pack(padx=20, fill="x")

        tk.Label(self.form, text=f"EMAIL {label.upper()}", font=("Arial", 9, "bold"),
                 fg="#64748b", bg="#13131a").pack(anchor="w", padx=20, pady=(14, 4))
        self.email_var = tk.StringVar()
        self._entry(self.form, self.email_var, f"ton@email-{label.lower()}.com").pack(padx=20, fill="x")

        tk.Label(self.form,
                 text=f"🔵  Tu seras redirigé vers {label} pour\nautoriser l'accès à tes données.",
                 font=("Arial", 9), fg=color, bg="#13131a", justify="left"
                 ).pack(padx=20, pady=(14, 20), anchor="w")

    def _build_coming_soon_form(self, label: str):
        """Formulaire pour les montres pas encore disponibles."""
        for w in self.form.winfo_children():
            w.destroy()

        tk.Label(self.form,
                 text=f"⏳  {label} — Bientôt disponible\n\nCette intégration arrive prochainement.",
                 font=("Arial", 11), fg="#64748b", bg="#13131a", justify="center"
                 ).pack(padx=20, pady=40)

    # ─────────────────────────────────────────────────────────
    # Events
    # ─────────────────────────────────────────────────────────

    def _on_watch_change(self, event=None):
        selected = self.watch_var.get()
        if selected == "— Choisir ma montre —":
            return

        provider, available = self._watch_map[selected]
        self._provider = provider
        self._mfa_pending = False

        # Affiche le formulaire et le bouton si pas encore visible
        if not self._form_visible:
            self.form.pack(padx=32, pady=16, fill="x")
            self.btn.pack(padx=32, fill="x")
            self._form_visible = True

        if not available:
            label = selected.replace("  (bientôt)", "")
            self._build_coming_soon_form(label)
            self.btn.configure(state="disabled", bg="#1e1e2e", fg="#64748b",
                               text="Bientôt disponible")
        elif provider == "polar":
            self._build_polar_form("Polar", "#38bdf8")
            self.btn.configure(state="normal", bg="#38bdf8", fg="#0a0a0f",
                               text="Connecter ma Polar →")
        elif provider == "withings":
            self._build_polar_form("Withings", "#a78bfa")
            self.btn.configure(state="normal", bg="#a78bfa", fg="#0a0a0f",
                               text="Connecter ma Withings →")
        else:
            self._build_garmin_form()
            self.btn.configure(state="normal", bg="#6ee7b7", fg="#0a0a0f",
                               text="Connecter mon compte")

        self._set_status("", error=False)

    def _on_submit(self):
        if self._mfa_pending:
            code = self.mfa_var.get().strip()
            if not code:
                self._set_status("→ Entre le code reçu par email.", error=True)
                return
            self.btn.configure(state="disabled", text="Vérification...")
            threading.Thread(target=self._submit_mfa, args=(code,), daemon=True).start()
            return

        email = self.email_var.get().strip()
        peakflow_email = self.peakflow_email_var.get().strip() if hasattr(self, "peakflow_email_var") else ""
        name = self.name_var.get().strip() if hasattr(self, "name_var") else ""

        placeholders = {"ton@email-peakflow.com", "ton@email-garmin.com", "ton@email.com", "ex: Jean", ""}
        if email in placeholders or (hasattr(self, "peakflow_email_var") and peakflow_email in placeholders):
            self._set_status("→ Remplis tous les champs pour continuer.", error=True)
            return

        if self._provider in ("polar", "withings"):
            self._connect_oauth(peakflow_email, email, self._provider)
        else:
            pwd = self.pwd_var.get().strip() if hasattr(self, "pwd_var") else ""
            if not pwd:
                self._set_status("→ Entre ton mot de passe Garmin Connect.", error=True)
                return
            self.btn.configure(state="disabled", text="Connexion en cours...")
            self._set_status("", error=False)
            threading.Thread(target=self._connect_garmin, args=(peakflow_email, email, pwd), daemon=True).start()

    # ─────────────────────────────────────────────────────────
    # Polar
    # ─────────────────────────────────────────────────────────

    def _connect_oauth(self, peakflow_email: str, provider_email: str, provider: str):
        """Ouvre le navigateur vers le flow OAuth Polar/Withings + polling pour détecter la fin."""
        params = urlencode({"peakflow_email": peakflow_email, "email": provider_email})
        url = f"{BACKEND_URL}/auth/{provider}/login?{params}"
        webbrowser.open(url)
        self._set_status(
            "🔵 Autorise l'accès sur le site Polar qui vient de s'ouvrir...\nL'app détectera automatiquement quand c'est fait.",
            error=False, color="#38bdf8"
        )
        self.btn.configure(state="disabled", text="En attente...")
        threading.Thread(target=self._poll_oauth_status, args=(name, provider), daemon=True).start()

    def _poll_oauth_status(self, name: str, provider: str):
        """Vérifie toutes les 3 secondes si l'auth Polar est complète."""
        import time
        max_attempts = 60  # 3 min max
        for _ in range(max_attempts):
            time.sleep(3)
            try:
                resp = requests.get(
                    f"{BACKEND_URL}/auth/{provider}/status",
                    params={"name": name},
                    timeout=10,
                )
                if resp.status_code == 200 and resp.json().get("connected"):
                    self.after(0, lambda: self._show_success(name))
                    return
            except Exception:
                pass
        # Timeout
        self._set_status(
            "→ Temps d'attente dépassé.\nFerme et rouvre l'app si tu n'as pas terminé l'autorisation.",
            error=True
        )
        self.btn.configure(state="normal", text="Connecter ma Polar →")

    # ─────────────────────────────────────────────────────────
    # Garmin
    # ─────────────────────────────────────────────────────────

    def _connect_garmin(self, peakflow_email: str, garmin_email: str, pwd: str):
        try:
            self._set_status("Connexion à Garmin...", error=False, color="#6ee7b7")

            api = Garmin(garmin_email, pwd, return_on_mfa=True)
            result = api.login()
            if result and isinstance(result, tuple):
                # 2FA requise — un seul code envoyé
                self._api            = api
                self._client_state   = result
                self._peakflow_email = peakflow_email
                self._email          = garmin_email
                self.after(0, self._show_mfa_form)
            else:
                self._send_token(api, peakflow_email, garmin_email)

        except GarminConnectAuthenticationError:
            self._set_status("→ Email ou mot de passe incorrect.\nVérifie tes identifiants Garmin Connect.", error=True)
            self.btn.configure(state="normal", text="Connecter mon compte")
        except Exception as e:
            err = str(e).lower()
            if "429" in str(e) or "rate" in err:
                self._set_status("→ Trop de tentatives détectées par Garmin.\nAttends 15-30 minutes avant de réessayer.", error=True)
            elif "timeout" in err or "connection" in err or "network" in err:
                self._set_status("→ Impossible de contacter Garmin.\nVérifie ta connexion internet.", error=True)
            else:
                self._set_status("→ Problème lors de la connexion à Garmin.\nVérifie ta connexion et réessaie.", error=True)
            self.btn.configure(state="normal", text="Connecter mon compte")

    def _show_mfa_form(self):
        self._mfa_pending = True
        tk.Label(self.form, text="CODE DE VÉRIFICATION (reçu par email)",
                 font=("Arial", 9, "bold"), fg="#fbbf24", bg="#13131a"
                 ).pack(anchor="w", padx=20, pady=(14, 4))
        self.mfa_var = tk.StringVar()
        tk.Entry(self.form, textvariable=self.mfa_var,
                 font=("Courier", 14), bg="#080810", fg="#fbbf24",
                 insertbackground="#fbbf24", relief="flat", bd=0,
                 highlightthickness=1, highlightbackground="#fbbf24",
                 justify="center").pack(padx=20, pady=(0, 20), fill="x")
        self.btn.configure(state="normal", text="Valider le code", bg="#fbbf24")
        self._set_status(
            f"Garmin a envoyé un code à :\n{self._email}\nEntre-le ci-dessus.",
            error=False, color="#fbbf24"
        )

    def _submit_mfa(self, code: str):
        try:
            self._api.resume_login(self._client_state, mfa_code=code)
            self._send_token(self._api, self._peakflow_email, self._email)
        except Exception as e:
            self._set_status("→ Code incorrect ou expiré.\nVérifie le code dans ton email et réessaie.", error=True)
            self.btn.configure(state="normal", text="Valider le code")

    def _send_token(self, api: Garmin, peakflow_email: str, garmin_email: str):
        try:
            token_json = dump_token(api)
            self._set_status("Envoi sécurisé au serveur...", error=False, color="#6ee7b7")
            pwd = self.pwd_var.get().strip() if hasattr(self, "pwd_var") else ""
            resp = requests.post(
                f"{BACKEND_URL}/users/register-token",
                json={
                    "name":            peakflow_email,
                    "email":           garmin_email,
                    "peakflow_email":  peakflow_email,
                    "token_json":      token_json,
                    "garmin_email":    garmin_email,
                    "garmin_password": pwd,
                },
                timeout=60,
            )
            if resp.status_code in (200, 201):
                self.after(0, lambda: self._show_success(peakflow_email))
            else:
                print(f"[DEBUG] status={resp.status_code} body={resp.text[:500]}")
                self._set_status("→ Connexion impossible pour le moment.\nRéessaie dans quelques instants.", error=True)
                self.btn.configure(state="normal", text="Connecter mon compte")
        except Exception as e:
            self._set_status("→ Impossible de finaliser la connexion.\nVérifie ta connexion internet et réessaie.", error=True)
            self.btn.configure(state="normal", text="Connecter mon compte")

    # ─────────────────────────────────────────────────────────
    # Succès
    # ─────────────────────────────────────────────────────────

    def _show_success(self, name: str):
        for widget in self.winfo_children():
            widget.destroy()
        tk.Label(self, text="✓", font=("Arial", 48), fg="#6ee7b7", bg="#0a0a0f").pack(pady=(60, 0))
        tk.Label(self, text="Compte connecté !", font=("Arial", 18, "bold"),
                 fg="#6ee7b7", bg="#0a0a0f").pack(pady=(8, 0))
        tk.Label(self,
                 text=f"Tes données, {name},\nvont être collectées automatiquement.",
                 font=("Arial", 11), fg="#94a3b8", bg="#0a0a0f", justify="center"
                 ).pack(pady=(12, 0))
        tk.Button(self, text="Fermer", font=("Arial", 11), bg="#1e1e2e", fg="#e2e8f0",
                  relief="flat", cursor="hand2", command=self.destroy, pady=10
                  ).pack(pady=32, padx=64, fill="x")

    # ─────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────

    def _entry(self, parent, var, placeholder, show=None):
        e = tk.Entry(parent, textvariable=var, font=("Courier", 11),
                     bg="#080810", fg="#e2e8f0", insertbackground="#6ee7b7",
                     relief="flat", bd=0, highlightthickness=1,
                     highlightbackground="#1e1e2e", highlightcolor="#6ee7b7",
                     show=show or "")
        if not show:
            e.insert(0, placeholder)
            e.configure(fg="#64748b")
            def on_focus_in(event, entry=e, ph=placeholder):
                if entry.get() == ph:
                    entry.delete(0, tk.END)
                    entry.configure(fg="#e2e8f0")
            def on_focus_out(event, entry=e, ph=placeholder):
                if not entry.get():
                    entry.insert(0, ph)
                    entry.configure(fg="#64748b")
            e.bind("<FocusIn>", on_focus_in)
            e.bind("<FocusOut>", on_focus_out)
        return e

    def _set_status(self, msg, error=True, color=None):
        self.after(0, lambda: self._update_status(msg, error, color))

    def _update_status(self, msg, error, color):
        self.status_var.set(msg)
        self.status_label.configure(fg=color or ("#f87171" if error else "#6ee7b7"))


if __name__ == "__main__":
    app = PeakflowApp()
    app.mainloop()