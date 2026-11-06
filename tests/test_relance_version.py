"""Une version plus ancienne encore ouverte (l'utilisateur a remplacé l'app sans la quitter) : la nouvelle lui demande
de s'arrêter proprement et prend sa place. Même version ou plus récente : on n'y touche pas."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import run


def _fake(version):
    """Une « Trip Compta » minimale : /ping, et /quitter qui l'arrête."""
    state = {"quit": 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/ping":
                body = json.dumps({"app": "tripcompta", "version": version}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            if self.path == "/quitter":
                state["quit"] += 1
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
                threading.Thread(target=srv.shutdown, daemon=True).start()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/", state


def _close(srv):
    srv.server_close()


def test_older_running_version_is_stopped_and_replaced():
    srv, url, state = _fake("1.2.0")
    try:
        srv.socket.settimeout(None)
        assert run.take_over_older(url, mine="1.5.0", wait=5) is True
        assert state["quit"] == 1
    finally:
        _close(srv)


def test_same_or_newer_running_version_is_kept():
    for running in ("1.5.0", "1.6.0", "1.5.0-dev"):
        srv, url, state = _fake(running)
        try:
            expected = running == "1.5.0-dev"                 # 1.5.0 publiée passe devant 1.5.0-dev
            assert run.take_over_older(url, mine="1.5.0", wait=5) is expected, running
            assert state["quit"] == (1 if expected else 0)
        finally:
            srv.shutdown()
            _close(srv)


def test_unreadable_running_version_is_kept():
    srv, url, state = _fake(None)
    try:
        assert run.take_over_older(url, mine="1.5.0", wait=5) is False and state["quit"] == 0
    finally:
        srv.shutdown()
        _close(srv)


def test_nobody_answering_is_not_a_takeover():
    assert run.take_over_older("http://127.0.0.1:9/", mine="1.5.0", wait=1) is False


LAUNCH = r'''
import sys
sys.path.insert(0, sys.argv[1])
from compta import version as V
v = sys.argv[2]
V.build_info = lambda: {"version": v, "commit": "", "dev": False}
import run
sys.argv = ["TripCompta", "--no-browser", "--data-dir", sys.argv[3], "--port", sys.argv[4]]
run.main()
'''


def test_end_to_end_new_version_takes_over(tmp_path, monkeypatch):
    """Deux vrais lancements sur les mêmes voyages : la 1.2.0 tourne, on lance la 1.5.0 → la 1.2.0 s'arrête (proprement)
    et la 1.5.0 répond à sa place. Relancer la 1.5.0 ensuite ne change rien (même version : on garde celle qui tourne)."""
    import os
    import socket
    import subprocess
    import sys
    import time
    import urllib.request
    from pathlib import Path
    root = Path(run.__file__).parent
    script = tmp_path / "lancer.py"
    script.write_text(LAUNCH, encoding="utf-8")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = dict(os.environ, TRIPCOMPTA_NO_UPDATE_CHECK="1", TRIPCOMPTA_FENETRE_DISCRETE="1",
               TRIPCOMPTA_CONFIG=str(tmp_path / "conf" / "reglages.json"), HOME=str(tmp_path / "home"))
    env.pop("TRIPCOMPTA_RESTARTED", None)
    data = tmp_path / "voyages"

    def launch(v):
        return subprocess.Popen([sys.executable, str(script), str(root), v, str(data), str(port)], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def ping(timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            for p in range(port, port + 5):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{p}/ping", timeout=0.5) as r:
                        return json.load(r).get("version")
                except Exception:
                    pass
            time.sleep(0.2)
        return None

    old = launch("1.2.0")
    new = again = None
    try:
        assert ping() == "1.2.0"
        new = launch("1.5.0")
        assert old.wait(timeout=30) == 0                          # l'ancienne s'est arrêtée
        assert ping() == "1.5.0" and new.poll() is None           # la nouvelle tourne à sa place
        again = launch("1.5.0")
        out, _ = again.communicate(timeout=30)
        assert again.returncode == 0 and "déjà ouvert" in out     # même version : on garde celle qui tourne
        assert ping() == "1.5.0" and new.poll() is None
    finally:
        for p in (old, new, again):
            if p and p.poll() is None:
                p.kill()
