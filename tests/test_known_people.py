"""Contacts des participants (mail au trésorier, PDF) et personnes reprises d'un voyage à l'autre.

Voyage A (2025) : Anne (06 11 11 11 11, anne@exemple.fr), Bruno (accompagnant, 06 22 22 22 22), Chloé (rien).
Voyage B (2026) : Anne (06 33 33 33 33, sans e-mail), David (david@exemple.fr).
Voyage C : nouveau, vide.
→ dans C on retrouve Anne (téléphone de B, le plus récent ; e-mail repris de A), Bruno (accompagnant), Chloé, David.
"""
import re

import pytest
from fastapi.testclient import TestClient

from compta import export as X, models as M, services as S
from compta.ops import Ops
from compta.web import create_app
from tests.test_amounts import norm, pdf_text


@pytest.fixture
def env(tmp_path):
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    a = ops.create_trip("Voyage A", debut="2025-05-01")["voyage_id"]
    ops.add_participants(a, [dict(nom="Anne", telephone="06 11 11 11 11", email="anne@exemple.fr"),
                             dict(nom="Bruno", telephone="06 22 22 22 22", accompagnant=True), "Chloé"])
    b = ops.create_trip("Voyage B", debut="2026-05-01")["voyage_id"]
    ops.add_participants(b, [dict(nom="Anne", telephone="06 33 33 33 33"), dict(nom="David", email="david@exemple.fr")])
    c = ops.create_trip("Voyage C", debut="2026-10-01")["voyage_id"]
    return app, ops, a, b, c


def people(app, tid):
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        return {p.name: (p.phone, p.email, p.companion, sorted(p.role_list)) for p in t.participants}


# ---------------------------------------------------------------- qui est proposé
def test_known_people_latest_info(env):
    app, ops, a, b, c = env
    with app.state.db.Session() as s:
        t = s.get(M.Trip, c)
        k = S.known_people(t)
        assert [(x["name"], x["phone"], x["email"], x["companion"], x["trip"]) for x in k] == [
            ("Anne", "06 33 33 33 33", "anne@exemple.fr", False, "Voyage B"),   # téléphone le plus récent, e-mail de A
            ("Bruno", "06 22 22 22 22", "", True, "Voyage A"),
            ("Chloé", "", "", False, "Voyage A"),
            ("David", "", "david@exemple.fr", False, "Voyage B")]
        assert [(x["name"], sorted(x["people"])) for x in S.known_trips(t, k)] == [
            ("Voyage B", ["anne", "david"]), ("Voyage A", ["anne", "bruno", "chloe"])]   # le plus récent d'abord
        tb = s.get(M.Trip, b)                                  # dans B : ni Anne ni David (déjà là)
        assert [x["name"] for x in S.known_people(tb)] == ["Bruno", "Chloé"]


def test_take_known_people(env):
    app, ops, a, b, c = env
    cl = TestClient(app)
    page = cl.get(f"/v/{c}/participants").text
    assert "Déjà venus sur un autre voyage" in page and "Tout le groupe « Voyage B » (2)" in page
    r = cl.post(f"/v/{c}/participants/reprendre", data={"known": ["anne", "bruno"]}, follow_redirects=False)
    assert r.status_code == 303
    assert people(app, c) == {"Anne": ("06 33 33 33 33", "anne@exemple.fr", False, ["plongeur"]),
                              "Bruno": ("06 22 22 22 22", "", True, ["accompagnant"])}
    page = cl.get(f"/v/{c}/participants").text                 # plus proposés une fois ajoutés
    known = page.split('class="known-list"')[1].split("</form>")[0]
    assert 'value="anne"' not in known and 'value="chloe"' in known
    # rien de coché : message, rien d'ajouté
    r = cl.post(f"/v/{c}/participants/reprendre", data={}, follow_redirects=True)
    assert "Coche au moins une personne" in r.text and len(people(app, c)) == 2
    # les voyages d'origine n'ont pas bougé
    assert people(app, a)["Anne"][:2] == ("06 11 11 11 11", "anne@exemple.fr")


def test_typed_known_name_gets_contacts(env):
    """Nom tapé à la main (accents et majuscules indifférents) : ses contacts sont repris s'ils sont vides ;
    ce qu'on a saisi l'emporte."""
    app, ops, a, b, c = env
    cl = TestClient(app)
    cl.post(f"/v/{c}/participants", data={"name": "ANNE", "phone": "", "email": "", "note": ""})
    cl.post(f"/v/{c}/participants", data={"name": "David", "phone": "07 00 00 00 00", "email": "", "note": ""})
    cl.post(f"/v/{c}/participants", data={"name": "Zoé", "phone": "", "email": "", "note": ""})   # inconnue
    p = people(app, c)
    assert p["ANNE"][:2] == ("06 33 33 33 33", "anne@exemple.fr")
    assert p["David"][:2] == ("07 00 00 00 00", "david@exemple.fr")
    assert p["Zoé"][:2] == ("", "")


def test_ai_add_known_name(env):
    """Ajout par l'IA : mêmes reprises ; un « accompagnant » donné explicitement l'emporte."""
    app, ops, a, b, c = env
    ops.add_participants(c, ["Bruno", dict(nom="Anne", accompagnant=True), dict(nom="David", email="d@autre.fr")])
    p = people(app, c)
    assert p["Bruno"][:3] == ("06 22 22 22 22", "", True)          # accompagnant, comme sur A
    assert p["Anne"][:3] == ("06 33 33 33 33", "anne@exemple.fr", True)
    assert p["David"][:2] == ("", "d@autre.fr")


# ---------------------------------------------------------------- les contacts dans le mail et les PDF
@pytest.fixture
def billed(env):
    app, ops, a, b, c = env
    ops.add_expense(a, "Gîte", categorie="Logement", montant_reel="300")
    ops.withdraw(a, "Chloé")
    return app, ops, a


def test_treasurer_mail_contacts(billed):
    app, ops, a = billed
    with app.state.db.Session() as s:
        t = s.get(M.Trip, a)
        _, body = S.treasurer_mail(t, S.compute_trip(t))
    block = body.split("CONTACTS DES PARTICIPANTS\n")[1].split("\n\n")[0].splitlines()
    assert block == ["- Anne : 06 11 11 11 11 · anne@exemple.fr",
                     "- Bruno : 06 22 22 22 22 · (pas d'e-mail)",
                     "- Chloé (désistement) : (aucun contact)"]
    assert body.index("CONTACTS DES PARTICIPANTS") > body.index("UNE FOIS TOUT RÉGLÉ")
    assert body.index("CONTACTS DES PARTICIPANTS") < body.index("Le bilan détaillé (PDF)")


def test_pdf_contacts(billed):
    app, ops, a = billed
    with app.state.db.Session() as s:
        t = s.get(M.Trip, a)
        bilan = norm(pdf_text(X.trip_pdf(t)))
        assert "Nom et contact" in bilan
        assert re.search(r"Anne\s*\n\s*06 11 11 11 11\s*\n\s*anne@exemple\.fr", bilan)
        assert "06 22 22 22 22" in bilan
        r = S.compute_trip(t)
        key = next(x.key for x in r.accounts if x.name == "Anne")
        data, _ = X.account_pdf(t, key)
        assert "06 11 11 11 11 · anne@exemple.fr" in norm(pdf_text(data))
        key = next(x.key for x in r.accounts if x.name == "Chloé")      # sans contact : pas de ligne vide
        head = norm(pdf_text(X.account_pdf(t, key)[0])).split("Compte de Chloé")[1].split("En bref")[0]
        assert [x for x in head.strip().splitlines()][1:] == ["Calculé sur les montants réels, le "
                                                              f"{__import__('datetime').date.today():%d/%m/%Y}."]


def test_browser_pick_and_autofill(env):
    """Dans le navigateur : recherche, « Tout le groupe », compteur du bouton, ajout ; nom tapé → contacts remplis.
    Clair, sombre, téléphone : pas de débordement. Captures dans TRIPCOMPTA_SHOTS si défini."""
    import os
    import threading
    import time
    from pathlib import Path

    import uvicorn
    from playwright.sync_api import sync_playwright
    from tests.conftest import _free_port
    app, ops, a, b, c = env
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    while not srv.started:
        time.sleep(0.05)
    shots = os.environ.get("TRIPCOMPTA_SHOTS")
    url = f"http://127.0.0.1:{port}/v/{c}/participants"
    try:
        with sync_playwright() as p:
            br = p.chromium.launch()
            for name, w, dark in (("clair", 1280, False), ("sombre", 1280, True), ("mobile", 390, False)):
                pg = br.new_page(viewport={"width": w, "height": 900}, color_scheme="dark" if dark else "light")
                errs = []
                pg.on("pageerror", lambda e: errs.append(str(e)))
                pg.goto(url)
                assert pg.locator("#ajout").get_attribute("open") is not None          # voyage vide : ouvert
                assert pg.evaluate("document.documentElement.scrollWidth") <= w + 1
                if shots:
                    Path(shots).mkdir(parents=True, exist_ok=True)
                    pg.locator("#ajout").screenshot(path=f"{shots}/reprendre-{name}.png")
                assert errs == []
                pg.close()
            pg = br.new_page(viewport={"width": 1280, "height": 900})
            pg.goto(url)
            btn = pg.locator("[data-known-submit]")
            assert btn.is_disabled()
            pg.get_by_role("button", name="Tout le groupe « Voyage B » (2)").click()
            assert btn.inner_text() == "Ajouter les 2 personnes cochées" and not btn.is_disabled()
            pg.get_by_role("button", name="Tout le groupe « Voyage B » (2)").click()      # 2e clic : décoche
            assert btn.is_disabled()
            pg.get_by_placeholder("Chercher un nom…").fill("chloe")                       # sans accent
            assert pg.locator(".known-list label:visible").count() == 1
            pg.locator(".known-list label:visible input").check()
            assert btn.inner_text() == "Ajouter la personne cochée"
            pg.get_by_placeholder("Chercher un nom…").fill("zz")
            assert pg.get_by_text("Personne ne porte ce nom.").is_visible()
            btn.click()
            pg.wait_for_load_state()
            assert "Chloé ajouté" in pg.locator(".flash").inner_text()
            # nom déjà connu tapé dans le formulaire : contacts et accompagnant remplis
            pg.goto(url + "?ajout=1#ajout")
            pg.locator("#a-name").fill("Bruno")
            pg.locator("#a-name").dispatch_event("change")
            assert pg.locator("#a-phone").input_value() == "06 22 22 22 22"
            assert pg.locator("#add-form input[name=r_accompagnant]").is_checked()
            pg.locator("#add-form").get_by_role("button", name="Ajouter", exact=True).click()
            pg.wait_for_load_state()
            br.close()
    finally:
        srv.should_exit = True
        th.join(5)
    assert people(app, c)["Bruno"][:3] == ("06 22 22 22 22", "", True)
    assert "Chloé" in people(app, c)
