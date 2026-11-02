"""Fabrique un exécutable autonome (pas besoin d'installer Python chez l'utilisateur).

    python build_tools/build.py

À lancer sur Mac pour obtenir l'app Mac, sur Windows pour obtenir le .exe Windows
(PyInstaller ne fait pas de compilation croisée). Résultat dans dist/.
"""
import os
import sys
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent.parent
sep = ";" if os.name == "nt" else ":"
args = [
    str(ROOT / "run.py"),
    "--name", "Trip Compta",
    "--noconfirm",
    "--clean",
    "--add-data", f"{ROOT / 'compta' / 'templates'}{sep}compta/templates",
    "--add-data", f"{ROOT / 'compta' / 'static'}{sep}compta/static",
    "--collect-submodules", "uvicorn",
    "--collect-submodules", "mcp.server",
    "--collect-submodules", "mcp.shared",
    "--collect-submodules", "mcp.types",
    "--collect-data", "fpdf",
    "--collect-all", "pypdfium2",
    "--collect-all", "pypdfium2_raw",
    "--hidden-import", "compta.mcp_server",
    "--distpath", str(ROOT / "dist"),
    "--workpath", str(ROOT / "build"),
    "--specpath", str(ROOT / "build"),
]
if sys.platform == "darwin":
    args += ["--windowed", "--osx-bundle-identifier", "org.asso.comptavoyages"]
else:
    args += ["--console"]   # une fenêtre affiche l'adresse ; la fermer arrête l'application
PyInstaller.__main__.run(args)
print("\nTerminé : voir le dossier dist/")
