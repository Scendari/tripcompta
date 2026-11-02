"""Catégories : à gauche celles disponibles (communes à tous les voyages), à droite celles du voyage ;
on les fait passer d'un côté à l'autre. Les catégories créées sont proposées partout et peuvent être supprimées."""
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from compta import models as M
from compta import services as S
from compta.db import Database
from compta.ops import Ops
from compta.web import create_app

RECOMMENDED = ["Logement", "Transport / Covoit", "Plongée", "Location matériel", "Repas",
               "Activités accompagnants", "Divers"]


@pytest.fixture()
def app(tmp_path):
    return create_app(tmp_path / "d")


def new_trip(c, name="Test"):
    r = c.post("/voyages", data={"name": name}, follow_redirects=False)
    return int(re.search(r"/v/(\d+)/", r.headers["location"]).group(1))


def cats(app, tid):
    with app.state.db.Session() as s:
        return [c.name for c in sorted(s.get(M.Trip, tid).categories, key=lambda c: c.position)]


def cat_id(app, tid, name):
    with app.state.db.Session() as s:
        return next(c.id for c in s.get(M.Trip, tid).categories if c.name == name)


def columns(page):
    """(disponibles, celles du voyage) : pastilles « + Nom » à droite, lignes « Nom » à gauche."""
    avail = page.split('id="available"')[1].split('<form method="post" action="/v/')[0:]
    avail_html = page.split('id="available"')[1].split('id="newname"')[0]
    sel_html = page.split('id="selected"')[1].split("</ul>")[0]
    left = re.findall(r'class="cat-pill"[^>]*>\+ ([^<]+)</button>', avail_html)
    right = re.findall(r'<b class="cat-name">([^<]+)</b>', sel_html)
    return left, right


def test_new_trip_gets_default_categories_and_two_columns(app):
    c = TestClient(app)
    tid = new_trip(c)
    assert cats(app, tid) == RECOMMENDED
    left, right = columns(c.get(f"/v/{tid}/categories").text.replace("&#39;", "'"))
    assert right == RECOMMENDED
    assert left == ["Gonflage / Nitrox", "Formation / Passage de niveau", "Courses", "Transport collectif",
                    "Bateau / Navette", "Assurance", "Taxes", "Frais divers"]
    page = c.get(f"/v/{tid}/categories").text
    assert "Qui est concerné" not in page


def test_move_between_columns(app):
    c = TestClient(app)
    tid = new_trip(c)
    c.post(f"/v/{tid}/categories/ajouter", data={"name": "Taxes"})
    assert cats(app, tid)[-1] == "Taxes"
    left, right = columns(c.get(f"/v/{tid}/categories").text)
    assert "Taxes" in right and "Taxes" not in left
    c.post(f"/v/{tid}/categories/{cat_id(app, tid, 'Repas')}/retirer")
    left, right = columns(c.get(f"/v/{tid}/categories").text)
    assert "Repas" in left and "Repas" not in right
    # Défaire est proposé juste après
    r = c.post(f"/v/{tid}/categories/ajouter", data={"name": "Repas"}, follow_redirects=True)
    assert "↶ Défaire" in r.text


def test_removing_a_used_category_moves_its_expenses_to_divers(app):
    c = TestClient(app)
    ops = Ops(app.state.db)
    tid = new_trip(c)
    ops.add_participants(tid, ["Anne"])
    ops.add_expense(tid, "Nitrox", categorie="Plongée", montant_reel="40")
    r = c.post(f"/v/{tid}/categories/{cat_id(app, tid, 'Plongée')}/retirer", follow_redirects=True)
    assert "sont maintenant dans « Divers »" in r.text
    assert ops.trip(tid)["depenses"][0]["categorie"] == "Divers"
    # retirer « Divers » utilisée : refusé
    r = c.post(f"/v/{tid}/categories/{cat_id(app, tid, 'Divers')}/retirer", follow_redirects=True)
    assert "change d&#39;abord" in r.text or "change d'abord" in r.text
    # retirer « Divers » puis une catégorie utilisée : « Divers » est recréée
    ops.update_expense(tid, "Nitrox", categorie="Repas")
    c.post(f"/v/{tid}/categories/{cat_id(app, tid, 'Divers')}/retirer")
    c.post(f"/v/{tid}/categories/{cat_id(app, tid, 'Repas')}/retirer")
    assert "Divers" in cats(app, tid) and ops.trip(tid)["depenses"][0]["categorie"] == "Divers"


def test_created_category_is_offered_in_every_trip_and_can_be_deleted(app):
    c = TestClient(app)
    t1, t2 = new_trip(c, "Un"), new_trip(c, "Deux")
    r = c.post(f"/v/{t1}/categories", data={"name": "  Sortie   bateau "}, follow_redirects=True)
    assert "créée dans les catégories disponibles" in r.text
    assert "Sortie bateau" not in cats(app, t1)                      # pas ajoutée d'office au voyage
    assert "Sortie bateau" in columns(c.get(f"/v/{t1}/categories").text)[0]
    c.post(f"/v/{t1}/categories/ajouter", data={"name": "Sortie bateau"})   # un clic pour l'ajouter
    assert "Sortie bateau" in cats(app, t1)
    left, _ = columns(c.get(f"/v/{t2}/categories").text)
    assert "Sortie bateau" in left                                   # proposée dans l'autre voyage
    t3 = new_trip(c, "Trois")
    assert "Sortie bateau" in columns(c.get(f"/v/{t3}/categories").text)[0]   # et dans les suivants
    page = c.get(f"/v/{t2}/categories").text
    assert page.count('action="/v/%d/categories/oublier"' % t2) == 1    # seules les créées sont supprimables
    r = c.post(f"/v/{t2}/categories/oublier", data={"name": "Sortie bateau"}, follow_redirects=True)
    assert "n'est plus proposée" in r.text.replace("&#39;", "'")
    assert "Sortie bateau" not in columns(c.get(f"/v/{t3}/categories").text)[0]
    assert "Sortie bateau" in cats(app, t1)                          # le voyage qui l'utilise la garde
    r = c.post(f"/v/{t2}/categories/oublier", data={"name": "Logement"}, follow_redirects=True)
    assert "Seules les catégories que tu as créées" in r.text
    # doublons refusés
    r = c.post(f"/v/{t1}/categories", data={"name": "logement"}, follow_redirects=True)
    assert "déjà dans ce voyage" in r.text
    r = c.post(f"/v/{t1}/categories", data={"name": "assurance"}, follow_redirects=True)
    assert "déjà dans les catégories disponibles" in r.text


def test_category_typed_in_expense_form_joins_the_library(app):
    c = TestClient(app)
    ops = Ops(app.state.db)
    t1, t2 = new_trip(c, "Un"), new_trip(c, "Deux")
    ops.add_participants(t1, ["Anne"])
    with app.state.db.Session() as s:
        pid = s.get(M.Trip, t1).participants[0].id
    c.post(f"/v/{t1}/depenses", data={"label": "Pot", "category": "Apéritif", "actual": "20", "payer": "asso",
                                      "payer_kind": "asso", f"inc_{pid}": "1"})
    assert "Apéritif" in cats(app, t1)
    assert "Apéritif" in columns(c.get(f"/v/{t2}/categories").text)[0]


def test_old_categories_are_migrated(tmp_path):
    d = tmp_path / "old"
    d.mkdir()
    con = sqlite3.connect(d / "compta.db")
    con.executescript("""
        CREATE TABLE trips (id INTEGER PRIMARY KEY, name VARCHAR(200), place VARCHAR(200), organizer VARCHAR(200),
                            start DATE, "end" DATE, created DATETIME);
        CREATE TABLE categories (id INTEGER PRIMARY KEY, trip_id INTEGER REFERENCES trips(id), name VARCHAR(100),
                                 position INTEGER);
        INSERT INTO trips (id, name, place, organizer) VALUES (1, 'Ancien', '', '');
        INSERT INTO categories VALUES (1, 1, 'Logement', 0), (2, 1, 'Plongée', 1);
    """)
    con.commit()
    con.close()
    db = Database(d)
    with db.Session() as s:
        assert [c.name for c in s.get(M.Trip, 1).categories] == ["Logement", "Plongée"]


def test_browser_categories(app):
    import socket
    import threading
    import time
    import uvicorn
    from playwright.sync_api import sync_playwright
    c = TestClient(app)
    tid = new_trip(c)
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    while not srv.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}/v/{tid}"
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(viewport={"width": 1280, "height": 900})
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(base + "/categories")
            pg.click("#available .cat-pill:has-text('Taxes')")
            assert pg.locator("#selected .cat-name:has-text('Taxes')").count() == 1
            pg.locator("#selected .cat-row", has=pg.locator(".cat-name:has-text('Taxes')")).locator("button:has-text('Retirer')").click()
            assert pg.locator("#available .cat-pill:has-text('Taxes')").count() == 1
            # « + Sous-catégorie » ouvre le champ seulement au clic
            row = pg.locator("#selected .cat-row").first
            assert not row.locator(".sub-add-row").is_visible()
            row.locator("button:has-text('+ Sous-catégorie')").click()
            assert row.locator(".sub-add-row").is_visible()
            pg.fill("#newname", "Sortie bateau")
            pg.click("button:has-text('Créer')")
            assert pg.locator("#available .cat-pill:has-text('Sortie bateau')").count() == 1
            assert pg.locator("#selected .cat-name:has-text('Sortie bateau')").count() == 0
            m = b.new_page(viewport={"width": 390, "height": 800})
            m.goto(base + "/categories")
            assert m.evaluate("document.documentElement.scrollWidth") <= 391
            assert errors == []
            b.close()
    finally:
        srv.should_exit = True
        th.join(5)


def test_expense_form_category_dropdown(app):
    c = TestClient(app)
    ops = Ops(app.state.db)
    tid = new_trip(c)
    ops.add_participants(tid, ["Anne"])
    with app.state.db.Session() as s:
        pid = s.get(M.Trip, tid).participants[0].id
    c.post(f"/v/{tid}/categories/ajouter", data={"name": "Taxes"})
    form = c.get(f"/v/{tid}/depenses/nouvelle").text
    sel = form.split('<select id="category" name="category">')[1].split("</select>")[0]
    opts = re.findall(r'<option value="([^"]*)"', sel)
    assert opts == [""] + RECOMMENDED + ["Taxes", "__new__"]          # les catégories du voyage, dans l'ordre
    assert "+ Nouvelle catégorie…" in sel
    # choisir dans la liste
    base = {"label": "Taxe", "actual": "15", "payer": "asso", "payer_kind": "asso", f"inc_{pid}": "1"}
    c.post(f"/v/{tid}/depenses", data={**base, "category": "Taxes"})
    # en créer une nouvelle depuis la liste
    c.post(f"/v/{tid}/depenses", data={**base, "label": "Pot", "category": "__new__", "category_new": "Apéritif"})
    deps = {e["libelle"]: e["categorie"] for e in ops.trip(tid)["depenses"]}
    assert deps == {"Taxe": "Taxes", "Pot": "Apéritif"}
    # la modification re-sélectionne la bonne catégorie
    eid = next(e["id"] for e in ops.trip(tid)["depenses"] if e["libelle"] == "Pot")
    edit = c.get(f"/v/{tid}/depenses/{eid}/modifier").text
    assert '<option value="Apéritif" selected>' in edit
