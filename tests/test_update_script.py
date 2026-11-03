"""Script de remplacement (remplacer.sh / remplacer.ps1), de bout en bout, avec de fausses applications :
un petit serveur qui répond sur /ping avec sa version. Tourne sur Mac et Linux (sh) et sous Windows (PowerShell) :
GitHub Actions le lance sur les trois systèmes avant de fabriquer l'application."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "compta" / "scripts"
WIN = sys.platform == "win32"

FAKE_SERVER = r'''
import json, os, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
version = sys.argv[1]
args = sys.argv[2:]
port = int(args[args.index("--port") + 1])
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "lance-avec.json"), "w") as f:
    json.dump(dict(args=args, restarted=os.environ.get("TRIPCOMPTA_RESTARTED")), f)
if version == "casse":
    sys.exit(3)
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        body = json.dumps({"app": "tripcompta", "boot": "x", "version": version}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(body)
    def do_POST(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        threading.Thread(target=srv.shutdown).start()
srv = HTTPServer(("127.0.0.1", port), H)
srv.serve_forever()
'''


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_app(folder: Path, version: str) -> str:
    """Une fausse application « Trip Compta » (dossier) ; renvoie le chemin du programme dedans."""
    folder.mkdir(parents=True)
    (folder / "serveur.py").write_text(FAKE_SERVER, encoding="utf-8")
    (folder / "VERSION.txt").write_text(version, encoding="utf-8")
    if WIN:
        exe = "Trip Compta.cmd"
        (folder / exe).write_text(f'@echo off\r\n"{sys.executable}" "%~dp0serveur.py" {version} %*\r\n',
                                  encoding="utf-8")
    else:
        exe = "Trip Compta"
        (folder / exe).write_text(f'#!/bin/sh\nexec "{sys.executable}" "$(dirname "$0")/serveur.py" {version} "$@"\n',
                                  encoding="utf-8")
        os.chmod(folder / exe, 0o755)
    return exe


def ping(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=2) as r:
            return json.load(r)["version"]
    except Exception:
        return None


def stop(port):
    try:
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/quitter", method="POST"), timeout=2)
    except Exception:
        pass


def run_script(target, new, exe, port, version, work, extra=(), wait=25):
    old_app = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5)"])   # « l'appli qui se ferme »
    threading.Thread(target=old_app.wait, daemon=True).start()     # récupérée dès sa fin (pas de « zombie »)
    env = dict(os.environ, TRIPCOMPTA_MAJ_ATTENTE=str(wait))
    env.pop("TRIPCOMPTA_RESTARTED", None)
    args = [str(old_app.pid), str(target), str(new), exe, str(port), version, str(work), *extra]
    if WIN:
        cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPTS / "remplacer.ps1"), *args]
    else:
        cmd = ["/bin/sh", str(SCRIPTS / "remplacer.sh"), *args]
    t0 = time.time()
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=wait + 60)
    r.duration = time.time() - t0
    log = Path(work) / "maj.log"
    print("--- journal du script ---\n" + (log.read_text(encoding="utf-8-sig", errors="replace") if log.exists() else "(aucun)")
          + "\n--- sortie ---\n" + r.stdout + r.stderr)          # visible si le test échoue
    return r


@pytest.fixture
def setup(tmp_path):
    base = tmp_path / "Mes Applications"                  # espaces dans le chemin, comme « Program Files »
    target = base / "Trip Compta"
    exe = make_app(target, "1.0.0")
    work = tmp_path / "maj"
    work.mkdir()
    port = free_port()
    yield target, exe, work, port, tmp_path
    stop(port)
    time.sleep(0.3)


def test_replace_and_relaunch(setup):
    target, exe, work, port, tmp = setup
    new = work / "nouvelle" / "Trip Compta"
    make_app(new, "1.1.0")
    r = run_script(target, new, exe, port, "1.1.0", work, extra=["--data-dir", str(tmp / "mes données")])
    # l'attente de la fermeture marche (sans aller jusqu'à l'arrêt forcé, au bout d'une minute)
    assert "arrêt forcé" not in (work / "maj.log").read_text(encoding="utf-8-sig", errors="replace")
    assert r.returncode == 0, r.stdout + r.stderr + (work / "maj.log").read_text(encoding="utf-8", errors="replace")
    assert (target / "VERSION.txt").read_text() == "1.1.0"                 # remplacée au même endroit
    assert ping(port) == "1.1.0"
    assert (work / "reussi.txt").read_text(encoding="utf-8-sig").strip() == "1.1.0"
    assert not (work / "echec.txt").exists()
    assert not Path(str(target) + ".ancienne").exists()                    # l'ancienne est effacée une fois sûr
    launched = json.loads((target / "lance-avec.json").read_text())
    assert launched["args"][:3] == ["--no-browser", "--port", str(port)]   # pas de nouvel onglet, même adresse
    assert launched["args"][3:] == ["--data-dir", str(tmp / "mes données")]  # mêmes options qu'avant
    assert launched["restarted"] == "1"


def test_broken_new_version_puts_old_back(setup):
    target, exe, work, port, tmp = setup
    new = work / "nouvelle" / "Trip Compta"
    make_app(new, "casse")                                                 # ne démarre pas
    r = run_script(target, new, exe, port, "1.1.0", work, wait=5)
    assert r.returncode == 1
    assert (target / "VERSION.txt").read_text() == "1.0.0"                 # l'ancienne est revenue
    assert not Path(str(target) + ".ancienne").exists()
    msg = (work / "echec.txt").read_text(encoding="utf-8-sig")
    assert "1.1.0 n'a pas démarré" in msg and "ancienne version a été remise" in msg
    for _ in range(120):                                                   # et elle est relancée
        if ping(port) == "1.0.0":
            break
        time.sleep(0.25)
    assert ping(port) == "1.0.0"


def test_missing_new_version_keeps_old(setup):
    target, exe, work, port, tmp = setup
    r = run_script(target, work / "nouvelle" / "absente", exe, port, "1.1.0", work, wait=5)
    assert r.returncode == 1
    assert (target / "VERSION.txt").read_text() == "1.0.0"
    assert "Impossible d'installer la nouvelle version" in (work / "echec.txt").read_text(encoding="utf-8-sig")
    for _ in range(120):
        if ping(port) == "1.0.0":
            break
        time.sleep(0.25)
    assert ping(port) == "1.0.0"
