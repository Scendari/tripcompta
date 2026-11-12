"""Préparer un voyage, dans un vrai navigateur (Chromium sans fenêtre : rien ne s'ouvre à l'écran).

Mêmes exigences que les autres pages (test_ux.py) : un <h1>, chaque champ a une étiquette, pas de défilement
horizontal sur un téléphone, les suppressions demandent confirmation, aucune erreur JavaScript ; et ce qui ne se
voit qu'avec le JavaScript : la répartition d'un forfait, l'aperçu en direct, le formulaire refusé qui revient rempli.
"""
import re
import threading
import time

import pytest

from test_preparation_ecran import Screen, clean
from test_ux import JARGON, unlabeled_fields

pw = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    import socket

    import uvicorn
    import datetime as dt
    sc = Screen(tmp_path_factory.mktemp("preparer")).reference()
    sc.app.state.aujourdhui = dt.date(2027, 1, 10)
    sc.choose("Forfait Club A")
    for offer, label, day in (("Forfait Club A", "Option à confirmer", "2027-01-08"),
                              ("Transavia", "Reste à payer", "2027-01-22"), ("Hôtel X", "Acompte", "2027-01-15")):
        sc.post(f"{sc.url}/offres/{sc.offer(offer)}/echeances", dict(label=label, date=day))
    sc.post(f"{sc.url}/duree", dict(days="7"))
    sc.post(f"{sc.url}/repas", dict(meal_per_day="35"))
    sc.post(f"{sc.url}/programme", dict(day="2", until="5", text="Plongées le matin", time="8h30",
                                        items=str(sc.item("Plongées"))))
    sc.create(name="Projet vide", people="4", place="")
    empty_url = sc.url
    sc.url, sc.pid = "/preparer/1", 1
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(sc.app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    sc.base, sc.empty_url = f"http://127.0.0.1:{port}", empty_url
    yield sc
    srv.should_exit = True
    th.join(5)


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def new_page(browser, width=1366, height=900):
    page = browser.new_context(viewport={"width": width, "height": height}, locale="fr-FR").new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.on("console", lambda m: page.errors.append(m.text) if m.type == "error" else None)
    return page


def urls(sc):
    return ["/", sc.url, sc.empty_url, f"{sc.url}/lancer", f"{sc.url}/ia", f"{sc.url}/programme",
            f"{sc.empty_url}/programme", f"{sc.url}/offres/nouvelle?poste={sc.item('Taxi')}",
            f"{sc.url}/offres/{sc.offer('Taxi')}/modifier", f"{sc.url}/offres/{sc.offer('Forfait Club A')}/modifier"]


def test_every_preparation_page_like_the_others(site, browser):
    page = new_page(browser)
    for u in urls(site):
        resp = page.goto(site.base + u)
        assert resp.status == 200, u
        assert page.locator("h1").count() == 1 and page.title(), u
        body = page.locator("main").inner_text()
        assert not JARGON.search(body), (u, JARGON.search(body).group(0))
        assert not unlabeled_fields(page), (u, unlabeled_fields(page))
        empty = page.evaluate("""() => [...document.querySelectorAll('button')]
            .filter(b => b.checkVisibility() && !b.innerText.trim() && !b.title).length""")
        assert empty == 0, u
        bad = page.evaluate("""() => [...document.querySelectorAll('form')]
            .filter(f => /supprimer/.test(f.getAttribute('action') || '') && !f.dataset.confirm)
            .map(f => f.getAttribute('action'))""")
        assert not bad, (u, bad)
    assert not page.errors, page.errors


@pytest.mark.parametrize("width", [390, 768])
def test_narrow_window_no_horizontal_scroll(site, browser, width):
    page = new_page(browser, width=width, height=844)
    for u in urls(site):
        for wide in (False, True):
            page.goto(site.base + u)
            if wide:     # polices de Windows et de Linux, plus larges
                page.add_style_tag(content="*{font-family:Verdana,'DejaVu Sans',sans-serif !important}")
            sw = page.evaluate("document.documentElement.scrollWidth")
            assert sw <= width + 1, (u, wide, sw)
    assert not page.errors, page.errors


def test_home_panels_one_at_a_time(site, browser):
    page = new_page(browser)
    page.goto(site.base + "/")
    assert page.locator("#onglet-preparer").is_visible() and page.locator("#onglet-compta").is_hidden()   # un projet
    page.click("button:has-text('+ Préparer un voyage')")
    assert page.locator("#preparer").is_visible() and not page.locator("#nouveau").is_visible()
    pw.expect(page.locator("#preparer input[name=name]")).to_be_focused()
    page.click("#preparer button:has-text('Fermer')")
    page.click("#tab-compta")                                           # l'autre onglet, sans recharger
    assert page.locator("#onglet-compta").is_visible() and page.locator("#onglet-preparer").is_hidden()
    assert "onglet=compta" in page.url and page.locator("#tab-compta").get_attribute("aria-selected") == "true"
    page.click("button:has-text('+ Nouveau voyage')")
    assert page.locator("#nouveau").is_visible() and not page.locator("#preparer").is_visible()
    page.goto(site.base + "/")                                          # sans paramètre : le dernier onglet revient
    assert page.locator("#onglet-compta").is_visible()
    page.focus("#tab-compta")
    page.keyboard.press("ArrowLeft")                                    # au clavier, comme des onglets
    assert page.locator("#onglet-preparer").is_visible()
    pw.expect(page.locator("#tab-preparer")).to_be_focused()
    # toute la ligne ouvre le projet (ici, un clic sur le nombre de personnes)
    page.goto(site.base + "/?onglet=preparer")
    box = page.locator("tr.plan-row:has-text('Mer Rouge 2027') td.num").first.bounding_box()
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.wait_for_url(re.compile(r"/preparer/1$"))
    assert page.url.endswith("/preparer/1") and "Prix par personne" in page.locator("main").inner_text()
    narrow = new_page(browser, width=390)                               # téléphone : une petite carte par ligne
    narrow.goto(site.base + "/?onglet=preparer")
    assert narrow.evaluate("document.documentElement.scrollWidth") <= 391
    row = narrow.locator("tr.plan-row:has-text('Mer Rouge 2027')")
    assert row.evaluate("e => getComputedStyle(e).display") == "block" and "\n10\n" in row.inner_text()
    assert row.locator("td.num").first.evaluate("e => getComputedStyle(e, '::before').content") == '"Personnes : "'
    assert not page.errors and not narrow.errors, (page.errors, narrow.errors)


def test_split_box_and_what_is_left(site, browser):
    page = new_page(browser)
    page.goto(site.base + f"{site.url}/offres/nouvelle?poste={site.item('Hôtel')}")
    assert page.locator("#repartir").is_hidden()
    page.fill("#label", "Forfait Club C")
    page.fill("#price", "600")
    page.check(f"input[name=items][value='{site.item('Plongées')}']")
    assert page.locator("#repartir").is_visible()
    assert page.locator(".plan-shares").is_hidden()                  # « pas de répartition » par défaut
    page.check("input[name=split][value=montant]")
    assert page.locator(f"#share_{site.item('Hôtel')}").is_visible()
    assert page.locator(f"#share_{site.item('Taxi')}").is_hidden()     # poste pas coché : pas de champ
    page.fill(f"#share_{site.item('Hôtel')}", "350")
    assert page.locator("#reste-a-repartir").inner_text().replace(" ", " ") == "Il reste 250,00 € à répartir."
    page.fill(f"#share_{site.item('Plongées')}", "300")
    assert page.locator("#reste-a-repartir").inner_text() == "C'est 50,00 € de trop."
    page.fill(f"#share_{site.item('Plongées')}", "250")
    assert page.locator("#reste-a-repartir").inner_text() == "✓ Tout est réparti."
    page.check("input[name=split][value=pourcent]")
    page.fill(f"#share_{site.item('Hôtel')}", "55")
    page.fill(f"#share_{site.item('Plongées')}", "40")
    assert page.locator("#reste-a-repartir").inner_text() == "Il reste 5,00 % à répartir."
    page.uncheck(f"input[name=items][value='{site.item('Plongées')}']")
    assert page.locator("#repartir").is_hidden()
    assert not page.errors, page.errors


def test_live_preview_in_the_form(site, browser):
    page = new_page(browser)
    page.goto(site.base + f"{site.url}/offres/nouvelle?poste={site.item('Taxi')}")
    page.fill("#label", "Taxi bis")
    page.fill("#price", "60")
    page.check("input[name=counting][value=nombre]")
    assert page.locator("#capacity").is_visible()
    page.fill("#capacity", "4")
    pw.expect(page.locator("#apercu")).to_contain_text("180,00", timeout=3000)
    text = page.locator("#apercu").inner_text().replace(" ", " ").replace("\xa0", " ")
    assert text == "Pour 10 personnes : 180,00 € au total, soit 18,00 € par personne."
    page.check("input[name=counting][value=groupe]")
    assert page.locator("#capacity").is_hidden()
    pw.expect(page.locator("#apercu")).to_contain_text("6,00", timeout=3000)
    assert not page.errors, page.errors


def test_refused_offer_comes_back_filled(site, browser):
    page = new_page(browser)
    page.on("dialog", lambda d: d.accept())
    taxi = site.item("Taxi")
    page.goto(site.base + f"{site.url}/offres/nouvelle?poste={taxi}")
    page.fill("#label", "Navette")
    page.fill("#price", "45")
    page.check("input[name=counting][value=nombre]")
    page.fill("#capacity", "0")
    page.evaluate("document.querySelector('#capacity').removeAttribute('min')")   # le navigateur l'arrêterait avant
    page.fill("#quantity_label", "trajets")
    page.fill("#note", "Réservée par téléphone")
    page.click("button:has-text(\"Ajouter l'offre\")")
    page.wait_for_selector("#champ-refuse")
    assert "Au moins 1" in page.locator("#champ-refuse").inner_text()
    assert page.locator("#capacity").get_attribute("aria-invalid") == "true"
    assert page.input_value("#label") == "Navette" and page.input_value("#price") == "45"
    assert page.input_value("#quantity_label") == "trajets" and page.input_value("#note") == "Réservée par téléphone"
    assert page.is_checked("input[name=counting][value=nombre]")
    assert page.is_checked(f"input[name=items][value='{taxi}']")
    page.fill("#capacity", "8")
    page.click("button:has-text(\"Ajouter l'offre\")")
    page.wait_for_url(re.compile(r"/preparer/1\?"))
    assert "Offre « Navette » ajoutée" in page.locator(".flash").inner_text()
    # projet : le nombre de participants refusé revient près de son champ
    page.fill("#people", "0")
    page.evaluate("document.querySelector('#people').removeAttribute('min')")
    page.press("#people", "Enter")                                   # plus de bouton : Entrée enregistre
    page.wait_for_selector("#champ-refuse")
    assert "au moins une personne" in page.locator("#champ-refuse").inner_text()
    assert page.input_value("#people") == "0"
    # nettoyage : la Navette est retirée pour les autres essais
    page.goto(site.base + site.url)
    page.locator(f"form[action$='/offres/{site.offer('Navette')}/supprimer'] button").click()
    page.wait_for_url(re.compile(r"/preparer/1\?"))
    assert not page.errors, page.errors


def test_choose_and_undo_by_clicking(site, browser):
    page = new_page(browser)
    page.goto(site.base + site.url)
    budget = page.locator("#budget").inner_text()
    page.locator(f"#offre-{site.offer('Air France')} button:has-text('Retenir')").click()
    page.wait_for_url(re.compile(r"/preparer/1\?"))
    assert "Offre « Air France » retenue (à la place de « Transavia »)" in page.locator(".flash").inner_text()
    assert page.locator("#budget").inner_text() != budget
    page.locator(".flash button:has-text('Défaire')").click()
    page.wait_for_url(re.compile(r"/preparer/1\?"))
    assert "Défait : offre « Air France » retenue." in page.locator(".flash").inner_text()
    assert page.locator("#budget").inner_text() == budget
    page.locator(".flash button:has-text('Rétablir')").click()
    page.wait_for_url(re.compile(r"/preparer/1\?"))
    assert page.locator("#budget").inner_text() != budget
    page.locator(f"#offre-{site.offer('Transavia')} button:has-text('Retenir')").click()     # remis comme avant
    page.wait_for_url(re.compile(r"/preparer/1\?"))
    assert page.locator("#budget").inner_text() == budget
    assert not page.errors, page.errors


def test_cancelled_delete_does_nothing(site, browser):
    page = new_page(browser)
    page.on("dialog", lambda d: d.dismiss())
    page.goto(site.base + site.url)
    page.locator(f"form[action$='/postes/{site.item('Avion')}/supprimer'] button").click()
    page.goto(site.base + site.url)
    assert "Avion" in clean(page.content())
    page.locator("form[action$='/preparer/1/supprimer'] button").click()
    page.goto(site.base + site.url)
    assert page.locator("aside.side .trip-name").inner_text() == "Mer Rouge 2027"


def test_deadline_checkbox_and_ideas(site, browser):
    page = new_page(browser)
    page.goto(site.base + site.url)
    soon = page.locator("#a-faire")
    assert "En retard de 2 jours" in soon.inner_text() and "Dans 12 jours (22/01/2027)" in soon.inner_text()
    # une idée remplit le champ ; l'échéance s'ajoute
    box = page.locator(f"#echeances-{site.offer('Taxi')}-{site.item('Taxi')}")
    box.locator("summary:has-text('+ Ajouter une échéance')").click()
    box.locator("button.chip:has-text('Acompte à verser')").click()
    assert box.locator("input[name=label]").input_value() == "Acompte à verser"
    box.locator("input[name=date]").fill("2027-01-11")
    box.locator("button:has-text('Ajouter')").click()
    page.wait_for_url(re.compile(r"/preparer/1\?"))
    assert "Échéance « Acompte à verser » ajoutée (11/01/2027)" in page.locator(".flash").inner_text()
    assert "Demain" in page.locator("#a-faire").inner_text()
    # cocher « Fait » l'enregistre tout de suite
    page.locator("#a-faire li:has-text('Taxi') input[type=checkbox]").check()
    page.wait_for_url(re.compile(r"#a-faire$"))
    assert "« Acompte à verser » (Taxi) : fait ✓" in page.locator(".flash").inner_text()
    assert "Taxi" not in page.locator("#a-faire").inner_text()
    row = page.locator(f"#echeances-{site.offer('Taxi')}-{site.item('Taxi')} li.plan-dl-row.done")
    assert row.count() == 1 and "line-through" in row.locator(".plan-dl-text").evaluate("e => getComputedStyle(e).textDecorationLine")
    row.locator("input[type=checkbox]").uncheck()                    # décocher : de nouveau à faire
    pw.expect(page.locator(".flash")).to_contain_text("de nouveau à faire")
    assert not page.errors, page.errors


def test_programme_quick_add_and_meal_box(site, browser):
    page = new_page(browser)
    page.goto(site.base + f"{site.url}/programme")
    field = page.locator("#text-d3")
    field.fill("Visite du village")
    field.press("Enter")                                            # Entrée ajoute la ligne (le cas courant)
    page.wait_for_url(re.compile(r"/programme\?.*#jour-3$"))
    assert "Jour 3 : « Visite du village » ajouté" in page.locator(".flash").inner_text()
    assert "Visite du village" in page.locator("#jour-3").inner_text()
    # « plus de détails » : l'heure et la période
    block = page.locator("#jour-6")
    block.locator("summary:has-text('plus de détails')").click()
    block.locator("input[name=text]").fill("Bateau")
    block.locator("input[name=time]").fill("14h")
    block.locator("select[name=until]").select_option("7")
    block.locator("button:has-text('Ajouter')").click()
    pw.expect(page.locator(".flash")).to_contain_text("Jour 6 : « Bateau » ajouté")
    assert "14 h Bateau Jours 6 à 7" in page.locator("#jour-6").inner_text().replace("\n", " ")
    assert "suite : Bateau (jours 6 à 7)" in page.locator("#jour-7").inner_text()
    # la case « Repas » s'enregistre seule
    page.locator("#jour-1 input[name=on]").uncheck()
    pw.expect(page.locator(".flash")).to_contain_text("Jour 1 : pas de repas compté")
    assert "6 jours comptés : 210,00 € par personne" in page.locator("#repas").inner_text().replace("\u202f", " ").replace("\xa0", " ")
    page.locator("#jour-1 input[name=on]").check()
    pw.expect(page.locator(".flash")).to_contain_text("Jour 1 : repas comptés")
    assert not page.errors, page.errors


def test_day_meal_by_keyboard_and_edit_from_programme(site, browser):
    page = new_page(browser)
    page.goto(site.base + f"{site.url}/programme")
    page.fill("#meal-4", "50")
    page.keyboard.press("Tab")                                       # quitter le champ l'enregistre
    pw.expect(page.locator(".flash")).to_contain_text("Jour 4 : repas comptés à 50,00")
    assert page.locator("#jour-4 .plan-day-meal button").count() == 0
    box = page.locator("#jour-4 input[name=on]")
    box.focus()
    page.keyboard.press("Space")                                     # décocher au clavier
    pw.expect(page.locator(".flash")).to_contain_text("Jour 4 : pas de repas compté")
    assert page.input_value("#meal-4") == "50" and page.locator("#jour-4 .money-field").is_visible()
    page.locator("#jour-4 input[name=on]").focus()
    page.keyboard.press("Space")                                     # recocher : le montant du jour revient
    pw.expect(page.locator(".flash")).to_contain_text("Jour 4 : repas comptés à 50,00")
    # « Modifier » depuis le programme : le panneau s'ouvre sur place, on revient au programme
    page.click(".page-head button:has-text('Modifier')")
    assert page.locator("#modifier").is_visible()
    page.fill("#e_place", "Hurghada, Égypte")
    page.click("#modifier button:has-text('Enregistrer')")
    pw.expect(page.locator(".flash")).to_contain_text("Projet modifié")
    assert "/programme" in page.url and "Hurghada, Égypte" in page.locator("aside.side .trip-meta").inner_text()
    page.fill("#meal-4", "")
    page.press("#meal-4", "Enter")                                   # rendu à l'enveloppe pour les autres essais
    pw.expect(page.locator(".flash")).to_contain_text("Jour 4 : repas comptés")
    assert not page.errors, page.errors


def test_result_saves_without_button(site, browser):
    """« Ce que ça donne » : plus de « Recalculer » ni d'« Appliquer ». Un champ s'enregistre avec Entrée ou quand on
    le quitte (Tab), le choix % / € dès qu'on le change ; le message de confirmation reste ; au clavier, après le
    rechargement, on est sur le champ suivant, pas en haut de la page."""
    try:
        _result_saves_without_button(site, browser)
    finally:                                                    # remis comme avant pour les autres essais
        site.post(f"{site.url}/participants", dict(people="10"))
        site.post(f"{site.url}/marge", dict(margin_kind="pourcent", margin_value="0"))
        site.post(f"{site.url}/prix-annonce", dict(announced=""))


def _result_saves_without_button(site, browser):
    page = new_page(browser)
    page.goto(site.base + site.url)
    assert page.locator("#resultat button").count() == 0
    page.fill("#people", "9")
    page.press("#people", "Enter")
    pw.expect(page.locator(".flash")).to_contain_text("Calculé pour 9 personnes")
    page.fill("#people", "10")
    page.keyboard.press("Tab")                                       # quitter le champ
    pw.expect(page.locator(".flash")).to_contain_text("Calculé pour 10 personnes")
    pw.expect(page.locator("#dont-repas a")).to_be_focused()         # Tab ne perd pas sa place : le lien qui suit
    page.fill("#margin_value", "5")
    page.keyboard.press("Tab")
    pw.expect(page.locator(".flash")).to_contain_text("Marge : 5 %")
    pw.expect(page.locator("#margin_pct")).to_be_focused()
    page.keyboard.press("ArrowRight")                                # % → € au clavier : enregistré tout de suite
    pw.expect(page.locator(".flash")).to_contain_text("Marge : 5,00 € par personne")
    assert page.is_checked("#margin_eur")
    page.fill("#announced", "950")
    page.press("#announced", "Enter")
    pw.expect(page.locator(".flash")).to_contain_text("Prix annoncé : 950,00 €")
    pw.expect(page.locator("#announced")).to_be_focused()            # Entrée : on reste dans le champ
    pw.expect(page.locator("#announced")).to_be_in_viewport()
    assert not page.errors, page.errors


def test_programme_line_edit_stays_in_place(site, browser):
    """« Modifier » une ligne du programme : Modifier et Supprimer ne bougent pas, le formulaire s'ouvre dessous sur
    toute la largeur ; tous les champs ont la même hauteur ; « Annuler » oublie ce qui a été tapé et rend la main au
    bouton ; « Supprimer » n'a pas de fond gris au survol."""
    page = new_page(browser)
    page.goto(site.base + f"{site.url}/programme?vue=jour")
    li = page.locator(".plan-line:has(.plan-line-form)").first
    mod, sup = li.locator(".plan-line-actions button[data-toggle-row]"), li.locator(".act.danger")
    before = (mod.bounding_box(), sup.bounding_box())
    mod.click()
    form = li.locator(".plan-line-form")
    assert form.is_visible() and mod.get_attribute("aria-expanded") == "true"
    assert (mod.bounding_box(), sup.bounding_box()) == before
    assert form.bounding_box()["width"] > li.bounding_box()["width"] * .9
    heights = form.evaluate("f => [...f.querySelectorAll('input[type=text], select')].map(e => Math.round(e.getBoundingClientRect().height))")
    assert len(set(heights)) == 1, heights
    labels = form.evaluate("f => [...f.querySelectorAll('.field > label, .plan-pick legend')].map(l => Math.round(l.getBoundingClientRect().height))")
    assert len(set(labels)) == 1, labels                             # chaque étiquette sur une ligne
    pw.expect(form.locator("input[name=text]")).to_be_focused()
    old = form.locator("input[name=text]").input_value()
    form.locator("input[name=text]").fill("autre chose")
    form.locator("button:has-text('Annuler')").click()
    assert form.is_hidden() and mod.get_attribute("aria-expanded") == "false"
    pw.expect(mod).to_be_focused()
    mod.click()
    assert form.locator("input[name=text]").input_value() == old
    sup.hover()
    assert sup.evaluate("e => getComputedStyle(e).backgroundColor") in ("rgba(0, 0, 0, 0)", "transparent")
    # « Lié à » : une pastille par poste, cochée au clavier (Espace), fond sarcelle clair et coche
    k = form.evaluate("f => [...f.querySelectorAll('.plan-pick input')].findIndex(i => !i.checked)")
    chip = form.locator(".plan-pick .chip").nth(k)                  # une pastille pas encore cochée
    plain = chip.evaluate("e => getComputedStyle(e).backgroundColor")
    chip.locator("input").focus()
    page.keyboard.press("Space")
    assert chip.locator("input").is_checked()
    assert chip.evaluate("e => getComputedStyle(e).backgroundColor") != plain                # fond sarcelle clair
    assert chip.evaluate("e => getComputedStyle(e, '::before').content") == '"✓"'
    assert chip.evaluate("e => getComputedStyle(e).outlineStyle") == "solid"            # le focus se voit
    assert not page.errors, page.errors


def test_finishing_touches(site, browser):
    """Tranche 5 : le forfait en entier une seule fois, « Taxi » seul, un jour sans repas le dit, mêmes montants ; et
    les alignements mesurés : les quatre actions d'un poste à la même hauteur, Modifier et Supprimer de même taille et
    sur la même ligne, partout."""
    from test_ux import _contrast
    site.post(f"{site.url}/programme", dict(day="3", text="Transfert", items=str(site.item("Taxi"))))
    page = new_page(browser)
    page.goto(site.base + site.url)
    fa = site.offer("Forfait Club A")
    assert page.locator(f"#offre-{fa}").count() == 1 and page.locator(f"#offre-{fa} .plan-deadlines").count() == 1
    short = page.locator(f"#offre-{fa}-{site.item('Plongées')}")
    assert short.count() == 1 and short.locator(".plan-deadlines, button").count() == 0
    text = short.inner_text().replace(" ", " ").replace("\xa0", " ")
    assert "Forfait Club A" in text and "✓ Retenue" in text and "580,00 € par personne" in text and "voir sous Hôtel" in text
    short.locator("a:has-text('voir sous Hôtel')").click()
    assert page.url.endswith(f"#offre-{fa}")
    # mesures
    def box(loc):
        return loc.evaluate("e => { const r = e.getBoundingClientRect(); return [Math.round(r.height), Math.round(r.top)] }")
    tools = page.locator(f"#poste-{site.item('Avion')} .plan-item-tools")
    heights = {box(tools.locator(sel).first)[0] for sel in ("button[aria-label^='Monter']", "button[aria-label^='Descendre']",
                                                           "summary:has-text('Renommer')", "button:has-text('Supprimer')")}
    assert len(heights) == 1 and heights.pop() >= 28
    def same_size_and_line(scope):
        m, s = scope.locator(".act:has-text('Modifier')").first, scope.locator(".act.danger:has-text('Supprimer')").first
        fm, fs = (x.evaluate("e => getComputedStyle(e).fontSize") for x in (m, s))
        assert fm == fs and abs(box(m)[1] - box(s)[1]) <= 1 and box(m)[0] >= 28 and box(s)[0] >= 28
    same_size_and_line(page.locator(f"#offre-{site.offer('Air France')} .plan-offer-actions"))
    same_size_and_line(page.locator(".plan-dl-row").first)
    page.goto(site.base + f"{site.url}/programme")
    same_size_and_line(page.locator("#jour-2 .plan-line").first)
    assert "Taxi" in page.locator("#jour-3").inner_text() and "Taxi : Taxi" not in page.locator("#jour-3").inner_text()
    # un jour décoché le dit
    site.post(f"{site.url}/jours/7/repas", {})
    page.goto(site.base + f"{site.url}/programme")
    assert "Aucun repas compté ce jour-là." in page.locator("#jour-7").inner_text()
    assert "Aucun repas compté" not in page.locator("#jour-6").inner_text()
    site.post(f"{site.url}/jours/7/repas", dict(on="1"))
    # l'onglet actif reste lisible en thème sombre (ici et sur la page IA d'un voyage)
    dark = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme="dark").new_page()
    for url in (f"{site.url}/programme", site.url):
        dark.goto(site.base + url)
        tab = dark.locator(".side nav a.on")
        fg, bg = tab.evaluate("e => [getComputedStyle(e).color, getComputedStyle(e).backgroundColor]")
        rgb = lambda c: tuple(int(x) for x in re.findall(r"\d+", c)[:3])                     # noqa: E731
        assert _contrast(rgb(fg), rgb(bg)) >= 4.5, (url, fg, bg)
    assert not page.errors, page.errors


def test_reference_amounts_unchanged(site, browser):
    """Les finitions ne changent aucun montant : retenues d'origine, 7 jours, enveloppe 35,00 €, jours 1 et 7 sans repas,
    jour 4 à 50,00 € : 10 830,00 € et 1 083,00 € par personne (brief de la tranche 4)."""
    site.choose("Hôtel X")
    site.choose("Club B")
    site.post(f"{site.url}/jours/1/repas", {})
    site.post(f"{site.url}/jours/7/repas", {})
    site.post(f"{site.url}/jours/4/repas", dict(on="1", amount="50"))
    try:
        page = new_page(browser)
        page.goto(site.base + site.url)
        sp = lambda s: s.replace("\u202f", " ").replace("\xa0", " ")                              # noqa: E731
        assert sp(page.locator("#budget").inner_text()) == "10 830,00 €"
        assert "1 083,00 €" in sp(page.locator("#par-personne").inner_text())
        assert "dont repas 1 900,00 €" in sp(page.locator("#dont-repas").inner_text())
    finally:                                                    # remis comme avant pour les autres essais
        site.choose("Forfait Club A")
        for d in (1, 7):
            site.post(f"{site.url}/jours/{d}/repas", dict(on="1"))
        site.post(f"{site.url}/jours/4/repas", dict(on="1", amount=""))


TRIP_MENU = ["🏠 Où j'en suis", "Saisir", "👥 Participants", "🧾 Dépenses", "💶 Argent reçu et rendu", "📎 Justificatifs",
             "🏷️ Catégories", "Suivre", "🧮 Qui paie quoi", "📊 En images", "🏛️", "Cas particuliers", "🚪 Désistement",
             "🤝 Entre nous", "🤖 IA", "Partager", "📤 Envoyer les comptes", "📄 Exporter le bilan",
             "📦 Exporter le voyage", "← Tous les voyages"]


def menu_entries(page):
    return page.evaluate("""() => [...document.querySelectorAll('aside.side nav > *')]
        .filter(e => !e.classList.contains('sep')).map(e => e.textContent.trim())""")


def test_side_menu_on_every_project_page(site, browser):
    page = new_page(browser)
    pages = [(site.url, "🧾 Postes et offres"), (f"{site.url}/programme", "📅 Programme"), (f"{site.url}/ia", "🤖 IA"),
             (f"{site.url}/lancer", "🚀 Lancer le voyage"),
             (f"{site.url}/offres/{site.offer('Taxi')}/modifier", "🧾 Postes et offres"),
             (f"{site.url}/offres/nouvelle?poste={site.item('Taxi')}", "🧾 Postes et offres")]
    for url, on in pages:
        page.goto(site.base + url)
        assert page.locator("aside.side .side-kicker").inner_text().strip().lower() == "en préparation", url
        assert page.locator("aside.side .trip-name").inner_text() == "Mer Rouge 2027", url
        assert " · Mer Rouge 2027 · Trip Compta" in page.title(), (url, page.title())      # le nom dans l'onglet
        h1 = page.locator("main h1").inner_text()
        assert "Mer Rouge" not in h1 and "En préparation" not in page.locator("main .page-head").inner_text(), (url, h1)
        assert menu_entries(page) == ["🧾 Postes et offres", "📅 Programme", "🤖 IA", "🚀 Lancer le voyage",
                                      "← Tous les voyages"], url
        lit = page.locator("aside.side nav a.on")
        assert lit.count() == 1 and lit.inner_text().strip() == on, (url, lit.all_inner_texts())
        assert page.locator(".tabs.plan-tabs").count() == 0
    titles = {}
    for url, _ in pages:
        page.goto(site.base + url)
        titles[url.split("?")[0].replace(site.url, "") or "/"] = (page.title().split(" · ")[0], page.locator("main h1").inner_text())
    assert titles["/"] == ("Postes et offres", "Postes et offres") and titles["/programme"] == ("Programme", "Programme")
    assert titles["/lancer"] == ("Lancer le voyage", "Lancer le voyage") and titles["/ia"][1] == "🤖 Ajouter des offres avec une IA"
    # « ← Retour au projet » reste seulement sur le formulaire d'une offre
    for url, _ in pages:
        page.goto(site.base + url)
        assert (page.locator("main a:has-text('Retour au projet')").count() == 1) == ("/offres/" in url), url
    # le bouton du haut et l'entrée du menu mènent à la même page
    page.goto(site.base + site.url)
    assert page.locator("#lancer").get_attribute("href") == page.locator("aside.side nav a:has-text('Lancer')").get_attribute("href")
    # fenêtre étroite : le menu passe au-dessus, sans défilement horizontal
    narrow = new_page(browser, width=390, height=844)
    for url, _ in pages:
        narrow.goto(site.base + url)
        assert narrow.evaluate("document.documentElement.scrollWidth") <= 391, url
        side, main = (narrow.locator(x).evaluate("e => e.getBoundingClientRect().top") for x in ("aside.side", "main"))
        assert side < main, url
    assert not page.errors and not narrow.errors, page.errors + narrow.errors


def test_menu_after_launch_and_trip_menu_unchanged(site, browser):
    import re as _re
    r = site.post(f"{site.url}/lancer", dict(caisse="asso"))
    tid = int(_re.match(r"/v/(\d+)", r.headers["location"]).group(1))
    try:
        page = new_page(browser)
        page.goto(site.base + site.url)
        assert menu_entries(page) == ["🧾 Postes et offres", "📅 Programme", "🤖 IA", "🧳 Voir le voyage", "← Tous les voyages"]
        assert page.locator("aside.side nav a:has-text('Voir le voyage')").get_attribute("href") == f"/v/{tid}"
        page.goto(site.base + f"/v/{tid}")                         # la comptabilité garde exactement son menu
        got = menu_entries(page)
        assert [x if not x.startswith("🏛️") else "🏛️" for x in got] == TRIP_MENU
        assert page.locator("aside.side .side-kicker").count() == 0
        assert not page.errors, page.errors
    finally:
        site.post(f"/v/{tid}/supprimer")                           # le projet peut être relancé : les autres essais


def test_choose_keeps_the_page_still(site, browser):
    """« Retenir » / « Ne plus retenir » : la page ne saute pas. L'offre cliquée revient à la même hauteur dans la
    fenêtre (dès le premier affichage, pas d'aller-retour vers l'ancre), le bouton garde sa place et sa largeur, et le
    focus est sur le bouton qui a remplacé celui qu'on a cliqué."""
    page = new_page(browser)
    page.add_init_script("""window.__tops = []; const id = sessionStorage.getItem('garder-place');
      const look = () => { const m = id && JSON.parse(id); const el = m && document.getElementById(m.id);
        if (el && getComputedStyle(document.body).visibility === 'visible') window.__tops.push(Math.round(el.getBoundingClientRect().top)); requestAnimationFrame(look); };
      requestAnimationFrame(look);""")
    page.goto(site.base + site.url)
    try:
        for offer, label, after in (("Air France", "Retenir", "Ne plus retenir"),
                                    ("Air France", "Ne plus retenir", "Retenir")):
            box = page.locator(f"#offre-{site.offer(offer)}")
            box.evaluate("el => window.scrollBy(0, el.getBoundingClientRect().top - 420)")
            btn = box.locator(f"button:has-text('{label}')")
            before, b1 = box.bounding_box(), btn.bounding_box()
            btn.click()
            page.wait_for_url(re.compile(r"/preparer/1\?"))
            page.wait_for_load_state("load")
            page.wait_for_timeout(150)
            box = page.locator(f"#offre-{site.offer(offer)}")
            now, b2 = box.bounding_box(), box.locator(f"button:has-text('{after}')").bounding_box()
            assert abs(now["y"] - before["y"]) <= 1, (before, now)
            assert abs(b2["x"] - b1["x"]) <= 1 and abs(b2["width"] - b1["width"]) <= 1, (b1, b2)
            tops = page.evaluate("window.__tops")
            assert tops and all(abs(t - round(before["y"])) <= 1 for t in tops), tops   # jamais affichée ailleurs
            pw.expect(box.locator(f"button:has-text('{after}')")).to_be_focused()
    finally:
        site.choose("Transavia")
    assert not page.errors, page.errors
