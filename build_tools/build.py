"""Fabrique un exécutable autonome (pas besoin d'installer Python chez l'utilisateur).

    python build_tools/build.py

À lancer sur Mac pour obtenir l'app Mac, sur Windows pour le .exe Windows (PyInstaller ne fait pas de
compilation croisée : c'est GitHub Actions qui fabrique les trois versions, voir
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


def windows_version_numbers(version: str) -> tuple:
    """« 1.5.0 », « 1.5.0-beta2 », « 1.5.0-dev » → (1, 5, 0, 0) : les quatre nombres qu'attend Windows."""
    nums = [int(x) for x in re.findall(r"\d+", version.split("-")[0])][:3]
    return tuple(nums + [0] * (4 - len(nums)))


def windows_version_file(version: str) -> str:
    """Le fichier de ressources de version que PyInstaller inscrit dans TripCompta.exe (--version-file)."""
    n = windows_version_numbers(version)
    return f"""# UTF-8 — écrit par build_tools/build.py
VSVersionInfo(
  ffi=FixedFileInfo(filevers={n}, prodvers={n}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
                    date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040C04B0', [
      StringStruct('CompanyName', 'Trip Compta'),
      StringStruct('FileDescription', 'Trip Compta'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'TripCompta'),
      StringStruct('OriginalFilename', 'TripCompta.exe'),
      StringStruct('ProductName', 'Trip Compta'),
      StringStruct('ProductVersion', '{version}'),
      StringStruct('LegalCopyright', 'Logiciel libre et gratuit (licence MIT)'),
    ])]),
    VarFileInfo([VarStruct('Translation', [0x040C, 1200])])
  ]
)
"""


def main():
    version, commit, dev = version_info()
    BUILD_FILE.write_text(f'"""Écrit par build_tools/build.py."""\nVERSION = {version!r}\nCOMMIT = {commit!r}\n'
                          f"DEV = {dev!r}\n", encoding="utf-8")
    print(f"Fabrication de Trip Compta {version}" + (f" ({commit})" if commit else "") +
          (" — version de développement" if dev else ""))
    # Mac, hors GitHub : « dist.noindex », que Spotlight n'indexe pas (sinon la version de développement apparaît
    # dans le Launchpad à côté de l'app installée, avec la même icône et le même nom)
    dist = ROOT / ("dist.noindex" if sys.platform == "darwin" and not os.environ.get("CI") else "dist")
    sep = ";" if os.name == "nt" else ":"
    args = [
        str(ROOT / "run.py"),
        "--name", "TripCompta",                       # fichiers sans espace ; nom affiché : « Trip Compta »
        "--noconfirm",
        "--clean",
        "--add-data", f"{ROOT / 'compta' / 'templates'}{sep}compta/templates",
        "--add-data", f"{ROOT / 'compta' / 'static'}{sep}compta/static",
        "--add-data", f"{ROOT / 'compta' / 'scripts'}{sep}compta/scripts",
        "--add-data", f"{ROOT / 'compta' / 'exemple'}{sep}compta/exemple",
        "--collect-submodules", "uvicorn",
        "--collect-submodules", "mcp.server",
        "--collect-submodules", "mcp.shared",
        "--collect-submodules", "mcp.types",
        "--collect-data", "fpdf",
        "--collect-all", "pypdfium2",
        "--collect-all", "pypdfium2_raw",
        "--collect-submodules", "keyring.backends",      # trousseau du système (protection par mot de passe)
        "--copy-metadata", "keyring",
        "--collect-submodules", "compta.pages",          # les pages de l'application (web.py les installe)
        "--hidden-import", "compta.mcp_server",
        "--hidden-import", "compta.fenetre",
        "--hidden-import", "compta._build",
        "--distpath", str(dist),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
    ]
    icons = ROOT / "build_tools" / "icone"          # voir build_tools/icone/LISEZMOI.md
    if sys.platform == "darwin":
        args += ["--windowed", "--osx-bundle-identifier", "org.asso.comptavoyages",
                 "--icon", str(icons / "TripCompta.icns")]
    elif sys.platform == "win32":
        # Trip Compta s'ouvre dans sa propre fenêtre (pywebview + WebView2) : plus de console noire
        args += ["--windowed", "--icon", str(icons / "TripCompta.ico"),
                 "--collect-submodules", "webview.platforms", "--hidden-import", "clr"]
        # nom, description et numéro de version dans « Propriétés → Détails » du programme (et dans les
        # fenêtres de Windows qui demandent « Voulez-vous exécuter ce fichier ? »)
        info = ROOT / "build" / "version_windows.txt"
        info.parent.mkdir(exist_ok=True)
        info.write_text(windows_version_file(version), encoding="utf-8")
        args += ["--version-file", str(info)]
    else:
        args += ["--console"]   # autre système (non publié) : une fenêtre affiche l'adresse
    try:
        PyInstaller.__main__.run(args)
    finally:
        BUILD_FILE.unlink(missing_ok=True)      # le code source reste en « version de développement »
    if sys.platform == "darwin":
        plist = dist / "TripCompta.app" / "Contents" / "Info.plist"
        if plist.exists():                       # numéro visible dans « Lire les informations » du Finder
            import plistlib
            with open(plist, "rb") as f:
                data = plistlib.load(f)
            data["CFBundleShortVersionString"] = version.split("-")[0]
            data["CFBundleVersion"] = version
            # nom affiché (Dock, menu, Launchpad) : avec l'espace ; en développement, distinct de l'app installée
            data["CFBundleDisplayName"] = data["CFBundleName"] = "Trip Compta (dév)" if dev else "Trip Compta"
            with open(plist, "wb") as f:
                plistlib.dump(data, f)
            # Info.plist modifié : signer de nouveau (sinon macOS refuse d'ouvrir l'app) : Developer ID si
            # APPLE_SIGN_IDENTITY est donné (voir docs/SIGNATURE.md), sinon « ad hoc »
            sys.path.insert(0, str(ROOT / "build_tools"))
            import signer_mac
            print("Signature :", signer_mac.sign(plist.parent.parent))
    print(f"\nTerminé : Trip Compta {version}, voir le dossier {dist.name}/")


if __name__ == "__main__":
    main()
