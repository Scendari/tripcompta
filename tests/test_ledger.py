"""Tableau des comptes (débit / crédit) : participants, dépenses, caisse. Montants vérifiés à la main sur la démo
« Week-end Banyuls » (plongeur 275 €, accompagnant 175 € ; famille Lebrun 900 € − 500 versés − 240 avancés)."""
import re

import pytest
from fastapi.testclient import TestClient

from compta import services as S
from compta.demo import seed_demo, seed_family_demo
from compta.ops import Ops
from compta.db import Database
from compta.web import create_app
from compta import models as M


def clean(t):
    return re.sub(r"[ \xa0]", " ", t)


@pytest.fixture()
def fam(tmp_path):
    app = create_app(tmp_path / "d")
    return app, TestClient(app), seed_family_demo(app.state.db)


def test_ledger_numbers_by_hand(fam):
    app, c, tid = fam
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        L = S.ledger(t, S.compute_trip(t))
    acc = {a["name"]: a for a in L["accounts"]}
    lebrun = acc["Famille Lebrun"]
    assert (lebrun["debit"], lebrun["credit"], lebrun["balance"]) == (90000, 74000, 16000)
    assert (lebrun["paid"], lebrun["advanced"]) == (50000, 24000)
    assert (acc["Karim"]["debit"], acc["Karim"]["credit"], acc["Karim"]["balance"]) == (27500, 30000, -2500)
    assert acc["Famille Roux"]["balance"] == 0 and acc["Inès"]["balance"] == 27500
    assert L["total_debit"] - L["total_credit"] == 16000 + 27500 - 2500
    assert all(e["ok"] for e in L["expenses"])
    courses = next(e for e in L["expenses"] if e["label"] == "Courses")
    assert courses["payers"] == [("Julie Lebrun", 24000)] and courses["parts"] == 24000 and courses["people"] == 8
    assert (L["cash_in"], L["cash_out"]) == (125000, 100000 + 60000 + 6000)


def test_ledger_page(fam):
    app, c, tid = fam
    page = clean(c.get(f"/v/{tid}/comptes").text)
    assert "Débit — ce qu'il doit" in page.replace("&#39;", "'") and "Crédit — ce qu'il a donné" in page.replace("&#39;", "'")
    assert "Famille Lebrun" in page and "doit 160,00 €" in page and "on lui doit 25,00 €" in page
    assert "✓ juste" not in page and "Il reste dans la caisse" not in page   # contrôle et caisse : retirés (doublons)
    raw = c.get(f"/v/{tid}/comptes").text                                    # ancienne adresse → onglet Comptes
    assert 'href="/v/%d/participants/soldes" class="on"' % tid in raw and "📒 Tableau des comptes" not in raw
    assert f'href="/v/{tid}/participants/comptes?p=' in raw                  # nom → détail de son compte


def test_ledger_income_and_pending(tmp_path):
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.create_trip("x")["voyage_id"]
    ops.add_participants(tid, ["A", "B", "C"])
    ops.add_expense(tid, "Gîte", montant_reel="300")
    ops.add_expense(tid, "Remboursement", montant_reel="30", type="recette", paye_par="A")
    ops.add_expense(tid, "Péage", a_venir=True, paye_par="B")
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        L = S.ledger(t, S.compute_trip(t))
    a = {x["name"]: x for x in L["accounts"]}["A"]
    assert (a["share"], a["received"], a["debit"], a["credit"]) == (9000, 3000, 12000, 0)
    rec = next(e for e in L["expenses"] if e["label"] == "Remboursement")
    assert rec["income"] and rec["amount"] == -3000 and rec["ok"]
    page = clean(TestClient(app).get(f"/v/{tid}/comptes").text)
    assert "A" in page and "Reçu pour" in page


def test_ledger_demo_is_all_balanced(tmp_path):
    app = create_app(tmp_path / "d")
    tid = seed_demo(app.state.db)
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        L = S.ledger(t, S.compute_trip(t))
    assert all(e["ok"] for e in L["expenses"]) and L["global_ok"]
    page = TestClient(app).get(f"/v/{tid}/depenses").text
    assert "⚠ à vérifier" not in page                                       # aucune dépense signalée


def test_tooltips_explain_each_amount(fam):
    app, c, tid = fam
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        L = S.ledger(t, S.compute_trip(t))
    lebrun = next(a for a in L["accounts"] if a["name"] == "Famille Lebrun")
    julie = next(m for m in lebrun["members"] if m["name"] == "Julie Lebrun")
    tips = {k: [clean(x) for x in v] for k, v in julie["tips"].items()}
    assert sorted(tips["share"][:-1]) == sorted(["Gîte 2 nuits : 125,00 €", "Plongées (4 par plongeur) : 120,00 €",
                                                 "Courses : 30,00 €"])
    assert tips["share"][-1] == "= 275,00 € au total"
    assert tips["advanced"] == ["Courses : 240,00 €"]
    fam_tips = {k: [clean(x) for x in v] for k, v in lebrun["tips"].items()}
    assert "Paul Lebrun : 500,00 €" in fam_tips["paid"]
    assert fam_tips["balance"][-2] == "= 160,00 € → doit encore 160,00 €"
    assert fam_tips["balance"][:3] == ["900,00 € de part", "− 500,00 € versés", "− 240,00 € avancés"]
    page = clean(c.get(f"/v/{tid}/comptes").text)
    assert 'class="tip" tabindex="0"' in page and "Versé le 02/05/2027 : 500,00 € (acompte)" in page


def test_browser_tooltip_shows_on_hover_focus_and_tap(fam):
    import socket
    import threading
    import time
    import uvicorn
    from playwright.sync_api import sync_playwright
    app, c, tid = fam
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
            pg.goto(f"http://127.0.0.1:{port}/v/{tid}/comptes")
            first = pg.locator(".ledger .tip").first
            first.hover()
            box = pg.locator("#tipbox")
            assert box.is_visible() and "Part de" in box.inner_text()
            bb = box.bounding_box()
            assert bb["x"] >= 0 and bb["x"] + bb["width"] <= 1280                  # jamais coupée
            pg.mouse.move(5, 5)
            assert not box.is_visible()
            first.focus()                                                          # clavier
            assert box.is_visible()
            pg.keyboard.press("Escape")
            assert not box.is_visible()
            m = b.new_page(viewport={"width": 390, "height": 800}, has_touch=True)
            m.goto(f"http://127.0.0.1:{port}/v/{tid}/comptes")
            m.locator(".ledger .tip").nth(2).tap()                                 # toucher sur téléphone
            mb = m.locator("#tipbox").bounding_box()
            assert m.locator("#tipbox").is_visible() and mb["x"] >= 0 and mb["x"] + mb["width"] <= 390
            assert errors == []
            b.close()
    finally:
        srv.should_exit = True
        th.join(5)


def test_person_account_by_category(fam):
    """Fiche d'une personne : débit / crédit par catégorie, totaux, solde = débit − crédit (vérifié à la main)."""
    app, c, tid = fam
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        julie = next(p for p in t.participants if p.name == "Julie Lebrun")
        acc = S.person_account(t, S.compute_trip(t), julie.id)
    g = {x["name"]: x for x in acc["groups"]}
    assert (g["Logement"]["debit"], g["Logement"]["credit"]) == (12500, 0)
    assert (g["Plongée"]["debit"], g["Repas"]["debit"], g["Repas"]["credit"]) == (12000, 3000, 24000)
    assert g["Repas"]["items"][0]["note"] == "payé par lui" and g["Logement"]["pct"] == 45      # 125 / 275
    assert (acc["debit"], acc["credit"], acc["balance"]) == (27500, 24000, 3500)
    page = clean(c.get(f"/v/{tid}/p/{julie.id}").text)
    assert "Son compte" in page and 'data-toggle-group aria-expanded="false"' in page
    assert "Tout déplier" in page


def test_browser_person_account_folds(fam):
    import socket
    import threading
    import time
    import uvicorn
    from playwright.sync_api import sync_playwright
    app, c, tid = fam
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
            pg.goto(f"http://127.0.0.1:{port}/v/{tid}/p/2")
            assert pg.locator("tr.item:visible").count() == 0                       # replié : vue par catégorie
            pg.locator("[data-toggle-group]:visible").first.click()
            assert pg.locator("tr.item:visible").count() >= 1
            pg.click("[data-toggle-all=open]")
            n = pg.locator("tr.item:visible").count()
            pg.locator("tr.item details summary:visible").first.click()
            assert pg.locator("tr.item .explain:visible").count() == 1               # le calcul d'une dépense
            pg.click("[data-toggle-all=close]")
            assert pg.locator("tr.item:visible").count() == 0 and n >= 3
            m = b.new_page(viewport={"width": 390, "height": 800})
            m.goto(f"http://127.0.0.1:{port}/v/{tid}/p/2")
            assert m.evaluate("document.documentElement.scrollWidth") <= 391
            b.close()
    finally:
        srv.should_exit = True
        th.join(5)


def test_rounding_row_everywhere(tmp_path):
    """La part totale est arrondie une seule fois : la ligne « Arrondi au centime » fait tomber juste
    la page de la personne, le tableau des comptes, « Qui paie quoi » et le PDF."""
    from compta.ops import Ops
    from compta.db import Database
    ops = Ops(Database(tmp_path / "d"))
    r = ops.import_trip({"voyage": {"nom": "x"}, "participants": ["Anne", "Bob", "Chloé"],
                         "depenses": [{"libelle": "Péage", "reel": "10", "paye_par": "asso", "pour": "tous"}]})
    tid = r["voyage_id"]
    app = create_app(tmp_path / "d")
    c = TestClient(app)
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        res = S.compute_trip(t)
        pid = t.participants[0].id
        acc = S.person_account(t, res, pid)
    assert acc["debit"] == res.people[pid].share == 334
    assert any(g["name"] == "Arrondi au centime" and g["debit"] == 1 for g in acc["groups"])
    assert "Arrondi au centime" in c.get(f"/v/{tid}/p/{pid}").text
    page = clean(c.get(f"/v/{tid}/comptes").text)
    assert "Arrondi au centime" in page and "à vérifier" not in page
    m = clean(c.get(f"/v/{tid}/repartition").text)
    assert "Arrondi" in m and "3,33 €" in m and "3,34 €" in m


def test_subtotals_match_a_calculator(tmp_path):
    """Cas réel (anonymisé) : 8 dépenses de transport pour 11 personnes, 1 522,29 €.
    Le sous-total « Transport » de chacun = 1 522,29 / 11 = 138,39 € (pas la somme de 8 lignes arrondies)."""
    ops = Ops(Database(tmp_path / "d"))
    people = [f"P{i:02d}" for i in range(11)]
    amounts = ["603", "41,30", "104,90", "35,40", "127,12", "149,22", "141,35", "320"]
    r = ops.import_trip({"voyage": {"nom": "x"}, "participants": people,
                         "depenses": [{"libelle": f"Trajet {i}", "categorie": "Transport", "reel": a,
                                       "paye_par": "asso", "pour": "tous"} for i, a in enumerate(amounts)]})
    tid = r["voyage_id"]
    app = create_app(tmp_path / "d")
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        res = S.compute_trip(t)
        pid = t.participants[0].id
        acc = S.person_account(t, res, pid)
        eids = [e.id for e in t.expenses]
    assert sum(l.cents for l in res.lines_for(pid)) == 13840          # 8 lignes arrondies au plus proche
    assert {g["name"]: g["debit"] for g in acc["groups"]} == {"Transport": 13839}
    assert res.people[pid].share == 13839 and res.subtotal(pid, eids) == 13839
    page = clean(TestClient(app).get(f"/v/{tid}/repartition?vue=totaux").text)
    assert "138,39 €" in page and "138,40 €" not in page


def test_rounding_line_explained_with_numbers(tmp_path):
    """10 € pour 3 : part exacte 3,3333… € arrondie à 3,34 € ; la ligne affichée fait 3,33 € : il manque 0,01 €."""
    ops = Ops(Database(tmp_path / "d"))
    tid = ops.import_trip({"voyage": {"nom": "x"}, "participants": ["Anne", "Bob", "Chloé"],
                           "depenses": [{"libelle": "Péage", "reel": "10", "paye_par": "asso", "pour": "tous"}]})["voyage_id"]
    app = create_app(tmp_path / "d")
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        r = S.compute_trip(t)
        pid = t.participants[0].id
        g = next(g for g in S.person_account(t, r, pid)["groups"] if g["kind"] == "arrondi")
    assert clean(g["explain"]) == clean("Sa part exacte est de 3,3333… €, arrondie à 3,34 €. Les montants ci-dessus, "
                            "arrondis chacun au centime, font 3,33 € : il manque 0,01 €.")
    page = clean(TestClient(app).get(f"/v/{tid}/p/{pid}").text)
    assert "Sa part exacte est de 3,3333… €, arrondie à 3,34 €" in page


def test_expenses_who_tooltip(tmp_path):
    """« Pour qui » : au survol, qui est exclu et la part de chacun."""
    ops = Ops(Database(tmp_path / "d"))
    tid = ops.import_trip({"voyage": {"nom": "x"}, "participants": ["Anne", "Bob", "Chloé", "Denis"], "depenses": [
        {"libelle": "Péage", "reel": "30", "paye_par": "asso", "pour": "tous", "sauf": ["Denis"]},
        {"libelle": "Bateau", "reel": "100", "paye_par": "asso", "parts": {"Anne": 3, "Bob": 1}}]})["voyage_id"]
    app = create_app(tmp_path / "d")
    page = clean(TestClient(app).get(f"/v/{tid}/depenses").text)
    assert re.search(r'class="tip"[^>]*>3 pers\.<span class="tip-box" role="tooltip"><b>Pour qui</b>'
                     r'<span>Sauf : Denis</span><span>Anne : 10,00 €</span>', page)
    assert "<span>Anne : 75,00 €</span><span>Bob : 25,00 €</span>" in page


def test_expense_badge_when_split_does_not_add_up(tmp_path, monkeypatch):
    """Une dépense dont la répartition ne tombe pas juste est signalée sur sa ligne, dans Dépenses."""
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.create_trip("x")["voyage_id"]
    ops.add_participants(tid, ["A", "B"])
    ops.add_expense(tid, "Gîte", montant_reel="100")
    real = S.ledger
    def broken(t, r):
        L = real(t, r)
        for e in L["expenses"]:
            e["ok"] = False
        return L
    monkeypatch.setattr(S, "ledger", broken)
    page = TestClient(app).get(f"/v/{tid}/depenses").text
    assert "⚠ à vérifier" in page
