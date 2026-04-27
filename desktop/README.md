# AION Garmin Connector — Desktop App

Petite application Windows qui permet à un utilisateur de connecter son compte Garmin à AION en double-cliquant sur un `.exe`.

## Compilation

```bash
cd aion_backend

# Installer les dépendances
pip install pyinstaller garminconnect requests

# Compiler
python desktop/build.py
```

Le `.exe` est généré dans `desktop/dist/AION Garmin Connector.exe`.

## Distribution

1. Mettre le `.exe` en téléchargement sur le site Railway (ou partager directement)
2. L'utilisateur double-clique
3. Rentre son prénom + email + mdp Garmin
4. Le token est envoyé automatiquement au backend Railway
5. Ferme l'app — c'est fait

## Modifier l'URL du backend

Dans `desktop/main.py`, ligne 17 :
```python
BACKEND_URL = "https://web-production-3668.up.railway.app"
```

## Structure

```
desktop/
├── main.py          ← code source de l'app
├── build.py         ← script de compilation
├── dist/            ← .exe généré (ignoré par git)
└── build_tmp/       ← fichiers temporaires (ignorés par git)
```
