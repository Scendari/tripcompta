"""Plusieurs payeurs, recettes (argent reçu), montant à venir, « tous sauf » / groupes, note du voyage.
Cas tirés d'un vrai voyage (plongée, 16 participants) : plongée payée par un organisateur + arrhes de l'asso +
chèques vacances ; remboursement d'un hébergeur encaissé par l'organisateur ; péages et essence inconnus."""
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from compta import engine as E
from compta import models as M
from compta.db import Database
from compta.ops import Ops, OpsError
from compta.web import create_app

P = [E.Participant(1, "Anne"), E.Participant(2, "Bob"), E.Participant(3, "Chloé")]
ALL = {1: 1, 2: 1, 3: 1}


def clean(t):
    return re.sub(r"[  ]", " ", t)


# ---------------------------------------------------------------- moteur

def test_engine_several_payers():
    # 300 € pour 3 : Anne a avancé 200, l'asso 60, le reste (40) est compté pour l'asso
    e = E.Expense(1, "Plongées", actual=30000, weights=ALL, payers=[(1, 20000), (None, 6000)])
    r = E.compute(P, [e])
    assert [r.people[i].share for i in (1, 2, 3)] == [10000] * 3
    assert r.people[1].advanced == 20000 and r.people[1].balance == -10000
    assert e.splits() == [(1, 20000), (None, 10000)]           # part de l'asso regroupée
    assert r.is_balanced() and not r.warnings


def test_engine_payers_exceeding_amount_warn():
    e = E.Expense(1, "X", actual=10000, weights=ALL, payers=[(1, 8000), (2, 5000)])
    r = E.compute(P, [e])
    assert any("plus que son montant" in w for w in r.warnings)


def test_engine_income_received_by_participant():
    # Gîte 300 € payé par l'asso ; Anne a encaissé un remboursement de 30 € de l'hébergeur (à reverser)
    exps = [E.Expense(1, "Gîte", actual=30000, weights=ALL),
            E.Expense(2, "Remboursement gîte", actual=3000, weights=ALL, payer=1, kind="recette")]
    r = E.compute(P, exps, movements=[E.Movement(1, "versement", 12000)])
    assert r.total_cost == 27000
    assert [r.people[i].share for i in (1, 2, 3)] == [9000] * 3
    # Anne : 90 de part + 30 encaissés pour l'asso − 120 versés = à jour
    assert r.people[1].balance == 0 and r.people[2].balance == 9000
    assert r.is_balanced() and not r.warnings          # pas d'alerte « part négative » sur une recette


def test_engine_income_rounding_never_costs_the_asso():
    exps = [E.Expense(1, "Gîte", actual=10000, weights=ALL),
            E.Expense(2, "Avoir", actual=1000, weights=ALL, kind="recette")]    # 10 / 3 = 3,333…
    r = E.compute(P, exps)
    assert r.total_cost == 9000 and r.rounding_gain >= 0 and r.is_balanced()
    assert r.share(1, 2) == -333                                            # crédit arrondi en faveur de l'asso


def test_engine_pending_amount_counts_zero():
    exps = [E.Expense(1, "Péage", weights=ALL, payer=2), E.Expense(2, "Gîte", actual=900, weights=ALL)]
    r = E.compute(P, exps)
    assert r.total_cost == 900 and r.share(1, 1) == 0 and r.people[2].advanced == 0 and r.is_balanced()


# ---------------------------------------------------------------- écrans

@pytest.fixture()
def trip(tmp_path):
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.create_trip("Test")["voyage_id"]
    ops.add_participants(tid, ["Anne", "Bob", "Chloé"])
    with app.state.db.Session() as s:
        P_ = {p.name: p.id for p in s.get(M.Trip, tid).participants}
    return app, TestClient(app), tid, P_, ops


def form(P_, **kw):
    d = {"label": "Plongées", "category": "Plongée", "actual": "300", "payer": "asso", "payer_kind": "asso",
         **{f"inc_{i}": "1" for i in P_.values()}, **{f"w_{i}": "1" for i in P_.values()}}
    d.update(kw)
    return d


def test_web_several_payers(trip):
    app, c, tid, P_, ops = trip
    r = c.post(f"/v/{tid}/depenses", data=form(P_, payer_kind="multi", mp_0=str(P_["Anne"]), ma_0="200",
                                               mp_1="asso", ma_1="60"), follow_redirects=True)
    assert "enregistrée" in r.text
    d = ops.trip(tid)
    assert d["depenses"][0]["paye_par"] == {"Anne": 200.0, "asso": 100.0}
    anne = next(p for p in d["participants"] if p["nom"] == "Anne")
    assert anne["a_avance"] == 200.0 and anne["reste"] == -100.0
    page = clean(c.get(f"/v/{tid}/depenses").text)
    assert "Anne 200,00 € + l'asso 100,00 €" in page.replace("&#39;", "'")
    money = clean(c.get(f"/v/{tid}/argent/mouvements").text)
    assert "Paiement : Plongées (part payée par l&#39;asso)" in money and "100,00 €" in money
    assert "(sa part du paiement)" in clean(c.get(f"/v/{tid}/participants/avances").text)
    fiche = clean(c.get(f"/v/{tid}/p/{P_['Anne']}").text)
    assert "Plongées" in fiche and "sa part du paiement" in fiche and "200,00 €" in fiche
    # le formulaire de modification ré-affiche les deux payeurs
    eid = ops.trip(tid)["depenses"][0]["id"]
    edit = c.get(f"/v/{tid}/depenses/{eid}/modifier").text
    assert 'value="multi" checked' in edit and 'name="ma_0" value="200"' in edit
    # trop payé : refusé avec un message clair
    r = c.post(f"/v/{tid}/depenses", data=form(P_, payer_kind="multi", mp_0=str(P_["Anne"]), ma_0="250",
                                               mp_1=str(P_["Bob"]), ma_1="100"), follow_redirects=True)
    assert "plus que le montant" in clean(r.text) and len(ops.trip(tid)["depenses"]) == 1


def test_web_income_and_pending(trip):
    app, c, tid, P_, ops = trip
    c.post(f"/v/{tid}/depenses", data=form(P_, label="Gîte", category="Logement", actual="300"))
    r = c.post(f"/v/{tid}/depenses", data=form(P_, label="Remboursement hébergeur", category="Logement",
                                               actual="30", kind="recette", payer=str(P_["Anne"]),
                                               payer_kind="p"), follow_redirects=True)
    assert "enregistrée" in r.text
    d = ops.trip(tid)
    assert d["cout_total"] == 270.0 and d["equilibre"]
    anne = next(p for p in d["participants"] if p["nom"] == "Anne")
    assert anne["sa_part"] == 90.0 and anne["reste"] == 120.0          # 90 + 30 encaissés pour l'asso
    fiche = clean(c.get(f"/v/{tid}/p/{P_['Anne']}").text)
    assert "reçu pour l'asso" in fiche and "Remboursement hébergeur" in fiche
    page = clean(c.get(f"/v/{tid}/depenses").text)
    assert ">recette</span>" in page and "− 30,00 €" in page and "reçu par Anne" in page
    import html as _h
    money = clean(c.get(f"/v/{tid}/argent").text)
    assert "Reçu par des participants" not in money                    # la caisse seulement
    adv = _h.unescape(clean(c.get(f"/v/{tid}/participants/avances").text))
    assert "Reçu par des participants pour l'asso" in adv and "Anne" in adv
    assert "Payé par des participants (avances)" not in adv            # aucune avance ici : pas de bloc vide
    # aperçu en direct d'une recette
    prev = clean(c.post(f"/v/{tid}/depenses/apercu", data=form(P_, actual="30", kind="recette")).text)
    assert "de moins pour chacun" in prev and "10,00 €" in prev
    # montant à venir : refusé sans la case, accepté avec
    r = c.post(f"/v/{tid}/depenses", data=form(P_, label="Péage", actual="", payer_kind="p", payer=str(P_["Bob"])),
               follow_redirects=True)
    assert "Montant pas encore connu" in r.text and len(ops.trip(tid)["depenses"]) == 2
    c.post(f"/v/{tid}/depenses", data=form(P_, label="Péage", actual="", pending="1", payer_kind="p",
                                           payer=str(P_["Bob"])))
    d = ops.trip(tid)
    peage = next(e for e in d["depenses"] if e["libelle"] == "Péage")
    assert peage["a_venir"] and d["cout_total"] == 270.0
    dash = clean(c.get(f"/v/{tid}").text)
    assert "1 montant pas encore connu (Péage)" in dash
    assert "montant à venir" in c.get(f"/v/{tid}/depenses").text


def test_trip_note(trip):
    app, c, tid, P_, ops = trip
    c.post(f"/v/{tid}/modifier", data={"name": "Test", "note": "Club Club Bleu\nDépart de Poitiers"})
    dash = c.get(f"/v/{tid}").text
    assert 'id="trip-note"' in dash and "Départ de Poitiers" in dash
    assert ops.trip(tid)["note"] == "Club Club Bleu\nDépart de Poitiers"


def test_pdf_shows_payers_and_income(trip):
    pdfium = pytest.importorskip("pypdfium2")
    app, c, tid, P_, ops = trip
    ops.add_expense(tid, "Plongées", montant_reel="300", paye_par={"Anne": "200", "asso": "60"})
    ops.add_expense(tid, "Avoir club", montant_reel="30", paye_par="Bob", type="recette")
    ops.add_expense(tid, "Péage", a_venir=True, paye_par="Chloé")
    doc = pdfium.PdfDocument(c.get(f"/v/{tid}/export.pdf").content)
    text = re.sub(r"\s+", " ", clean("\n".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))))
    assert "Avoir club (recette)" in text and "reçu par Bob" in text
    assert "Péage (montant à venir)" in text
    assert "Argent reçu par des participants pour le compte de l'asso" in text
    assert "Paiement : Plongées (part de l'asso)" in text


# ---------------------------------------------------------------- IA

def test_import_groups_except_payers_income_pending(tmp_path):
    ops = Ops(Database(tmp_path / "d"))
    data = {
        "voyage": {"nom": "Les Issambres", "note": "Club Club Bleu, départ de Poitiers"},
        "groupes": {"covoit": ["Stéphane", "Rémi", "Inès", "Thierry G"], "minibus": ["covoit"]},
        "participants": ["Stéphane", "Rémi", "Inès", "Thierry Garnier", "Gabriel",
                         {"nom": "Juliette", "accompagnant": True, "note": "enfant"}],
        "depenses": [
            {"libelle": "Logement", "reel": "1924,64", "paye_par": "asso", "pour": "tous"},
            {"libelle": "Remboursement Hébergeur", "type": "recette", "reel": "90,29", "paye_par": "Stéphane",
             "pour": "tous"},
            {"libelle": "Taxe de séjour", "reel": "75", "pour": "tous", "sauf": ["Juliette"],
             "paye_par": [{"qui": "Gabriel", "montant": "15"}, {"qui": "Stéphane", "montant": "60"}]},
            {"libelle": "Plongées", "reel": "1600", "paye_par": {"Stéphane": "1000", "asso": "300", "Rémi": "300"},
             "parts": {"Stéphane": 400, "Rémi": 400, "Inès": 400, "Gabriel": 400}},
            {"libelle": "Location minibus", "reel": "603", "paye_par": "Stéphane", "pour": "minibus",
             "sauf": "Thierry G"},
            {"libelle": "Essence voiture", "a_venir": True, "paye_par": "Thierry G", "pour": "covoit"},
        ],
        "versements": [{"participant": "Stéphane", "montant": "367,29"}],
    }
    r = ops.import_trip(data)
    a = r["apercu"]
    assert a["equilibre"] and not r["avertissements"] and a["note"].startswith("Club Club Bleu")
    deps = {e["libelle"]: e for e in a["depenses"]}
    assert deps["Taxe de séjour"]["paye_par"] == {"Gabriel": 15.0, "Stéphane": 60.0}
    assert "Juliette" not in deps["Taxe de séjour"]["pour"] and len(deps["Taxe de séjour"]["pour"]) == 5
    assert set(deps["Location minibus"]["pour"]) == {"Stéphane", "Rémi", "Inès"}       # groupe dans groupe, sauf
    assert deps["Essence voiture"]["a_venir"] and set(deps["Essence voiture"]["pour"]) == \
        {"Stéphane", "Rémi", "Inès", "Thierry Garnier"}
    assert deps["Remboursement Hébergeur"]["type"] == "recette"
    assert a["cout_total"] == round(1924.64 - 90.29 + 75 + 1600 + 603, 2)
    actions = " | ".join(r["actions"]).replace(" ", " ").replace("\xa0", " ")
    assert "Nouvelle recette « Remboursement Hébergeur » : 90,29 €, reçue par Stéphane" in actions
    assert "Nouvelle dépense « Essence voiture » : montant à venir, payée par Thierry Garnier" in actions
    assert "payée par Stéphane 1 000,00 € + l'asso 300,00 € + Rémi 300,00 €" in actions
    p = {x["nom"]: x for x in a["participants"]}
    # Stéphane : il a encaissé 90,29 pour l'asso, et versé 367,29 : seuls 277 € comptent comme « sa poche »
    assert p["Stéphane"]["a_avance"] == round(60 + 1000 + 603 - 90.29, 2)


def test_import_errors_for_new_fields(tmp_path):
    ops = Ops(Database(tmp_path / "d"))
    base = {"voyage": {"nom": "x"}, "participants": ["A", "B"]}
    cases = [
        ({"depenses": [{"libelle": "X", "reel": "10", "paye_par": {"A": "8", "B": "5"}}]}, "plus que son montant"),
        ({"depenses": [{"libelle": "X", "reel": "10", "type": "don"}]}, "« depense » ou « recette »"),
        ({"depenses": [{"libelle": "X"}]}, "a_venir"),
        ({"depenses": [{"libelle": "X", "reel": "-10"}]}, "« type »: « recette »"),
        ({"depenses": [{"libelle": "X", "reel": "10", "paye_par": {"Zoé": "5"}}]}, "Personne inconnue"),
        ({"groupes": {"g": 3}}, "« groupes » doit être"),
        ({"depenses": [{"libelle": "X", "reel": "10", "pour": "tous", "sauf": ["A", "B"]}]}, "Pour qui"),
    ]
    for extra, msg in cases:
        with pytest.raises(OpsError) as e:
            ops.import_trip({**base, **extra}, simulation=True)
        assert any(msg in x for x in getattr(e.value, "errors", [str(e.value)])), (extra, e.value)


def test_update_payers_and_income(tmp_path):
    ops = Ops(Database(tmp_path / "d"))
    tid = ops.import_trip({"voyage": {"nom": "x"}, "participants": ["A", "B"],
                           "depenses": [{"libelle": "Gîte", "reel": "100"}]})["voyage_id"]
    r = ops.import_trip({"voyage_id": tid, "depenses_a_modifier": [{"depense": "Gîte", "paye_par": {"A": "60"}}]},
                        str(tid), verifier_id=True)
    acts = re.sub(r"[  ]", " ", r["actions"][0])
    assert acts == "« Gîte » : payé par l’asso → A 60,00 € + l’asso 40,00 €"
    assert ops.trip(tid)["depenses"][0]["paye_par"] == {"A": 60.0, "asso": 40.0}
    ops.import_trip({"voyage_id": tid, "depenses_a_modifier": [{"depense": "Gîte", "paye_par": "asso"}]},
                    str(tid), verifier_id=True)
    assert ops.trip(tid)["depenses"][0]["paye_par"] == "asso"


def test_old_database_gets_new_columns(tmp_path):
    d = tmp_path / "old"
    d.mkdir()
    con = sqlite3.connect(d / "compta.db")
    con.executescript("""
        CREATE TABLE trips (id INTEGER PRIMARY KEY, name VARCHAR(200), place VARCHAR(200), organizer VARCHAR(200),
                            start DATE, "end" DATE, created DATETIME);
        CREATE TABLE expenses (id INTEGER PRIMARY KEY, trip_id INTEGER, category_id INTEGER, label VARCHAR(200),
                               date DATE, planned INTEGER, actual INTEGER, payer_id INTEGER, note TEXT);
        INSERT INTO trips (id, name, place, organizer) VALUES (1, 'Ancien', '', '');
        INSERT INTO expenses (id, trip_id, label, actual, note) VALUES (1, 1, 'Gîte', 1000, '');
    """)
    con.commit()
    con.close()
    d_ = Ops(Database(d)).trip(1)
    assert d_["note"] == "" and d_["depenses"][0]["type"] == "depense" and d_["depenses"][0]["paye_par"] == "asso"


def test_browser_expense_form(trip):
    import socket
    import threading
    import time
    import uvicorn
    from playwright.sync_api import sync_playwright
    app, c, tid, P_, ops = trip
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    while not srv.started:
        time.sleep(0.05)
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(viewport={"width": 1280, "height": 900})
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(f"http://127.0.0.1:{port}/v/{tid}/depenses/nouvelle")
            assert not pg.locator("#payers").is_visible()
            pg.check("input[name=payer_kind][value=multi]")
            assert pg.locator("#payers").is_visible()
            assert pg.locator("#payers .payer-row:visible").count() == 2
            pg.click("#add-payer")
            assert pg.locator("#payers .payer-row:visible").count() == 3
            pg.check("input[name=kind][value=recette]")
            assert "Qui l'a reçu ?" in pg.locator("legend").nth(1).inner_text()
            pg.check("input[name=kind][value=depense]")
            assert "Qui a payé ?" in pg.locator("legend").nth(1).inner_text()
            pg.fill("#label", "Plongées")
            pg.fill("#actual", "300")
            pg.select_option("select[name=mp_0]", label="Anne")
            pg.fill("input[name=ma_0]", "200")
            pg.fill("input[name=ma_1]", "100")
            pg.click("form button.btn:has-text('Enregistrer')")
            assert "enregistrée" in pg.locator(".flash").inner_text()
            assert ops.trip(tid)["depenses"][0]["paye_par"] == {"Anne": 200.0, "asso": 100.0}
            m = b.new_page(viewport={"width": 390, "height": 800})
            m.goto(f"http://127.0.0.1:{port}/v/{tid}/depenses/nouvelle")
            m.check("input[name=payer_kind][value=multi]")
            assert m.evaluate("document.documentElement.scrollWidth") <= 391
            assert errors == []
            b.close()
    finally:
        srv.should_exit = True
        th.join(5)


def test_cancelled_booking_refund_received_by_organiser(tmp_path):
    """Cas réel (anonymisé) : l'asso paie un appartement 120,29 € puis l'annule ; l'hébergeur rembourse 90,29 € sur
    le compte de l'organisateur, qui les reverse à l'asso dans son premier versement (277 + 90,29). Le logement
    occupé (1 924,64 €) n'est pas touché ; l'annulation coûte net 30 € ; l'organisateur ne doit ni ne perd rien."""
    ops = Ops(Database(tmp_path / "d"))
    people = [f"P{i:02d}" for i in range(15)]
    r = ops.import_trip({
        "voyage": {"nom": "x"}, "participants": people + [{"nom": "Enfant", "accompagnant": True}],
        "depenses": [
            {"libelle": "Logement", "reel": "1924,64", "paye_par": "asso", "pour": "tous"},
            {"libelle": "Appartement annulé", "reel": "120,29", "paye_par": "asso", "pour": "tous", "sauf": ["Enfant"]},
            {"libelle": "Remboursement appartement annulé", "type": "recette", "reel": "90,29", "paye_par": "P00",
             "pour": "tous", "sauf": ["Enfant"]},
        ],
        "versements": [{"participant": "P00", "montant": "367,29"}] + [{"participant": p, "montant": "277"}
                                                                       for p in people[1:]],
        "ajustements": [{"participant": "Enfant", "type": "nouveau_montant", "montant": "60", "sur": "Logement",
                         "compense_par": "autres", "verrouiller": True}],
    })
    a = r["apercu"]
    assert a["equilibre"] and a["cout_total"] == round(1924.64 + 30, 2)
    p = {x["nom"]: x for x in a["participants"]}
    # logement (1924,64 − 60) / 15 = 124,3093… ; annulation net 30/15 = 2 -> 126,3093… arrondi une fois -> 126,31
    assert p["P01"]["sa_part"] == 126.31 and p["Enfant"]["sa_part"] == 60.0
    # même part pour l'organisateur ; ses 90,29 € reçus puis reversés s'annulent : il est au même point que les autres
    assert p["P00"]["sa_part"] == 126.31 and p["P00"]["reste"] == p["P01"]["reste"] == round(126.31 - 277, 2)
    # caisse de l'asso = vrais mouvements : entrées 367,29 + 14 × 277 ; sorties 1924,64 + 120,29
    with Database(tmp_path / "d").Session() as s:
        from compta import services as S
        cash_in, cash_out = S.asso_cash(s.get(M.Trip, r["voyage_id"]))
    assert (cash_in, cash_out) == (36729 + 14 * 27700, 192464 + 12029)


def test_edit_a_payment(trip):
    app, c, tid, P_, ops = trip
    ops.add_movement(tid, "Anne", "277", date="2026-10-01", note="acompte")
    mid = ops.trip(tid)["mouvements"][0]["id"]
    page = c.get(f"/v/{tid}/argent/mouvements").text
    assert "a versé de l&#39;argent à l&#39;asso" in page or "a versé de l'argent à l'asso" in page
    assert "m'a versé" not in page and "m&#39;a versé" not in page
    assert f'action="/v/{tid}/argent/{mid}"' in page and 'value="277"' in page
    r = c.post(f"/v/{tid}/argent/{mid}", data={"kind": "versement", "participant": P_["Bob"], "amount": "300",
                                              "date": "2026-10-02", "note": "corrigé"}, follow_redirects=True)
    assert "modifié" in r.text
    m = ops.trip(tid)["mouvements"][0]
    assert (m["participant"], m["montant"], m["date"], m["note"]) == ("Bob", 300.0, "2026-10-02", "corrigé")
    r = c.post(f"/v/{tid}/argent/{mid}", data={"kind": "versement", "participant": P_["Bob"], "amount": "-3"},
               follow_redirects=True)
    assert "doit être positif" in r.text
    assert ops.trip(tid)["mouvements"][0]["montant"] == 300.0
    c.post("/annuler")
    assert ops.trip(tid)["mouvements"][0]["montant"] == 277.0
    assert "m'a versé" not in c.get(f"/v/{tid}").text


def test_browser_money_rows_actions_on_one_line(trip):
    import socket
    import threading
    import time
    import uvicorn
    from playwright.sync_api import sync_playwright
    app, c, tid, P_, ops = trip
    ops.add_expense(tid, "Gîte", montant_reel="300")
    for n in ("Anne", "Bob", "Chloé"):
        ops.add_movement(tid, n, "277", note="acompte versé un peu en avance pour la réservation du gîte")
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    while not srv.started:
        time.sleep(0.05)
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(viewport={"width": 1280, "height": 900})
            pg.on("dialog", lambda d: d.accept())
            pg.goto(f"http://127.0.0.1:{port}/v/{tid}/argent/mouvements")
            cell = pg.locator("table:not(.settle-table) td.row-actions").nth(1)
            mod, sup = cell.locator("button", has_text="Modifier"), cell.locator("button", has_text="Supprimer")
            assert abs(mod.bounding_box()["y"] - sup.bounding_box()["y"]) < 3          # même ligne
            assert pg.locator("tr.edit-row:visible").count() == 0
            mod.click()
            row = pg.locator("tr.edit-row:visible")
            assert row.count() == 1 and mod.get_attribute("aria-expanded") == "true"
            row.locator("input[name=amount]").fill("250")
            row.locator("button", has_text="Enregistrer").click()
            assert "modifié" in pg.locator(".flash").inner_text()
            assert sorted(m["montant"] for m in ops.trip(tid)["mouvements"]) == [250.0, 277.0, 277.0]
            # juste après : « Défaire » dans le message
            pg.click(".flash button:has-text('↶ Défaire')")
            assert "Défait" in pg.locator(".flash").inner_text()
            assert sorted(m["montant"] for m in ops.trip(tid)["mouvements"]) == [277.0] * 3
            b.close()
    finally:
        srv.should_exit = True
        th.join(5)


def test_settle_partial_payment_with_date(trip):
    """« Reste à régler » : « ✓ Reçu » ouvre un petit formulaire ; on peut enregistrer une partie, avec une date."""
    app, c, tid, P_, ops = trip
    c.post(f"/v/{tid}/depenses", data=form(P_, label="Gîte", actual="300"))
    page = c.get(f"/v/{tid}/argent").text
    assert 'class="subtabs"' in page and "Reste à régler" in page and 'id="ajout"' not in page
    assert f'id="pay-versement-{P_["Anne"]}" hidden' in page
    r = c.post(f"/v/{tid}/argent", data={"kind": "versement", "participant": P_["Anne"], "amount": "40",
                                         "date": "2026-10-20", "note": "chèque", "next": f"/v/{tid}/argent"},
               follow_redirects=False)
    assert r.headers["location"].startswith(f"/v/{tid}/argent?msg=")
    anne = next(p for p in ops.trip(tid)["participants"] if p["nom"] == "Anne")
    assert anne["reste"] == 60.0                                    # 100 - 40 : le reste reste à régler
    mv = ops.trip(tid)["mouvements"][-1]
    assert mv["date"] == "2026-10-20" and mv["note"] == "chèque"
    moves = c.get(f"/v/{tid}/argent/mouvements").text
    assert "Entrées et sorties de la caisse" in moves and 'id="ajout" hidden' in moves
