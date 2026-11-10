"""Vérifie qu'une application fabriquée démarre et répond avec la bonne version (utilisé par GitHub Actions).

    python build_tools/smoke.py "dist/TripCompta.app/Contents/MacOS/TripCompta" 1.2.0
    python build_tools/smoke.py "dist/TripCompta/TripCompta.exe" 1.2.0 --fenetre

--fenetre (Mac, Windows) : ouvre la VRAIE fenêtre (pywebview : WebKit, ou WebView2 + .NET sous Windows), en mode
discret (hors de l'écran, sans prendre la main) ; vérifie qu'elle affiche Trip Compta et qu'une action faite DANS la
page passe le contrôle d'origine du serveur.
"""
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Jeton de session : l'essai le choisit (variable TRIPCOMPTA_JETON, comme après un redémarrage) et le joint à toutes
# ses requêtes ; sans lui, l'application ne répond qu'à /ping.
TOKEN = secrets.token_hex(16)
_opener = urllib.request.build_opener()
_opener.addheaders = [("X-TripCompta-Jeton", TOKEN)]
urllib.request.install_opener(_opener)


def refused_without_token(base: str) -> bool:
    """Sans le jeton, l'application refuse (403) : ni page, ni API."""
    for path in ("", "api/voyages"):
        try:
            urllib.request.build_opener().open(base + path, timeout=10).close()
            return False
        except urllib.error.HTTPError as e:
            if e.code != 403:
                return False
        except OSError:
            return False
    return True


try:                                                      # Windows : console en cp1252, le journal a des accents
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def get(url, data=None):
    import urllib.parse
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode() if data is not None else None)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8"), r.geturl()


def protection(base: str, docs: str) -> bool:
    """Protection par mot de passe dans l'application fabriquée : chiffrement (cryptography), redémarrage,
    déverrouillage ; rien de lisible dans le dossier."""
    import re
    pw = "essai du chiffrement"
    get(base + "voyages", {"name": "Voyage secret essai"})
    page, _ = get(base + "protection/activer", {"mot_de_passe": pw, "confirmation": pw})
    key = re.search(r'id="recovery">([A-Z2-9-]+)<', page).group(1)
    n = int(re.search(r"groupe n° (\d+)", page).group(1))
    page, _ = get(base + "protection/confirmer", {"groupe": key.split("-")[n - 1]})
    if "maintenant protégés" not in page:
        print("Activation : ÉCHEC")
        return False
    folder = os.path.join(docs, "TripCompta")
    blob = open(os.path.join(folder, "voyages.chiffre"), "rb").read()
    clear = any(b"Voyage secret essai" in open(os.path.join(dp, f), "rb").read()
                for dp, _, fs in os.walk(folder) for f in fs)
    print(f"Chiffré : {len(blob)} octets ; texte lisible dans le dossier : {'OUI (ÉCHEC)' if clear else 'non'}")
    for _ in range(60):                                  # l'application redémarre toute seule
        time.sleep(1)
        try:
            page, url = get(base)
            if url.endswith("/deverrouiller"):
                break
        except OSError:
            pass
    else:
        print("Après redémarrage : pas d'écran de déverrouillage")
        return False
    print("Trousseau du système :", "oui" if "Se souvenir sur cet ordinateur" in page else "non (repli)")
    page, url = get(base + "deverrouiller", {"mot_de_passe": pw})
    ok = "Voyage secret essai" in page and not clear
    print("Déverrouillage :", "ok" if ok else "ÉCHEC")
    return ok


def main(exe: str, expected: str) -> int:
    tmp = tempfile.mkdtemp(prefix="tripcompta-essai-")
    port = free_port()
    docs = os.path.join(tmp, "Documents")
    env = dict(os.environ, TRIPCOMPTA_CONFIG=os.path.join(tmp, "reglages.json"), TRIPCOMPTA_NO_UPDATE_CHECK="1",
               TRIPCOMPTA_DOCUMENTS=docs, TRIPCOMPTA_JETON=TOKEN)
    env.pop("TRIPCOMPTA_DATA_DIR", None)
    log = open(os.path.join(tmp, "sortie.log"), "w")
    p = subprocess.Popen([os.path.abspath(exe), "--no-browser", "--port", str(port)], env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(180):
            if p.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=2) as r:
                    info = json.load(r)
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as r:
                    home = r.read().decode("utf-8")
                ok = info.get("app") == "tripcompta" and info.get("version") == expected and "Trip Compta" in home
                print(f"Réponse : {info} ; page d'accueil : {'ok' if 'Trip Compta' in home else 'INCORRECTE'}")
                if ok:
                    ok = bool(info.get("jeton")) and refused_without_token(f"http://127.0.0.1:{port}/")
                    print("Jeton de session : " + ("exigé" if ok else "PAS EXIGÉ"))
                if ok:
                    # la vérification de signature des mises à jour marche dans l'application fabriquée
                    diag, _ = get(f"http://127.0.0.1:{port}/reglages/diagnostic.txt")
                    ok = "vérification de la signature des mises à jour : prête" in diag
                    print("Signature des mises à jour : " + ("prête" if ok else "INDISPONIBLE dans cette application"))
                if ok:
                    ok = protection(f"http://127.0.0.1:{port}/", docs)
                return 0 if ok else 1
            except OSError:
                time.sleep(1)
        print("L'application n'a pas répondu.")
        return 1
    finally:
        try:
            urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/quitter", method="POST"), timeout=3)
        except OSError:
            pass
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()
        log.close()
        print(open(os.path.join(tmp, "sortie.log"), encoding="utf-8", errors="replace").read()[-3000:])


def window(exe: str, expected: str) -> int:
    tmp = tempfile.mkdtemp(prefix="tripcompta-fenetre-")
    port = free_port()
    env = dict(os.environ, TRIPCOMPTA_CONFIG=os.path.join(tmp, "reglages.json"), TRIPCOMPTA_NO_UPDATE_CHECK="1",
               TRIPCOMPTA_DOCUMENTS=os.path.join(tmp, "Documents"), TRIPCOMPTA_FENETRE_DISCRETE="1",
               TRIPCOMPTA_ESSAI_FENETRE="1", TRIPCOMPTA_JETON=TOKEN)
    env.pop("TRIPCOMPTA_DATA_DIR", None)
    log = open(os.path.join(tmp, "sortie.log"), "w")
    p = subprocess.Popen([os.path.abspath(exe), "--port", str(port)], env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}/"
    ok = False
    try:
        info = {}
        for _ in range(120):
            if p.poll() is not None:
                print("L'application s'est arrêtée.")
                break
            try:
                with urllib.request.urlopen(base + "ping", timeout=2) as r:
                    info = json.load(r)
                if info.get("fenetre"):
                    break
            except OSError:
                pass
            time.sleep(1)
        print(f"Réponse : {info}")
        win = info.get("fenetre") or {}
        engine = "WebView2" if sys.platform == "win32" else "WebKit"
        ok = info.get("version") == expected and "Trip Compta" in (win.get("titre") or "") and win.get("moteur") == engine
        print(f"Vraie fenêtre ({engine}) : {'affiche Trip Compta' if ok else 'ÉCHEC'}")
        if ok:
            ok = False
            for _ in range(30):
                with urllib.request.urlopen(base + "api/voyages", timeout=5) as r:
                    if "Essai de la fenêtre" in r.read().decode("utf-8"):
                        ok = True
                        break
                time.sleep(1)
            print("Action dans la fenêtre (contrôle d'origine, jeton de session reçu par la fenêtre) :",
                  "ok" if ok else "ÉCHEC")
        return 0 if ok else 1
    finally:
        try:
            urllib.request.urlopen(urllib.request.Request(base + "quitter", method="POST"), timeout=3)
        except OSError:
            pass
        try:
            p.wait(15)
        except subprocess.TimeoutExpired:
            p.kill()
        log.close()
        print(open(os.path.join(tmp, "sortie.log"), encoding="utf-8", errors="replace").read()[-3000:])


if __name__ == "__main__":
    if "--fenetre" in sys.argv[3:]:
        sys.exit(window(sys.argv[1], sys.argv[2]))
    sys.exit(main(sys.argv[1], sys.argv[2]))
