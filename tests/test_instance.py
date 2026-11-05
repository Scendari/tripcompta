"""Une seule Trip Compta par dossier de voyages sur un ordinateur (verrou du système, comme sur Mac et Windows) :
un 2e lancement montre celle qui tourne et s'arrête ; deux lancements simultanés n'en démarrent qu'une ; un
plantage libère le verrou ; un redémarrage attend que l'ancienne ait rendu la main."""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ping(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=1) as r:
            return json.load(r)
    except Exception:
        return None


def env_for(tmp_path, **extra):
    env = dict(os.environ, TRIPCOMPTA_CONFIG=str(tmp_path / "conf" / "reglages.json"), TRIPCOMPTA_NO_UPDATE_CHECK="1",
               PYTHONIOENCODING="utf-8")   # sortie lue en UTF-8 quel que soit le réglage du poste
    env.pop("TRIPCOMPTA_DATA_DIR", None)
    env.pop("TRIPCOMPTA_RESTARTED", None)
    env.pop("TRIPCOMPTA_PORT_WAIT", None)
    env.update(extra)
    return env


def launch(tmp_path, port, data="voyages", **extra):
    return subprocess.Popen([sys.executable, "-u", str(ROOT / "run.py"), "--no-browser", "--data-dir",
                             str(tmp_path / data), "--port", str(port)], env=env_for(tmp_path, **extra),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")


def wait_ping(port, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if ping(port):
            return True
        time.sleep(0.2)
    return False


def stop(*procs):
    for p in procs:
        if p.poll() is None:
            p.kill()
        p.wait(10)


def test_second_launch_shows_the_running_one_and_stops(tmp_path):
    p1, p2 = free_port(), free_port()
    a = launch(tmp_path, p1)
    try:
        assert wait_ping(p1)
        b = launch(tmp_path, p2)                     # même dossier, autre port : c'est quand même la même app
        out, _ = b.communicate(timeout=30)
        assert b.returncode == 0 and f"Trip Compta est déjà ouvert : http://127.0.0.1:{p1}/" in out
        assert ping(p2) is None and ping(p1)          # une seule Trip Compta
        c = launch(tmp_path, p2, data="autres")       # un AUTRE dossier : une 2e Trip Compta est permise
        try:
            assert wait_ping(p2)
        finally:
            stop(c)
    finally:
        stop(a)


def test_simultaneous_launches_start_only_one(tmp_path):
    """Double-double-clic sur un dossier neuf : une seule démarre (avant, la 2e plantait en créant la base)."""
    p1 = free_port()
    a, b = launch(tmp_path, p1), launch(tmp_path, p1)
    try:
        assert wait_ping(p1)
        time.sleep(1.5)
        alive = [p for p in (a, b) if p.poll() is None]
        assert len(alive) == 1
        gone = a if a.poll() is not None else b
        out = gone.stdout.read()
        assert gone.returncode == 0 and "déjà ouvert" in out and "Traceback" not in out
    finally:
        stop(a, b)


def test_crash_releases_the_lock(tmp_path):
    p1 = free_port()
    a = launch(tmp_path, p1)
    assert wait_ping(p1)
    a.kill()                                         # plantage : le système libère le verrou
    a.wait(10)
    b = launch(tmp_path, p1)
    try:
        assert wait_ping(p1)
    finally:
        stop(b)


def test_restart_waits_for_the_old_one(tmp_path):
    """Relance (mise à jour, changement de dossier…) : la nouvelle attend que l'ancienne rende la main."""
    p1 = free_port()
    a = launch(tmp_path, p1)
    assert wait_ping(p1)
    b = launch(tmp_path, p1, TRIPCOMPTA_RESTARTED="1")
    try:
        time.sleep(2)
        assert b.poll() is None                      # elle attend (elle ne s'est pas crue en double)
        a.kill()
        a.wait(10)
        assert wait_ping(p1, timeout=40)
        assert b.poll() is None
    finally:
        stop(a, b)
