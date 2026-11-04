"""Dossier des données : nom TripCompta, choix dans l'application."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from compta import paths as P
from compta.ops import Ops
from compta.web import create_app


def test_priorities(tmp_path, monkeypatch):
    monkeypatch.delenv("TRIPCOMPTA_DATA_DIR", raising=False)
    monkeypatch.setenv("TRIPCOMPTA_CONFIG", str(tmp_path / "reglages.json"))
    monkeypatch.setattr(P, "documents_dir", lambda: tmp_path / "Docs")
    assert P.default_data_dir() == tmp_path / "Docs" / "TripCompta"            # par défaut
    P.write_config(data_dir=str(tmp_path / "Ailleurs"))
    assert P.default_data_dir() == tmp_path / "Ailleurs"                          # choisi dans l'appli
    monkeypatch.setenv("TRIPCOMPTA_DATA_DIR", str(tmp_path / "Env"))              # variable d'environnement : prioritaire
    assert P.default_data_dir() == tmp_path / "Env"
    assert P.looks_synced(tmp_path / "Dropbox" / "x") and not P.looks_synced(tmp_path / "Docs")


@pytest.fixture
def chosen(tmp_path, monkeypatch):
    """Une installation dont le dossier a été choisi dans l'application ; la relance est simulée."""
    monkeypatch.delenv("TRIPCOMPTA_DATA_DIR", raising=False)
    monkeypatch.setenv("TRIPCOMPTA_CONFIG", str(tmp_path / "reglages.json"))
    monkeypatch.setattr(P, "documents_dir", lambda: tmp_path / "Docs")
    P.write_config(data_dir=str(tmp_path / "a"))
    calls = []
    app = create_app(restart=lambda: calls.append("relance"))
    ops = Ops(app.state.db)
    tid = ops.create_trip("Sortie")["voyage_id"]
    ops.add_participants(tid, ["Anne", "Bob"])
    (app.state.db.receipts_dir / "1_x.jpg").write_bytes(b"IMG")
    return app, TestClient(app), tmp_path, calls


def test_move_data_to_another_folder(chosen):
    app, c, tmp, calls = chosen
    page = c.get("/reglages/donnees").text
    assert str(tmp / "a") in page and "que tu as choisi" in page and 'value="__local__"' in page
    r = c.post("/reglages/donnees", data={"dossier": str(tmp / "b"), "mode": "deplacer"})
    import html
    assert "Redémarrage" in r.text and "n'a pas été touché" in html.unescape(r.text)
    import time; time.sleep(0.8)
    assert calls == ["relance"]
    conf = json.loads((tmp / "reglages.json").read_text())
    assert conf["data_dir"] == str(tmp / "b") and conf["partage"] is True   # dossier protégé (verrou…)
    con = sqlite3.connect(tmp / "b" / "compta.db")
    assert con.execute("SELECT name FROM trips").fetchall() == [("Sortie",)]
    assert (tmp / "b" / "justificatifs" / "1_x.jpg").read_bytes() == b"IMG"
    assert (tmp / "a" / "compta.db").exists()                       # l'ancien n'est pas touché


def test_change_errors(chosen):
    app, c, tmp, calls = chosen
    def post(**d):
        return c.post("/reglages/donnees", data=d).text.replace("&#39;", "'").replace("&quot;", '"')
    assert "chemin complet" in post(dossier="relatif/dossier", mode="deplacer")
    assert "déjà rangés dans ce dossier" in post(dossier=str(tmp / "a"), mode="deplacer")
    assert "pas encore de voyages Trip Compta" in post(dossier=str(tmp / "vide"), mode="utiliser")
    assert "est introuvable" in post(dossier=str(tmp / "nulle part" / "x"), mode="deplacer")   # faute de frappe
    (tmp / "plein").mkdir()
    (tmp / "plein" / "compta.db").write_bytes(b"")
    assert "contient déjà des voyages" in post(dossier=str(tmp / "plein"), mode="deplacer")
    assert calls == []
    # utiliser un dossier qui a déjà des données ; puis revenir sur cet ordinateur (Documents/TripCompta)
    assert "va utiliser les voyages" in post(dossier=str(tmp / "plein"), mode="utiliser")
    assert "de nouveau rangés sur cet ordinateur" in post(action="defaut")
    conf = json.loads((tmp / "reglages.json").read_text())
    assert conf["data_dir"] is None and conf["partage"] is False
    con = sqlite3.connect(tmp / "Docs" / "TripCompta" / "compta.db")      # les voyages y ont été copiés
    assert con.execute("SELECT name FROM trips").fetchall() == [("Sortie",)]


def test_imposed_folder_and_open(tmp_path, monkeypatch):
    app = create_app(tmp_path / "d")                                  # imposé au lancement
    c = TestClient(app)
    page = c.get("/reglages/donnees").text
    assert "imposé au lancement" in page and 'value="ranger"' not in page
    assert "imposé au lancement" in c.post("/reglages/donnees", data={"dossier": "/tmp/x"}).text
    opened = []
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", lambda args, **k: opened.append(args))
    import os
    monkeypatch.setattr(os, "startfile", lambda p: opened.append([p]), raising=False)   # Windows : Explorateur
    r = c.post("/reglages/ouvrir-dossier", headers={"referer": "http://t/"}, follow_redirects=True)
    assert "Dossier ouvert" in r.text and opened and opened[0][-1] == str(tmp_path / "d")
    home = c.get("/").text
    assert 'href="/reglages"' in home and "Tes données sont dans" not in home     # plus de long chemin en bas
    reg = c.get("/reglages").text
    assert "Où sont mes voyages" in reg and 'href="/reglages/donnees"' in reg and 'href="/reglages/protection"' in reg


# ---------------------------------------------------------------- « Changer de dossier » : dire ce qu'on va faire
def _folder_setup(tmp_path, monkeypatch):
    from compta import models as M
    from compta.db import Database
    monkeypatch.delenv("TRIPCOMPTA_DATA_DIR", raising=False)
    monkeypatch.setenv("TRIPCOMPTA_CONFIG", str(tmp_path / "conf" / "reglages.json"))
    monkeypatch.setenv("TRIPCOMPTA_DOCUMENTS", str(tmp_path / "Mes Documents"))
    app = create_app()
    Ops(app.state.db).create_trip("Voyage d'ici")
    full = tmp_path / "Mon Drive" / "TripCompta"                  # voyages rangés depuis un autre ordinateur
    other = Database(full)
    with other.Session() as s:
        for n in ("Hendaye", "La Londe", "Banyuls", "Port-Cros"):
            s.add(M.Trip(name=n))
        s.commit()
    other.engine.dispose()
    empty = tmp_path / "Dropbox" / "TripCompta"
    empty.mkdir(parents=True)
    return app, full, empty


def test_folder_info_says_what_is_inside(tmp_path, monkeypatch):
    app, full, empty = _folder_setup(tmp_path, monkeypatch)
    c = TestClient(app)
    j = c.get("/reglages/dossier-info", params={"chemin": str(full)}).json()
    assert j["ok"] and j["voyages"] == 4 and len(j["noms"]) == 3 and set(j["noms"]) <= {"Hendaye", "La Londe", "Banyuls", "Port-Cros"}
    j = c.get("/reglages/dossier-info", params={"chemin": str(empty)}).json()
    assert j["ok"] and j["existe"] and j["voyages"] is None
    j = c.get("/reglages/dossier-info", params={"chemin": str(empty.parent / "Nouveau")}).json()
    assert j["ok"] and not j["existe"]                           # sera créé
    j = c.get("/reglages/dossier-info", params={"chemin": str(app.state.db.home)}).json()
    assert j["actuel"]
    assert c.get("/reglages/dossier-info", params={"chemin": "pas/absolu"}).json()["ok"] is False
    page = c.get("/reglages/donnees").text
    assert "Changer de dossier" in page and "Ranger mes voyages ici" not in page
    assert 'data-cur-trips="1"' in page


def test_folder_choice_explains_and_guards_in_browser(tmp_path, monkeypatch):
    """Dans un vrai navigateur : le dossier choisi est inspecté, le bon choix présélectionné, la phrase dit ce qui va
    se passer (et ce qui ne se passe pas), le bouton suit le choix ; copier par-dessus des voyages est impossible."""
    pw = pytest.importorskip("playwright.sync_api")
    import socket
    import threading
    import time
    import uvicorn
    app, full, empty = _folder_setup(tmp_path, monkeypatch)
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    while not srv.started:
        time.sleep(0.05)
    try:
        with pw.sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page()
            pg.goto(f"http://127.0.0.1:{port}/reglages/donnees")
            pg.fill("input[name=autre]", str(full))
            pw.expect(pg.locator("[data-folder-status]")).to_contain_text("4 voyages")
            assert pg.is_checked("input[name=mode][value=utiliser]")        # présélectionné
            pw.expect(pg.locator("[data-go]")).to_have_text("Ouvrir les voyages de ce dossier")
            what = pg.inner_text("[data-what-happens]")
            assert "ouvrir les voyages de ce dossier" in what and "rien n'est effacé" in what
            pg.check("input[name=mode][value=copier]")                       # copier par-dessus : refusé d'avance
            assert "ne copie jamais par-dessus" in pg.inner_text("[data-what-happens]")
            assert pg.is_disabled("[data-go]")
            pg.fill("input[name=autre]", str(empty))
            pw.expect(pg.locator("[data-folder-status]")).to_contain_text("vide")
            assert pg.is_checked("input[name=mode][value=copier]")
            pw.expect(pg.locator("[data-go]")).to_have_text("Copier mes voyages dans ce dossier")
            what = pg.inner_text("[data-what-happens]")
            assert "copier ton voyage" in what and "n'est pas touché" in what and pg.is_enabled("[data-go]")
            pg.check("input[name=mode][value=utiliser]")                     # ouvrir un dossier vide : impossible
            assert pg.is_disabled("[data-go]")
            b.close()
    finally:
        srv.should_exit = True
        th.join(5)
