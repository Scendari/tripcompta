"""« Pour qui ? » : à parts égales ; un montant tapé est fixé 🔒 et le reste se répartit entre les autres."""
import re
import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient

from compta import models as M
from compta.ops import Ops
from compta.web import create_app


@pytest.fixture()
def trip(tmp_path):
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.create_trip("Test")["voyage_id"]
    ops.add_participants(tid, ["Anne", "Bob", "Chloé", "Dan"])
    with app.state.db.Session() as s:
        P = {p.name: p.id for p in s.get(M.Trip, tid).participants}
    return app, TestClient(app), tid, P, ops


def base(P, **kw):
    d = {"label": "Plongées", "actual": "1196", "payer": "asso", "payer_kind": "asso"}
    d.update(kw)
    return d


def shares(ops, tid):
    d = ops.trip(tid)
    return {p["nom"]: p["sa_part"] for p in d["participants"]}


def test_equal_split_ignores_unchecked(trip):
    app, c, tid, P, ops = trip
    c.post(f"/v/{tid}/depenses", data=base(P, split="egal", **{f"inc_{P[n]}": "1" for n in ("Anne", "Bob")},
                                           **{f"m_{P['Chloé']}": "999"}))
    assert shares(ops, tid) == {"Anne": 598.0, "Bob": 598.0, "Chloé": 0.0, "Dan": 0.0}


def test_fixed_amount_rest_split_between_others(trip):
    """Montant fixé 🔒 pour Anne (400 €) ; Bob et Chloé se partagent le reste : (1 196 − 400) / 2 = 398 €."""
    app, c, tid, P, ops = trip
    data = base(P, **{f"inc_{P[n]}": "1" for n in ("Anne", "Bob", "Chloé")},
                **{f"m_{P['Anne']}": "400", f"fx_{P['Anne']}": "1", f"m_{P['Bob']}": "1", f"m_{P['Chloé']}": "2"})
    r = c.post(f"/v/{tid}/depenses", data=data, follow_redirects=True)
    assert "enregistrée" in r.text
    assert shares(ops, tid) == {"Anne": 400.0, "Bob": 398.0, "Chloé": 398.0, "Dan": 0.0}   # m_ non fixés : ignorés
    fiche = re.sub("[\u202f\xa0]", " ", c.get(f"/v/{tid}/p/{P['Anne']}").text)
    assert "montant fixé pour lui (400,00 € sur 1 196,00 €)" in fiche
    fiche = re.sub("[\u202f\xa0]", " ", c.get(f"/v/{tid}/p/{P['Bob']}").text)
    assert "1 196,00 € − 400,00 € fixés (Anne) = 796,00 € ÷ 2 personnes" in fiche
    # le formulaire de modification ré-affiche : Anne fixée, les autres calculés
    eid = ops.trip(tid)["depenses"][0]["id"]
    edit = c.get(f"/v/{tid}/depenses/{eid}/modifier").text
    assert re.search(rf'class="who is-fixed">\s*<label><input type="checkbox" data-who data-pid="{P["Anne"]}"', edit)
    assert f'name="fx_{P["Anne"]}" value="1"' in edit and f'name="fx_{P["Bob"]}" value=""' in edit
    assert f'name="m_{P["Anne"]}" value="400"' in edit and f'name="m_{P["Bob"]}" value="398"' in edit
    assert 'name="split"' not in edit and "Montants différents" not in edit
    # la dépense change de montant : Anne reste à 400, les autres absorbent → (1 296 − 400) / 2 = 448
    ops.update_expense(tid, eid, montant_reel="1296")
    assert shares(ops, tid) == {"Anne": 400.0, "Bob": 448.0, "Chloé": 448.0, "Dan": 0.0}


def test_fixed_amounts_refused_with_clear_messages(trip):
    app, c, tid, P, ops = trip
    def post(amounts, fixed, **kw):
        d = base(P, **kw, **{f"inc_{P[n]}": "1" for n in amounts}, **{f"m_{P[n]}": v for n, v in amounts.items()},
                 **{f"fx_{P[n]}": "1" for n in fixed})
        return re.sub("[\u202f\xa0]", " ", c.post(f"/v/{tid}/depenses", data=d, follow_redirects=True).text)
    assert "Les montants fixés font 1 300,00 €, plus que la dépense (1 196,00 €)" in post(
        {"Anne": "900", "Bob": "400", "Chloé": ""}, ["Anne", "Bob"])
    assert "Il reste 96,00 € à répartir : tous les montants sont fixés" in post(
        {"Anne": "400", "Bob": "400", "Chloé": "300"}, ["Anne", "Bob", "Chloé"])
    assert "Indique le montant de la dépense avant de fixer" in post({"Anne": "10"}, ["Anne"], actual="", pending="1")
    assert ops.trip(tid)["depenses"] == []
    # ancien formulaire (« montants différents ») : tous fixés, la somme doit tomber juste → accepté
    r = c.post(f"/v/{tid}/depenses", data=base(P, split="montants", **{f"inc_{P[n]}": "1" for n in ("Anne", "Bob", "Chloé")},
               **{f"m_{P['Anne']}": "400", f"m_{P['Bob']}": "400", f"m_{P['Chloé']}": "396"}), follow_redirects=True)
    assert shares(ops, tid) == {"Anne": 400.0, "Bob": 400.0, "Chloé": 396.0, "Dan": 0.0}


def test_unequal_parts_become_fixed_amounts(trip):
    """Parts inégales (une IA : 6, 4, 3 parts sur 1 300 €) : plus de parts. Chloé (3 parts, la plus petite) reste
    libre, 300 € ; Anne et Bob sont fixés 🔒 à 600 et 400 €. Mêmes montants."""
    app, c, tid, P, ops = trip
    ops.add_expense(tid, "Plongées club", montant_reel="1300", parts={"Anne": 6, "Bob": 4, "Chloé": 3})
    eid = ops.trip(tid)["depenses"][0]["id"]
    edit = c.get(f"/v/{tid}/depenses/{eid}/modifier").text
    assert f'name="m_{P["Anne"]}" value="600"' in edit and f'name="m_{P["Chloé"]}" value="300"' in edit
    assert f'name="w_{P["Anne"]}" value="1.0"' in edit and f'name="fx_{P["Anne"]}" value="1"' in edit
    assert f'name="fx_{P["Chloé"]}" value=""' in edit and f'name="fx_{P["Bob"]}" value="1"' in edit
    # ré-enregistrer sans rien toucher garde les montants
    data = base(P, label="Plongées club", actual="1300", **{f"inc_{P[n]}": "1" for n in ("Anne", "Bob", "Chloé")},
                **{f"fx_{P[n]}": "1" for n in ("Anne", "Bob")},
                **{f"m_{P[n]}": v for n, v in (("Anne", "600"), ("Bob", "400"))})
    c.post(f"/v/{tid}/depenses/{eid}", data=data)
    assert shares(ops, tid) == {"Anne": 600.0, "Bob": 400.0, "Chloé": 300.0, "Dan": 0.0}


def test_browser_fixed_amount_live(trip):
    import uvicorn
    from playwright.sync_api import sync_playwright
    app, c, tid, P, ops = trip
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
            pg.fill("#label", "Plongées")
            pg.fill("#actual", "1000")
            m = lambda n: pg.input_value(f"input[name=m_{P[n]}]")
            assert [m(n) for n in ("Anne", "Bob", "Chloé", "Dan")] == ["250,00"] * 4          # parts égales, visibles
            pg.uncheck(f"input[name=inc_{P['Dan']}]")
            # comme le calcul : 333,33 chacun, et le centime restant pour une personne tirée au sort
            assert sorted(m(n) for n in ("Anne", "Bob", "Chloé")) == ["333,33", "333,33", "333,34"]
            assert "0,01 € payé par" in pg.inner_text("[data-cents-text]").replace("\u202f", " ")
            assert pg.locator(f"input[name=m_{P['Dan']}]").is_disabled()
            # je tape 400 pour Anne : fixé 🔒, le reste (600) se répartit tout seul
            pg.fill(f"input[name=m_{P['Anne']}]", "400")
            assert [m(n) for n in ("Bob", "Chloé")] == ["300,00", "300,00"]
            assert pg.locator(".who.is-fixed").count() == 1
            status = pg.inner_text("#split-status").replace("\u202f", " ")
            assert "1 montant fixé (400,00 €)" in status and "le reste, 600,00 €, est réparti entre les 2 autres" in status
            # trop : bloqué
            pg.fill(f"input[name=m_{P['Bob']}]", "700")
            assert "plus que la dépense" in pg.inner_text("#split-status")
            pg.click("form button.btn:has-text('Enregistrer')")
            assert "/nouvelle" in pg.url and ops.trip(tid)["depenses"] == []
            # je libère Bob (🔒×) : il reprend une part du reste
            pg.click(f".who:has(input[name=inc_{P['Bob']}]) [data-unfix]")
            assert [m(n) for n in ("Bob", "Chloé")] == ["300,00", "300,00"]
            # « Tout le monde » recoche Dan : le reste (600) se partage entre 3
            pg.click("button[data-reset=all]")
            assert [m(n) for n in ("Bob", "Chloé", "Dan")] == ["200,00", "200,00", "200,00"]
            pg.click("form button.btn:has-text('Enregistrer')")
            assert "enregistrée" in pg.locator(".flash").inner_text()
            assert shares(ops, tid) == {"Anne": 400.0, "Bob": 200.0, "Chloé": 200.0, "Dan": 200.0}
            assert errors == []
            b.close()
    finally:
        srv.should_exit = True
        th.join(5)


def test_no_parts_wording_anywhere_amounts_in_euros(trip):
    """Une IA donne souvent des montants en euros entiers (400, 376…) : ce sont des montants, pas des « parts »."""
    pdfium = pytest.importorskip("pypdfium2")
    app, c, tid, P, ops = trip
    ops.add_expense(tid, "Plongées", montant_reel="1196", parts={"Anne": 400, "Bob": 400, "Chloé": 396})
    ops.add_expense(tid, "Nuits", montant_reel="130", parts={"Anne": 6, "Bob": 4, "Chloé": 3})
    fiche = re.sub("[ \xa0]", " ", c.get(f"/v/{tid}/p/{P['Chloé']}").text)
    assert "montant fixé pour lui (396,00 € sur 1 196,00 €)" in fiche
    fiche = re.sub("[ \xa0]", " ", c.get(f"/v/{tid}/p/{P['Anne']}").text)   # Nuits : 3 parts = la part libre
    assert "Nuits : montant fixé pour lui (60,00 € sur 130,00 €)" in fiche
    doc = pdfium.PdfDocument(c.get(f"/v/{tid}/export.pdf").content)
    text = re.sub(r"\s+", " ", "\n".join(doc[i].get_textpage().get_text_range() for i in range(len(doc))))
    assert "part(s)" not in text and "parts au total" not in text
    assert "montant fixé pour lui (396,00" in re.sub("[ \xa0]", " ", text)


def test_fixed_names_in_explanation(trip):
    """« fixés (qui) » : un nom ; jusqu'à 3 : nom et montant ; au-delà : le nombre de personnes."""
    from compta import engine as E
    P_ = [E.Participant(i, n) for i, n in enumerate(["Anne", "Bob", "Chloé", "Dan", "Eve"], 1)]
    def txt(fixed):
        r = E.compute(P_, [E.Expense(1, "Gîte", "x", None, 50000, None, {i: 1 for i in range(1, 6)}, fixed=fixed)])
        return " ".join(c.text for ln in r.lines_for(5) for c in ln.components).replace(" ", " ").replace("\xa0", " ")
    assert "500,00 € − 60,00 € fixés (Anne) = 440,00 € ÷ 4 personnes" in txt({1: 6000})
    assert "− 110,00 € fixés (Anne 60,00 €, Bob 50,00 €) = 390,00 € ÷ 3 personnes" in txt({1: 6000, 2: 5000})
    assert "fixés (4 personnes) = 300,00 € pour lui seul" in txt({1: 5000, 2: 5000, 3: 5000, 4: 5000})


def test_browser_prorata_and_typed_equal_amount(trip):
    """Montants fixés sur le prévu : quand le réel est tapé, « Recalculer en proportion » (11 € → 13,33 €) ; et un
    montant tapé égal à la part de la répartition n'est pas fixé."""
    import threading
    import time
    import uvicorn
    from playwright.sync_api import sync_playwright
    from tests.conftest import _free_port
    app, c, tid, P, ops = trip
    eid = ops.add_expense(tid, "Pique-nique", montant_prevu="66", parts={"Anne": 2, "Bob": 1, "Chloé": 1, "Dan": 2})["depense_id"]
    port = _free_port()
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
            pg.goto(f"http://127.0.0.1:{port}/v/{tid}/depenses/{eid}/modifier")
            m = lambda n: float(pg.input_value(f"input[name=m_{P[n]}]").replace(",", ".") or 0)
            assert m("Anne") == 22 and m("Bob") == 11                          # 2 parts = 22 € 🔒 ; Bob libre
            assert pg.input_value(f"input[name=fx_{P['Anne']}]") == "1" and pg.input_value(f"input[name=fx_{P['Bob']}]") == ""
            assert pg.locator("[data-prorata]").is_hidden()
            pg.fill("#actual", "80")
            pg.wait_for_selector("[data-prorata]:not([hidden])")
            assert "fixés pour le prévu (66,00" in pg.inner_text("[data-prorata]")
            assert "22,00 € → 26,67 €" in pg.inner_text("[data-prorata-go]").replace(" ", " ").replace("\xa0", " ")
            pg.click("[data-prorata-go]")
            assert m("Anne") == 26.67 and m("Dan") == 26.67 and m("Bob") == 13.33
            assert pg.locator("[data-prorata]").is_hidden()
            # Chloé : on tape exactement ce qu'elle paie déjà (13,33) : pas de cadenas
            pg.fill(f"input[name=m_{P['Chloé']}]", "13,33")
            pg.press(f"input[name=m_{P['Chloé']}]", "Tab")
            assert pg.input_value(f"input[name=fx_{P['Chloé']}]") == ""
            # Bob : un autre montant : cadenas
            pg.fill(f"input[name=m_{P['Bob']}]", "10")
            pg.press(f"input[name=m_{P['Bob']}]", "Tab")
            assert pg.input_value(f"input[name=fx_{P['Bob']}]") == "1"
            assert errors == []
            b.close()
    finally:
        srv.should_exit = True
        th.join(5)
