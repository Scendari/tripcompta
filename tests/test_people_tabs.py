"""Page Participants en 3 onglets : Liste · Organisation (équipe, familles, groupes) · Comptes."""
import re

import pytest
from fastapi.testclient import TestClient

from compta.ops import Ops
from compta import services as S
from compta.web import create_app


def clean(t):
    import html
    return re.sub(r"\s+", " ", html.unescape(t).replace(" ", " ").replace("\xa0", " "))


@pytest.fixture
def env(tmp_path):
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.import_trip({"voyage": {"nom": "x"}, "participants": ["Anne", "Bob", "Chloé", "Denis"],
                           "depenses": [{"libelle": "Gîte", "reel": "400", "paye_par": "asso", "pour": "tous"}]})["voyage_id"]
    P = {p["nom"]: p["id"] for p in ops.trip(tid)["participants"]}
    return app, ops, tid, TestClient(app), P


def test_three_tabs_everywhere(env):
    app, ops, tid, c, P = env
    for url, on in (("/participants", "Liste"), ("/participants/organisation", "Organisation"),
                    ("/participants/soldes", "Comptes"), ("/participants/comptes", "Détail des comptes"),
                    ("/participants/avances", "Payé / reçu")):
        page = clean(c.get(f"/v/{tid}{url}").text)
        assert 'class="subtabs"' in page
        assert re.search(rf'class="on" aria-current=page[^>]*>{re.escape(on)}[ <]', page), url
    liste = clean(c.get(f"/v/{tid}/participants").text)
    assert liste.index("Ajouter un participant") < liste.index('class="people-table"')       # ajout au-dessus
    assert "join_" not in liste and "dépenses de groupe déjà saisies" not in liste


def test_edit_person_from_list_with_groups_and_roles(env):
    app, ops, tid, c, P = env
    c.post(f"/v/{tid}/groupes", data={"name": "Minibus", "members": [P["Anne"]]})
    c.post(f"/v/{tid}/participants/{P['Bob']}", data={"name": "Bob", "phone": "0611", "email": "", "note": "",
                                                     "family": "", "role": "plongeur", "roles_form": "1", "r_dp": "1",
                                                     "groups_form": "1", "groups": ["Minibus"]})
    d = ops.trip(tid)
    assert d["groupes"] == {"Minibus": ["Anne", "Bob"]} and d["equipe"]["dp"] == ["Bob"]
    c.post(f"/v/{tid}/participants/{P['Anne']}", data={"name": "Anne", "groups_form": "1"})   # plus aucun groupe
    assert ops.trip(tid)["groupes"] == {"Minibus": ["Bob"]}


def test_families_tab(env):
    app, ops, tid, c, P = env
    r = c.post(f"/v/{tid}/familles", data={"name": "Famille Martin", "members": [P["Anne"], P["Bob"]],
                                           "referent": P["Bob"]}, follow_redirects=True)
    assert "Famille Martin créée" in clean(r.text)
    fam = ops.trip(tid)["familles"][0]
    assert fam["nom"] == "Famille Martin" and fam["paie"] == "Bob" and fam["membres"] == ["Anne", "Bob"]
    assert fam["sa_part"] == 200.0                        # 2 × 100 €
    fid = re.search(r'action="/v/\d+/familles/(\d+)"', c.get(f"/v/{tid}/participants/organisation").text).group(1)
    # ajouter Chloé, Anne paie maintenant
    c.post(f"/v/{tid}/familles/{fid}", data={"name": "Martin", "members": [P["Anne"], P["Bob"], P["Chloé"]],
                                             "referent": P["Anne"]})
    fam = ops.trip(tid)["familles"][0]
    assert fam["membres"] == ["Anne", "Bob", "Chloé"] and fam["paie"] == "Anne"
    # une famille d'une seule personne est refusée
    r = c.post(f"/v/{tid}/familles", data={"name": "Roy", "members": [P["Denis"]]}, follow_redirects=True)
    assert "au moins 2 personnes" in clean(r.text)
    c.post(f"/v/{tid}/familles/{fid}/supprimer")
    assert ops.trip(tid)["familles"] == []
    assert all(not p.get("famille") for p in ops.trip(tid)["participants"])


def test_team_tab(env):
    app, ops, tid, c, P = env
    r = c.post(f"/v/{tid}/roles", data={"organisateur": P["Anne"], f"co_{P['Bob']}": "1", f"dp_{P['Chloé']}": "1"},
               follow_redirects=True)
    assert "Équipe enregistrée" in clean(r.text)
    assert ops.trip(tid)["equipe"] == {"organisateur": "Anne", "co_organisateurs": ["Bob"], "dp": ["Chloé"]}


def test_accounts_tab_filter_and_old_link(env):
    app, ops, tid, c, P = env
    page = c.get(f"/v/{tid}/participants/comptes").text
    assert page.count('class="person-block"') == 4
    assert page.count('type="checkbox" value=') == 4 and page.count("checked><span>") == 4
    one = c.get(f"/v/{tid}/participants/comptes?p={P['Bob']}").text
    assert one.count("checked><span>") == 1 and f'value="{P["Bob"]}" checked' in one
    assert "var fromUrl = true" in one
    # ancien lien de fiche : redirige vers l'onglet filtré, en gardant le message
    r = c.get(f"/v/{tid}/p/{P['Bob']}?msg=Bravo", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/v/{tid}/participants/comptes?p={P['Bob']}&msg=Bravo")
    assert "Coordonnées" not in page                      # plus de coordonnées dans les comptes


def test_browser_filter_and_mobile(env):
    import socket
    import threading
    import time
    import uvicorn
    from playwright.sync_api import sync_playwright
    app, ops, tid, c, P = env
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
            base = f"http://127.0.0.1:{port}/v/{tid}/participants"
            pg.goto(base + "/comptes")
            assert pg.locator(".person-block:visible").count() == 4
            # la barre d'onglets ne « bouge » pas verticalement (pas de débordement vertical)
            assert pg.evaluate("(() => { const n = document.querySelector('.subtabs'); return n.scrollHeight <= n.clientHeight; })()")
            pg.click("[data-people=none]")
            assert pg.locator(".person-block:visible").count() == 0 and pg.is_visible("#people-empty")
            pg.click(".people-filter label:has-text('Bob')")
            pg.click(".people-filter label:has-text('Denis')")
            assert pg.locator(".person-block:visible").count() == 2
            assert "p=" in pg.url
            pg.goto(base + "/comptes")                     # le choix est retenu
            assert pg.locator(".person-block:visible").count() == 2
            # liste : « Modifier » ouvre la ligne sur place
            pg.goto(base)
            pg.locator("tr", has=pg.locator(".name-btn b", has_text="Anne")).first.locator("button", has_text="Modifier").click()
            assert pg.locator("tr.edit-row:visible input[name=phone]").count() == 1
            for url in ("", "/organisation", "/comptes"):
                m = b.new_page(viewport={"width": 390, "height": 800})
                m.goto(base + url)
                assert m.evaluate("document.documentElement.scrollWidth") <= 391, url
            assert errors == []
            b.close()
    finally:
        srv.should_exit = True
        th.join(5)


def test_add_one_person_with_everything(env):
    app, ops, tid, c, P = env
    c.post(f"/v/{tid}/groupes", data={"name": "Minibus", "members": [P["Anne"]]})
    r = c.post(f"/v/{tid}/participants", data={"name": "Émile Roux", "phone": "06 01", "email": "e@x.fr",
                                               "note": "niveau 2", "family": "Roux", "role": "plongeur",
                                               "r_dp": "1", "r_co-organisateur": "1", "groups": ["Minibus"]})
    assert r.status_code == 200 or r.status_code == 303
    loc = c.post(f"/v/{tid}/participants", data={"name": "Fanny Roux", "family": "Roux", "role": "accompagnant"},
                 follow_redirects=False).headers["location"]
    assert "ajout=1" not in loc and "#pe" in loc          # le formulaire se referme ; la liste revient
    page = clean(c.get(loc).text)
    assert "Fanny Roux ajouté (accompagnant) dans la famille Roux" in page
    assert "pense à le cocher dans « Pour qui ? »" in page
    d = ops.trip(tid)
    e = next(p for p in d["participants"] if p["nom"] == "Émile Roux")
    assert (e["telephone"], e["email"], e["note"], e["famille"]) == ("06 01", "e@x.fr", "niveau 2", "Roux")
    assert sorted(e["roles"]) == ["co-organisateur", "dp"] and d["groupes"]["Minibus"] == ["Anne", "Émile Roux"]
    # pas inclus dans les dépenses déjà saisies : sa part est nulle
    assert e["sa_part"] == 0 and next(p for p in d["participants"] if p["nom"] == "Anne")["sa_part"] == 100.0


def test_add_form_chips_family_and_groups(env):
    app, ops, tid, c, P = env
    page = c.get(f"/v/{tid}/participants").text
    assert "Il ne sera compté dans aucune dépense" not in page
    assert 'name="family_pick" value="" checked' in page and "Aucun groupe pour l'instant" not in page
    # nouvelle famille via « + Nouvelle… »
    c.post(f"/v/{tid}/participants", data={"name": "Paul Lebrun", "family_pick": "__new__", "family_new": "Lebrun",
                                           "role": "plongeur", "roles_form": "1"})
    c.post(f"/v/{tid}/groupes", data={"name": "Minibus", "members": [P["Anne"]]})
    page = c.get(f"/v/{tid}/participants").text
    assert 'name="family_pick" value="Lebrun"' in page and 'name="groups" value="Minibus"' in page
    # famille existante choisie par sa pastille, accompagnant
    c.post(f"/v/{tid}/participants", data={"name": "Léa Lebrun", "family_pick": "Lebrun", "role": "accompagnant",
                                           "roles_form": "1", "groups_form": "1", "groups": ["Minibus"]})
    d = ops.trip(tid)
    lea = next(p for p in d["participants"] if p["nom"] == "Léa Lebrun")
    assert lea["famille"] == "Lebrun" and lea["accompagnant"] and d["groupes"]["Minibus"] == ["Anne", "Léa Lebrun"]
    # « Modifier » : retirer de la famille (« Aucune »)
    c.post(f"/v/{tid}/participants/{lea['id']}", data={"name": "Léa Lebrun", "family_pick": "", "role": "accompagnant",
                                                       "roles_form": "1", "groups_form": "1", "groups": ["Minibus"]})
    assert not next(p for p in ops.trip(tid)["participants"] if p["nom"] == "Léa Lebrun").get("famille")


def test_nav_comptes_tab_does_not_light_ledger(env):
    app, ops, tid, c, P = env
    page = c.get(f"/v/{tid}/participants/comptes").text
    assert "📒" not in page                                       # plus d'entrée « Tableau des comptes » dans le menu
    assert re.search(r'href="/v/\d+/participants" class="on">', page)
    assert re.search(r'href="/v/\d+/participants" class="on">', c.get(f"/v/{tid}/participants/soldes").text)


def test_none_chips_for_roles_and_groups(env):
    app, ops, tid, c, P = env
    page = c.get(f"/v/{tid}/participants").text
    add = page.split('id="add-form"')[1].split("</form>")[0]
    assert add.count("data-none checked") == 2 and "Aucun groupe pour l'instant" not in page
    c.post(f"/v/{tid}/groupes", data={"name": "Minibus", "members": [P["Anne"]]})
    page = c.get(f"/v/{tid}/participants").text
    anne = page.split(f'id="pe{P["Anne"]}"')[1].split("</tr>")[0]
    bob = page.split(f'id="pe{P["Bob"]}"')[1].split("</tr>")[0]
    # rôles : « Plongeur » coché (pas « Aucun ») ; groupes : Minibus pour Anne, « Aucun » pour Bob
    assert 'name="r_plongeur" value="1" data-pair="plonge" checked' in anne and anne.count("data-none checked") == 0
    assert bob.count("data-none checked") == 1


def test_browser_none_chip_is_exclusive(env):
    import socket
    import threading
    import time
    import uvicorn
    from playwright.sync_api import sync_playwright
    app, ops, tid, c, P = env
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
            pg = b.new_page()
            pg.goto(f"http://127.0.0.1:{port}/v/{tid}/participants?ajout=1")
            form = pg.locator("#add-form")
            none = form.locator("[data-none-group]").first.locator("input[data-none]")
            assert none.is_checked()
            form.locator("input[name=r_dp]").check()
            assert not none.is_checked()
            form.locator("input[name=r_dp]").uncheck()
            assert none.is_checked()                      # plus rien : « Aucun » revient
            form.locator("input[name=r_dp]").check()
            none.check()
            assert not form.locator("input[name=r_dp]").is_checked()
            # Plongeur / Accompagnant : l'un ou l'autre
            form.locator("input[name=r_plongeur]").check()
            form.locator("input[name=r_accompagnant]").check()
            assert not form.locator("input[name=r_plongeur]").is_checked() and not none.is_checked()
            b.close()
    finally:
        srv.should_exit = True
        th.join(5)



def test_plongeur_accompagnant_are_roles_like_the_others(env):
    app, ops, tid, c, P = env
    add = c.get(f"/v/{tid}/participants").text.split('id="add-form"')[1].split("</form>")[0]
    labels = re.findall(r"> (Aucun|Plongeur|Accompagnant|Organisateur|Co-organisateur|DP)</label>", add)
    assert labels[:6] == ["Aucun", "Plongeur", "Accompagnant", "Organisateur", "Co-organisateur", "DP"]
    assert 'type="radio" name="role"' not in add
    # accompagnant + DP cochés ensemble
    c.post(f"/v/{tid}/participants", data={"name": "Zoé", "role": "accompagnant", "roles_form": "1", "r_dp": "1"})
    z = next(p for p in ops.trip(tid)["participants"] if p["nom"] == "Zoé")
    assert z["accompagnant"] and z["roles"] == ["dp"]
    # « Aucun » : ni rôle d'équipe ni accompagnant (compté avec les plongeurs)
    c.post(f"/v/{tid}/participants/{z['id']}", data={"name": "Zoé", "roles_form": "1"})
    z = next(p for p in ops.trip(tid)["participants"] if p["nom"] == "Zoé")
    assert not z["accompagnant"] and z["roles"] == []


def test_withdrawal_from_the_list(env):
    app, ops, tid, c, P = env
    page = c.get(f"/v/{tid}/participants").text
    bob = page.split(f'id="pe{P["Bob"]}"')[1].split("</tr>")[0]
    assert f'href="/v/{tid}/desistement?p={P["Bob"]}"' in bob and "Désister…" in bob and "Supprimer du voyage" in bob
    assert bob.index('class="danger-zone"') > bob.index("Enregistrer")          # sous « Enregistrer », à part
    c.post(f"/v/{tid}/desistement", data={"participant": P["Bob"], "absorber": "group", "date": "2026-10-20"})
    page = clean(c.get(f"/v/{tid}/participants").text)
    assert "Désisté</a>" in page and "Bob s'est désisté le 20/10/2026" in page
    r = c.post(f"/v/{tid}/desistement/{P['Bob']}/annuler", data={"next": f"/v/{tid}/participants"},
               follow_redirects=False)
    assert r.headers["location"].startswith(f"/v/{tid}/participants?msg=")
    assert not any(p.get("desiste") for p in ops.trip(tid)["participants"])


def test_edit_an_existing_withdrawal(env):
    """Le désistement se modifie depuis sa page : date, parts gardées, qui reprend le reste — sans doublon."""
    app, ops, tid, c, P = env
    ops.import_trip({"voyage_id": tid, "depenses": [{"libelle": "Bateau", "reel": "80", "pour": "tous"}]},
                    voyage=tid, verifier_id=tid)
    dep = {d["libelle"]: d["id"] for d in ops.trip(tid)["depenses"]}
    c.post(f"/v/{tid}/desistement", data={"participant": P["Bob"], "absorber": "group", "date": "2026-10-20",
                                          "kept": [dep["Gîte"]]})
    bob = lambda: next(p for p in ops.trip(tid)["participants"] if p["nom"] == "Bob")
    assert bob()["sa_part"] == 100.0                          # garde le gîte (400 / 4)
    page = clean(c.get(f"/v/{tid}/desistement?p={P['Bob']}").text)
    assert "Modifier le désistement de Bob" in page and "Enregistrer les modifications" in page
    assert re.search(rf'name="kept" value="{dep["Gîte"]}" checked', c.get(f"/v/{tid}/desistement?p={P["Bob"]}").text)
    assert 'value="2026-10-20"' in c.get(f"/v/{tid}/desistement?p={P['Bob']}").text
    # il garde aussi le bateau, et c'est l'asso qui reprend le reste
    r = c.post(f"/v/{tid}/desistement", data={"participant": P["Bob"], "absorber": "asso", "date": "2026-10-21",
                                              "kept": [dep["Gîte"], dep["Bateau"]]}, follow_redirects=True)
    assert "Désistement de Bob modifié" in clean(r.text)
    assert bob()["sa_part"] == 120.0                          # 100 + 80 / 4
    with app.state.db.Session() as s:
        from compta import models as M
        ws = s.get(M.Trip, tid).withdrawals
        assert len(ws) == 1 and ws[0].absorber == "asso" and str(ws[0].date) == "2026-10-21"
    # l'aperçu compare à la situation actuelle (pas de double désistement)
    pv = c.post(f"/v/{tid}/desistement/apercu", data={"participant": P["Bob"], "absorber": "asso",
                                                      "kept": [dep["Gîte"]]}).text
    assert "100,00" in pv.replace(" ", " ").replace("\xa0", " ")


def test_roles_card_in_organisation(env):
    """Onglet Organisation : une carte Rôles sur le modèle de Familles et Groupes."""
    app, ops, tid, c, P = env
    page = clean(c.get(f"/v/{tid}/participants/organisation").text)
    assert "Équipe" not in page and 'id="h-roles">Rôles' in page
    for role in ("Plongeur", "Accompagnant", "Organisateur", "Co-organisateur", "DP"):
        assert f">{role}</b>" in page
    assert ">Plongeur</b> <span class=\"muted small\">4 pers.</span>" in page          # tous plongeurs par défaut
    assert "Anne, Bob, Chloé, Denis" not in page                # les noms ne sont listés qu'une fois : dans le détail
    # Chloé devient accompagnante : elle quitte les plongeurs
    r = c.post(f"/v/{tid}/roles/accompagnant", data={"members": [P["Chloé"]]}, follow_redirects=True)
    assert "Accompagnant : 1 personne" in clean(r.text)
    d = {p["nom"]: p for p in ops.trip(tid)["participants"]}
    assert d["Chloé"]["accompagnant"] and d["Anne"]["accompagnant"] is False
    with app.state.db.Session() as s:
        from compta import models as M
        assert [p.name for p in S.with_role(s.get(M.Trip, tid), "plongeur")] == ["Anne", "Bob", "Denis"]
    # deux DP, un organisateur (un seul accepté)
    c.post(f"/v/{tid}/roles/dp", data={"members": [P["Anne"], P["Bob"]]})
    r = c.post(f"/v/{tid}/roles/organisateur", data={"members": [P["Anne"], P["Bob"]]}, follow_redirects=True)
    assert "Un seul organisateur" in clean(r.text)
    c.post(f"/v/{tid}/roles/organisateur", data={"members": [P["Denis"]]})
    eq = ops.trip(tid)["equipe"]
    assert eq["dp"] == ["Anne", "Bob"] and eq["organisateur"] == "Denis"


def test_browser_organisation_accordion(env):
    """Organisation : un clic sur un rôle déplie ses membres ; ouvrir un autre rôle referme le premier."""
    import socket
    import threading
    import time
    import uvicorn
    from playwright.sync_api import sync_playwright
    app, ops, tid, c, P = env
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
            pg.goto(f"http://127.0.0.1:{port}/v/{tid}/participants/organisation")
            assert pg.locator("#roles form.group-form:visible").count() == 0
            pg.click("#roles .item-toggle:has-text('Plongeur')")
            plong, dp = "#role-1", "#role-5"                       # formulaires dans l'ordre des rôles
            assert pg.is_visible(plong) and not pg.is_visible(dp)
            pg.click("#roles .item-toggle:has-text('DP')")
            assert pg.is_visible(dp) and not pg.is_visible(plong)
            pg.check(f"{dp} input[value='{P['Bob']}']")
            pg.click(f"{dp} button:has-text('Enregistrer')")
            assert "DP : 1 personne" in pg.locator(".flash").inner_text()
            b.close()
    finally:
        srv.should_exit = True
        th.join(5)



def test_add_and_remove_roles(env):
    """Rôles propres au voyage : ajout, membres, pastilles, « Pour qui ? », IA ; suppression ; rôles prévus remis."""
    app, ops, tid, c, P = env
    r = c.post(f"/v/{tid}/roles-du-voyage", data={"name": "Moniteur"}, follow_redirects=True)
    assert "Rôle « Moniteur » ajouté" in clean(r.text)
    c.post(f"/v/{tid}/roles/perso%3AMoniteur", data={"members": [P["Anne"], P["Bob"]]})
    liste = c.get(f"/v/{tid}/participants").text
    assert "role-perso" in liste and 'name="r_perso:Moniteur"' in liste
    form = c.get(f"/v/{tid}/depenses/nouvelle").text
    assert ">Moniteur</button>" in form
    # l'IA : « pour »: « moniteurs », et un rôle perso sur un participant
    ops.import_trip({"voyage_id": tid, "participants_a_modifier": [{"participant": "Chloé", "roles": ["Moniteur"]}],
                     "depenses": [{"libelle": "Cadeau", "reel": "30", "pour": "moniteurs"}]}, voyage=tid, verifier_id=tid)
    p = {x["nom"]: x for x in ops.trip(tid)["participants"]}
    assert p["Chloé"]["roles"] == ["Moniteur"] and p["Anne"]["sa_part"] == 100.0 + 10.0
    # la fiche « Modifier » garde le rôle perso
    c.post(f"/v/{tid}/participants/{P['Anne']}", data={"name": "Anne", "roles_form": "1", "r_plongeur": "1",
                                                      "r_perso:Moniteur": "1"})
    assert "Moniteur" in next(x for x in ops.trip(tid)["participants"] if x["nom"] == "Anne")["roles"]
    # suppression : retiré de tout le monde
    c.post(f"/v/{tid}/roles-du-voyage/supprimer", data={"role": "perso:Moniteur"})
    assert all("Moniteur" not in x["roles"] for x in ops.trip(tid)["participants"])
    assert ">Moniteur</button>" not in c.get(f"/v/{tid}/depenses/nouvelle").text
    # DP retiré du voyage, puis remis en recréant un rôle « DP »
    c.post(f"/v/{tid}/roles/dp", data={"members": [P["Bob"]]})
    c.post(f"/v/{tid}/roles-du-voyage/supprimer", data={"role": "dp"})
    assert ">DP</b>" not in c.get(f"/v/{tid}/participants/organisation").text and ops.trip(tid)["equipe"]["dp"] == []
    c.post(f"/v/{tid}/roles-du-voyage", data={"name": "dp"})
    assert ">DP</b>" in c.get(f"/v/{tid}/participants/organisation").text
    # plongeur ne se supprime pas
    r = c.post(f"/v/{tid}/roles-du-voyage/supprimer", data={"role": "plongeur"}, follow_redirects=True)
    assert "ne se suppriment pas" in clean(r.text)


def test_no_default_date_today(env):
    """Aucune date d'office : versement, dépense, désistement — la case reste vide, et le mouvement aussi."""
    app, ops, tid, c, P = env
    for url in ("/argent", "/depenses/nouvelle", f"/desistement?p={P['Bob']}"):
        page = c.get(f"/v/{tid}{url}").text
        assert re.search(r'<input type="date"[^>]*name="date" value=""', page), url
    c.post(f"/v/{tid}/argent", data={"kind": "versement", "participant": P["Anne"], "amount": "10", "date": ""})
    assert ops.trip(tid)["mouvements"][-1]["date"] is None
