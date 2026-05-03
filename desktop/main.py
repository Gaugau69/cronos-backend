"""
desktop/main.py — Application desktop CRONOS Garmin Connector

Flow :
  1. L'utilisateur rentre prénom + email + mdp
  2. Login Garmin LOCAL (depuis l'IP de l'utilisateur)
  3. Si 2FA requise → affiche un champ pour entrer le code
  4. Le token est envoyé à Railway — jamais le mdp
"""

import json
import pickle
import base64
import threading
import tkinter as tk

import requests
from garminconnect import Garmin, GarminConnectAuthenticationError

BACKEND_URL = "https://web-production-3668.up.railway.app"


def dump_token(api: Garmin) -> str:
    try:
        # Nouvelle API 0.3.x — utilise dump() natif du client
        dumped = api.client.dumps()
        token_data = {
            "version": "0.3",
            "client_dump": dumped,
            "username": getattr(api, "username", ""),
            "display_name": getattr(api, "display_name", ""),
        }
        return json.dumps(token_data)
    except Exception as e:
        raise Exception(f"Impossible de sérialiser le token: {e}")


class AionApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CRONOS — Connexion Garmin")
        self.geometry("420x600")
        self.resizable(False, False)
        self.configure(bg="#0a0a0f")

        # État interne pour la 2FA
        self._api = None
        self._name = None
        self._email = None
        self._client_state = None
        self._mfa_pending = False

        self._build_ui()

    def _build_ui(self):
        header = tk.Frame(self, bg="#0a0a0f")
        header.pack(pady=(32, 0))

        tk.Label(header, text="CRONOS", font=("Arial", 26, "bold"),
                 fg="#6ee7b7", bg="#0a0a0f").pack()
        tk.Label(header, text="Connecte ton compte Garmin",
                 font=("Arial", 13), fg="#e2e8f0", bg="#0a0a0f").pack(pady=(4, 0))
        tk.Label(header,
                 text="Tes données restent privées.\nTon mot de passe n'est jamais stocké.",
                 font=("Arial", 10), fg="#64748b", bg="#0a0a0f", justify="center").pack(pady=(8, 0))

        self.form = tk.Frame(self, bg="#13131a", bd=0, highlightthickness=1,
                             highlightbackground="#1e1e2e")
        self.form.pack(padx=32, pady=24, fill="x")

        tk.Label(self.form, text="TON PRÉNOM", font=("Arial", 9, "bold"),
                 fg="#64748b", bg="#13131a").pack(anchor="w", padx=20, pady=(20, 4))
        self.name_var = tk.StringVar()
        self._entry(self.form, self.name_var, "ex: Laurent").pack(padx=20, fill="x")

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
        self.status_label.pack(pady=12)

    def _entry(self, parent, var, placeholder, show=None):
        e = tk.Entry(
            parent, textvariable=var,
            font=("Courier", 11),
            bg="#080810", fg="#e2e8f0",
            insertbackground="#6ee7b7",
            relief="flat", bd=0,
            highlightthickness=1,
            highlightbackground="#1e1e2e",
            highlightcolor="#6ee7b7",
            show=show or ""
        )
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

    def _on_submit(self):
        if self._mfa_pending:
            # L'utilisateur soumet le code 2FA
            code = self.mfa_var.get().strip()
            if not code:
                self._set_status("→ Entre le code reçu par email.", error=True)
                return
            self.btn.configure(state="disabled", text="Vérification...")
            threading.Thread(target=self._submit_mfa, args=(code,), daemon=True).start()
        else:
            # Login normal
            name  = self.name_var.get().strip()
            email = self.email_var.get().strip()
            pwd   = self.pwd_var.get().strip()

            if name in {"ex: Laurent", ""} or email in {"ton@email.com", ""} or not pwd:
                self._set_status("→ Tous les champs sont requis.", error=True)
                return

            self.btn.configure(state="disabled", text="Connexion en cours...")
            self._set_status("", error=False)
            threading.Thread(target=self._connect, args=(name, email, pwd), daemon=True).start()

    def _connect(self, name: str, email: str, pwd: str):
        try:
            self._set_status("Connexion à Garmin...", error=False, color="#6ee7b7")
            api = Garmin(email, pwd, return_on_mfa=True)
            result = api.login()
            print(dir(api.client))

            if result:
                client_state, _ = result
                self._api = api
                self._client_state = client_state  # sauvegarde le state
                self._name = name
                self._email = email
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
        """Affiche le champ de saisie du code 2FA."""
        self._mfa_pending = True

        # Ajoute le champ 2FA dans le formulaire
        tk.Label(self.form, text="CODE DE VÉRIFICATION (reçu par email)",
                 font=("Arial", 9, "bold"), fg="#fbbf24", bg="#13131a"
                 ).pack(anchor="w", padx=20, pady=(14, 4))

        self.mfa_var = tk.StringVar()
        tk.Entry(
            self.form, textvariable=self.mfa_var,
            font=("Courier", 14),
            bg="#080810", fg="#fbbf24",
            insertbackground="#fbbf24",
            relief="flat", bd=0,
            highlightthickness=1,
            highlightbackground="#fbbf24",
            justify="center"
        ).pack(padx=20, pady=(0, 20), fill="x")

        self.btn.configure(
            state="normal",
            text="Valider le code",
            bg="#fbbf24",
        )
        self._set_status(
            "Garmin a envoyé un code à ton email.\nEntre-le ci-dessus.",
            error=False, color="#fbbf24"
        )

    def _submit_mfa(self, code: str):
        try:
            print(f"Submitting MFA code: {code}")
            self._set_status("Vérification du code...", error=False, color="#6ee7b7")
            self._api.resume_login(self._client_state, mfa_code=code)
            print("MFA OK")
            self._send_token(self._api, self._name, self._email)
        except Exception as e:
            print(f"MFA ERROR: {e}")
            self._set_status(f"→ Code invalide ou expiré : {e}", error=True)
            self.btn.configure(state="normal", text="Valider le code")

    def _send_token(self, api, name, email):
        print("_send_token appelé")
        try:
            token_json = dump_token(api)
            print(f"Token généré : {token_json[:50]}...")
            self._set_status("Envoi sécurisé au serveur...", error=False, color="#6ee7b7")
            resp = requests.post(
                f"{BACKEND_URL}/users/register-token",
                json={"name": name, "email": email, "token_json": token_json},
                timeout=60,
            )
            print(f"Status: {resp.status_code}")
            print(f"Response: {resp.text}")
            if resp.status_code in (200, 201):
                self.after(0, lambda: self._show_success(name))
            else:
                detail = resp.json().get("detail", "Erreur inconnue")
                self._set_status(f"→ Erreur serveur : {detail}", error=True)
                self.btn.configure(state="normal", text="Connecter mon compte")
        except Exception as e:
            print(f"SEND TOKEN ERROR: {e}")
            self._set_status(f"→ Erreur envoi token : {e}", error=True)
            self.btn.configure(state="normal", text="Connecter mon compte")

    def _show_success(self, name: str):
        for widget in self.winfo_children():
            widget.destroy()

        tk.Label(self, text="✓", font=("Arial", 48), fg="#6ee7b7", bg="#0a0a0f").pack(pady=(60, 0))
        tk.Label(self, text="Compte connecté !", font=("Arial", 18, "bold"),
                 fg="#6ee7b7", bg="#0a0a0f").pack(pady=(8, 0))
        tk.Label(self,
                 text=f"Tes données Garmin, {name},\nvont être collectées automatiquement.",
                 font=("Arial", 11), fg="#94a3b8", bg="#0a0a0f", justify="center"
                 ).pack(pady=(12, 0))
        tk.Button(self, text="Fermer",
                  font=("Arial", 11), bg="#1e1e2e", fg="#e2e8f0",
                  relief="flat", cursor="hand2",
                  command=self.destroy, pady=10
                  ).pack(pady=32, padx=64, fill="x")

    def _set_status(self, msg: str, error: bool = True, color: str = None):
        self.after(0, lambda: self._update_status(msg, error, color))

    def _update_status(self, msg: str, error: bool, color: str):
        self.status_var.set(msg)
        c = color or ("#f87171" if error else "#6ee7b7")
        self.status_label.configure(fg=c)


if __name__ == "__main__":
    app = AionApp()
    app.mainloop()