"""Signature et notarisation de l'application Mac.

Sans réglage : signature « ad hoc » (comme jusqu'ici : l'app s'ouvre après « Ouvrir quand même » au premier
lancement). Avec un compte Apple Developer (99 $/an), voir docs/SIGNATURE.md :

    APPLE_SIGN_IDENTITY="Developer ID Application: Nom (EQUIPE1234)"      → signature Developer ID
    APPLE_ID, APPLE_TEAM_ID, APPLE_APP_PASSWORD                           → notarisation (et agrafage)

    python build_tools/signer_mac.py signer   dist/TripCompta.app
    python build_tools/signer_mac.py notariser dist/TripCompta.app

build.py appelle « signer » ; GitHub Actions (release.yml) appelle « notariser » après l'essai de démarrage.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTITLEMENTS = ROOT / "build_tools" / "mac" / "entitlements.plist"
MACHO = {b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"}


def is_macho(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            return f.read(4) in MACHO
    except OSError:
        return False


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"Échec : {' '.join(cmd)}\n{r.stdout}{r.stderr}")
    return r


def sign(app: Path) -> str:
    """Signe l'application ; renvoie « developer-id » ou « ad-hoc »."""
    identity = (os.environ.get("APPLE_SIGN_IDENTITY") or "").strip()
    if not identity:
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], capture_output=True)
        return "ad-hoc"
    base = ["codesign", "--force", "--timestamp", "--options", "runtime", "--entitlements", str(ENTITLEMENTS),
            "--sign", identity]
    # de l'intérieur vers l'extérieur (« --deep » ne convient pas à la distribution) : bibliothèques et
    # programmes d'abord, l'application en dernier
    inner = [p for p in app.rglob("*") if p.is_file() and not p.is_symlink() and is_macho(p)]
    main_exe = app / "Contents" / "MacOS" / app.stem
    for p in sorted(inner, key=lambda x: len(x.parts), reverse=True):
        if p != main_exe:
            run(base + [str(p)])
    run(base + [str(app)])
    run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)])
    return "developer-id"


def notarize(app: Path) -> bool:
    """Envoie l'application à Apple, attend le verdict, puis « agrafe » le ticket (l'app s'ouvre ensuite sans
    avertissement, même hors ligne). False si les identifiants ne sont pas fournis."""
    apple_id, team, pw = (os.environ.get(k, "").strip() for k in ("APPLE_ID", "APPLE_TEAM_ID", "APPLE_APP_PASSWORD"))
    if not (apple_id and team and pw):
        print("Notarisation : identifiants Apple non fournis, étape sautée.")
        return False
    with tempfile.TemporaryDirectory() as tmp:
        z = Path(tmp) / "TripCompta.zip"
        run(["ditto", "-c", "-k", "--keepParent", str(app), str(z)])
        r = run(["xcrun", "notarytool", "submit", str(z), "--apple-id", apple_id, "--team-id", team,
                 "--password", pw, "--wait"])
        print(r.stdout)
        if "status: Accepted" not in r.stdout:
            sys.exit("Notarisation refusée par Apple (voir ci-dessus : xcrun notarytool log <id>).")
    run(["xcrun", "stapler", "staple", str(app)])
    run(["spctl", "--assess", "--type", "execute", "--verbose", str(app)])
    print("Notarisation : acceptée et agrafée.")
    return True


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ("signer", "notariser"):
        sys.exit(__doc__)
    app = Path(sys.argv[2])
    if sys.argv[1] == "signer":
        print("Signature :", sign(app))
    else:
        notarize(app)


if __name__ == "__main__":
    main()
