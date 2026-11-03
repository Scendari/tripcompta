"""Centimes d'arrondi : chaque dépense tombe juste au centime. Les centimes qui restent (100 € pour 3 = 33,333… €)
sont payés par des participants tirés au sort (par défaut, chacun son tour) ou par l'asso ; réglage du voyage,
et choix possible dépense par dépense. Montants vérifiés à la main."""
import html
import re
import threading
import time

import pytest
from fastapi.testclient import TestClient

from compta import engine as E
from compta import models as M
from compta import services as S
from compta.ops import Ops
from compta.web import create_app

P3 = [E.Participant(i, f"P{i}") for i in (1, 2, 3)]
ALL3 = {1: 1, 2: 1, 3: 1}


def norm(t):
    return re.sub(r"[  \xa0]", " ", html.unescape(t))


# ---------------------------------------------------------------- moteur

def test_engine_participants_pay_cents_by_default():
    r = E.compute(P3, [E.Expense(1, "Gîte", actual=10000, weights=ALL3)])
    # 33,333… → 33,33 chacun, et le centime restant pour une personne : 33,33 + 33,33 + 33,34 = 100,00
    assert sorted(r.people[i].share for i in (1, 2, 3)) == [3333, 3333, 3334]
    assert r.total_shares == 10000 and r.rounding_gain == 0 and r.asso_total == 0 and r.is_balanced()
    assert r.rounding_mode == "participants" and len(r.cent_winners(1)) == 1


def test_engine_asso_takes_cents():
    r = E.compute(P3, [E.Expense(1, "Gîte", actual=10000, weights=ALL3)], rounding=E.ROUND_ASSO)
    assert [r.people[i].share for i in (1, 2, 3)] == [3333, 3333, 3333]
    assert r.asso_total == 1 and r.asso_charges[0].reason == "centimes d'arrondi" and r.is_balanced()
    assert r.cent_info[1]["asso"] == 1 and r.cent_winners(1) == []
    # montant rond : aucun centime à payer, quel que soit le choix
    r = E.compute(P3, [E.Expense(1, "Gîte", actual=9000, weights=ALL3)], rounding=E.ROUND_ASSO)
    assert [r.people[i].share for i in (1, 2, 3)] == [3000] * 3 and r.asso_total == 0
    # cas du voyage : 189,22 € ÷ 11 = 17,2018… €
    p11 = [E.Participant(i, f"P{i}") for i in range(1, 12)]
    e = E.Expense(1, "Essence", actual=18922, weights={i: 1 for i in range(1, 12)})
    a, b = E.compute(p11, [e], rounding=E.ROUND_ASSO), E.compute(p11, [e])
    assert {p.share for p in a.people.values()} == {1720} and a.asso_total == 2         # l'asso paie 0,02 €
    assert sorted(p.share for p in b.people.values()) == [1720] * 9 + [1721] * 2        # 2 personnes tirées
    assert b.total_shares == 18922 and b.asso_total == 0


def test_engine_asso_mode_with_asso_charges_balances():
    """Une part prise en charge par l'asso (désistement) + les centimes pour l'asso : l'équation reste juste."""
    w = E.Withdrawal(3, E.ASSO, [])
    r = E.compute(P3, [E.Expense(1, "Gîte", actual=10000, weights=ALL3)], withdrawals=[w], rounding=E.ROUND_ASSO)
    assert r.total_shares + r.asso_total == r.total_cost and r.is_balanced()


# ---------------------------------------------------------------- application

@pytest.fixture()
def trip(tmp_path):
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.create_trip("x")["voyage_id"]
    ops.add_participants(tid, ["Anne", "Bob", "Chloé"])
    ops.import_trip({"voyage_id": tid, "depenses": [{"libelle": "Gîte", "reel": "100", "pour": "tous"}],
                     "versements": [{"participant": n, "montant": "33,33"} for n in ("Anne", "Bob", "Chloé")]},
                    voyage=tid, verifier_id=tid)
    return app, TestClient(app), tid, ops


def test_draw_is_stored_and_kept(trip):
    app, c, tid, ops = trip
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        e = t.expenses[0]
        winner = int(e.cents_who)                                # l'import a enregistré le tirage
        r = S.compute_trip(t)
        assert r.cent_info[e.id]["drawn"] and r.people[winner].share == 3334
        m = S.money_summary(t, r)
        # chacun a versé 33,33 € ; la personne tirée doit encore 0,01 € ; la caisse finira à 0,00 € pile
        assert (m["to_collect"], m["to_refund"], m["final"]) == (1, 0, 0)
    # d'autres modifications du voyage ne refont pas le tirage du gîte
    ops.add_expense(tid, "Pot", montant_reel="7")
    ops.add_movement(tid, "Anne", "10")
    with app.state.db.Session() as s:
        assert int(s.get(M.Trip, tid).expenses[0].cents_who) == winner
    # mais si le gîte ne laisse plus de centime (4 personnes : 25 € pile), le tirage est oublié
    ops.add_participants(tid, ["Denis"])
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        gite = next(x for x in t.expenses if x.label == "Gîte")
        assert len(gite.shares) == 4 and gite.cents_who == ""


def test_web_choice_changes_everything(trip):
    app, c, tid, ops = trip
    page = norm(c.get(f"/v/{tid}/asso").text)
    assert "Centimes d'arrondi : tirés au sort" in page and 'value="participants" checked' in page
    r = c.post(f"/v/{tid}/asso/arrondi", data={"rounding": "asso"}, follow_redirects=True)
    page = norm(r.text)
    assert "Les centimes d'arrondi sont pris en charge par" in page and "↶ Défaire" in page
    assert 'value="asso" checked' in page and "l'asso paie 0,01 € de centimes d'arrondi" in page
    plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))
    assert "Gîte — centimes d'arrondi : 0,01 €" in plain                # dans « ce que l'asso prend à sa charge »
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        r_ = S.compute_trip(t)
        assert {p.share for p in r_.people.values()} == {3333} and r_.asso_total == 1
        m = S.money_summary(t, r_)
        # chacun a versé 33,33 € pour une part de 33,33 € : rien à régler ; l'asso a payé 100 € et reçu 99,99 €
        assert (m["to_collect"], m["to_refund"], m["final"]) == (0, 0, -1)
        assert norm(S.final_sentence(m, "l'asso", E.fmt)) == "Le voyage coûte 0,01 € à l'asso."
    mx = norm(c.get(f"/v/{tid}/repartition").text)
    assert "le coût du voyage ✓" in mx
    pdf = c.get(f"/v/{tid}/export.pdf")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    c.post("/annuler", headers={"referer": f"http://testserver/v/{tid}/asso"})       # Défaire
    with app.state.db.Session() as s:
        assert s.get(M.Trip, tid).rounding == "participants"
    r = c.post(f"/v/{tid}/asso/arrondi", data={"rounding": "n'importe quoi"}, follow_redirects=True)
    assert "Choix d'arrondi inconnu" in norm(r.text)


def test_choice_and_draw_kept_by_export_import(trip, tmp_path):
    from compta import transfer as T
    app, c, tid, ops = trip
    c.post(f"/v/{tid}/asso/arrondi", data={"rounding": "asso"})
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        t.expenses[0].cents_mode = "participants"                 # cette dépense : les participants
        s.commit()
        before = {p.name: S.compute_trip(t).people[p.id].share for p in t.participants}
        z = T.export_zip(s, [t], app.state.db.receipts_dir)
    app2 = create_app(tmp_path / "autre")
    with app2.state.db.Session() as s:
        trips, _ = T.import_zip(s, z, app2.state.db.receipts_dir)
        s.commit()
        t2 = trips[0]
        assert t2.rounding == "asso" and t2.expenses[0].cents_mode == "participants"
        assert {p.name: S.compute_trip(t2).people[p.id].share for p in t2.participants} == before


def test_expense_preview_follows_choice(trip):
    app, c, tid, ops = trip
    pids = [p["id"] for p in ops.trip(tid)["participants"]]
    form = {"label": "Bateau", "actual": "10", **{f"inc_{i}": "1" for i in pids}}
    txt = norm(re.sub(r"<[^>]+>", "", c.post(f"/v/{tid}/depenses/apercu", data=form).text))
    assert "3,33 € chacun" in txt and "+ 1 centime pour l'un d'eux (tiré au sort)" in txt
    c.post(f"/v/{tid}/asso/arrondi", data={"rounding": "asso"})
    txt = norm(re.sub(r"<[^>]+>", "", c.post(f"/v/{tid}/depenses/apercu", data=form).text))
    assert "3,33 € chacun" in txt and "0,01 € de centimes d'arrondi payés par" in txt


# ---------------------------------------------------------------- navigateur

@pytest.fixture()
def browser_url(trip):
    import uvicorn
    from tests.conftest import _free_port
    app = trip[0]
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    while not srv.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    th.join(5)


def test_form_draw_change_and_save(trip, browser_url):
    """Dans le formulaire : qui paie les centimes (tirés au sort), « Changer » → autres personnes ou l'asso,
    et c'est ce qui est enregistré."""
    from playwright.sync_api import sync_playwright
    app, c, tid, ops = trip
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(f"{browser_url}/v/{tid}/depenses/nouvelle")
        pg.fill("#label", "Bateau")
        pg.fill("#actual", "10")
        pg.locator("[data-who-chips] button", has_text="Tout le monde").click()
        vals = lambda: pg.eval_on_selector_all(".who-amount", "els => els.map(e => e.value).filter(v => v)")
        assert sorted(vals()) == ["3,33", "3,33", "3,34"]
        assert norm(pg.inner_text("#split-status")) == "10,00 € réparti entre 3 personnes."
        line = norm(pg.inner_text("[data-cents-text]"))
        assert re.fullmatch(r"🎲 Centimes d’arrondi : 0,01 € payé par (Anne|Bob|Chloé) \(tiré au sort\)", line), line
        first = line.split("par ")[1].split(" (")[0]
        assert pg.locator(".cent-badge:not([hidden])").count() == 1
        pg.click("[data-cents-toggle]")
        pg.click("[data-cents-redraw]")                          # « Tirer d'autres personnes »
        second = norm(pg.inner_text("[data-cents-text]")).split("par ")[1].split(" (")[0]
        assert second != first
        pg.click("[data-cents-asso]")                            # « Faire payer l'asso »
        assert vals() == ["3,33"] * 3 and norm(pg.inner_text("[data-cents-text]")).startswith(
            "🏛️ Centimes d’arrondi : 0,01 € payé par l'asso")
        assert pg.locator(".cent-badge:not([hidden])").count() == 0
        pg.click("[data-cents-people]")                          # « Tirer au sort parmi les participants »
        assert sorted(vals()) == ["3,33", "3,33", "3,34"]
        shown = norm(pg.inner_text("[data-cents-text]")).split("par ")[1].split(" (")[0]
        pg.click("form button.btn:has-text('Enregistrer')")
        pg.wait_for_load_state()
        d = {x["libelle"]: x for x in ops.trip(tid)["depenses"]}["Bateau"]
        with app.state.db.Session() as s:
            e = s.get(M.Expense, d["id"])
            names = {p.id: p.name for p in s.get(M.Trip, tid).participants}
            assert e.cents_mode == "" and names[int(e.cents_who)] == shown        # le tirage vu à l'écran
        # la réouverture montre le même tirage ; « Faire payer l'asso » enregistré pour cette dépense seulement
        pg.goto(f"{browser_url}/v/{tid}/depenses/{d['id']}/modifier")
        assert shown in pg.inner_text("[data-cents-text]")
        pg.click("[data-cents-toggle]")
        pg.click("[data-cents-asso]")
        pg.click("form button.btn:has-text('Enregistrer')")
        pg.wait_for_load_state()
        with app.state.db.Session() as s:
            t = s.get(M.Trip, tid)
            e = s.get(M.Expense, d["id"])
            r = S.compute_trip(t)
            assert e.cents_mode == "asso" and t.rounding == "participants" and r.cent_info[e.id]["asso"] == 1
        # le lien vers le réglage du voyage ouvre le panneau sur la page de l'asso
        pg.goto(f"{browser_url}/v/{tid}/depenses/{d['id']}/modifier")
        pg.click("[data-cents-toggle]")
        pg.click(".cents-choices a")
        assert pg.url.endswith("/asso#arrondi") and pg.locator("#arrondi").get_attribute("open") is not None
        assert errors == []
        b.close()


def test_form_and_tooltip_show_the_same_amounts(trip, browser_url):
    """189,22 € pour 11 : le formulaire montre exactement ce que donne le calcul (et la bulle « Pour qui »)."""
    from playwright.sync_api import sync_playwright
    app, c, tid, ops = trip
    ops.add_participants(tid, [f"P{i}" for i in range(4, 12)])
    ops.import_trip({"voyage_id": tid, "depenses": [{"libelle": "Essence", "reel": "189,22", "pour": "tous"}]},
                    voyage=tid, verifier_id=tid)
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        e = next(x for x in t.expenses if x.label == "Essence")
        r = S.compute_trip(t)
        want = {p.id: r.share(p.id, e.id) for p in t.participants}
        winners = {p.name for p in t.participants if p.id in r.cent_winners(e.id)}
        eid = e.id
    assert sorted(want.values()) == [1720] * 9 + [1721] * 2 and len(winners) == 2
    tip = norm(c.get(f"/v/{tid}/depenses").text)
    for n in winners:
        assert f"{n} : 17,21 € (+1 c tiré au sort)" in tip
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.goto(f"{browser_url}/v/{tid}/depenses/{eid}/modifier")
        got = dict(pg.eval_on_selector_all(".who-amount:not([disabled])",
                                           "els => els.map(e => [e.name.slice(2), e.value])"))
        assert {int(k): v for k, v in got.items()} == {k: f"{v // 100},{v % 100:02d}" for k, v in want.items()}
        text = norm(pg.inner_text("[data-cents-text]"))
        assert all(n in text for n in winners) and "0,02 €" in text
        b.close()
