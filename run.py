"""Lance Trip Compta : dans sa propre fenêtre (application fabriquée, Mac et Windows), sinon dans le navigateur.

    python run.py                 # usage normal (depuis le code source : navigateur)
    python run.py --fenetre       # dans sa propre fenêtre (essai depuis le code source)
    python run.py --navigateur    # toujours dans le navigateur
    python run.py --demo          # ajoute aussi le voyage de démonstration
    python run.py --port 9000 --no-browser --data-dir ./donnees
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path


def running_instance(first: int = 8765, count: int = 50) -> str | None:
    """Adresse d'une Trip Compta déjà ouverte sur cet ordinateur (ou None) : une seule à la fois, sinon la 2e
    s'ouvrirait en lecture seule (verrou du dossier synchronisé) et on ne saurait plus laquelle utiliser."""
    import json
    import urllib.request
    for port in range(first, first + count):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.05)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                continue
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=1) as r:
                if json.load(r).get("app") == "tripcompta":
                    return f"http://127.0.0.1:{port}/"
        except Exception:
            continue
    return None


# ---------------------------------------------------------------- une seule Trip Compta par dossier de voyages
class InstanceLock:
    """Verrou du système (flock / msvcrt) sur un dossier de voyages : une seule Trip Compta à la fois sur ces données,
    sur cet ordinateur. Sans course entre deux lancements simultanés, et libéré tout seul si l'application plante.
    À côté, un petit fichier dit où joindre celle qui tourne (pid, port)."""

    def __init__(self, data_dir: Path):
        import hashlib
        from compta import paths
        # normcase : sous Windows, D:\Voyages et d:\voyages sont le même dossier (resolve() ne rétablit la casse que
        # pour un dossier qui existe déjà) ; idem sur Mac (APFS ignore la casse par défaut)
        name = os.path.normcase(str(data_dir))
        if sys.platform == "darwin":
            name = name.lower()
        key = hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]
        folder = paths.config_file().parent
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / f"instance-{key}.lock"
        self.info = folder / f"instance-{key}.json"
        self.fh = None

    def acquire(self, wait: float = 0) -> bool:
        """Prend le verrou (en attendant au plus « wait » secondes : relance après un redémarrage)."""
        end = time.time() + wait
        while True:
            fh = open(self.path, "a+")
            try:
                if sys.platform == "win32":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.fh = fh                       # gardé ouvert : le verrou dure autant que le programme
                return True
            except OSError:
                fh.close()
                if time.time() >= end:
                    return False
                time.sleep(0.2)

    def publish(self, port: int):
        import json
        tmp = self.info.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pid": os.getpid(), "port": port}), encoding="utf-8")
        os.replace(tmp, self.info)

    def running_url(self, timeout: float = 15) -> str | None:
        """Adresse de la Trip Compta qui tient le verrou (elle peut être encore en train de démarrer)."""
        import json
        import urllib.request
        end = time.time() + timeout
        while time.time() < end:
            try:
                port = json.loads(self.info.read_text(encoding="utf-8"))["port"]
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=1) as r:
                    if json.load(r).get("app") == "tripcompta":
                        return f"http://127.0.0.1:{port}/"
            except Exception:
                pass
            time.sleep(0.25)
        return None


def show_running_window(url: str) -> bool:
    """Windows : un 2e lancement demande à Trip Compta déjà ouvert de remettre sa fenêtre devant (False si celui-ci
    tourne dans le navigateur)."""
    import urllib.request
    try:
        req = urllib.request.Request(url + "fenetre/montrer", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _quiet_streams():
    """Application sans console (Windows, fenêtre) : pas de sortie standard ; on écrit dans un journal à côté des
    réglages, sinon le serveur échouerait au premier message."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        from compta import paths
        f = paths.config_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        log = open(f.parent / "tripcompta.log", "a", encoding="utf-8", buffering=1)
    except Exception:
        log = open(os.devnull, "w")
    sys.stdout = sys.stdout or log
    sys.stderr = sys.stderr or log


def free_port(preferred: int) -> int:
    for port in [preferred] + list(range(preferred + 1, preferred + 50)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                # comme uvicorn : un port qui vient d'être libéré (TIME_WAIT) est réutilisable tout de suite
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    ap = argparse.ArgumentParser(description="Trip Compta")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--data-dir", help="Dossier des données (défaut : Documents/TripCompta)")
    ap.add_argument("--demo", action="store_true", help="Ajoute le voyage de démonstration")
    ap.add_argument("--mcp", action="store_true", help="Démarre le serveur MCP (stdio) pour une IA")
    ap.add_argument("--fenetre", action="store_true", help="Dans sa propre fenêtre (même depuis le code source)")
    ap.add_argument("--navigateur", action="store_true", help="Dans le navigateur, même pour l'application fabriquée")
    ap.add_argument("--home", help=argparse.SUPPRESS)          # MCP : dossier synchronisé (voir compta/cloud.py)
    a = ap.parse_args()

    if a.mcp:  # ne rien écrire sur la sortie standard : c'est le canal MCP
        from compta.mcp_server import build_server
        build_server(a.data_dir, a.home).run("stdio")
        return

    _quiet_streams()
    from compta import fenetre
    restarted = bool(os.environ.get("TRIPCOMPTA_RESTARTED"))
    # fenêtre : application fabriquée (Mac, Windows), sauf --navigateur. --no-browser (essais, serveur seul) n'ouvre
    # rien, sauf après un redémarrage ou une mise à jour : l'ancienne fenêtre a disparu avec l'ancien programme.
    window = fenetre.wanted(getattr(sys, "frozen", False), a.navigateur, a.fenetre) and (not a.no_browser or restarted)

    # une seule Trip Compta par dossier de voyages : si elle tourne déjà, on montre celle-là et on s'arrête
    from compta import paths as PATHS
    try:
        data_dir = Path(a.data_dir or os.environ.get("TRIPCOMPTA_DATA_DIR") or PATHS.default_data_dir()).expanduser().resolve()
    except OSError:
        data_dir = None
    lock = InstanceLock(data_dir) if data_dir is not None else None
    # après un redémarrage (mise à jour, changement de dossier…), l'ancienne n'a peut-être pas encore rendu la main
    if lock is not None and not lock.acquire(wait=30 if (restarted or os.environ.get("TRIPCOMPTA_PORT_WAIT")) else 0):
        url = lock.running_url()
        if url:
            print(f"Trip Compta est déjà ouvert : {url}")
            if not (window and show_running_window(url)) and not a.no_browser:
                webbrowser.open(url)
        else:
            print("Trip Compta est déjà en train de s'ouvrir sur ces voyages.")
        return
    main.lock = lock                           # garder le verrou jusqu'à la fin du programme

    # une version plus ancienne (sans verrou) déjà ouverte sur le port habituel ? on montre celle-là
    if (a.port == 8765 and not a.data_dir and not restarted and not os.environ.get("TRIPCOMPTA_DATA_DIR")):
        url = running_instance()
        if url:
            print(f"Trip Compta est déjà ouvert : {url}")
            if window and show_running_window(url):
                return
            if not a.no_browser:
                webbrowser.open(url)
            return

    import uvicorn
    from compta.web import create_app

    def restart():
        """Relance le même programme, à la MÊME adresse (sans rouvrir d'onglet : la page ouverte se recharge seule)."""
        os.environ["TRIPCOMPTA_RESTARTED"] = "1"
        port_now = getattr(app.state, "port", None) or a.port
        os.environ["TRIPCOMPTA_PORT_WAIT"] = str(port_now)
        args, skip = [], False
        for x in sys.argv[1:]:
            if skip:
                skip = False
                continue
            if x == "--port":
                skip = True
                continue
            args.append(x)
        args += ["--port", str(port_now)]
        cmd = [sys.executable] + ([] if getattr(sys, "frozen", False) else [sys.argv[0]]) + args
        sys.stdout.flush()
        if sys.platform == "win32":
            # Windows ne remplace pas le programme en cours : le nouveau démarrerait avant que l'ancien ait libéré
            # l'adresse, et en prendrait une autre (l'onglet ouvert le perdrait). Il attend qu'elle se libère.
            import subprocess
            subprocess.Popen(cmd, close_fds=True)
            os._exit(0)
        os.execv(sys.executable, cmd)

    app = create_app(a.data_dir, restart=restart)
    if a.demo and app.state.db is not None:
        from compta.demo import seed_demo, seed_family_demo
        seed_family_demo(app.state.db, replace=False)
        seed_demo(app.state.db, replace=False)

    if os.environ.get("TRIPCOMPTA_PORT_WAIT") == str(a.port):     # relancé : attendre que l'ancien libère l'adresse
        os.environ.pop("TRIPCOMPTA_PORT_WAIT", None)
        for _ in range(60):
            if free_port(a.port) == a.port:
                break
            time.sleep(0.25)
    port = free_port(a.port)
    if lock is not None:
        lock.publish(port)
    app.state.port = port                    # pour relancer au même endroit après une mise à jour
    app.state.argv = sys.argv[1:]
    url = f"http://127.0.0.1:{port}/"
    print("=" * 60)
    print(" Trip Compta")
    print(f" Ouvre ton navigateur sur : {url}")
    if app.state.db is not None:
        print(f" Données : {app.state.db.dir}")
    else:
        print(" ⚠️  Ces données viennent d'une version plus récente : installe-la (voir le navigateur).")
    print(" Pour arrêter : ferme cette fenêtre (ou Ctrl+C)")
    print("=" * 60)
    if window:
        fenetre.run(app, "127.0.0.1", port)
        return
    if not a.no_browser and not restarted:
        threading.Thread(target=lambda: (time.sleep(1.2), webbrowser.open(url)), daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    sys.exit(main())
