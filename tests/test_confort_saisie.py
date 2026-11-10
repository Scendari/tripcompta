"""Confort de saisie : prévenir avant de quitter un formulaire commencé, bouton « Aujourd'hui », états vides avec un
bouton pour avancer, remise ou supplément payé par une personne précise (à l'écran)."""
import datetime as dt
import html
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


def clean(t):
    return re.sub(r"\s+", " ", html.unescape(t).replace(" ", " ").replace("\xa0", " "))


@pytest.fixture
def trip(tmp_path):
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.create_trip("Week-end")["voyage_id"]
    ops.add_participants(tid, ["A", "B", "C"])
    ops.add_expense(tid, "Gîte", montant_reel="300")
    return app, ops, tid, TestClient(app)


def people(ops, tid):
    return {p["nom"]: p for p in ops.trip(tid)["participants"]}


# ---------------------------------------------------------------- remise compensée par une personne
def test_discount_paid_by_one_person_by_hand(trip):
    """Gîte 300 € à 3 : 100 € chacun. Remise de 30 € pour A, payée par B → A 70, B 130, C 100 (total 300)."""
    app, ops, tid, c = trip
    P = people(ops, tid)
    r = c.post(f"/v/{tid}/remises", data={"participant": P["A"]["id"], "kind": "reduce", "value": "30",
                                          "reason": "encadrant", "by": "person", "compensator": P["B"]["id"]},
               follow_redirects=True)
    page = clean(r.text)
    assert "Remise de 30,00 € pour A enregistrée" in page and "payée par B" in page
    P = people(ops, tid)
    assert (P["A"]["sa_part"], P["B"]["sa_part"], P["C"]["sa_part"]) == (70.0, 130.0, 100.0)
    assert ops.trip(tid)["cout_total"] == 300.0


def test_supplement_given_to_one_person_by_hand(trip):
    """Supplément de 20 € pour C, reversé à A → C 120, A 80, B 100."""
    app, ops, tid, c = trip
    P = people(ops, tid)
    r = c.post(f"/v/{tid}/remises", data={"participant": P["C"]["id"], "kind": "increase", "value": "20",
                                          "by": "person", "compensator": P["A"]["id"]}, follow_redirects=True)
    assert "Supplément de 20,00 € pour C enregistré" in clean(r.text) and "reversé à A" in clean(r.text)
    P = people(ops, tid)
    assert (P["A"]["sa_part"], P["B"]["sa_part"], P["C"]["sa_part"]) == (80.0, 100.0, 120.0)


def test_person_choice_is_checked_and_can_be_edited(trip):
    app, ops, tid, c = trip
    P = people(ops, tid)
    base = {"participant": P["A"]["id"], "kind": "reduce", "value": "30", "by": "person"}
    r = c.post(f"/v/{tid}/remises", data=base, follow_redirects=False)
    assert "Choisis la personne qui compense" in unquote(r.headers["location"]) and "e=1" in r.headers["location"]
    r = c.post(f"/v/{tid}/remises", data={**base, "compensator": P["A"]["id"]}, follow_redirects=False)
    assert "ne peut pas être celle qui a la remise" in unquote(r.headers["location"])
    r = c.post(f"/v/{tid}/remises", data={**base, "compensator": "abc"}, follow_redirects=False)
    assert "Choisis la personne qui compense" in unquote(r.headers["location"])
    assert people(ops, tid)["A"]["sa_part"] == 100.0                       # rien n'a été enregistré
    c.post(f"/v/{tid}/remises", data={**base, "compensator": P["B"]["id"]})
    page = c.get(f"/v/{tid}/participants/remises").text
    rm = re.search(r"remises\?rm=(\d+)", page).group(1)
    edit = c.get(f"/v/{tid}/participants/remises?rm={rm}").text
    assert re.search(r'name="by" value="person"\s+checked', edit)
    assert f'<option value="{P["B"]["id"]}" selected>' in edit
    # repasser à « les autres » : le nom de la personne est oublié
    c.post(f"/v/{tid}/remises", data={**base, "aid": rm, "by": "others", "compensator": P["B"]["id"]})
    P = people(ops, tid)
    assert (P["A"]["sa_part"], P["B"]["sa_part"], P["C"]["sa_part"]) == (70.0, 115.0, 115.0)
    assert "compensée par les autres" in clean(c.get(f"/v/{tid}/participants/remises").text)


# ---------------------------------------------------------------- états vides
def test_empty_states_have_a_button(tmp_path):
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.create_trip("Vide")["voyage_id"]
    c = TestClient(app)
    add_people = f'href="/v/{tid}/participants?ajout=1#ajout"'
    for path in ("/repartition", "/en-images"):
        page = c.get(f"/v/{tid}{path}").text
        assert "Il faut d'abord des participants" in clean(page) and add_people in page, path
    page = c.get(f"/v/{tid}/participants").text
    assert "Personne pour l'instant" in page and add_people in page
    ops.add_participants(tid, ["A", "B"])
    add_expense = f'href="/v/{tid}/depenses/nouvelle"'
    for path in ("/repartition", "/en-images", "/depenses", "/participants/avances"):
        page = c.get(f"/v/{tid}{path}").text
        assert add_expense in page, path
    assert "Il n'y a pas encore de dépense" in clean(c.get(f"/v/{tid}/en-images").text)
    sub = c.get(f"/v/{tid}/entre-nous").text
    assert "Créer le premier compte" in sub and 'data-focus="#name"' in sub


# ---------------------------------------------------------------- dans le navigateur
@pytest.fixture
def live(trip):
    app, ops, tid, _ = trip
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
        yield pg, f"http://127.0.0.1:{port}", tid, ops
        b.close()
    srv.should_exit = True
    th.join(5)


def test_today_button_fills_the_date(live):
    pg, base, tid, ops = live
    pg.goto(f"{base}/v/{tid}/depenses/nouvelle")
    assert pg.locator("#date").input_value() == ""
    pg.locator("#date + .today-btn").click()
    assert pg.locator("#date").input_value() == dt.date.today().isoformat()
    # enregistré avec la dépense
    pg.fill("#label", "Courses")
    pg.fill("#actual", "42")
    pg.locator("form#expense-form button.btn:not(.ghost)").last.click() if pg.locator("form#expense-form").count() \
        else pg.locator("button:has-text('Enregistrer')").first.click()
    pg.wait_for_url(re.compile(r"/depenses"))
    exp = next(e for e in ops.trip(tid)["depenses"] if e["libelle"] == "Courses")
    assert exp["date"] == dt.date.today().isoformat()
    # les dates du voyage (début, fin) n'ont pas ce bouton ; les versements, si
    pg.goto(f"{base}/")
    assert pg.locator(".today-btn").count() == 0
    pg.goto(f"{base}/v/{tid}/argent/mouvements")
    assert pg.locator(".today-btn").count() >= 1
    assert pg.errors == []


def hint_of(pg, sel="#date"):
    return pg.evaluate(f"getComputedStyle(document.querySelector('{sel} + .today-btn'), '::after').content")


def test_tab_accepts_today_in_an_empty_date_field(live):
    """Un champ date vide n'est jamais rempli tout seul (la date grisée que montre le Mac n'est qu'un décor). Au
    clavier, Tab accepte la date d'aujourd'hui, comme on accepte une suggestion grisée ; Échap la refuse. Cliquer
    ailleurs, revenir en arrière ou taper une date à la main ne remplit rien."""
    pg, base, tid, ops = live
    today = dt.date.today().isoformat()
    new = f"{base}/v/{tid}/depenses/nouvelle"

    pg.goto(new)                                         # 1. Tab accepte
    assert "Tab" not in hint_of(pg)                      # le rappel n'apparaît que dans le champ
    pg.focus("#date")
    assert "Tab : aujourd'hui" in hint_of(pg) and "Échap : sans date" in hint_of(pg)
    pg.keyboard.press("Tab")
    assert pg.locator("#date").input_value() == today
    assert pg.evaluate("document.activeElement.id") == "date" and "Échap : retirer la date" in hint_of(pg)
    pg.keyboard.press("Escape")                          # … et Échap retire ce que Tab vient de mettre
    assert pg.locator("#date").input_value() == ""
    for _ in range(4):                                   # refusée : Tab ne la propose plus
        pg.keyboard.press("Tab")
    assert pg.locator("#date").input_value() == ""

    pg.goto(new)                                         # 2. Échap d'abord : « sans date », Tab passe sans remplir
    pg.focus("#date")
    pg.keyboard.press("Escape")
    assert "Tab" not in hint_of(pg)
    for _ in range(4):
        pg.keyboard.press("Tab")
    assert pg.locator("#date").input_value() == ""

    pg.goto(new)                                         # 3. cliquer ailleurs ou revenir en arrière : rien
    pg.focus("#date")
    pg.click("#label")
    assert pg.locator("#date").input_value() == ""
    pg.focus("#date")
    pg.keyboard.press("Shift+Tab")
    assert pg.locator("#date").input_value() == ""

    pg.goto(new)                                         # 4. une date tapée à la main n'est pas remplacée
    pg.focus("#date")
    pg.keyboard.type("15")                               # le jour, puis Tab vers le mois : toujours pas de date
    pg.keyboard.press("Tab")
    assert pg.locator("#date").input_value() == ""
    pg.fill("#date", "2026-01-15")
    pg.focus("#date")
    assert "Tab" not in hint_of(pg) and "Échap" not in hint_of(pg)
    pg.keyboard.press("Tab")
    pg.keyboard.press("Escape")                          # Échap ne retire pas une date que la personne a choisie
    assert pg.locator("#date").input_value() == "2026-01-15"

    pg.goto(new)                                         # 5. enregistré comme affiché : acceptée, ou sans date
    pg.fill("#label", "Avec la date du jour")
    pg.fill("#actual", "10")
    pg.focus("#date")
    pg.keyboard.press("Tab")
    pg.locator("form#expense-form button.btn:not(.ghost)").last.click() if pg.locator("form#expense-form").count() \
        else pg.locator("button:has-text('Enregistrer')").first.click()
    pg.wait_for_url(re.compile(r"/depenses"))
    pg.goto(new)
    pg.fill("#label", "Sans date")
    pg.fill("#actual", "10")
    pg.locator("form#expense-form button.btn:not(.ghost)").last.click() if pg.locator("form#expense-form").count() \
        else pg.locator("button:has-text('Enregistrer')").first.click()
    pg.wait_for_url(re.compile(r"/depenses"))
    by_name = {e["libelle"]: e for e in ops.trip(tid)["depenses"]}
    assert by_name["Avec la date du jour"]["date"] == today and not by_name["Sans date"]["date"]

    # 6. les dates du voyage et les dates de paiement ne proposent rien : Tab n'y met pas de date
    pg.goto(f"{base}/v/{tid}/argent/dates")
    pg.focus("#date_0")
    before = pg.locator("#date_0").input_value()
    pg.keyboard.press("Tab")
    assert pg.locator("#date_0").input_value() == before
    assert pg.errors == []


def test_leaving_a_started_form_asks_first(live):
    pg, base, tid, ops = live
    seen = []

    def on_dialog(d):
        seen.append(d.message)
        d.dismiss() if len(seen) == 1 else d.accept()
    pg.on("dialog", on_dialog)
    pg.goto(f"{base}/v/{tid}/depenses/nouvelle")
    # rien de saisi : on quitte sans question
    pg.click(".side nav a:has-text('Participants')")
    pg.wait_for_url(re.compile(r"/participants$"))
    assert seen == []
    pg.goto(f"{base}/v/{tid}/depenses/nouvelle")
    pg.fill("#label", "Péage")
    pg.click(".side nav a:has-text('Participants')")           # 1re fois : « Annuler » → on reste, la saisie aussi
    assert len(seen) == 1 and "Quitter sans l'enregistrer" in seen[0]
    assert pg.url.endswith("/depenses/nouvelle") and pg.locator("#label").input_value() == "Péage"
    pg.click(".side nav a:has-text('Participants')")           # 2e fois : « OK » → on part
    pg.wait_for_url(re.compile(r"/participants$"))
    assert len(seen) == 2
    assert all(e["libelle"] != "Péage" for e in ops.trip(tid)["depenses"])
    assert pg.errors == []


def test_saving_or_links_inside_the_page_never_ask(live):
    pg, base, tid, ops = live
    seen = []
    pg.on("dialog", lambda d: (seen.append(d.message), d.accept()))
    pg.goto(f"{base}/v/{tid}/depenses/nouvelle")
    pg.fill("#label", "Essence")
    pg.fill("#actual", "60")
    pg.locator("#date + .today-btn").click()                    # un bouton de la page : pas de question
    pg.locator("button:has-text('Enregistrer')").first.click()  # enregistrer : pas de question
    pg.wait_for_url(re.compile(r"/depenses"))
    assert seen == []
    assert any(e["libelle"] == "Essence" for e in ops.trip(tid)["depenses"])
    assert pg.errors == []


def test_cancel_link_of_the_form_itself_never_asks(live):
    """« Annuler » ou « Pas maintenant », dans le formulaire commencé : c'est un abandon voulu, pas de question."""
    pg, base, tid, ops = live
    seen = []
    pg.on("dialog", lambda d: (seen.append(d.message), d.dismiss()))
    P = {p["nom"]: p["id"] for p in ops.trip(tid)["participants"]}
    pg.goto(f"{base}/v/{tid}/participants/remises?remise={P['A']}")
    ops.add_participants(tid, ["D"])                          # (sans effet sur la page ouverte)
    pg.fill("#rv", "30")
    # une remise existe déjà ? non : le lien « Annuler » n'apparaît qu'avec une liste ; on en crée une d'abord
    pg.locator("#remise-form button.btn:not(.ghost)").click()
    pg.wait_for_url(re.compile(r"/participants/remises"))
    edit = pg.locator("a:has-text('Modifier')").first
    edit.click()
    pg.fill("#rv", "45")
    pg.click("#remise-form a:has-text('Annuler')")
    pg.wait_for_url(re.compile(r"/participants/remises$"))
    assert seen == []
    assert pg.errors == []
