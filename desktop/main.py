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

BACKEND_URL = "https://web-production-3668.up.railway.app"

WATCHES = [
    ("Garmin",  "garmin",  True),
    ("Polar",   "polar",   True),
    ("Fitbit",  "fitbit",  False),   # Bientôt
    ("Whoop",   "whoop",   False),   # Bientôt
    ("Suunto",  "suunto",  False),   # Bientôt
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
        self.geometry("420x600")
        self.resizable(False, False)
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
        # ── Header ──
        header = tk.Frame(self, bg="#0a0a0f")
        header.pack(pady=(28, 0))

        tk.Label(header, text="Peakflow", font=("Arial", 26, "bold"),
                 fg="#6ee7b7", bg="#0a0a0f").pack()
        tk.Label(header, text="Connecte ta montre",
                 font=("Arial", 13), fg="#e2e8f0", bg="#0a0a0f").pack(pady=(4, 0))
        tk.Label(header,
                 text="Tes données restent privées.\nTon mot de passe n'est jamais stocké.",
                 font=("Arial", 10), fg="#64748b", bg="#0a0a0f", justify="center").pack(pady=(6, 0))

        # ── Sélecteur de montre ──
        watch_frame = tk.Frame(self, bg="#0a0a0f")
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

        self.watch_var = tk.StringVar(value=watch_labels[0])
        self.watch_combo = ttk.Combobox(
            watch_frame,
            textvariable=self.watch_var,
            values=watch_labels,
            state="readonly",
            style="Custom.TCombobox",
            font=("Arial", 11),
        )
        self.watch_combo.pack(fill="x")
        self.watch_combo.bind("<<ComboboxSelected>>", self._on_watch_change)

        # ── Formulaire ──
        self.form = tk.Frame(self, bg="#13131a", bd=0, highlightthickness=1,
                             highlightbackground="#1e1e2e")
        self.form.pack(padx=32, pady=16, fill="x")

        self._build_garmin_form()

        # ── Bouton ──
        self.btn = tk.Button(
            self, text="Connecter mon compte",
            font=("Arial", 12, "bold"),
            bg="#6ee7b7", fg="#0a0a0f",
            relief="flat", cursor="hand2",
            activebackground="#4ade80", activeforeground="#0a0a0f",
            command=self._on_submit, pady=12
        )
        self.btn.pack(padx=32, fill="x")

        self.status_var = tk.StringVar()
        self.status_label = tk.Label(
            self, textvariable=self.status_var,
            font=("Arial", 10), bg="#0a0a0f", fg="#f87171",
            wraplength=360, justify="center"
        )
        self.status_label.pack(pady=10)

    def _build_garmin_form(self):
        """Formulaire Garmin : prénom + email + mdp."""
        for w in self.form.winfo_children():
            w.destroy()

        tk.Label(self.form, text="TON PRÉNOM", font=("Arial", 9, "bold"),
                 fg="#64748b", bg="#13131a").pack(anchor="w", padx=20, pady=(20, 4))
        self.name_var = tk.StringVar()
        self._entry(self.form, self.name_var, "ex: Jean").pack(padx=20, fill="x")

        tk.Label(self.form, text="EMAIL GARMIN CONNECT", font=("Arial", 9, "bold"),
                 fg="#64748b", bg="#13131a").pack(anchor="w", padx=20, pady=(14, 4))
        self.email_var = tk.StringVar()
        self._entry(self.form, self.email_var, "ton@email.com").pack(padx=20, fill="x")

        tk.Label(self.form, text="MOT DE PASSE GARMIN CONNECT", font=("Arial", 9, "bold"),
                 fg="#64748b", bg="#13131a").pack(anchor="w", padx=20, pady=(14, 4))
        self.pwd_var = tk.StringVar()
        self._entry(self.form, self.pwd_var, "••••••••", show="•").pack(padx=20, fill="x")

        tk.Label(self.form,
                 text="🔒  Ton mot de passe est utilisé une seule fois\npour générer un token sécurisé.",
                 font=("Arial", 9), fg="#6ee7b7", bg="#13131a", justify="left"
                 ).pack(padx=20, pady=(14, 20), anchor="w")

    def _build_polar_form(self):
        """Formulaire Polar : prénom + email seulement (pas de mdp)."""
        for w in self.form.winfo_children():
            w.destroy()

        tk.Label(self.form, text="TON PRÉNOM", font=("Arial", 9, "bold"),
                 fg="#64748b", bg="#13131a").pack(anchor="w", padx=20, pady=(20, 4))
        self.name_var = tk.StringVar()
        self._entry(self.form, self.name_var, "ex: Jean").pack(padx=20, fill="x")

        tk.Label(self.form, text="TON EMAIL", font=("Arial", 9, "bold"),
                 fg="#64748b", bg="#13131a").pack(anchor="w", padx=20, pady=(14, 4))
        self.email_var = tk.StringVar()
        self._entry(self.form, self.email_var, "ton@email.com").pack(padx=20, fill="x")

        tk.Label(self.form,
                 text="🔵  Tu seras redirigé vers Polar pour\nautoriser l'accès à tes données.",
                 font=("Arial", 9), fg="#38bdf8", bg="#13131a", justify="left"
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
        provider, available = self._watch_map[selected]
        self._provider = provider
        self._mfa_pending = False

        if not available:
            label = selected.replace("  (bientôt)", "")
            self._build_coming_soon_form(label)
            self.btn.configure(state="disabled", bg="#1e1e2e", fg="#64748b",
                               text="Bientôt disponible")
        elif provider == "polar":
            self._build_polar_form()
            self.btn.configure(state="normal", bg="#38bdf8", fg="#0a0a0f",
                               text="Connecter ma Polar →")
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

        name  = self.name_var.get().strip()
        email = self.email_var.get().strip()

        if name in {"ex: Jean", ""} or email in {"ton@email.com", ""}:
            self._set_status("→ Tous les champs sont requis.", error=True)
            return

        if self._provider == "polar":
            self._connect_polar(name, email)
        else:
            pwd = self.pwd_var.get().strip() if hasattr(self, "pwd_var") else ""
            if not pwd:
                self._set_status("→ Mot de passe requis.", error=True)
                return
            self.btn.configure(state="disabled", text="Connexion en cours...")
            self._set_status("", error=False)
            threading.Thread(target=self._connect_garmin, args=(name, email, pwd), daemon=True).start()

    # ─────────────────────────────────────────────────────────
    # Polar
    # ─────────────────────────────────────────────────────────

    def _connect_polar(self, name: str, email: str):
        """Ouvre le navigateur vers le flow OAuth Polar + polling pour détecter la fin."""
        params = urlencode({"name": name, "email": email})
        url = f"{BACKEND_URL}/auth/polar/login?{params}"
        webbrowser.open(url)
        self._set_status(
            "🔵 Autorise l'accès sur le site Polar qui vient de s'ouvrir...\nL'app détectera automatiquement quand c'est fait.",
            error=False, color="#38bdf8"
        )
        self.btn.configure(state="disabled", text="En attente...")
        # Démarre le polling en arrière-plan
        threading.Thread(target=self._poll_polar_status, args=(name,), daemon=True).start()

    def _poll_polar_status(self, name: str):
        """Vérifie toutes les 3 secondes si l'auth Polar est complète."""
        import time
        max_attempts = 60  # 3 min max
        for _ in range(max_attempts):
            time.sleep(3)
            try:
                resp = requests.get(
                    f"{BACKEND_URL}/auth/polar/status",
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
            "→ Délai dépassé. Réessaie si tu n'as pas terminé l'autorisation.",
            error=True
        )
        self.btn.configure(state="normal", text="Connecter ma Polar →")

    # ─────────────────────────────────────────────────────────
    # Garmin
    # ─────────────────────────────────────────────────────────

    def _connect_garmin(self, name: str, email: str, pwd: str):
        try:
            self._set_status("Connexion à Garmin...", error=False, color="#6ee7b7")
            api = Garmin(email, pwd, return_on_mfa=True)
            result = api.login()

            if result:
                client_state, _ = result
                self._api          = api
                self._client_state = client_state
                self._name         = name
                self._email        = email
                self.after(0, self._show_mfa_form)
            else:
                self._send_token(api, name, email)

        except GarminConnectAuthenticationError:
            self._set_status("→ Email ou mot de passe incorrect.", error=True)
            self.btn.configure(state="normal", text="Connecter mon compte")
        except Exception as e:
            self._set_status(f"→ Erreur : {e}", error=True)
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
        self._set_status("Garmin a envoyé un code à ton email.\nEntre-le ci-dessus.",
                         error=False, color="#fbbf24")

    def _submit_mfa(self, code: str):
        try:
            self._api.resume_login(self._client_state, mfa_code=code)
            self._send_token(self._api, self._name, self._email)
        except Exception as e:
            self._set_status(f"→ Code invalide ou expiré : {e}", error=True)
            self.btn.configure(state="normal", text="Valider le code")

    def _send_token(self, api: Garmin, name: str, email: str):
        try:
            token_json = dump_token(api)
            self._set_status("Envoi sécurisé au serveur...", error=False, color="#6ee7b7")
            resp = requests.post(
                f"{BACKEND_URL}/users/register-token",
                json={"name": name, "email": email, "token_json": token_json},
                timeout=60,
            )
            if resp.status_code in (200, 201):
                self.after(0, lambda: self._show_success(name))
            else:
                detail = resp.json().get("detail", "Erreur inconnue")
                self._set_status(f"→ Erreur serveur : {detail}", error=True)
                self.btn.configure(state="normal", text="Connecter mon compte")
        except Exception as e:
            self._set_status(f"→ Erreur envoi token : {e}", error=True)
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