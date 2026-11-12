"""Captures d'écran de « Préparer un voyage », pour les regarder une par une.

    .venv/bin/python tests/captures_preparation.py <dossier des captures>

Tout se passe dans un dossier de données temporaire (jamais les vrais voyages) et dans un Chromium sans fenêtre :
rien ne s'ouvre à l'écran, rien ne prend la main sur ce que tu fais.
"""
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from test_preparation_ecran import Screen  # noqa: E402


def serve(app):
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}", srv


def main(out: Path):
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, locale="fr-FR", device_scale_factor=2)
        page = ctx.new_page()
        page.on("dialog", lambda d: d.accept())
        sc = Screen(Path(tmp))
        base, srv = serve(sc.app)

        def shot(name, url=None, full=True, pg=None):
            pg = pg or page
            if url:
                pg.goto(base + url)
            if full:
                pg.evaluate("window.scrollTo(0, 0)")      # sinon la barre du haut apparaît au milieu de l'image
            pg.wait_for_timeout(350)
            pg.screenshot(path=str(out / f"{name}.png"), full_page=full)
            print(out / f"{name}.png")

        shot("01-premiere-utilisation", "/")
        page.click("summary:has-text('Prépare-le d')")
        shot("01b-premiere-utilisation-ouvert")
        sc.post("/voyages", dict(name="Hendaye, mai 2027", caisse="asso", place="Hendaye"))
        shot("02-accueil-sans-projet", "/")
        page.goto(base + "/?onglet=preparer")                   # sans projet, l'accueil s'ouvre sur « Faire la compta »
        page.click("button:has-text('+ Préparer un voyage')")
        shot("02b-accueil-panneau-preparer")
        sc.create(name="Week-end Annecy", people="6", place="", start="2027-06-12", end="2027-06-14")
        shot("03-projet-vide", sc.url)
        sc.add_item("Gîte")
        sc.add_item("Repas")
        sc.reference()
        shot("04-accueil-avec-projets", "/")
        shot("05-projet-de-reference", sc.url)
        shot("06-offre-pour-un-nombre-de-personnes", f"{sc.url}/offres/{sc.offer('Taxi')}/modifier")
        h, d = sc.item("Hôtel"), sc.item("Plongées")
        sc.post(f"{sc.url}/offres/{sc.offer('Forfait Club A')}",
                dict(label="Forfait Club A", price="580", counting="personne", quantity="1", items=[str(h), str(d)],
                     split="montant", note="Hôtel au club, 10 plongées encadrées", **{f"share_{h}": "300", f"share_{d}": "280"}))
        shot("07-forfait-avec-repartition", f"{sc.url}/offres/{sc.offer('Forfait Club A')}/modifier")
        page.check("input[name=split][value=pourcent]")
        page.fill(f"#share_{h}", "55")
        page.fill(f"#share_{d}", "40")
        shot("07b-forfait-repartition-en-cours", full=True)
        sc.choose("Forfait Club A")
        sc.post(f"{sc.url}/marge", dict(margin_kind="pourcent", margin_value="5"))
        sc.post(f"{sc.url}/prix-annonce", dict(announced="950"))
        shot("08-forfait-retenu", sc.url)
        sc.post(f"{sc.url}/prix-annonce", dict(announced="800"))
        page.goto(base + sc.url)
        page.locator("#resultat").scroll_into_view_if_needed()
        shot("09-alerte-il-manque", full=False)
        page.goto(base + f"{sc.url}/offres/nouvelle?poste={sc.item('Taxi')}")
        page.fill("#label", "Navette")
        page.fill("#price", "45")
        page.check("input[name=counting][value=nombre]")
        page.fill("#capacity", "0")
        page.evaluate("document.querySelector('#capacity').removeAttribute('min')")
        page.click("button:has-text(\"Ajouter l'offre\")")
        page.wait_for_selector("#champ-refuse")
        shot("10-formulaire-refuse", full=False)
        sc.post(f"{sc.url}/prix-annonce", dict(announced="950"))
        narrow = browser.new_context(viewport={"width": 390, "height": 844}, locale="fr-FR", device_scale_factor=2).new_page()
        shot("11-projet-fenetre-etroite", sc.url, pg=narrow)
        shot("12-offre-fenetre-etroite", f"{sc.url}/offres/{sc.offer('Forfait Club A')}/modifier", pg=narrow)
        shot("13-accueil-fenetre-etroite", "/", pg=narrow)
        sc.post(f"{sc.url}/offres/{sc.offer('Taxi')}/ne-plus-retenir")
        sc.post(f"{sc.url}/offres/{sc.offer('Transavia')}/ne-plus-retenir")
        shot("14-budget-incomplet", sc.url)

        # ---- tranche 3 : échéances, « À faire bientôt », lancement, IA (aujourd'hui fixé au 10/01/2027)
        import datetime as dt
        import json
        from compta import models as M
        from compta import writes as W
        sc.app.state.aujourdhui = dt.date(2027, 1, 10)
        sc.post(f"{sc.url}/offres/{sc.offer('Transavia')}/retenir")
        for offer, label, day, amount in (("Forfait Club A", "Option à confirmer", "2027-01-08", ""),
                                          ("Forfait Club A", "Acompte à verser", "2027-01-25", "1000"),
                                          ("Minibus", "Reste à payer", "2027-01-10", "600"),
                                          ("Transavia", "Prix garanti jusqu'à cette date", "2027-01-11", ""),
                                          ("Hôtel X", "Acompte à verser", "2027-01-12", "500")):
            sc.post(f"{sc.url}/offres/{sc.offer(offer)}/echeances", dict(label=label, date=day, amount=amount))
        page.goto(base + sc.url)
        page.locator(f"#poste-{sc.item('Hôtel')}").screenshot(path=str(out / "15-offre-avec-echeances.png"))
        print(out / "15-offre-avec-echeances.png")
        page.locator("#a-faire").screenshot(path=str(out / "16-a-faire-bientot-projet.png"))
        print(out / "16-a-faire-bientot-projet.png")
        with sc.db.Session() as s:
            t = s.query(M.Trip).first()
            a = W.add_participant(s, t, "Anne Martin")
            b = W.add_participant(s, t, "Bruno Petit")
            c = W.add_participant(s, t, "Chloé Roux")
            W.save_expense(s, t, label="Gîte", planned=90000, weights={a.id: 1, b.id: 1, c.id: 1})
            W.set_pay_dates(t, [("Acompte", dt.date(2027, 1, 5), 10000)])
            s.commit()
        shot("17-a-faire-bientot-accueil", "/", full=False)
        shot("18-confirmation-lancement", f"{sc.url}/lancer")
        sc.post(f"{sc.url}/offres/{sc.offer('Taxi')}/ne-plus-retenir")
        shot("18b-confirmation-lancement-poste-sans-offre", f"{sc.url}/lancer")
        r = sc.post(f"{sc.url}/lancer", dict(caisse="asso", asso_name="Club Subaqua"))
        tid = int(r.headers["location"].split("?")[0].rsplit("/", 1)[1])
        shot("19-projet-apres-lancement", sc.url, full=False)
        shot("20-depenses-du-voyage-cree", f"/v/{tid}/depenses")
        shot("20b-ou-j-en-suis-du-voyage-cree", f"/v/{tid}", full=False)
        page.goto(base + f"{sc.url}/ia")
        page.fill("#texte", "Voici ma proposition.\n```json\n" + json.dumps({
            "projet_id": sc.pid, "offres": [{"prestataire": "Navette", "prix": 45, "compte": "pour un nombre de personnes",
                                             "pour_combien": 8, "postes": ["Taxi"], "retenue": True,
                                             "echeances": [{"libelle": "Acompte à verser", "date": "2027-01-20",
                                                            "montant": 30}]}]}, ensure_ascii=False) + "\n```")
        page.click("button:has-text('Voir ce qui changera')")
        page.wait_for_selector("#resultat")
        shot("21-page-ia-du-projet")
        shot("22-projet-echeances-fenetre-etroite", sc.url, pg=narrow)

        # ---- tranche 4 : programme du séjour et enveloppe repas
        sc.create(name="Week-end au lac", people="6", place="")
        shot("23-programme-vide", f"{sc.url}/programme", full=False)
        sc.post(f"{sc.url}/duree", dict(days="3"))
        for d, txt in ((1, "Arrivée et pique-nique"), (2, "Randonnée"), (3, "Retour")):
            sc.post(f"{sc.url}/programme", dict(day=str(d), text=txt))
        shot("28-programme-sans-dates", f"{sc.url}/programme")
        sc.reference()
        sc.post(f"{sc.url}/modifier", dict(name="Mer Rouge 2027", place="Hurghada", start="2027-05-01", end="2027-05-07"))
        sc.post(f"{sc.url}/repas", dict(meal_per_day="35"))
        sc.post(f"{sc.url}/jours/1/repas", {})
        sc.post(f"{sc.url}/jours/7/repas", {})
        sc.post(f"{sc.url}/jours/4/repas", dict(on="1", amount="50"))
        lines = [dict(day="1", text="Vol aller Paris → Hurghada", time="7h30", item=str(sc.item("Avion"))),
                 dict(day="1", text="Taxi jusqu'à l'hôtel", time="14h", item=str(sc.item("Taxi"))),
                 dict(day="2", until="6", text="Deux plongées chaque matin", item=str(sc.item("Plongées")),
                      note="Rendez-vous au club à 8 h"),
                 dict(day="2", text="Installation et briefing"), dict(day="3", text="Après-midi libre"),
                 dict(day="4", text="Dîner au restaurant du port", note="Repas plus cher ce soir-là"),
                 dict(day="5", text="Excursion en minibus", item=str(sc.item("Minibus"))),
                 dict(day="6", text="Journée sans plongée avant le vol"), dict(day="7", text="Vol retour", time="10h")]
        for x in lines:
            sc.post(f"{sc.url}/programme", x)
        shot("24-programme-7-jours", f"{sc.url}/programme")
        page.goto(base + f"{sc.url}/programme")
        page.locator("#repas").screenshot(path=str(out / "25-enveloppe-repas.png"))
        page.locator("#jour-4").screenshot(path=str(out / "25b-jour-4-repas-change.png"))
        print(out / "25-enveloppe-repas.png")
        page.goto(base + sc.url)
        page.locator("#resultat").screenshot(path=str(out / "26-carte-dont-repas.png"))
        print(out / "26-carte-dont-repas.png")
        shot("27-confirmation-lancement-repas", f"{sc.url}/lancer")
        shot("29-programme-fenetre-etroite", f"{sc.url}/programme", pg=narrow)
        page.goto(base + f"{sc.url}/ia")
        page.fill("#texte", json.dumps({"projet_id": sc.pid, "programme": [
            {"jour": 3, "heure": "18h", "texte": "Apéritif au coucher du soleil"},
            {"jour": 5, "texte": "Snorkeling l'après-midi", "note": "Masques fournis par le club"}],
            "repas": {"montants": {"5": 40}}}, ensure_ascii=False))
        page.click("button:has-text('Voir ce qui changera')")
        page.wait_for_selector("#resultat")
        shot("30-page-ia-programme")

        # ---- thème sombre et fenêtre étroite, regardés à chaque fois (tranche 5)
        page.goto(base + f"{sc.url}/programme")
        page.locator(".plan-line-actions button[data-toggle-row]").first.click()
        page.mouse.move(0, 0)
        page.locator(".plan-line:has(.plan-line-form:not([hidden]))").first.screenshot(path=str(out / "31-ligne-programme-modifier-ouvert.png"))
        print(out / "31-ligne-programme-modifier-ouvert.png")
        dark = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme="dark", locale="fr-FR",
                                   device_scale_factor=2).new_page()
        for name, url in (("32-sombre-accueil", "/"), ("33-sombre-projet", sc.url), ("34-sombre-programme", f"{sc.url}/programme"),
                          ("35-sombre-ia-projet", f"{sc.url}/ia"), ("36-sombre-ia-voyage", f"/v/{tid}/ia"),
                          ("38-sombre-offre", f"{sc.url}/offres/{sc.offer('Taxi')}/modifier"),
                          ("39-sombre-lancer", f"{sc.url}/lancer")):
            shot(name, url, full=False, pg=dark)
        shot("37-projet-fenetre-etroite-finitions", sc.url, pg=narrow)
        # ---- menu de gauche (tranche 6) : offre et lancement, grand écran et fenêtre étroite
        shot("40-offre-avec-menu", f"{sc.url}/offres/{sc.offer('Forfait Club A')}/modifier", full=False)
        shot("41-lancer-avec-menu", f"{sc.url}/lancer", full=False)
        shot("42-offre-avec-menu-etroite", f"{sc.url}/offres/{sc.offer('Taxi')}/modifier", full=False, pg=narrow)
        shot("43-programme-avec-menu-etroite", f"{sc.url}/programme", full=False, pg=narrow)
        # ---- vue Tableau du programme (tranche 7)
        shot("44-programme-tableau", f"{sc.url}/programme?vue=tableau")
        page.goto(base + f"{sc.url}/programme?vue=jour")
        page.locator("#vue-choix").screenshot(path=str(out / "45-choix-de-la-vue.png"))
        print(out / "45-choix-de-la-vue.png")
        shot("46-programme-tableau-sombre", f"{sc.url}/programme?vue=tableau", pg=dark)
        shot("47-programme-tableau-etroit", f"{sc.url}/programme?vue=tableau", pg=narrow)
        # ---- accueil en deux onglets et projet d'exemple
        shot("48-accueil-onglet-preparer", "/?onglet=preparer", full=False)
        shot("49-accueil-onglet-compta", "/?onglet=compta", full=False)
        r = sc.post("/preparer/exemple/ouvrir")
        demo_url = r.headers["location"].split("?")[0]
        shot("50-accueil-avec-exemple", "/?onglet=preparer", full=False)
        shot("51-projet-exemple", demo_url, full=False)
        shot("52-projet-exemple-programme-tableau", f"{demo_url}/programme?vue=tableau")
        shot("53-accueil-fenetre-etroite", "/?onglet=preparer", pg=narrow)
        shot("54-accueil-sombre", "/?onglet=preparer", full=False, pg=dark)
        shot("55-accueil-compta-sombre", "/?onglet=compta", full=False, pg=dark)
        # ---- « Ce que ça donne » en deux colonnes, formulaire d'une ligne du programme (critique Impeccable)
        for name, pg in (("56-exemple-resultat", page), ("57-exemple-resultat-sombre", dark)):
            pg.goto(base + demo_url)
            pg.locator("#resultat").screenshot(path=str(out / f"{name}.png"))
            print(out / f"{name}.png")
        shot("58-exemple-fenetre-etroite", demo_url, full=False, pg=narrow)
        for name, pg in (("59-exemple-ligne-modifier", page), ("60-exemple-ligne-modifier-sombre", dark)):
            pg.goto(base + f"{demo_url}/programme?vue=jour")
            pg.locator("#jour-1 .plan-line-actions button[data-toggle-row]").nth(1).click()
            pg.mouse.move(0, 0)
            pg.locator("#jour-1").screenshot(path=str(out / f"{name}.png"))
            print(out / f"{name}.png")
        shot("61-exemple-ligne-modifier-etroite", f"{demo_url}/programme?vue=jour", full=False, pg=narrow)
        srv.should_exit = True
        browser.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(Path(sys.argv[1]).expanduser())
