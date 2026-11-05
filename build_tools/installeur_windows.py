"""Fabrique l'installeur Windows (Inno Setup 6) autour de l'application fabriquée par build_tools/build.py.

    python build_tools/build.py
    python build_tools/installeur_windows.py 1.3.2          → dist/TripCompta-windows-setup.exe

Le programme d'installation de WebView2 de Microsoft (« Evergreen bootstrapper », 2 Mo, il télécharge le reste)
est ajouté à l'installeur : lancé seulement s'il manque WebView2 (Windows 10 ancien).
"""
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = ROOT / "build_tools" / "windows"
WEBVIEW2_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
BOOTSTRAPPER = HERE / "MicrosoftEdgeWebview2Setup.exe"


def iscc() -> str:
    for cand in (shutil.which("iscc"), shutil.which("ISCC"),
                 os.path.expandvars(r"%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"),
                 os.path.expandvars(r"%ProgramFiles%\Inno Setup 6\ISCC.exe"),
                 os.path.expandvars(r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe")):
        if cand and Path(cand).exists():
            return cand
    sys.exit("Inno Setup 6 introuvable (ISCC.exe) : https://jrsoftware.org/isdl.php, ou « choco install innosetup ».")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    version = sys.argv[1].lstrip("v")
    if not (ROOT / "dist" / "TripCompta" / "TripCompta.exe").exists():
        sys.exit("dist/TripCompta/TripCompta.exe introuvable : lance d'abord build_tools/build.py.")
    if not BOOTSTRAPPER.exists():
        print("Téléchargement du programme d'installation de WebView2…")
        with urllib.request.urlopen(WEBVIEW2_URL, timeout=60) as r, open(BOOTSTRAPPER, "wb") as f:
            shutil.copyfileobj(r, f)
    out = ROOT / "dist" / "TripCompta-windows-setup.exe"
    out.unlink(missing_ok=True)
    subprocess.run([iscc(), f"/DAppVersion={version}", "/Qp", str(HERE / "TripCompta.iss")], check=True)
    if not out.exists():
        sys.exit("L'installeur n'a pas été fabriqué.")
    print(f"Terminé : {out.relative_to(ROOT)} ({out.stat().st_size // 1024 // 1024} Mo)")


if __name__ == "__main__":
    main()
