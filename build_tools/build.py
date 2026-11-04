"""Fabrique un exécutable autonome (pas besoin d'installer Python chez l'utilisateur).

    python build_tools/build.py

À lancer sur Mac pour obtenir l'app Mac, sur Windows pour le .exe Windows, sur Linux pour Linux (PyInstaller ne
fait pas de compilation croisée : c'est GitHub Actions qui fabrique les quatre versions, voir
.github/workflows/release.yml). Résultat dans dist/.

La version est inscrite dans l'application (compta/_build.py, effacé après) :
- variable d'environnement TRIPCOMPTA_RELEASE=v1.2.0 (ou v1.2.0-beta1), donnée par GitHub Actions : version
  publiée, qui pourra se mettre à jour toute seule. Elle doit commencer par compta.__version__ ;
- sinon : version de développement « X.Y.Z-dev » (commit), qui ne se met jamais à jour toute seule.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from compta import __version__  # noqa: E402

BUILD_FILE = ROOT / "compta" / "_build.py"


def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def version_info() -> tuple[str, str, bool]:
    commit = git_commit()
    tag = (os.environ.get("TRIPCOMPTA_RELEASE") or "").strip()
    if not tag:
        return f"{__version__}-dev", commit, True
    v = tag[1:] if tag.startswith("v") else tag
    m = re.match(r"^(\d+\.\d+\.\d+)(-(alpha|beta|rc)\d*)?$", v)
    if not m:
        sys.exit(f"Étiquette de version illisible : {tag} (attendu : v1.2.0 ou v1.2.0-beta1)")
    if m.group(1) != __version__:
        sys.exit(f"L'étiquette {tag} ne correspond pas à la version du code ({__version__}, dans compta/__init__.py).")
    return v, commit, False


def main():
    version, commit, dev = version_info()
    BUILD_FILE.write_text(f'"""Écrit par build_tools/build.py."""\nVERSION = {version!r}\nCOMMIT = {commit!r}\n'
                          f"DEV = {dev!r}\n", encoding="utf-8")
    print(f"Fabrication de Trip Compta {version}" + (f" ({commit})" if commit else "") +
          (" — version de développement" if dev else ""))
    sep = ";" if os.name == "nt" else ":"
    args = [
        str(ROOT / "run.py"),
        "--name", "Trip Compta",
        "--noconfirm",
        "--clean",
        "--add-data", f"{ROOT / 'compta' / 'templates'}{sep}compta/templates",
        "--add-data", f"{ROOT / 'compta' / 'static'}{sep}compta/static",
        "--add-data", f"{ROOT / 'compta' / 'scripts'}{sep}compta/scripts",
        "--collect-submodules", "uvicorn",
        "--collect-submodules", "mcp.server",
        "--collect-submodules", "mcp.shared",
        "--collect-submodules", "mcp.types",
        "--collect-data", "fpdf",
        "--collect-all", "pypdfium2",
        "--collect-all", "pypdfium2_raw",
        "--collect-submodules", "keyring.backends",      # trousseau du système (protection par mot de passe)
        "--copy-metadata", "keyring",
        "--hidden-import", "compta.mcp_server",
        "--hidden-import", "compta._build",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
    ]
    icons = ROOT / "build_tools" / "icone"          # voir build_tools/icone/LISEZMOI.md
    if sys.platform == "darwin":
        args += ["--windowed", "--osx-bundle-identifier", "org.asso.comptavoyages",
                 "--icon", str(icons / "TripCompta.icns")]
    else:
        args += ["--console"]   # une fenêtre affiche l'adresse ; la fermer arrête l'application
        if sys.platform == "win32":
            args += ["--icon", str(icons / "TripCompta.ico")]
    try:
        PyInstaller.__main__.run(args)
    finally:
        BUILD_FILE.unlink(missing_ok=True)      # le code source reste en « version de développement »
    if sys.platform == "darwin":
        plist = ROOT / "dist" / "Trip Compta.app" / "Contents" / "Info.plist"
        if plist.exists():                       # numéro visible dans « Lire les informations » du Finder
            import plistlib
            with open(plist, "rb") as f:
                data = plistlib.load(f)
            data["CFBundleShortVersionString"] = version.split("-")[0]
            data["CFBundleVersion"] = version
            if dev:                              # distincte de l'app installée dans le Launchpad et Spotlight
                data["CFBundleDisplayName"] = data["CFBundleName"] = "Trip Compta (dév)"
            with open(plist, "wb") as f:
                plistlib.dump(data, f)
            # Info.plist modifié : refaire la signature « ad hoc » (sinon macOS refuse d'ouvrir l'app)
            subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(plist.parent.parent)],
                           capture_output=True)
    print(f"\nTerminé : Trip Compta {version}, voir le dossier dist/")


if __name__ == "__main__":
    main()
