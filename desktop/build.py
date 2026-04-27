"""
desktop/build.py — Compile main.py en .exe avec PyInstaller

Usage:
    cd aion_backend
    python desktop/build.py
"""

import subprocess
import sys

cmd = [
    sys.executable, "-m", "PyInstaller",
    "--onefile",                          # Un seul .exe
    "--windowed",                         # Pas de console noire
    "--name", "AION Garmin Connector",
    "--distpath", "desktop/dist",         # Sortie dans desktop/dist/
    "--workpath", "desktop/build_tmp",    # Fichiers temporaires
    "--specpath", "desktop",
    "desktop/main.py",
]

print("Compilation en cours...")
result = subprocess.run(cmd)

if result.returncode == 0:
    print("\n✓ Compilation réussie !")
    print("→ Fichier : desktop/dist/AION Garmin Connector.exe")
    print("→ Envoie ce fichier à tes utilisateurs")
else:
    print("\n✗ Erreur de compilation")
    sys.exit(1)
