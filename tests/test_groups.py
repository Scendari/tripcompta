"""Groupes de personnes (covoiturage…) : créés sur la page Participants, boutons dans « Pour qui ? », IA."""
import re

import pytest
from fastapi.testclient import TestClient

from compta import models as M
from compta import services as S
from compta.ops import Ops
from compta.web import create_app


def clean(t):
    import html
    return re.sub(r"\s+", " ", html.unescape(t).replace(" ", " ").replace("\xa0", " "))


@pytest.fixture
def env(tmp_path):
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.import_trip({"voyage": {"nom": "x"}, "participants": ["Anne", "Bob", "Chloé", "Denis"]})["voyage_id"]
    P = {p["nom"]: p["id"] for p in ops.trip(tid)["participants"]}
    return app, ops, tid, TestClient(app), P


def test_create_edit_delete_group_in_participants_page(env):
    app, ops, tid, c, P = env
    r = c.post(f"/v/{tid}/groupes", data={"name": "Minibus", "members": [P["Anne"], P["Bob"], P["Chloé"]]},
               follow_redirects=True)
    assert "Groupe « Minibus » enregistré (3 personnes)" in clean(r.text)
    assert ops.trip(tid)["groupes"] == {"Minibus": ["Anne", "Bob", "Chloé"]}
    page = clean(c.get(f"/v/{tid}/participants/organisation").text)
    assert "<b>Minibus</b> <span class=\"muted small\">3 pers.</span>" in page
    assert '<span class="chip">Minibus</span>' in c.get(f"/v/{tid}/participants").text    # colonne Groupes de la liste
    # renommer et changer les membres
    c.post(f"/v/{tid}/groupes", data={"old_name": "Minibus", "name": "Voiture de Bob", "members": [P["Bob"], P["Denis"]]})
    assert ops.trip(tid)["groupes"] == {"Voiture de Bob": ["Bob", "Denis"]}
    # doublon de nom et mot réservé refusés
    r = c.post(f"/v/{tid}/groupes", data={"name": "voiture de bob", "members": [P["Anne"]]}, follow_redirects=True)
    assert "Il y a déjà un groupe" in clean(r.text)
    r = c.post(f"/v/{tid}/groupes", data={"name": "Plongeurs", "members": [P["Anne"]]}, follow_redirects=True)
    assert "mot réservé" in clean(r.text)
    c.post(f"/v/{tid}/groupes/supprimer", data={"name": "Voiture de Bob"})
    assert ops.trip(tid)["groupes"] == {}


def test_group_button_in_expense_form_and_label(env):
    app, ops, tid, c, P = env
    c.post(f"/v/{tid}/groupes", data={"name": "Minibus", "members": [P["Anne"], P["Bob"], P["Chloé"]]})
    form = c.get(f"/v/{tid}/depenses/nouvelle").text
    ids = ",".join(str(P[n]) for n in sorted(["Anne", "Bob", "Chloé"], key=lambda n: P[n]))
    assert re.search(rf'class="only-btn" data-only="{ids}"[^>]*>Minibus</button>', form)
    # une dépense répartie exactement sur le groupe s'affiche avec son nom
    c.post(f"/v/{tid}/depenses", data={"label": "Essence", "actual": "90", "payer": "asso", "split": "egal",
                                       **{f"inc_{P[n]}": "1" for n in ("Anne", "Bob", "Chloé")}})
    dep = clean(c.get(f"/v/{tid}/depenses").text)
    assert "3 pers." in dep and "<span>Groupe Minibus</span>" in dep          # le nombre ; le groupe dans la bulle
    p = {x["nom"]: x["sa_part"] for x in ops.trip(tid)["participants"]}
    assert p == {"Anne": 30.0, "Bob": 30.0, "Chloé": 30.0, "Denis": 0}


def test_ai_saves_and_reuses_groups(env):
    app, ops, tid, c, P = env
    r = ops.import_trip({"voyage_id": tid, "groupes": {"Minibus": ["Anne", "Bob"]},
                         "depenses": [{"libelle": "Péage", "reel": "20", "pour": "Minibus"}]}, voyage=tid, verifier_id=tid)
    assert any(a.startswith("Groupe « Minibus »") for a in r["actions"])
    # plus tard, sans redonner le groupe
    ops.import_trip({"voyage_id": tid, "depenses": [{"libelle": "Essence", "reel": "40", "pour": "minibus",
                                                      "sauf": ["Bob"]}]}, voyage=tid, verifier_id=tid)
    p = {x["nom"]: x["sa_part"] for x in ops.trip(tid)["participants"]}
    assert p == {"Anne": 50.0, "Bob": 10.0, "Chloé": 0, "Denis": 0}
    from compta.importer import ImportErrors
    with pytest.raises(ImportErrors, match="Groupe « Voiture »"):
        ops.import_trip({"voyage_id": tid, "groupes": {"Voiture": ["Zorro"]}}, voyage=tid, verifier_id=tid)


def test_removed_participant_leaves_groups(env):
    app, ops, tid, c, P = env
    c.post(f"/v/{tid}/groupes", data={"name": "Minibus", "members": [P["Anne"], P["Denis"]]})
    c.post(f"/v/{tid}/participants/{P['Denis']}/supprimer")
    assert ops.trip(tid)["groupes"] == {"Minibus": ["Anne"]}


def test_browser_group_button_checks_exactly_its_members(env):
    import socket
    import threading
    import time
    import uvicorn
    from playwright.sync_api import sync_playwright
    app, ops, tid, c, P = env
    c.post(f"/v/{tid}/groupes", data={"name": "Minibus", "members": [P["Anne"], P["Chloé"]]})
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
            pg.click(".only-btn:has-text('Minibus')")
            checked = pg.eval_on_selector_all("input[data-who]:checked", "els => els.map(e => +e.dataset.pid)")
            assert sorted(checked) == sorted([P["Anne"], P["Chloé"]])
            # en modifiant une dépense déjà « pour le Minibus » : le bouton est allumé dès l'ouverture
            pg.fill("#label", "Essence minibus")
            pg.fill("#actual", "40")
            pg.locator("form button[type=submit], form button:not([type])").filter(has_text="Enregistrer").first.click()
            pg.wait_for_load_state()
            eid = ops.trip(tid)["depenses"][-1]["id"]
            pg.goto(f"http://127.0.0.1:{port}/v/{tid}/depenses/{eid}/modifier")
            assert pg.locator(".only-btn.on").all_inner_texts() == ["Minibus"]
            assert "Seulement pour : Minibus (2 pers.)" in pg.inner_text("[data-only-hint]")
            # une case changée à la main : ce n'est plus le Minibus, le bouton s'éteint
            others = [x for x in P.values() if x not in (P["Anne"], P["Chloé"])]
            pg.check(f"input[data-who][data-pid='{others[0]}']")
            assert pg.locator(".only-btn.on").count() == 0
            pg.uncheck(f"input[data-who][data-pid='{others[0]}']")              # et revient s'il le redevient
            assert pg.locator(".only-btn.on").all_inner_texts() == ["Minibus"]
            # une dépense pour tout le monde : « Tout le monde », pas un groupe
            pg.goto(f"http://127.0.0.1:{port}/v/{tid}/depenses/nouvelle")
            pg.click(".only-btn:has-text('Tout le monde')")
            assert pg.locator(".only-btn.on").all_inner_texts() == ["Tout le monde"]
            pg.goto(f"http://127.0.0.1:{port}/v/{tid}/participants")
            m = b.new_page(viewport={"width": 390, "height": 800})
            m.goto(f"http://127.0.0.1:{port}/v/{tid}/participants")
            assert m.evaluate("document.documentElement.scrollWidth") <= 391
            assert errors == []
            b.close()
    finally:
        srv.should_exit = True
        th.join(5)
