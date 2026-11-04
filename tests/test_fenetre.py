"""Trip Compta dans sa propre fenêtre (pywebview) : choix du mode, routes de la fenêtre, arrêt propre, et la VRAIE
fenêtre pilotée en mode discret (Mac seulement, hors GitHub : aucune fenêtre visible, pas de focus pris)."""
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import run
from compta import fenetre
from compta.db import Database
from compta.web import create_app

ROOT = Path(__file__).resolve().parent.parent


class FakeWindows:
    def __init__(self):
        self.shown = self.created = 0

    def show(self):
        self.shown += 1

    def new_later(self, url=None):
        self.created += 1


def test_window_mode_choice(monkeypatch):
    monkeypatch.setattr(fenetre, "available", lambda: True)
    assert fenetre.wanted(frozen=True, navigateur=False, forcer=False)          # app fabriquée : fenêtre
    assert not fenetre.wanted(frozen=False, navigateur=False, forcer=False)     # code source : navigateur
    assert fenetre.wanted(frozen=False, navigateur=False, forcer=True)          # --fenetre
    assert not fenetre.wanted(frozen=True, navigateur=True, forcer=True)        # --navigateur l'emporte
    monkeypatch.setattr(fenetre, "available", lambda: False)                    # Linux, pywebview absent
    assert not fenetre.wanted(frozen=True, navigateur=False, forcer=True)


def test_available_only_mac_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert not fenetre.available()


def test_window_routes(tmp_path):
    app = create_app(tmp_path / "d")
    c = TestClient(app)
    assert c.post("/fenetre/montrer").status_code == 404          # dans le navigateur : pas de fenêtre à montrer
    assert c.post("/fenetre/nouvelle").status_code == 404
    f = FakeWindows()
    app.state.fenetres = f
    assert c.post("/fenetre/montrer").json() == {"ok": True} and f.shown == 1
    assert c.post("/fenetre/nouvelle").json() == {"ok": True} and f.created == 1


def test_window_routes_on_too_old_screen(tmp_path):
    from compta import updater as U
    d = tmp_path / "d"
    Database(d, app_version="9.0.0")
    app = create_app(d, updater=U.Updater(info=dict(version="1.0.0", commit="x", dev=False), fetch=lambda: None))
    f = FakeWindows()
    app.state.fenetres = f
    c = TestClient(app)
    assert c.post("/fenetre/montrer").json() == {"ok": True} and f.shown == 1
    app.state.arreter()                                            # rien d'ouvert : rien à faire, pas d'erreur


def test_clean_stop_is_idempotent(tmp_path):
    app = create_app(tmp_path / "d")
    app.state.arreter()
    app.state.arreter()                                            # Cmd+Q puis fin du programme : une seule fois


def test_second_launch_asks_running_window(monkeypatch):
    calls = []

    class R:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=0):
        calls.append((req.full_url, req.get_method()))
        return R()
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert run.show_running_window("http://127.0.0.1:8765/")
    assert calls == [("http://127.0.0.1:8765/fenetre/montrer", "POST")]

    def refused(req, timeout=0):
        raise OSError("404")
    monkeypatch.setattr(urllib.request, "urlopen", refused)
    assert not run.show_running_window("http://127.0.0.1:8765/")   # il tourne dans le navigateur : onglet


def test_no_console_streams_go_to_log(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIPCOMPTA_CONFIG", str(tmp_path / "conf" / "reglages.json"))
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    run._quiet_streams()
    print("message sans console")
    sys.stdout.flush()
    assert "message sans console" in (tmp_path / "conf" / "tripcompta.log").read_text(encoding="utf-8")


def test_find_bar_and_shortcuts_only_in_window():
    js = (ROOT / "compta" / "static" / "app.js").read_text(encoding="utf-8")
    assert "window.tcFind" in js and "function inWindow() { return !!window.pywebview; }" in js
    assert '"/fenetre/nouvelle"' in js and "if (!inWindow()" in js


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(sys.platform != "darwin" or os.environ.get("CI") or not fenetre.available(),
                    reason="vraie fenêtre : Mac, hors GitHub")
@pytest.mark.parametrize("end", ["fermer", "quitter"])
def test_real_window_discreet(tmp_path, end):
    """La vraie fenêtre (WebKit), invisible : menus et raccourcis, recherche, liens, « Enregistrer sous… », choix de
    fichier, Dock, 2e lancement, puis fermeture de la dernière fenêtre ou Cmd+Q = arrêt propre."""
    port = _free_port()
    env = dict(os.environ, ESSAI_PORT=str(port), TRIPCOMPTA_FENETRE_DISCRETE="1")
    p = subprocess.run([sys.executable, str(ROOT / "tests" / "essai_fenetre.py"), str(tmp_path), end],
                       env=env, capture_output=True, text=True, timeout=120)
    r = json.loads((tmp_path / "resultats.json").read_text(encoding="utf-8"))
    assert "erreur" not in r, r.get("erreur")
    assert r["visible"] is False and r["app_au_premier_plan"] is False and r["toujours_invisible"]
    assert r["menu_fichier"][0] == ["Nouvelle fenêtre", "n"] and r["menu_fichier"][1] == ["Rechercher dans la page…", "f"]
    assert r["menu_fichier"][3][0] == "Ouvrir dans le navigateur"
    assert r["cmd_n_menu"] == 2 and r["ctrl_n_page"] == 3 and r["une_fenetre_fermee_app_continue"]
    assert r["cmd_f_barre"] == "tc-find" and r["cmd_f_trouve"] == "Gîte" and r["cmd_f_introuvable"] is True
    assert r["retour"]
    assert r["lien_interne_nouvelle_fenetre"].endswith("/justificatifs")
    assert r["ouvert_dans_navigateur"][0] == "https://github.com/Scendari/tripcompta"
    assert r["ouvert_dans_navigateur"][1].startswith(f"http://127.0.0.1:{port}/")
    assert len(r["enregistrer_sous"]) == 2 and r["enregistrer_sous"][0].endswith(".zip")
    assert r["enregistrer_sous"][1].startswith("Bilan_") and r["enregistrer_sous"][1].endswith(".pdf")
    sizes = r["fichiers_enregistres"]
    assert all(v > 1000 for v in sizes.values()) and len(sizes) == 2
    assert r["page_apres_pdf"].endswith("/export")              # le PDF ne remplace pas l'application
    assert r["fichier_choisi"] == ["ticket-essai.jpg 204"]
    # E-mail (bouton « Envoyer ») : la messagerie s'ouvre, le PDF du compte va dans Téléchargements sans dialogue
    assert r["mail_lien"] == "mailto:" and r["mail_ouvert"] == ["mailto:"]
    assert len(r["mail_pdf_telechargements"]) == 1 and r["mail_pdf_telechargements"][0].startswith("Compte_")
    assert "Téléchargements" in r["mail_message"] and r["mail_page_inchangee"]
    assert r["dock_reponse"] is True and r["dock_fenetre_ramenee"] == 1
    assert r["dock_apple_event_rouvrir"] is True                  # vrai message « rouvrir » du Dock
    assert r["deuxieme_lancement"] == 200 and r["dock_fenetre_ramenee_2"] == 3
    assert r["arret_propre"] is True                             # enregistré, main rendue
    if end == "fermer":
        assert r.get("programme_rend_la_main") is True
    with socket.socket() as s:                                   # le serveur est bien arrêté
        assert s.connect_ex(("127.0.0.1", port)) != 0
    assert p.returncode == 0, p.stderr[-2000:]


FIRST = r'''
import os, sys, threading, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from compta import fenetre
from compta.web import create_app
work = Path(sys.argv[2]); port = int(sys.argv[3])
app = create_app(work / "donnees"); app.state.port = port
def wait_stop(f):
    def loop():
        while not (work / "stop").exists():
            time.sleep(0.2)
        import webview
        for w in list(webview.windows):
            w.destroy()
    threading.Thread(target=loop, daemon=True).start()
fenetre.run(app, "127.0.0.1", port, on_start=wait_stop)
'''

SECOND = r'''
import sys, webbrowser
sys.path.insert(0, sys.argv[1])
import os
import run
import compta.web
def no_app(*a, **k):                                 # garde-fou : le 2e lancement ne doit RIEN ouvrir
    raise SystemExit("ÉCHEC : une 2e application aurait démarré")
compta.web.create_app = no_app
os.environ.pop("TRIPCOMPTA_DATA_DIR", None)          # comme un vrai lancement (dossier non imposé)
url = "http://127.0.0.1:%s/" % sys.argv[2]
run.running_instance = lambda *a, **k: url          # « Trip Compta tourne déjà » (au lieu du port 8765 réel)
def no_tab(*a, **k):
    raise SystemExit("ÉCHEC : un onglet aurait été ouvert")
webbrowser.open = no_tab
if len(sys.argv) > 3:
    # « windows » : ce qui diffère sous Windows dans run.py, c'est seulement « pywebview disponible ? » (vérifié par
    # la fabrication GitHub) et l'application fabriquée lancée SANS option (double-clic sur TripCompta.exe)
    sys.frozen = True
    from compta import fenetre
    fenetre.available = lambda: True
    sys.argv = ["TripCompta.exe"]
    print("2e lancement : comme TripCompta.exe (application fabriquée, sans option)")
else:
    sys.argv = ["TripCompta", "--fenetre"]           # comme l'application fabriquée (mode fenêtre)
run.main()
print("2e lancement terminé sans ouvrir d'onglet")
'''


@pytest.mark.skipif(sys.platform != "darwin" or os.environ.get("CI") or not fenetre.available(),
                    reason="vraie fenêtre : Mac, hors GitHub")
@pytest.mark.parametrize("systeme", ["mac", "windows"])
def test_second_launch_brings_running_window_end_to_end(tmp_path, systeme):
    """Trip Compta tourne déjà dans sa fenêtre ; on le relance (comme un double-clic sous Windows, où chaque clic
    lance un nouveau programme) : le 2e programme demande au 1er de ramener sa fenêtre, n'ouvre pas d'onglet et
    s'arrête. Même code que sous Windows (run.py → POST /fenetre/montrer → Fenetres.show)."""
    port = _free_port()
    env = dict(os.environ, TRIPCOMPTA_FENETRE_DISCRETE="1", TRIPCOMPTA_CONFIG=str(tmp_path / "conf.json"),
               TRIPCOMPTA_NO_UPDATE_CHECK="1")
    log = open(tmp_path / "premier.log", "w")
    first = subprocess.Popen([sys.executable, "-u", "-c", FIRST, str(ROOT), str(tmp_path), str(port)],
                             env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        import time
        import urllib.request
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        time.sleep(1)
        second = subprocess.run([sys.executable, "-c", SECOND, str(ROOT), str(port)] +
                                (["windows"] if systeme == "windows" else []), env=env,
                                capture_output=True, text=True, timeout=30)
        assert second.returncode == 0, second.stdout + second.stderr
        assert "Trip Compta est déjà ouvert" in second.stdout
        assert ("comme TripCompta.exe" in second.stdout) == (systeme == "windows")
        assert "2e lancement terminé sans ouvrir d'onglet" in second.stdout
        time.sleep(0.5)
        assert "fenêtre ramenée (1)" in (tmp_path / "premier.log").read_text()
        assert first.poll() is None                                  # le 1er tourne toujours
    finally:
        (tmp_path / "stop").write_text("1")
        first.wait(timeout=20)
        log.close()
