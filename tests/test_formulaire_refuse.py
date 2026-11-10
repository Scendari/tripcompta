"""Après un refus, le formulaire revient avec ce qui avait été saisi, et le message est écrit à côté du champ en
cause (en plus du bandeau rouge en haut de page)."""
import datetime as dt
import re
import socket
import threading
import time
from urllib.parse import unquote

import pytest
import uvicorn
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

from compta.ops import Ops
from compta.web import create_app


@pytest.fixture
def setup(tmp_path):
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.create_trip("Week-end")["voyage_id"]
    ops.add_participants(tid, ["Anne", "Bob", "Chloé"])
    ops.add_expense(tid, "Gîte", montant_reel="300")
    return app, ops, tid


@pytest.fixture
def live(setup):
    app, ops, tid = setup
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    while not srv.started:
        time.sleep(0.05)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.errors = []
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.on("dialog", lambda d: d.accept())
        yield pg, f"http://127.0.0.1:{port}", tid, ops
        b.close()
    srv.should_exit = True
    th.join(5)


# ---------------------------------------------------------------- côté serveur : le champ en cause est nommé
def test_refusal_names_the_field(setup):
    app, ops, tid = setup
    c = TestClient(app)
    P = {p["nom"]: p["id"] for p in ops.trip(tid)["participants"]}

    def loc(url, data, referer=None):
        r = c.post(url, data=data, follow_redirects=False, headers={"referer": referer} if referer else {})
        assert r.status_code == 303
        return unquote(r.headers["location"])
    here = f"http://testserver/v/{tid}/depenses/nouvelle"
    u = loc(f"/v/{tid}/depenses", {"label": "X", "actual": "abc", f"inc_{P['Anne']}": "1"}, here)
    assert u.startswith(f"/v/{tid}/depenses/nouvelle?msg=Montant invalide") and u.endswith("&e=1&f=actual")
    u = loc(f"/v/{tid}/depenses", {"label": "X", "planned": "-5", f"inc_{P['Anne']}": "1"}, here)
    assert "doit être positif" in u and u.endswith("&f=planned")
    u = loc(f"/v/{tid}/depenses", {"label": "", "actual": "5"}, here)
    assert "Donne un nom à la dépense" in u and u.endswith("&f=label") and "/depenses/nouvelle?" in u
    u = loc(f"/v/{tid}/depenses", {"label": "X", f"inc_{P['Anne']}": "1"}, here)
    assert "Indique au moins un montant" in u and u.endswith("&f=actual")
    u = loc(f"/v/{tid}/argent", {"participant": P["Anne"], "kind": "versement", "amount": "0"},
            f"http://testserver/v/{tid}/argent")
    assert u.startswith(f"/v/{tid}/argent?msg=") and u.endswith("&e=1&f=amount")
    u = loc(f"/v/{tid}/remises", {"participant": P["Anne"], "kind": "reduce", "value": "x"})
    assert u.endswith("&e=1&f=value")
    u = loc("/voyages", {"name": " "})
    assert u.startswith("/?nouveau=1&msg=Donne un nom au voyage") and u.endswith("&f=name")


def test_second_refusal_does_not_pile_up_messages(setup):
    """On revient d'une page qui portait déjà un message de refus : il n'y en a qu'un dans l'adresse."""
    app, ops, tid = setup
    c = TestClient(app)
    first = f"http://testserver/v/{tid}/depenses/nouvelle?msg=Montant%20invalide&e=1&f=actual"
    r = c.post(f"/v/{tid}/depenses", data={"label": "", "actual": "5"}, follow_redirects=False,
               headers={"referer": first})
    u = unquote(r.headers["location"])
    assert u.count("msg=") == 1 and u.count("e=1") == 1 and u.endswith("&f=label")


# ---------------------------------------------------------------- dans le navigateur
def test_expense_form_comes_back_filled_with_message_at_the_field(live):
    pg, base, tid, ops = live
    pg.goto(f"{base}/v/{tid}/depenses/nouvelle")
    pg.fill("#label", "Courses du samedi")
    pg.select_option("#category", index=2)
    cat = pg.locator("#category").input_value()
    pg.fill("#planned", "80")
    pg.fill("#actual", "soixante")                              # pas un nombre
    pg.locator("#date + .today-btn").click()
    bob = pg.locator(".who-row, li, label").filter(has_text="Bob").locator("input[data-who]").first
    bob.uncheck()
    pg.locator("button:has-text('Enregistrer')").first.click()
    pg.wait_for_url(re.compile(r"e=1"))
    assert "/depenses/nouvelle" in pg.url
    # la saisie est revenue
    assert pg.locator("#label").input_value() == "Courses du samedi"
    assert pg.locator("#category").input_value() == cat
    assert pg.locator("#planned").input_value() == "80"
    assert pg.locator("#actual").input_value() == "soixante"
    assert pg.locator("#date").input_value() == dt.date.today().isoformat()
    assert not pg.locator("input[data-who]").nth(1).is_checked()          # Bob, toujours décoché
    assert pg.locator("input[data-who]").nth(0).is_checked() and pg.locator("input[data-who]").nth(2).is_checked()
    # le message : en haut (bandeau rouge) et à côté du champ, qui a le curseur
    assert "Montant invalide" in pg.locator(".flash.err").inner_text()
    note = pg.locator("#champ-refuse")
    assert "Montant invalide : « soixante »" in note.inner_text()
    assert pg.locator("#actual").get_attribute("aria-invalid") == "true"
    assert pg.locator("#actual").get_attribute("aria-describedby") == "champ-refuse"
    assert pg.evaluate("document.activeElement.id") == "actual"
    assert pg.evaluate("document.querySelector('#actual').closest('.field').contains(document.querySelector('#champ-refuse'))")
    # on corrige : le message s'efface, et l'enregistrement passe avec tout le reste
    pg.fill("#actual", "60")
    assert note.count() == 0 and pg.locator("#actual").get_attribute("aria-invalid") is None
    pg.locator("button:has-text('Enregistrer')").first.click()
    pg.wait_for_url(re.compile(r"/depenses(\?|#|$)"))
    e = next(x for x in ops.trip(tid)["depenses"] if x["libelle"] == "Courses du samedi")
    assert e["reel"] == 60.0 and e["prevu"] == 80.0 and e["date"] == dt.date.today().isoformat()
    assert sorted(e["pour"]) == ["Anne", "Chloé"]                          # 60 € à deux : 30 € chacune
    P = {p["nom"]: p for p in ops.trip(tid)["participants"]}
    assert (P["Anne"]["sa_part"], P["Bob"]["sa_part"], P["Chloé"]["sa_part"]) == (130.0, 100.0, 130.0)
    assert pg.errors == []


def test_payment_row_comes_back_open_and_filled(live):
    """Un versement refusé (montant illisible) : la ligne de la bonne personne se rouvre, avec le montant et la note."""
    pg, base, tid, ops = live
    pg.goto(f"{base}/v/{tid}/argent")
    rows = pg.locator(".settle-table tr:not(.edit-row)")
    name = rows.nth(1).locator("td:not(.tick)").first.inner_text().strip()
    rows.nth(1).locator("button[data-toggle-row]").click()
    form = pg.locator(".edit-row:not([hidden]) form.pay-form")
    form.locator("input[name=amount]").fill("cent")
    form.locator("input[name=note]").fill("chèque n° 123")
    form.locator("button:has-text('Enregistrer')").click()
    pg.wait_for_url(re.compile(r"e=1"))
    opened = pg.locator(".edit-row:not([hidden])")
    assert opened.count() == 1                                             # une seule ligne rouverte : la bonne
    pid = opened.locator("input[name=participant]").input_value()
    assert {p["nom"]: str(p["id"]) for p in ops.trip(tid)["participants"]}[name] == pid
    assert opened.locator("input[name=amount]").input_value() == "cent"
    assert opened.locator("input[name=note]").input_value() == "chèque n° 123"
    assert opened.locator("input[name=amount]").get_attribute("aria-invalid") == "true"
    assert "Montant invalide" in opened.locator("#champ-refuse").inner_text()
    opened.locator("input[name=amount]").fill("100")
    opened.locator("button:has-text('Enregistrer')").click()
    pg.wait_for_url(re.compile(r"/argent\?msg"))
    mv = ops.trip(tid)["mouvements"]
    assert len(mv) == 1 and mv[0]["montant"] == 100.0 and mv[0]["participant"] == name and mv[0]["note"] == "chèque n° 123"
    assert pg.errors == []


def test_discount_form_keeps_everything(live):
    pg, base, tid, ops = live
    pg.goto(f"{base}/v/{tid}/participants/remises")
    pg.select_option("#rp", label="Bob")
    pg.check("input[name=kind][value=increase]")
    pg.fill("#rv", "beaucoup")
    pg.fill("#rr", "chambre seule")
    pg.check("input[name=by][value=person]")
    pg.select_option("#rc", label="Anne")
    pg.locator("#remise-form button.btn:not(.ghost)").click()
    pg.wait_for_url(re.compile(r"e=1"))
    assert pg.locator("#rp option:checked").inner_text() == "Bob"
    assert pg.locator("input[name=kind][value=increase]").is_checked()
    assert pg.locator("#rv").input_value() == "beaucoup" and pg.locator("#rr").input_value() == "chambre seule"
    assert pg.locator("input[name=by][value=person]").is_checked()
    assert pg.locator("#rc").is_visible() and pg.locator("#rc option:checked").inner_text() == "Anne"
    assert "Montant invalide" in pg.locator("#champ-refuse").inner_text()
    assert pg.errors == []


def test_nothing_is_restored_after_a_success_or_on_another_page(live):
    """La saisie gardée ne sert qu'au retour d'un refus : après un enregistrement réussi, le formulaire est vide."""
    pg, base, tid, ops = live
    pg.goto(f"{base}/v/{tid}/depenses/nouvelle")
    pg.fill("#label", "Péage")
    pg.fill("#actual", "12")
    pg.locator("button:has-text('Enregistrer')").first.click()
    pg.wait_for_url(re.compile(r"/depenses(\?|#|$)"))
    pg.goto(f"{base}/v/{tid}/depenses/nouvelle?msg=Montant%20invalide&e=1&f=actual")    # adresse rejouée à la main
    assert pg.locator("#label").input_value() == "" and pg.locator("#actual").input_value() == ""
    assert "Montant invalide" in pg.locator("#champ-refuse").inner_text()     # le message, lui, est bien placé
    assert pg.evaluate("sessionStorage.getItem('formulaire-refuse')") is None
    assert pg.errors == []
