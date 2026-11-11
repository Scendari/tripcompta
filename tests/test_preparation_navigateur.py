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
    sc = Screen(tmp_path_factory.mktemp("preparer")).reference()
    sc.choose("Forfait Club A")
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
    return ["/", sc.url, sc.empty_url, f"{sc.url}/offres/nouvelle?poste={sc.item('Taxi')}",
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
    page.click("button:has-text('+ Préparer un voyage')")
    assert page.locator("#preparer").is_visible() and not page.locator("#nouveau").is_visible()
    pw.expect(page.locator("#preparer input[name=name]")).to_be_focused()
    page.click("#preparer button:has-text('Fermer')")
    page.click("button:has-text('+ Nouveau voyage')")
    assert page.locator("#nouveau").is_visible() and not page.locator("#preparer").is_visible()
    # un clic sur la carte ouvre le projet
    page.goto(site.base + "/")
    page.click("a.plan-card:has-text('Mer Rouge 2027')")
    assert page.url.endswith("/preparer/1") and "Ça revient à" in page.locator("main").inner_text()


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
    page.click(".plan-people button")
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
    assert page.locator("h1").inner_text() == "Mer Rouge 2027"
