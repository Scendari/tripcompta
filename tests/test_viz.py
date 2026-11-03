"""Page « Le voyage en images » : chiffres clés et 4 graphiques. Montants vérifiés à la main.

Voyage : Anne, Bruno, Chloé, David.
  Gîte (Logement) prévu 380, réel 400, pour tous        → 100 chacun
  Bateau (Plongée › Bateau) 240, payé par Anne           → 60 chacun (80 après le désistement de David)
  Bouteilles (Plongée › Gonflage) prévu 100, réel 120    → 30 chacun (40 après)
  David se désiste et garde le Gîte : il paie 100 ; les autres 100 + 80 + 40 = 220.
  Versements : Bruno 100, Chloé 160.
"""
import re

import pytest
from fastapi.testclient import TestClient

from compta import models as M, services as S
from compta.ops import Ops
from compta.web import create_app


@pytest.fixture
def v(tmp_path):
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.create_trip("Voyage test")["voyage_id"]
    ops.add_participants(tid, ["Anne", "Bruno", "Chloé", "David"])
    ops.add_expense(tid, "Gîte", categorie="Logement", montant_prevu="380", montant_reel="400")
    ops.add_expense(tid, "Bateau", categorie="Plongée", sous_categorie="Bateau", montant_reel="240", paye_par="Anne")
    ops.add_expense(tid, "Bouteilles", categorie="Plongée", sous_categorie="Gonflage", montant_prevu="100",
                    montant_reel="120")
    ops.add_movement(tid, "Bruno", "100")
    ops.add_movement(tid, "Chloé", "160")
    ops.withdraw(tid, "David", parts_conservees=["Gîte"])
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        V = S.trip_visuals(t, S.compute_trip(t))
    return app, tid, V


def test_key_figures(v):
    app, tid, V = v
    assert V["total"] == 76000 and V["n_people"] == 4
    assert V["median"] == 22000 and (V["share_min"], V["share_max"]) == (22000, 22000)
    assert (V["wd_n"], V["wd_pct"], V["wd_kept"], V["wd_refund"], V["wd_names"]) == (1, 25, 10000, 0, ["David"])


def test_where_the_money_goes(v):
    app, tid, V = v
    assert [(c["name"], c["cents"], c["pct"]) for c in V["costs"]] == [("Logement", 40000, 53), ("Plongée", 36000, 47)]
    plongee = V["costs"][1]
    assert [(x["name"], x["cents"], x["pct"]) for x in plongee["subs"]] == [("Bateau", 24000, 67), ("Gonflage", 12000, 33)]
    assert V["costs"][0]["subs"] == []                       # pas de sous-catégorie : rien à déplier


def test_what_each_pays_by_category(v):
    app, tid, V = v
    by = {p["name"]: p for p in V["people"]}
    assert {s["name"]: s["cents"] for s in by["Anne"]["segs"]} == {"Logement": 10000, "Plongée": 12000}
    assert {s["name"]: s["cents"] for s in by["David"]["segs"]} == {"Logement": 10000}
    assert by["David"]["withdrawn"] and by["Anne"]["share"] == 22000
    for p in V["people"]:                                    # les segments font la part de chacun
        assert sum(s["cents"] for s in p["segs"]) == p["share"]
    # la couleur suit la catégorie (sa place dans le voyage), pas son rang dans le graphique
    slots = {c["name"]: c["slot"] for c in V["legend"]}
    assert all(s["slot"] == slots[s["name"]] for p in V["people"] for s in p["segs"])


def test_settlements(v):
    app, tid, V = v
    st = {a["name"]: (a["debit"], a["credit"], a["balance"]) for a in V["settle"]}
    assert st == {"Anne": (22000, 24000, -2000), "Bruno": (22000, 10000, 12000), "Chloé": (22000, 16000, 6000),
                  "David": (10000, 0, 10000)}
    assert (V["owed"], V["covered"], V["to_collect"], V["to_refund"]) == (76000, 48000, 28000, 2000)
    assert V["covered"] + V["to_collect"] == V["owed"]


def test_planned_vs_actual(v):
    app, tid, V = v
    assert [(c["name"], c["planned"], c["actual"], c["diff"]) for c in V["plan"]] == [
        ("Logement", 38000, 40000, 2000), ("Plongée", 10000, 12000, 2000)]


def test_page_renders_everything(v):
    app, tid, V = v
    c = TestClient(app)
    page = re.sub(r"[ \xa0]", " ", c.get(f"/v/{tid}/en-images").text)
    for txt in ("Le voyage en images", "Coût du voyage", "760,00 €", "1 sur 4", "(25 %)", "Où part l'argent",
                "Ce que paie chacun", "Où en sont les règlements", "63 % réglé", "Prévu ou réel",
                "▲ 20,00 € de plus", "on lui doit 20,00 €", "doit 120,00 €", "Voir en tableau"):
        assert txt in page.replace("&#39;", "'"), txt
    assert 'href="/v/%d/en-images" class="on"' % tid in page                    # entrée du menu
    assert page.count('tabindex="0" data-tip=') >= 8                             # chaque barre a sa bulle (clavier aussi)


def test_page_without_planned_amounts_explains(tmp_path):
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.create_trip("x")["voyage_id"]
    ops.add_participants(tid, ["A", "B"])
    ops.add_expense(tid, "Gîte", montant_reel="100")
    page = TestClient(app).get(f"/v/{tid}/en-images").text
    assert "Aucune dépense n" in page and "0 sur 2" in page and "personne ne s" in page
    empty = TestClient(app).get(f"/v/{ops.create_trip('vide')['voyage_id']}/en-images").text
    assert "Ajoute des participants et des dépenses" in empty


def test_amounts_written_on_bars(v):
    """« Ce que paie chacun » : le montant de chaque morceau est écrit sur la barre quand il y tient ;
    sinon il est masqué (la bulle au survol le donne toujours). Jamais de débordement, en grand comme sur téléphone."""
    import threading
    import time

    import uvicorn
    from playwright.sync_api import sync_playwright
    from tests.conftest import _free_port
    app, tid, V = v
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    while not srv.started:
        time.sleep(0.05)
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            for width in (1400, 390):
                pg = b.new_page(viewport={"width": width, "height": 900})
                pg.goto(f"http://127.0.0.1:{port}/v/{tid}/en-images")
                nums = pg.locator(".seg-num")
                texts = [t.replace(" ", " ").replace("\xa0", " ") for t in nums.all_inner_texts()]
                tips = pg.eval_on_selector_all(".seg-num", "els => els.map(e => e.parentElement.dataset.tip)")
                assert texts and all(t.split(" : ")[-1].replace(" ", " ").replace("\xa0", " ") == x
                                     for t, x in zip(tips, texts))          # le même montant que la bulle
                # chaque montant visible tient dans son morceau de barre
                ok = pg.evaluate("""() => [...document.querySelectorAll('.seg-num:not(.hide)')].every(n => {
                    const r = document.createRange(); r.selectNodeContents(n);
                    return r.getBoundingClientRect().width <= n.parentElement.clientWidth - 8; })""")
                assert ok and pg.evaluate("document.documentElement.scrollWidth") <= width + 1
                if width == 1400:
                    assert pg.locator(".seg-num:not(.hide)").count() == nums.count()   # en grand : tous lisibles
                pg.close()
            b.close()
    finally:
        srv.should_exit = True
        th.join(5)
