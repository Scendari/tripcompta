"""Essai de l'installeur Windows, pour de vrai (GitHub Actions, Windows) :

    python build_tools/essai_installeur.py dist/TripCompta-windows-setup.exe 1.3.2-dev

1. installation silencieuse (dossier d'essai), raccourci du menu Démarrer, désinstalleur ;
2. l'application installée démarre et répond ;
3. mise à jour comme Trip Compta la lance (compta/updater.py) : l'installeur attend la fermeture de « l'ancienne »,
   la met de côté, installe, relance « TripCompta.exe --apres-maj » avec les options d'avant (même adresse), attend
   que la nouvelle dise « j'ai démarré », puis efface l'ancienne ;
3 bis. une nouvelle version qui ne dit jamais « j'ai démarré » : l'installeur l'arrête, remet l'ancienne à sa place,
   écrit le message d'échec et relance l'ancienne ;
4. désinstallation : le programme est retiré, les voyages restent.
Rien n'est envoyé au vrai dossier de l'utilisateur : réglages et voyages dans un dossier temporaire.
"""
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# Jeton de session : l'essai le choisit (variable TRIPCOMPTA_JETON, comme après un redémarrage) et le joint à toutes
# ses requêtes ; sans lui, l'application ne répond qu'à /ping.
TOKEN = secrets.token_hex(16)
_opener = urllib.request.build_opener()
_opener.addheaders = [("X-TripCompta-Jeton", TOKEN)]
urllib.request.install_opener(_opener)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ping(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=2) as r:
            return json.load(r).get("version")
    except Exception:
        return None


def wait_ping(port, version, seconds=90):
    for _ in range(seconds * 2):
        if ping(port) == version:
            return True
        time.sleep(0.5)
    return False


def quit_app(port):
    try:
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/quitter", data=b"", method="POST"),
                               timeout=5)
    except Exception:
        pass
    for _ in range(40):
        if ping(port) is None:
            return
        time.sleep(0.25)


def wait_for(cond, seconds):
    for _ in range(seconds * 2):
        if cond():
            return True
        time.sleep(0.5)
    return False


def check(ok, what):
    print(("OK   " if ok else "ÉCHEC ") + what)
    if not ok:
        sys.exit(1)


def main():
    setup, version = Path(sys.argv[1]).resolve(), sys.argv[2]
    tmp = Path(tempfile.mkdtemp(prefix="essai installeur "))       # espaces dans le chemin
    inst = tmp / "Programmes" / "Trip Compta essai"
    data = tmp / "Mes voyages"
    conf = tmp / "réglages" / "reglages.json"
    env = dict(os.environ, TRIPCOMPTA_CONFIG=str(conf), TRIPCOMPTA_DATA_DIR=str(data), TRIPCOMPTA_NO_UPDATE_CHECK="1",
               TRIPCOMPTA_JETON=TOKEN,
               TRIPCOMPTA_FENETRE_DISCRETE="1")
    base = [str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", f"/DIR={inst}"]

    # 1. installation
    r = subprocess.run(base + [f"/LOG={tmp / 'installation-1.log'}"], env=env, timeout=600)
    exe = inst / "TripCompta.exe"
    check(r.returncode == 0 and exe.exists(), f"installation silencieuse dans « {inst} »")
    check(any(inst.glob("unins*.exe")), "désinstalleur présent")
    menu = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Trip Compta.lnk"
    check(menu.exists(), "raccourci du menu Démarrer")
    check((inst / "_internal").is_dir(), "fichiers de l'application")

    # 2. l'application installée démarre
    port = free_port()
    p = subprocess.Popen([str(exe), "--navigateur", "--no-browser", "--port", str(port)], env=env)
    check(wait_ping(port, version), f"l'application installée répond (version {version})")
    quit_app(port)
    p.wait(30)

    # 3. mise à jour comme la lance Trip Compta : « l'ancienne » se ferme pendant que l'installeur attend
    from compta import updater as U
    work = conf.parent / "maj"
    work.mkdir(parents=True, exist_ok=True)
    port2 = free_port()
    U.write_relaunch(work, port2, ["--navigateur"])
    old = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(4)"])      # « l'appli qui se ferme »
    cmd = U.installer_command(setup, work, exe, old.pid)          # sans /DIR : l'installeur reprend « inst »
    before = exe.stat().st_mtime
    t0 = time.time()
    subprocess.Popen(cmd, env=env)
    check(wait_ping(port2, version, 180), "après la mise à jour, Trip Compta est relancé à la même adresse")
    check(old.poll() is not None and time.time() - t0 >= 3.5, "l'installeur a attendu la fermeture de l'ancienne")
    check(not (work / U.RELAUNCH).exists(), "options de relance lues (et effacées)")
    check(not (work / "echec.txt").exists(), "pas d'échec signalé")
    check(exe.stat().st_mtime != before or (inst / "unins000.dat").stat().st_mtime > t0 - 1,
          "mise à jour installée au même endroit que la première installation")
    aside, spoiled = Path(str(inst) + ".ancien"), Path(str(inst) + ".rate")
    check(wait_for(lambda: (work / U.STARTED).exists(), 60), "la nouvelle version a dit « j'ai démarré »")
    check((work / U.STARTED).read_text(encoding="utf-8").strip() == version, "… avec son numéro de version")
    check(wait_for(lambda: not aside.exists(), 120), "l'ancienne version, mise de côté, est effacée après coup")
    quit_app(port2)
    time.sleep(2)

    # 3 bis. la nouvelle version ne dit jamais « j'ai démarré » (l'installeur regarde un autre dossier que le sien)
    (inst / "temoin-ancienne.txt").write_text("ancienne version", encoding="utf-8")
    elsewhere = tmp / "maj sans réponse"
    elsewhere.mkdir()
    port3, port4 = free_port(), free_port()
    U.write_relaunch(work, port3, ["--navigateur"])
    old = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"])
    cmd = [str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", f"/LOG={tmp / 'installation-3.log'}",
           f"/ATTENDRE={old.pid}", f"/ECHEC={elsewhere / 'echec.txt'}", f"/ANCIEN={exe}", "/ATTENTE=25"]
    setup_run = subprocess.Popen(cmd, env=env)
    check(wait_ping(port3, version, 180), "essai du retour arrière : la nouvelle version est lancée")
    check(not (inst / "temoin-ancienne.txt").exists() and (aside / "temoin-ancienne.txt").exists(),
          "… pendant ce temps, l'ancienne est de côté")
    U.write_relaunch(work, port4, ["--navigateur"])            # pour la relance de l'ancienne
    try:
        setup_run.wait(300)
    except subprocess.TimeoutExpired:
        check(False, "l'installeur a fini")
    check(wait_for(lambda: (elsewhere / "echec.txt").exists(), 30), "sans « j'ai démarré » : échec signalé")
    said = (elsewhere / "echec.txt").read_text(encoding="utf-8-sig")
    check("n'a pas démarré correctement" in said and "remise en place" in said, f"message d'échec lisible : {said.strip()}")
    check((inst / "temoin-ancienne.txt").exists(), "l'ancienne version est remise à sa place")
    check(not aside.exists() and not spoiled.exists(), "aucun dossier de secours ne traîne")
    check(ping(port3) is None, "la version qui ne répondait pas est arrêtée")
    check(wait_ping(port4, version, 180), "l'ancienne version est relancée")
    quit_app(port4)
    time.sleep(2)

    # 4. désinstallation : les voyages restent
    data.mkdir(parents=True, exist_ok=True)
    (data / "témoin.txt").write_text("à garder", encoding="utf-8")
    unins = sorted(inst.glob("unins*.exe"))[0]
    r = subprocess.run([str(unins), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], env=env, timeout=300)
    for _ in range(60):                                      # le désinstalleur se recopie et finit en arrière-plan
        if not exe.exists():
            break
        time.sleep(1)
    check(not exe.exists(), "désinstallation : programme retiré")
    check((data / "témoin.txt").exists(), "désinstallation : les voyages ne sont pas touchés")
    check(not menu.exists(), "désinstallation : raccourci retiré")
    print("Installeur : tout est bon.")


if __name__ == "__main__":
    main()
