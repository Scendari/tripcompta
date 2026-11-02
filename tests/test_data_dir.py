"""Dossier des données : nom TripCompta, ancien dossier « Compta Voyages » renommé, choix dans l'application."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from compta import paths as P
from compta.ops import Ops
from compta.web import create_app


def test_old_folder_renamed_once(tmp_path):
    docs = tmp_path / "Documents"
    (docs / "Compta Voyages" / "justificatifs").mkdir(parents=True)
    (docs / "Compta Voyages" / "compta.db").write_bytes(b"BASE")
    (docs / "Compta Voyages" / "justificatifs" / "t.jpg").write_bytes(b"IMG")
    assert P.migrate_old_folder(docs) == docs / "TripCompta"
    assert not (docs / "Compta Voyages").exists()
    assert (docs / "TripCompta" / "compta.db").read_bytes() == b"BASE"
    assert (docs / "TripCompta" / "justificatifs" / "t.jpg").read_bytes() == b"IMG"
    assert (docs / "TripCompta" / "sauvegardes" / "avant-renommage-tripcompta.db").read_bytes() == b"BASE"
    assert P.migrate_old_folder(docs) is None                       # une seule fois
    # s'il existe déjà un dossier TripCompta, l'ancien n'est pas touché
    (docs / "Compta Voyages").mkdir()
    assert P.migrate_old_folder(docs) is None and (docs / "Compta Voyages").exists()


def test_priorities(tmp_path, monkeypatch):
    monkeypatch.delenv("TRIPCOMPTA_DATA_DIR", raising=False)
    monkeypatch.delenv("COMPTA_DATA_DIR", raising=False)
    monkeypatch.setenv("TRIPCOMPTA_CONFIG", str(tmp_path / "reglages.json"))
    monkeypatch.setattr(P, "documents_dir", lambda: tmp_path / "Docs")
    assert P.default_data_dir() == tmp_path / "Docs" / "TripCompta"            # par défaut
    P.write_config(data_dir=str(tmp_path / "Ailleurs"))
    assert P.default_data_dir() == tmp_path / "Ailleurs"                          # choisi dans l'appli
    monkeypatch.setenv("COMPTA_DATA_DIR", str(tmp_path / "Env"))                  # ancienne variable : acceptée
    assert P.default_data_dir() == tmp_path / "Env"
    assert P.looks_synced(tmp_path / "Dropbox" / "x") and not P.looks_synced(tmp_path / "Docs")


@pytest.fixture
def chosen(tmp_path, monkeypatch):
    """Une installation dont le dossier a été choisi dans l'application ; la relance est simulée."""
    monkeypatch.delenv("TRIPCOMPTA_DATA_DIR", raising=False)
    monkeypatch.delenv("COMPTA_DATA_DIR", raising=False)
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
    r = c.post("/reglages/donnees", data={"dossier": str(tmp / "b"), "mode": "deplacer"})
    import html
    assert "Redémarrage" in r.text and "n'a pas été touché" in html.unescape(r.text)
    import time; time.sleep(0.8)
    assert calls == ["relance"]
    assert json.loads((tmp / "reglages.json").read_text())["data_dir"] == str(tmp / "b")
    con = sqlite3.connect(tmp / "b" / "compta.db")
    assert con.execute("SELECT name FROM trips").fetchall() == [("Sortie",)]
    assert (tmp / "b" / "justificatifs" / "1_x.jpg").read_bytes() == b"IMG"
    assert (tmp / "a" / "compta.db").exists()                       # l'ancien n'est pas touché


def test_change_errors(chosen):
    app, c, tmp, calls = chosen
    def post(**d):
        return c.post("/reglages/donnees", data=d).text.replace("&#39;", "'").replace("&quot;", '"')
    assert "chemin complet" in post(dossier="relatif/dossier", mode="deplacer")
    assert "déjà le dossier utilisé" in post(dossier=str(tmp / "a"), mode="deplacer")
    assert "pas de données Trip Compta" in post(dossier=str(tmp / "vide"), mode="utiliser")
    (tmp / "plein").mkdir()
    (tmp / "plein" / "compta.db").write_bytes(b"")
    assert "contient déjà des données" in post(dossier=str(tmp / "plein"), mode="deplacer")
    assert calls == []
    # utiliser un dossier qui a déjà des données ; puis revenir au défaut
    assert "va utiliser les données" in post(dossier=str(tmp / "plein"), mode="utiliser")
    assert "Retour au dossier par défaut" in post(action="defaut")
    assert json.loads((tmp / "reglages.json").read_text())["data_dir"] is None


def test_imposed_folder_and_open(tmp_path, monkeypatch):
    app = create_app(tmp_path / "d")                                  # imposé au lancement
    c = TestClient(app)
    page = c.get("/reglages/donnees").text
    assert "imposé au lancement" in page and "Changer de dossier</button>" not in page
    assert "imposé au lancement" in c.post("/reglages/donnees", data={"dossier": "/tmp/x"}).text
    opened = []
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", lambda args, **k: opened.append(args))
    r = c.post("/reglages/ouvrir-dossier", headers={"referer": "http://t/"}, follow_redirects=True)
    assert "Dossier ouvert" in r.text and opened and opened[0][-1] == str(tmp_path / "d")
    home = c.get("/").text
    assert "Ouvrir le dossier" in home and 'href="/reglages/donnees"' in home
