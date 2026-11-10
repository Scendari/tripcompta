"""Le dossier des voyages, sans synchronisation : Trip Compta lit et écrit directement son dossier (même dans un
dossier Cloud : à tes risques, un seul ordinateur à la fois). Garde-fous : nom du dernier ordinateur qui a modifié,
copies en conflit (exportables, rangées, jamais fusionnées ni effacées), l'IA (autre programme) et l'application
voient les mêmes données. « Changer de dossier » : un chemin ; des voyages dedans → ouverts, sinon → copiés.

Voyage de test (montants vérifiés à la main) : Anne, Bruno, Chloé.
  Gîte 300 € payé par l'asso, pour tous            → 100 € chacun
  Essence 90 € avancée par Anne, pour Anne et Bruno → 45 € chacun
  Parts : Anne 145, Bruno 145, Chloé 100 (total 390) ; soldes : Anne 145 − 90 = 55, Bruno 145, Chloé 100.
"""
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from compta import dossier as C
from compta import models as M
from compta import services as S
from compta.ops import Ops
from compta.web import create_app

EXPECTED = {"Anne": (14500, 5500), "Bruno": (14500, 14500), "Chloé": (10000, 10000)}   # (part, solde) en centimes


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Réglages et Documents isolés ; un dossier « Google Drive » avec espaces et accents, choisi dans les réglages."""
    monkeypatch.delenv("TRIPCOMPTA_DATA_DIR", raising=False)
    monkeypatch.setenv("TRIPCOMPTA_CONFIG", str(tmp_path / "reglages" / "reglages.json"))
    monkeypatch.setenv("TRIPCOMPTA_DOCUMENTS", str(tmp_path / "Mes Documents"))
    monkeypatch.setenv("TRIPCOMPTA_ORDINATEUR", "PC-Anne")
    shared = tmp_path / "Mon Drive é" / "TripCompta"
    shared.mkdir(parents=True)
    from compta import paths as P
    P.write_config(data_dir=str(shared))
    return tmp_path, shared


def stop(app):
    app.state.arreter()


def seed(app) -> int:
    ops = Ops(app.state.db)
    tid = ops.create_trip("Sortie test")["voyage_id"]
    ops.add_participants(tid, ["Anne", "Bruno", "Chloé"])
    ops.add_expense(tid, "Gîte", categorie="Logement", montant_reel="300")
    ops.add_expense(tid, "Essence", categorie="Transport", montant_reel="90", paye_par="Anne", pour=["Anne", "Bruno"])
    return tid


def amounts(app, tid) -> dict:
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        r = S.compute_trip(t)
        return {p.name: (p.share, p.balance) for p in r.people.values()}


def trip_names(path: Path) -> list:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return sorted(r[0] for r in con.execute("SELECT name FROM trips"))
    finally:
        con.close()


def clean(t):
    import html
    return re.sub(r"\s+", " ", html.unescape(t).replace("\xa0", " "))


# ---------------------------------------------------------------- directement dans le dossier
def test_works_directly_in_the_chosen_folder(env):
    tmp, shared = env
    app = create_app()
    assert app.state.db.dir == shared and app.state.coffre is None
    tid = seed(app)
    assert trip_names(shared / C.DB) == ["Sortie test"]                    # écrit dans le dossier, tout de suite
    assert amounts(app, tid) == EXPECTED
    assert not (shared / "sauvegardes").exists() and not (shared / "Justificatifs à trier").exists()
    assert app.state.db.backups_dir.is_dir() and shared not in app.state.db.backups_dir.parents   # à part
    assert not (tmp / "reglages" / "copies").exists()                      # aucune copie de travail
    assert not list(shared.glob("tripcompta-verrou*"))                     # aucun verrou
    page = TestClient(app).get("/").text
    assert "save-chip" in page and "Enregistré automatiquement à" in page    # 💾 l'heure du dernier enregistrement
    assert "cloud" not in page.lower() and "Google Drive" not in page
    stop(app)
    app = create_app()
    assert amounts(app, tid) == EXPECTED
    stop(app)


def test_old_shared_setting_is_simply_ignored(env):
    """Un ancien réglage « dossier partagé » : le dossier est ouvert directement, comme les autres."""
    tmp, shared = env
    from compta import paths as P
    P.write_config(data_dir=str(shared), partage=True)
    app = create_app()
    assert app.state.db.dir == shared and app.state.coffre is None
    stop(app)


def test_two_computers_in_turn(env, monkeypatch):
    """Chacun son tour (le cloud recopie le dossier) : rien ne se perd, et l'ouverture dit qui a modifié en dernier."""
    tmp, shared = env
    a = create_app()
    tid = seed(a)
    stop(a)
    monkeypatch.setenv("TRIPCOMPTA_ORDINATEUR", "MacBook de Bruno")
    b = create_app()
    page = clean(TestClient(b).get("/").text)
    assert "Tes voyages ont été modifiés il y a moins d'une minute sur « PC-Anne »" in page
    assert amounts(b, tid) == EXPECTED
    TestClient(b).post("/voyages", data={"name": "Saisi sur le Mac"})
    data = clean(TestClient(b).get("/reglages/donnees").text)
    assert "Dernière modification sur « MacBook de Bruno »" in data and "Un seul ordinateur à la fois" in data
    stop(b)
    monkeypatch.setenv("TRIPCOMPTA_ORDINATEUR", "PC-Anne")
    a = create_app()
    assert trip_names(shared / C.DB) == ["Saisi sur le Mac", "Sortie test"]
    assert "sur « MacBook de Bruno »" in clean(TestClient(a).get("/").text)
    stop(a)


# ---------------------------------------------------------------- copies en conflit
def test_conflicted_copies_exported_imported_and_put_away(env):
    tmp, shared = env
    a = create_app()
    ca = TestClient(a)
    ca.post("/voyages", data={"name": "Actuel"})
    other = tmp / "x.db"
    shutil.copyfile(shared / C.DB, other)
    con = sqlite3.connect(other)
    con.execute("UPDATE trips SET name='Saisi sur l''autre ordinateur'")
    con.commit()
    con.close()
    for n in ("compta (conflicted copy 2026-11-03).db", "compta (copie en conflit de Paul).db",
              "compta-PC-DE-PAUL.db", "compta (1).db", "compta 2.db"):
        shutil.copyfile(other, shared / n)
    (shared / "compta (abîmée 2026-11-04 10h00).db").write_bytes(b"pas une base" * 200)
    (shared / "comptable.txt").write_text("sans rapport")
    page = clean(ca.get("/").text)
    assert "6 autres versions de tes voyages sont apparues" in page
    page = clean(ca.get("/reglages/donnees").text)
    assert "Une autre version de tes voyages" in page and "compta-PC-DE-PAUL.db" in page
    assert "abîmée, illisible" in page and "Importer un voyage" in page
    assert "Utiliser cette version" not in page                           # jamais de remplacement
    # exporter : un .zip que « Importer un voyage » ajoute à côté, sans rien écraser
    r = ca.get("/reglages/conflit/export", params={"nom": "compta (1).db"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "autre%20version%20des%20voyages" in r.headers["content-disposition"]
    before = (shared / "compta (1).db").read_bytes()
    r = ca.post("/importer", files={"fichier": ("autre.zip", r.content, "application/zip")}, follow_redirects=True)
    token = re.search(r'action="/importer/([^"]+)"', r.text).group(1)
    ca.post(f"/importer/{token}", data={})
    assert trip_names(shared / C.DB) == ["Actuel", "Saisi sur l'autre ordinateur"]
    assert (shared / "compta (1).db").read_bytes() == before              # l'autre version n'a pas été touchée
    r = ca.get("/reglages/conflit/export", params={"nom": "compta (abîmée 2026-11-04 10h00).db"},
               follow_redirects=True)
    assert "abîmée" in clean(r.text)
    # ranger : dans « Anciennes versions », jamais effacée
    ca.post("/reglages/conflit", data={"nom": "compta (1).db"})
    assert (shared / "Anciennes versions" / "compta (1).db").exists() and not (shared / "compta (1).db").exists()
    assert "5 autres versions" in clean(ca.get("/").text)
    # noms piégés : refusés
    assert ca.get("/reglages/conflit/export", params={"nom": "../reglages/reglages.json"}).status_code == 404
    assert ca.post("/reglages/conflit", data={"nom": "compta.db"}).status_code == 404
    stop(a)


def test_no_inbox_folder_anymore(env):
    """« Justificatifs à trier » (photos déposées depuis un téléphone) n'existe plus : ni dossier, ni pages."""
    tmp, shared = env
    a = create_app()
    tid = seed(a)
    c = TestClient(a)
    assert not (shared / "Justificatifs à trier").exists()
    assert "à trier" not in clean(c.get(f"/v/{tid}/justificatifs").text)
    assert c.get("/a-trier/fichier", params={"nom": "x.jpg"}).status_code == 404
    stop(a)


# ---------------------------------------------------------------- l'IA (autre programme) et l'application
def test_app_sees_ai_writes_without_restart(env):
    """Le serveur MCP (un autre programme) écrit dans la même base : l'application le voit tout de suite."""
    tmp, shared = env
    app = create_app()
    cl = TestClient(app)
    seed(app)
    from compta.db import Database
    ai = Ops(Database(shared))                                   # comme le serveur MCP, dans son propre programme
    ai.create_trip("Saisi par l'IA")
    assert "Saisi par l'IA" in clean(cl.get("/").text)
    page = cl.get("/v/1/ia").text.replace("&#34;", '"').replace("&quot;", '"')
    assert '"--home"' not in page and str(shared) in page.replace("\\\\", "\\")
    stop(app)


def test_ai_refuses_a_protected_folder(env):
    """Le dossier chiffré lui-même n'est jamais ouvert par l'IA : aucune base en clair n'y est créée."""
    tmp, shared = env
    (shared / "cle.tripcompta").write_text("{}")
    from compta.mcp_server import build_server
    build_server(str(shared))
    assert not (shared / C.DB).exists()


# ---------------------------------------------------------------- choisir le dossier
def test_launching_twice_shows_the_running_one(monkeypatch):
    """Cliquer sur l'icône alors que Trip Compta tourne déjà : l'onglet de celle qui tourne s'ouvre, aucune 2e
    n'est lancée."""
    import importlib
    import uvicorn
    from tests.conftest import _free_port
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    run = importlib.import_module("run")
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    while not srv.started:
        time.sleep(0.05)
    try:
        assert run.running_instance(port, 1) == f"http://127.0.0.1:{port}/"
        assert run.running_instance(_free_port(), 1) is None
        opened, started = [], []
        monkeypatch.setattr(run, "running_instance", lambda *a: f"http://127.0.0.1:{port}/")
        monkeypatch.setattr(run.webbrowser, "open", opened.append)
        from compta import fenetre
        monkeypatch.setattr(fenetre, "DISCRETE", False)          # en essai discret, rien ne s'ouvrirait
        monkeypatch.setattr("compta.web.create_app", lambda *a, **k: started.append(1))
        monkeypatch.delenv("TRIPCOMPTA_DATA_DIR", raising=False)
        monkeypatch.delenv("TRIPCOMPTA_RESTARTED", raising=False)
        monkeypatch.setattr(sys, "argv", ["run.py"])
        run.main()
        assert opened == [f"http://127.0.0.1:{port}/"] and started == []
    finally:
        srv.should_exit = True
        th.join(5)


def test_change_folder_then_back_to_default(tmp_path, monkeypatch):
    """« Changer de dossier » : un seul chemin (plus de dossiers Cloud repérés). Vide → les voyages y sont copiés ;
    « Revenir au dossier par défaut » → Documents/TripCompta, qui contient déjà des voyages → ils sont ouverts."""
    monkeypatch.delenv("TRIPCOMPTA_DATA_DIR", raising=False)
    monkeypatch.setenv("TRIPCOMPTA_CONFIG", str(tmp_path / "conf" / "reglages.json"))
    docs = tmp_path / "Mes Documents"
    monkeypatch.setenv("TRIPCOMPTA_DOCUMENTS", str(docs))
    app = create_app()                                           # dossier par défaut
    tid = seed(app)
    page = TestClient(app).get("/reglages/donnees").text
    assert "Revenir au dossier par défaut" not in page          # on y est déjà
    assert "Google Drive (" not in page and "iCloud Drive" not in page and 'type="radio"' not in page
    cloud = tmp_path / "Dropbox" / "TripCompta"
    cloud.parent.mkdir()
    r = TestClient(app).post("/reglages/donnees", data={"dossier": str(cloud)})
    assert "ont été copiés" in clean(r.text)
    assert json.loads((tmp_path / "conf" / "reglages.json").read_text()) == {"data_dir": str(cloud)}
    stop(app)
    app2 = create_app()
    assert app2.state.db.dir == cloud
    cl = TestClient(app2)
    page = cl.get("/reglages/donnees").text
    assert "Revenir au dossier par défaut" in page and str(docs / "TripCompta") in page
    Ops(app2.state.db).create_trip("Saisi dans le cloud")
    r = cl.post("/reglages/donnees", data={"dossier": str(docs / "TripCompta")})   # contient l'ancienne copie
    assert "va ouvrir les voyages rangés dans" in clean(r.text)
    assert json.loads((tmp_path / "conf" / "reglages.json").read_text())["data_dir"] is None
    stop(app2)
    app3 = create_app()
    assert app3.state.db.dir == docs / "TripCompta" and amounts(app3, tid) == EXPECTED
    assert "Saisi dans le cloud" not in TestClient(app3).get("/").text   # rien n'a été copié par-dessus
    assert trip_names(cloud / C.DB) == ["Saisi dans le cloud", "Sortie test"]   # l'autre dossier est intact
    stop(app3)


def test_settings_page(env):
    tmp, shared = env
    a = create_app()
    tid = seed(a)
    cl = TestClient(a)
    assert 'href="/reglages"' in cl.get(f"/v/{tid}").text
    page = clean(cl.get("/reglages").text)
    assert "Où sont mes voyages" in page and str(shared) in page and "Non protégés" in page   # complet au survol
    assert "…/Mon Drive é/TripCompta · enregistré automatiquement" in page                    # sa fin, sur une ligne
    assert "un seul ordinateur à la fois" in page and "enregistré automatiquement à" in page
    stop(a)


def test_screens_light_dark_phone(env):
    """Page du dossier (copie en conflit, aide), justificatifs : clair, sombre, téléphone ; pas de débordement ni
    d'erreur JavaScript."""
    import uvicorn
    from playwright.sync_api import sync_playwright
    from tests.conftest import _free_port
    tmp, shared = env
    a = create_app()
    tid = seed(a)
    shutil.copyfile(shared / C.DB, shared / "compta (conflicted copy 2026-11-03).db")
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(a, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    while not srv.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    shots = os.environ.get("TRIPCOMPTA_SHOTS")
    try:
        with sync_playwright() as p:
            br = p.chromium.launch()
            for dark in (False, True):
                for w in (1280, 390):
                    tag = ("sombre" if dark else "clair") + ("-mobile" if w < 500 else "")
                    pg = br.new_page(viewport={"width": w, "height": 900}, color_scheme="dark" if dark else "light")
                    errs = []
                    pg.on("pageerror", lambda e: errs.append(str(e)))
                    for name, url in (("dossier", base + "/reglages/donnees"), ("voyage", base + f"/v/{tid}"),
                                      ("justificatifs", base + f"/v/{tid}/justificatifs")):
                        pg.goto(url)
                        wide = pg.evaluate(f"""() => [...document.querySelectorAll('body *')]
                            .filter(e => e.getBoundingClientRect().right > {w + 1})
                            .map(e => e.tagName + '.' + e.className + ' « ' + (e.textContent || '').trim().slice(0, 50) + ' »')""")
                        assert pg.evaluate("document.documentElement.scrollWidth") <= w + 1, (name, tag, wide[:6])
                        if shots:
                            Path(shots).mkdir(parents=True, exist_ok=True)
                            pg.screenshot(path=f"{shots}/dossier-{name}-{tag}.png", full_page=True)
                    assert errs == []
                    pg.close()
            pg = br.new_page()
            pg.goto(base + f"/v/{tid}")
            assert "Une autre version de tes voyages" in pg.locator(".folder-banner.warn").inner_text()
            pg.locator(".folder-banner.warn a").click()
            assert pg.url.endswith("/reglages/donnees#conflits")
            with pg.expect_download() as dl:
                pg.get_by_role("link", name="Exporter cette version (.zip)").click()
            assert dl.value.suggested_filename.startswith("autre version des voyages")
            br.close()
    finally:
        srv.should_exit = True
        th.join(5)
        stop(a)
