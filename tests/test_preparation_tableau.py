"""Préparer un voyage, tranche 7 : la vue « Tableau » du programme (lecture seule), dans un vrai navigateur sans fenêtre.

Le programme de référence (celui des captures) : départ le 01/05/2027, retour le 07/05/2027, enveloppe 35,00 €, jours 1
et 7 sans repas, jour 4 à 50,00 € ; jour 1 : vol à 7 h 30 (Avion) et taxi à 14 h (Taxi) ; jours 2 à 6 : deux plongées
chaque matin ; jour 7 : rien.
"""
import re
import socket
import threading
import time

import pytest

from test_preparation_ecran import FORBIDDEN, TEMPLATES, Screen, clean

pw = pytest.importorskip("playwright.sync_api")


def serve(sc):
    import uvicorn
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(sc.app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}", srv


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    sc = Screen(tmp_path_factory.mktemp("tableau")).reference()
    sc.post(f"{sc.url}/modifier", dict(name="Mer Rouge 2027", place="Hurghada", start="2027-05-01", end="2027-05-07"))
    sc.post(f"{sc.url}/repas", dict(meal_per_day="35"))
    sc.post(f"{sc.url}/jours/1/repas", {})
    sc.post(f"{sc.url}/jours/7/repas", {})
    sc.post(f"{sc.url}/jours/4/repas", dict(on="1", amount="50"))
    for x in (dict(day="1", text="Taxi jusqu'à l'hôtel", time="14h", item=str(sc.item("Taxi"))),
              dict(day="1", text="Vol aller Paris → Hurghada", time="7h30", item=str(sc.item("Avion"))),
              dict(day="2", until="6", text="Deux plongées chaque matin", item=str(sc.item("Plongées")),
                   note="Rendez-vous au club à 8 h, avec tout le matériel ; les débutants commencent par un baptême "
                        "dans la baie, les autres partent en bateau vers le récif"),
              dict(day="3", text="Après-midi libre")):
        sc.post(f"{sc.url}/programme", x)
    sc.base, srv = serve(sc)
    yield sc
    srv.should_exit = True


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def new_page(browser, width=1280, scheme="light"):
    page = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=scheme, locale="fr-FR").new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    return page


def sp(s):
    return " ".join(s.replace(" ", " ").replace("\xa0", " ").split())


def test_table_reads_the_reference_programme(site, browser):
    page = new_page(browser)
    page.goto(site.base + f"{site.url}/programme?vue=tableau")
    assert page.locator("#vue-tableau").is_visible() and page.locator("#vue-jour").is_hidden()
    days = page.locator("#programme-tableau tbody.prog-day")
    assert days.count() == 7
    heads = [sp(days.nth(i).locator("th.prog-when").inner_text()) for i in range(7)]
    assert heads == ["Jour 1 sam. 1er mai", "Jour 2 dim. 2 mai", "Jour 3 lun. 3 mai", "Jour 4 mar. 4 mai",
                     "Jour 5 mer. 5 mai", "Jour 6 jeu. 6 mai", "Jour 7 ven. 7 mai"]
    d1 = sp(days.nth(0).inner_text())
    assert d1.index("7 h 30 Vol aller") < d1.index("14 h Taxi jusqu'à l'hôtel")
    assert [sp(x) for x in days.nth(0).locator("td.prog-who").all_inner_texts()] == ["Transavia", "Taxi"]   # « Taxi » seul
    d2 = sp(days.nth(1).inner_text())
    assert "Deux plongées chaque matin Jours 2 à 6" in d2 and "Club B" in d2
    assert "Rendez-vous au club à 8 h" in d2                              # la note, en petit dessous
    note = days.nth(1).locator(".prog-note")
    assert note.evaluate("e => getComputedStyle(e).webkitLineClamp") == "2"   # longue : coupée proprement (2 lignes)
    narrow = new_page(browser, width=390)
    narrow.goto(site.base + f"{site.url}/programme?vue=tableau")
    assert narrow.locator("#t-jour-2 .prog-note").evaluate("e => e.scrollHeight > e.clientHeight")   # coupée, ici
    for i in (2, 3, 4, 5):
        cont = days.nth(i).locator("td.prog-cont")
        assert sp(cont.inner_text()) == "↳ Deux plongées chaque matin", i
        assert "Jours 2 à 6" not in days.nth(i).inner_text()
    assert "Après-midi libre" in days.nth(2).inner_text()
    meals = [sp(days.nth(i).locator("td.prog-meal").inner_text()) for i in range(7)]
    assert meals == ["—", "35 €", "35 €", "50 €", "35 €", "35 €", "—"]
    assert days.nth(3).locator(".prog-meal-changed").count() == 1 and days.nth(1).locator(".prog-meal-changed").count() == 0
    assert sp(page.locator(".prog-total").inner_text()) == "Repas : 190,00 € par personne sur 5 jours"
    assert sp(days.nth(6).locator("td.prog-nothing").inner_text()) == "rien de prévu"
    # mêmes montants qu'ailleurs
    page.goto(site.base + site.url)
    assert sp(page.locator("#budget").inner_text()) == "10 830,00 €"
    assert "1 083,00 €" in sp(page.locator("#par-personne").inner_text())
    assert not page.errors, page.errors


def test_day_link_and_remembered_choice(site, browser):
    page = new_page(browser)
    page.goto(site.base + f"{site.url}/programme")
    assert page.locator("#vue-jour").is_visible()
    page.click("#vue-choix a:has-text('Tableau')")                      # sans recharger
    assert page.locator("#vue-tableau").is_visible() and "vue=tableau" in page.url
    assert page.locator("#vue-choix a.on").inner_text() == "Tableau"
    page.goto(site.base + f"{site.url}/programme")                      # sans paramètre : le choix est retrouvé
    assert page.locator("#vue-tableau").is_visible() and page.locator("#vue-jour").is_hidden()
    page.reload()
    assert page.locator("#vue-tableau").is_visible()
    page.click("#t-jour-3 th.prog-when a")                               # « Jour 3 » mène au bon bloc de la vue Par jour
    page.wait_for_url(re.compile(r"vue=jour#jour-3$"))
    assert page.locator("#vue-jour").is_visible() and page.locator("#jour-3").is_visible()
    assert page.locator("#jour-3").evaluate("e => { const r = e.getBoundingClientRect(); return r.top < innerHeight && r.bottom > 0 }")
    page.goto(site.base + f"{site.url}/programme")                      # le dernier choix : Par jour
    assert page.locator("#vue-jour").is_visible()
    assert not page.errors, page.errors


def test_narrow_and_dark(site, browser):
    page = new_page(browser, width=390)
    page.goto(site.base + f"{site.url}/programme?vue=tableau")
    assert page.evaluate("document.documentElement.scrollWidth") <= 391
    card = page.locator("#t-jour-1")
    assert card.evaluate("e => getComputedStyle(e).display") == "flex"             # une petite carte par jour
    assert sp(card.locator("th.prog-when").inner_text()) == "Jour 1 · sam. 1er mai"
    tops = card.evaluate("""e => ['th.prog-when', 'td.prog-meal', 'td.prog-what'].map(s => e.querySelector(s).getBoundingClientRect().top)""")
    assert tops == sorted(tops)                                          # titre, repas, puis les lignes
    head = card.locator("th.prog-when").evaluate("e => e.getBoundingClientRect().height")
    assert head < 40                                                      # « Jour 1 · sam. 1er mai » sur une ligne
    dark = new_page(browser, scheme="dark")
    dark.goto(site.base + f"{site.url}/programme?vue=tableau")
    from test_ux import _contrast
    rgb = lambda c: tuple(int(x) for x in re.findall(r"\d+", c)[:3])                # noqa: E731
    for sel in ("#t-jour-2 td.prog-what", "#t-jour-1 td.prog-what >> nth=0", "#t-jour-2 th.prog-when a", "#t-jour-3 td.prog-cont"):
        fg, bg = dark.locator(sel).evaluate("""e => { let b = e, c;
            while (b && ((c = getComputedStyle(b).backgroundColor) === 'rgba(0, 0, 0, 0)' || c === 'transparent')) b = b.parentElement;
            return [getComputedStyle(e).color, c] }""")
        assert _contrast(rgb(fg), rgb(bg)) >= 4.5, (sel, fg, bg)
    assert not page.errors and not dark.errors


def test_without_meals_or_duration(tmp_path):
    sc = Screen(tmp_path).reference()
    sc.post(f"{sc.url}/duree", dict(days="3"))
    page = sc.cl.get(f"{sc.url}/programme?vue=tableau").text
    table = page[page.index('id="programme-tableau"'):page.index("</table>")]
    assert "Repas" not in clean(table) and "prog-meal" not in table and "prog-total" not in page.split("</table>")[1][:400]
    assert clean(table).count("rien de prévu") == 3
    sc.create(name="Sans durée", people="4", place="")
    t = clean(sc.cl.get(f"{sc.url}/programme?vue=tableau").text)
    assert "Indique la durée du séjour pour voir les jours." in t and 'id="programme-tableau"' not in sc.cl.get(
        f"{sc.url}/programme?vue=tableau").text


def test_no_forbidden_words():
    src = (TEMPLATES / "plan_program.html").read_text(encoding="utf-8")
    assert not FORBIDDEN.search(src)
