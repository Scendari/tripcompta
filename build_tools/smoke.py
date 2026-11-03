"""Vérifie qu'une application fabriquée démarre et répond avec la bonne version (utilisé par GitHub Actions).

    python build_tools/smoke.py "dist/Trip Compta.app/Contents/MacOS/Trip Compta" 1.2.0
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main(exe: str, expected: str) -> int:
    tmp = tempfile.mkdtemp(prefix="tripcompta-essai-")
    port = free_port()
    env = dict(os.environ, TRIPCOMPTA_CONFIG=os.path.join(tmp, "reglages.json"), TRIPCOMPTA_NO_UPDATE_CHECK="1")
    log = open(os.path.join(tmp, "sortie.log"), "w")
    p = subprocess.Popen([exe, "--no-browser", "--port", str(port), "--data-dir", os.path.join(tmp, "donnees")],
                         env=env, stdout=log, stderr=subprocess.STDOUT)
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


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
