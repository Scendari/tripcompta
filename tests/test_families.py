"""Familles (comptes réglés ensemble) et accompagnants (ne plongent pas).

Démo « Week-end Banyuls » (montants vérifiables de tête) :
    plongeur     = gîte 1000/8 = 125 + plongées 600/5 = 120 + courses 240/8 = 30 = 275 €
    accompagnant = gîte 125 + kayak 60/3 = 20 + courses 30                       = 175 €
    Famille Lebrun = 2 × 275 + 2 × 175 = 900 € − 240 avancés (Julie) − 500 versés (Paul) = 160 € à verser
    Famille Roux      = 275 + 175 = 450 € − 450 versés = à jour
    Karim 275 − 300 = on lui doit 25 € ; Inès 275 € à verser.
"""
import re
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from compta import engine as E
from compta import models as M
from compta.db import Database
from compta.demo import FAMILY_DEMO_NAME, seed_family_demo
from compta.ops import Ops, OpsError
from compta.services import compute_trip
from compta.web import create_app


def clean(t: str) -> str:
    return re.sub(r"[  ]", " ", t)


# ---------------------------------------------------------------- moteur

def test_engine_accounts_group_families():
    P = [E.Participant(1, "Paul", family=10), E.Participant(2, "Léa", family=10, companion=True),
         E.Participant(3, "Karim")]
    exps = [E.Expense(1, "Gîte", actual=30000, weights={1: 1, 2: 1, 3: 1}),
            E.Expense(2, "Plongée", actual=10000, weights={1: 1, 3: 1}, payer=2)]
    r = E.compute(P, exps, movements=[E.Movement(1, "versement", 5000)],
                  families=[E.Family(10, "Lebrun", referent=1)])
    assert r.people[2].companion and r.people[1].family == 10
    acc = {a.name: a for a in r.accounts}
    assert set(acc) == {"Famille Lebrun", "Karim"}
    fam = acc["Famille Lebrun"]
    # Paul 100 + 50 = 150, Léa 100 ; Léa a avancé 100, Paul a versé 50
    assert fam.share == 25000 and fam.balance == 25000 - 10000 - 5000 == 10000
    assert r.people[2].balance == 0 and r.people[1].balance == 10000
    assert r.to_collect == 10000 + 15000 and r.to_refund == 0
    st = r.settlements()
    assert (fam, E.ASSO, 10000) in st and next(a for a, _, _ in st if a.is_family).referent == 1


def test_engine_family_nets_member_balances():
    """Dans une famille, l'un « doit », l'autre « est en avance » : seul le net compte."""
    P = [E.Participant(1, "A", family=1), E.Participant(2, "B", family=1)]
    exps = [E.Expense(1, "Gîte", actual=20000, weights={1: 1, 2: 1}, payer=1)]
    r = E.compute(P, exps, families=[E.Family(1, "X")])
    assert r.people[1].balance == -10000 and r.people[2].balance == 10000
    assert r.to_collect == 0 and r.to_refund == 0 and r.settlements() == []
    assert r.accounts[0].status == "ok" and r.accounts[0].referent == 1   # 1er membre par défaut


def test_engine_without_families_unchanged():
    P = [E.Participant(1, "A"), E.Participant(2, "B")]
    r = E.compute(P, [E.Expense(1, "x", actual=1001, weights={1: 1, 2: 1})])
    assert [a.name for a in r.accounts] == ["A", "B"] and all(not a.is_family for a in r.accounts)
    assert r.to_collect == 501 + 501


# ---------------------------------------------------------------- démo familles, à la main

@pytest.fixture()
def fam(tmp_path):
    app = create_app(tmp_path / "d")
    tid = seed_family_demo(app.state.db)
    c = TestClient(app)
    s = app.state.db.Session()
    t = s.get(M.Trip, tid)
    P = {p.name: p.id for p in t.participants}
    s.close()
    return c, app, tid, P


def result(app, tid, mode=E.ACTUAL):
    with app.state.db.Session() as s:
        return compute_trip(s.get(M.Trip, tid), mode)


def test_family_demo_amounts_by_hand(fam):
    c, app, tid, P = fam
    r = result(app, tid)
    sh = {r.people[i].name: r.people[i].share for i in P.values()}
    for n in ["Paul Lebrun", "Julie Lebrun", "Bruno Roux", "Karim", "Inès"]:
        assert sh[n] == 27500, n
    for n in ["Léa Lebrun", "Tom Lebrun", "Nadia Roux"]:
        assert sh[n] == 17500, n
    acc = {a.name: a for a in r.accounts}
    assert [a.name for a in r.accounts] == ["Inès", "Karim", "Famille Lebrun", "Famille Roux"]
    assert acc["Famille Lebrun"].share == 90000 and acc["Famille Lebrun"].balance == 16000
    assert acc["Famille Roux"].balance == 0 and acc["Karim"].balance == -2500 and acc["Inès"].balance == 27500
    assert r.total_cost == 190000 and r.is_balanced() and r.rounding_gain == 0
    assert r.to_collect == 16000 + 27500 and r.to_refund == 2500
    assert acc["Famille Lebrun"].referent == P["Paul Lebrun"]


def test_family_screens(fam):
    c, app, tid, P = fam
    home = clean(c.get("/").text)
    assert "Reste à recevoir : 435,00 €" in home
    dash = clean(c.get(f"/v/{tid}").text)
    assert "Famille Lebrun" in dash and "doit encore 160,00 €" in dash and "Famille Roux" in dash
    assert "de 2 personnes ou familles" in dash and "à 1 personne ou famille" in dash and "doivent encore" not in dash
    assert "Famille Lebrun n&#39;a encore rien versé" not in dash and "Inès n&#39;a encore rien versé" in dash
    parts = clean(c.get(f"/v/{tid}/participants").text)
    assert "dont 3 accompagnants" in parts and "2 familles" in parts
    assert "paie : Paul Lebrun" in parts and "sa part 175,00 €" in parts
    lea = clean(c.get(f"/v/{tid}/p/{P['Léa Lebrun']}").text)
    assert "Les comptes se font en famille : Famille Lebrun" in lea
    assert "Famille Lebrun doit encore 160,00 €" in lea and "paie pour la famille" in lea
    assert f"p={P['Paul Lebrun']}&a=160#ajout" in lea          # le versement sera au nom de Paul
    exps = c.get(f"/v/{tid}/depenses").text
    assert "<span>Tout le monde</span>" in exps and "les plongeurs" not in exps and "les accompagnants" not in exps
    money = clean(c.get(f"/v/{tid}/argent").text)
    flat = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", money))
    assert "Famille Lebrun (à Paul Lebrun) 160,00 € ✓ Reçu" in flat
    assert "À rendre 1 personne ou famille 25,00 € Karim 25,00 € ✓ Remboursé" in flat
    assert 'optgroup label="Famille Lebrun — doit 160,00 €"' in clean(c.get(f"/v/{tid}/argent/mouvements").text)
    form = c.get(f"/v/{tid}/depenses/nouvelle").text
    assert "Plongeurs seulement" not in form and 'data-companion="1"' in form
    assert ">Plongeur</button>" in form and ">Accompagnant</button>" in form and ">Lebrun</button>" in form
    assert "Gérer les groupes" not in form


def test_settle_family_with_one_click(fam):
    c, app, tid, P = fam
    r = result(app, tid)
    for a, b, amount in r.settlements():
        kind = "remboursement" if a == E.ASSO else "versement"
        who = (b if a == E.ASSO else a).referent
        c.post(f"/v/{tid}/argent", data={"kind": kind, "participant": who,
                                         "amount": f"{amount // 100},{amount % 100:02d}"})
    r = result(app, tid)
    assert r.to_collect == 0 and r.to_refund == 0
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        paul = [m.amount for m in t.movements if m.participant_id == P["Paul Lebrun"]]
    assert sorted(paul) == [16000, 50000]
    assert "Tout est réglé" in c.get(f"/v/{tid}/argent").text


def test_family_pdf(fam):
    pdfium = pytest.importorskip("pypdfium2")
    c, app, tid, P = fam
    data = c.get(f"/v/{tid}/export.pdf").content
    doc = pdfium.PdfDocument(data)
    text = clean("\n".join(doc[i].get_textpage().get_text_range() for i in range(len(doc))))
    assert "Famille Lebrun (à Paul Lebrun) 160,00 €" in text
    assert "Inès 275,00 €" in text and "À recevoir —" in text
    assert "Léa Lebrun (acc.)" in text and "acc. = accompagnant" in text
    flat = re.sub(r"\s+", " ", text)
    assert "règle ses comptes avec la Famille Lebrun : doit 160,00 € pour toute la famille" in flat


# ---------------------------------------------------------------- saisie à l'écran

def test_add_family_and_companions_from_form(fam):
    c, app, tid, P = fam
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        gite = next(e.id for e in t.expenses if e.label.startswith("Gîte"))
        plong = next(e.id for e in t.expenses if e.label.startswith("Plongées"))
    page = c.get(f"/v/{tid}/participants").text
    assert "join_" not in page                       # le formulaire n'inclut plus dans les dépenses déjà saisies
    # (l'ancien envoi « names + join_… » reste accepté, ex. scripts)
    r = c.post(f"/v/{tid}/participants", data={"names": "Zoé Martin\nMax Martin", "role": "accompagnant",
                                              "family": "Martin", "join_form": "1", f"join_{gite}": "1"},
               follow_redirects=False)
    assert "dans la famille Martin" in clean(c.get(r.headers["location"]).text)
    r = result(app, tid)
    acc = {a.name: a for a in r.accounts}
    assert len(acc["Famille Martin"].members) == 2 and all(m.companion for m in acc["Famille Martin"].members)
    assert acc["Famille Martin"].share == 2 * 10000          # gîte 1000 / 10
    # un plongeur ajouté ensuite rejoint aussi les plongées (dépense « des plongeurs »)
    c.post(f"/v/{tid}/participants", data={"names": "Rémi", "role": "plongeur", "join_form": "1",
                                           f"join_{gite}": "1", f"join_{plong}": "1"})
    r = result(app, tid)
    remi = next(p for p in r.people.values() if p.name == "Rémi")
    assert r.share(remi.id, plong) == 10000 and r.share(remi.id, gite) == round(100000 / 11 + 0.5)


def test_edit_family_membership_and_referent(fam):
    c, app, tid, P = fam
    with app.state.db.Session() as s:
        roux = next(f.id for f in s.get(M.Trip, tid).families if f.name == "Roux")
    # Nadia devient la personne qui paie, puis Bruno quitte la famille
    c.post(f"/v/{tid}/familles/{roux}", data={"name": "Famille Roux-Ait", "referent": P["Nadia Roux"]})
    c.post(f"/v/{tid}/participants/{P['Bruno Roux']}", data={"name": "Bruno Roux", "family": "", "role": "plongeur"})
    r = result(app, tid)
    acc = {a.name: a for a in r.accounts}
    assert "Bruno Roux" in acc and acc["Famille Roux-Ait"].referent == P["Nadia Roux"]
    # Bruno avait versé 450 pour la famille : il est en avance, Nadia doit sa part
    assert acc["Bruno Roux"].balance == 27500 - 45000 and acc["Famille Roux-Ait"].balance == 17500
    # Nadia quitte aussi : la famille vide disparaît ; Nadia peut devenir accompagnante d'une autre famille
    c.post(f"/v/{tid}/participants/{P['Nadia Roux']}", data={"name": "Nadia Roux", "family": "famille lebrun",
                                                            "role": "accompagnant"})
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        assert sorted(f.name for f in t.families) == ["Lebrun"]
        assert len(t.families[0].members) == 5
    # renommer une famille avec un nom déjà pris est refusé
    with app.state.db.Session() as s:
        tho = s.get(M.Trip, tid).families[0].id
    c.post(f"/v/{tid}/participants/{P['Karim']}", data={"name": "Karim", "family": "Benali"})
    r = c.post(f"/v/{tid}/familles/{tho}", data={"name": "Benali"}, follow_redirects=False)
    assert "d%C3%A9j%C3%A0%20une%20famille" in r.headers["location"]


def test_delete_referent_reassigns(fam):
    c, app, tid, P = fam
    c.post(f"/v/{tid}/participants", data={"names": "Ana Lopez\nIvan Lopez", "family": "Lopez", "join_form": "1"})
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        lopez = next(f for f in t.families if f.name == "Lopez")
        first, other = lopez.referent_id, [m.id for m in lopez.members if m.id != lopez.referent_id][0]
    c.post(f"/v/{tid}/participants/{first}/supprimer")
    with app.state.db.Session() as s:
        lopez = next(f for f in s.get(M.Trip, tid).families if f.name == "Lopez")
        assert lopez.referent_id == other


def test_undo_family_change(fam):
    c, app, tid, P = fam
    c.post(f"/v/{tid}/participants/{P['Karim']}", data={"name": "Karim", "family": "Lebrun"})
    assert len(result(app, tid).account_of(P["Karim"]).members) == 5
    c.post("/annuler")
    assert not result(app, tid).account_of(P["Karim"]).is_family


# ---------------------------------------------------------------- IA / ops

def test_ops_groups_and_family_payments(tmp_path):
    db = Database(tmp_path / "d")
    tid = seed_family_demo(db)
    ops = Ops(db)
    r = ops.add_expense(tid, "Bateau", montant_reel="50", pour="plongeurs", categorie="Plongée", simulation=True)
    assert {x["personne"] for x in r["changements"]} == {"Inès", "Karim", "Famille Roux", "Famille Lebrun"}
    ops.add_expense(tid, "Glaces", montant_reel="30", pour=["famille Roux", "Karim"], paye_par="Karim")
    d = ops.trip(tid)
    gl = next(e for e in d["depenses"] if e["libelle"] == "Glaces")
    assert set(gl["pour"]) == {"Bruno Roux", "Nadia Roux", "Karim"}
    ops.add_movement(tid, "Famille Lebrun", "160")
    d = ops.trip(tid)
    fam = {f["nom"]: f for f in d["familles"]}
    assert fam["Famille Lebrun"]["reste"] == 0
    paul = next(p for p in d["participants"] if p["nom"] == "Paul Lebrun")
    assert paul["a_verse"] == 660.0 and paul["reste"] is None and "famille" in paul["situation"]
    with pytest.raises(OpsError, match="Famille inconnue"):
        ops.add_expense(tid, "x", montant_reel="1", pour=["famille Dupont"])
    # un accompagnant ajouté par l'IA n'est pas compté dans les plongées
    ops.add_participants(tid, ["Jade"], famille="Lebrun", accompagnant=True)
    d = ops.trip(tid)
    plong = next(e for e in d["depenses"] if e["libelle"].startswith("Plongées"))
    gite = next(e for e in d["depenses"] if e["libelle"].startswith("Gîte"))
    assert "Jade" not in plong["pour"] and "Jade" in gite["pour"]
    assert next(p for p in d["participants"] if p["nom"] == "Jade")["famille"] == "Lebrun"


def test_import_family_errors(tmp_path):
    ops = Ops(Database(tmp_path / "d"))
    data = {"voyage": {"nom": "x"}, "participants": [{"nom": "A", "famille": "F"}, "B"],
            "familles": [{"nom": "F", "paie": "B"}, {"nom": "G"}],
            "depenses": [{"libelle": "Gîte", "reel": "10", "pour": "accompagnants"}]}
    with pytest.raises(OpsError) as e:
        ops.import_trip(data, simulation=True)
    errs = e.value.errors
    assert "B ne fait pas partie de la famille F" in errs[0]
    assert errs[1].startswith("Famille n°2") and "inconnue" in errs[1]
    assert "Pour qui" in errs[2]                       # aucun accompagnant : dépense répartie sur personne


def test_ai_template_and_guide_mention_families():
    from compta import importer
    t, g = importer.template(), importer.guide()
    assert "accompagnant" in t and "FAMILLES" in t and "Famille Lebrun" in t
    assert '"accompagnant": true' in g and '"familles"' in g and '"pour": "plongeurs"' in g


# ---------------------------------------------------------------- base existante (avant familles)

def test_old_database_is_migrated(tmp_path):
    d = tmp_path / "old"
    d.mkdir()
    con = sqlite3.connect(d / "compta.db")
    con.executescript("""
        CREATE TABLE trips (id INTEGER PRIMARY KEY, name VARCHAR(200), place VARCHAR(200), organizer VARCHAR(200),
                            start DATE, "end" DATE, created DATETIME);
        CREATE TABLE participants (id INTEGER PRIMARY KEY, trip_id INTEGER REFERENCES trips(id), name VARCHAR(200),
                                   phone VARCHAR(50), email VARCHAR(200), note TEXT);
        INSERT INTO trips (id, name, place, organizer) VALUES (1, 'Ancien', '', '');
        INSERT INTO participants (id, trip_id, name, phone, email, note) VALUES (1, 1, 'Luc', '', '', '');
    """)
    con.commit()
    con.close()
    db = Database(d)
    ops = Ops(db)
    assert ops.trip(1)["participants"][0]["accompagnant"] is False
    ops.add_participants(1, ["Léa"], famille="Martin", accompagnant=True)
    assert ops.trip(1)["familles"][0]["membres"] == ["Léa"]
    # « Annuler » ramène l'ancienne base (sans colonnes) : elle est remise à niveau
    ops.undo()
    assert [p["nom"] for p in ops.trip(1)["participants"]] == ["Luc"]
    ops.add_participants(1, ["Max"], accompagnant=True)
    assert ops.trip(1)["participants"][1]["accompagnant"] is True


# ---------------------------------------------------------------- navigateur

@pytest.fixture(scope="module")
def fam_server(tmp_path_factory):
    import uvicorn
    import socket
    app = create_app(tmp_path_factory.mktemp("fam"))
    tid = seed_family_demo(app.state.db)
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    while not srv.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", tid
    srv.should_exit = True
    th.join(5)


def test_browser_family_screens(fam_server):
    from playwright.sync_api import sync_playwright
    base, tid = fam_server
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        # Ajout d'une personne avec sa famille
        pg.goto(f"{base}/v/{tid}/participants")
        pg.click("summary:has-text('Ajouter un participant')")
        pg.fill("#a-name", "Pia Lebrun")
        pg.click("#add-form label:has-text('Lebrun')")                 # famille existante : une pastille
        pg.check('#add-form input[name="r_accompagnant"]')
        pg.click("#add-form button:has-text('Ajouter')")
        assert "dans la famille Lebrun" in pg.locator(".flash").inner_text()
        assert pg.locator("tr.family-row", has_text="Famille Lebrun").count() == 1
        # Nouvelle dépense : pastilles de rôle / famille dans « Pour qui ? »
        pg.goto(f"{base}/v/{tid}/depenses/nouvelle")
        comp = pg.locator('#who input[data-companion="1"]')
        assert comp.count() == 4 and pg.locator("text=Plongeurs seulement").count() == 0
        on = pg.locator(".only-btn.on")                                      # au départ : « Tout le monde » allumé
        assert on.count() == 1 and on.inner_text() == "Tout le monde"
        pg.click(".only-btn:has-text('Plongeur')")                           # « seulement pour les plongeurs »
        assert pg.locator(".only-btn.on").all_inner_texts() == ["Plongeur"]  # « Tout le monde » s'éteint
        assert not any(comp.nth(i).is_checked() for i in range(comp.count()))
        assert pg.locator('#who input[data-companion="0"]').first.is_checked()
        pg.click(".only-btn:has-text('Accompagnant')")                       # + les accompagnants (multi-sélection)
        assert all(comp.nth(i).is_checked() for i in range(comp.count()))
        assert pg.locator('#who input[data-companion="0"]').first.is_checked()
        pg.click(".only-btn:has-text('Plongeur')")                           # on retire les plongeurs
        assert not pg.locator('#who input[data-companion="0"]').first.is_checked()
        assert "Seulement pour : Accompagnant" in pg.inner_text("[data-only-hint]")
        comp.first.uncheck()
        assert "ajustée à la main" in pg.inner_text("[data-only-hint]")
        pg.click(".only-btn:has-text('Personne')")
        assert pg.locator(".only-btn.on").all_inner_texts() == ["Personne"]
        pg.locator('#who input[data-who]').first.check()                     # une case à la main : « Personne » s'éteint
        assert pg.locator(".only-btn.on").count() == 0
        # Mobile : pas de débordement sur les écrans touchés par les familles
        m = b.new_page(viewport={"width": 390, "height": 800})
        for url in ["", "/participants", "/argent", f"/p/1"]:
            m.goto(f"{base}/v/{tid}{url}")
            assert m.evaluate("document.documentElement.scrollWidth") <= 391, url
        assert errors == []
        b.close()
