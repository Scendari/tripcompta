"""Lance Trip Compta et ouvre le navigateur.

    python run.py                 # usage normal
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
                if json.load(r).get("app") in ("tripcompta", "compta-voyages"):
                    return f"http://127.0.0.1:{port}/"
        except Exception:
            continue
    return None


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
    ap.add_argument("--home", help=argparse.SUPPRESS)          # MCP : dossier synchronisé (voir compta/cloud.py)
    a = ap.parse_args()

    if a.mcp:  # ne rien écrire sur la sortie standard : c'est le canal MCP
        from compta.mcp_server import build_server
        build_server(a.data_dir, a.home).run("stdio")
        return

    # déjà ouverte sur cet ordinateur ? on montre celle-là au lieu d'en lancer une 2e (sauf dossier imposé : tests)
    if (a.port == 8765 and not a.data_dir and not os.environ.get("TRIPCOMPTA_RESTARTED")
            and not os.environ.get("TRIPCOMPTA_DATA_DIR")):
        url = running_instance()
        if url:
            print(f"Trip Compta est déjà ouvert : {url}")
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
    if not a.no_browser and not os.environ.get("TRIPCOMPTA_RESTARTED"):
        threading.Thread(target=lambda: (time.sleep(1.2), webbrowser.open(url)), daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    sys.exit(main())
