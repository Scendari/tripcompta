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
    assert str(tmp / "a") in page and "que tu as choisi" in page and "Revenir au dossier par défaut" in page
    assert "Google Drive" not in page.split("Mettre Trip Compta dans")[0]    # plus de dossiers repérés
    r = c.post("/reglages/donnees", data={"dossier": str(tmp / "b")})      # vide : on y copie
    import html
    assert "Redémarrage" in r.text and "n'a pas été touché" in html.unescape(r.text)
    import time; time.sleep(0.8)
    assert calls == ["relance"]
    conf = json.loads((tmp / "reglages.json").read_text())
    assert conf["data_dir"] == str(tmp / "b") and "partage" not in conf     # ouvert directement
    con = sqlite3.connect(tmp / "b" / "compta.db")
    assert con.execute("SELECT name FROM trips").fetchall() == [("Sortie",)]
    assert (tmp / "b" / "justificatifs" / "1_x.jpg").read_bytes() == b"IMG"
    assert (tmp / "a" / "compta.db").exists()                       # l'ancien n'est pas touché


def test_change_errors(chosen):
    app, c, tmp, calls = chosen
    def post(**d):
        return c.post("/reglages/donnees", data=d).text.replace("&#39;", "'").replace("&quot;", '"')
    assert "chemin complet" in post(dossier="relatif/dossier")
    assert "déjà rangés dans ce dossier" in post(dossier=str(tmp / "a"))
    assert "est introuvable" in post(dossier=str(tmp / "nulle part" / "x"))   # faute de frappe
    assert "Choisis un dossier" in post(dossier="")
    assert calls == []
    # un dossier qui contient déjà des voyages : ouverts, jamais de copie par-dessus
    (tmp / "plein").mkdir()
    (tmp / "plein" / "compta.db").write_bytes(b"x")
    before = (tmp / "plein" / "compta.db").read_bytes()
    assert "va ouvrir les voyages rangés dans" in post(dossier=str(tmp / "plein"), mode="copier")
    assert (tmp / "plein" / "compta.db").read_bytes() == before
    assert json.loads((tmp / "reglages.json").read_text())["data_dir"] == str(tmp / "plein")
    # revenir au dossier par défaut (Documents/TripCompta, vide) : les voyages y sont copiés, rien à retenir
    assert "ont été copiés dans" in post(dossier=str(tmp / "Docs" / "TripCompta"))
    conf = json.loads((tmp / "reglages.json").read_text())
    assert conf["data_dir"] is None and "partage" not in conf
    con = sqlite3.connect(tmp / "Docs" / "TripCompta" / "compta.db")
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
    """Dans un vrai navigateur : le dossier tapé est inspecté ; s'il contient des voyages, alerte et « Ouvrir les
    voyages de ce dossier » ; vide, « Copier mes voyages dans ce dossier » ; copier par-dessus est impossible."""
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
            assert pg.inner_text("[data-go]") == "Continuer" and pg.is_disabled("[data-go]")
            pg.fill("input[name=dossier]", str(full))
            pw.expect(pg.locator("[data-folder-status]")).to_contain_text("4 voyages")
            assert "contient déjà des voyages" in pg.inner_text("[data-folder-status]")
            pw.expect(pg.locator("[data-go]")).to_have_text("Ouvrir les voyages de ce dossier")
            what = pg.inner_text("[data-what-happens]")
            assert "ne copie jamais par-dessus" in what and "ouvrir les voyages de ce dossier" in what
            pg.fill("input[name=dossier]", str(empty))
            pw.expect(pg.locator("[data-folder-status]")).to_contain_text("ne contient pas de voyages")
            pw.expect(pg.locator("[data-go]")).to_have_text("Copier mes voyages dans ce dossier")
            what = pg.inner_text("[data-what-happens]")
            assert "copier ton voyage" in what and "n'est pas touché" in what and pg.is_enabled("[data-go]")
            pg.fill("input[name=dossier]", str(empty.parent / "Nouveau"))
            pw.expect(pg.locator("[data-folder-status]")).to_contain_text("sera créé")
            pg.fill("input[name=dossier]", str(app.state.db.home))
            pw.expect(pg.locator("[data-folder-status]")).to_contain_text("déjà les voyages ouverts")
            assert pg.is_disabled("[data-go]")
            b.close()
    finally:
        srv.should_exit = True
        th.join(5)


def test_default_folder_is_ok_even_if_documents_is_missing(chosen):
    """« Revenir au dossier par défaut » : Documents/TripCompta est créé au besoin (pas « introuvable »)."""
    app, c, tmp, calls = chosen
    j = c.get("/reglages/dossier-info", params={"chemin": str(tmp / "Docs" / "TripCompta")}).json()
    assert j["ok"] and not j["existe"]
