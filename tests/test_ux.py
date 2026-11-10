"""Tests d'ergonomie (UX) dans un vrai navigateur, sur chaque écran, en version ordinateur et mobile.

- chaque page a un titre <h1> clair et un <title> ;
- pas de défilement horizontal de la page sur mobile (390 px) ;
- chaque champ de saisie a une étiquette (accessibilité, lecteur d'écran) ;
- aucun mot de jargon comptable dans l'interface ;
- les actions destructrices demandent confirmation ;
- aucune erreur JavaScript ; pages rapides ;
- le thème sombre garde un texte lisible (contraste) ;
- navigation au clavier possible sur les formulaires principaux.
"""
import re
import time

import pytest

pw = pytest.importorskip("playwright.sync_api")

PAGES = ["", "/participants", "/p/{pid}", "/depenses", "/categories", "/depenses/nouvelle", "/depenses/{eid}/modifier",
         "/repartition", "/en-images", "/desistement", "/desistement?p={pid}", "/argent", "/argent/mouvements", "/justificatifs",
         "/entre-nous", "/entre-nous/{sid}", "/export", "/ia", "/ia?modele=initialisation", "/comptes", "/participants/soldes", "/participants/remises", "/asso", "/envoyer", "/participants/organisation", "/participants/comptes", "/participants/avances"]
GLOBAL_PAGES = ["/"]
JARGON = re.compile(r"\b(débits?|crédits?|debit|credit|soldes?|écriture|grand livre|créance|ventilation)\b", re.I)


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def urls(server):
    base, tid, _ = server
    out = [base + u for u in GLOBAL_PAGES]
    out += [f"{base}/v/{tid}" + u.format(pid=7, eid=1, sid=1) for u in PAGES]
    return out


def new_page(browser, width=1366, height=900, scheme="light"):
    ctx = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
    page = ctx.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.on("console", lambda m: page.errors.append(m.text) if m.type == "error" else None)
    return page


def unlabeled_fields(page):
    return page.evaluate("""() => {
      const out = [];
      for (const el of document.querySelectorAll('input, select, textarea')) {
        if (el.type === 'hidden' || !el.checkVisibility()) continue;
        const id = el.id && document.querySelector(`label[for="${el.id}"]`);
        const wrapped = el.closest('label');
        const aria = el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('placeholder');
        if (!id && !wrapped && !aria) out.push(el.outerHTML.slice(0, 120));
      }
      return out;
    }""")


def test_every_page_desktop(server, browser):
    page = new_page(browser)
    for url in urls(server):
        t0 = time.perf_counter()
        resp = page.goto(url)
        elapsed = time.perf_counter() - t0
        assert resp.status == 200, url
        assert elapsed < 2.0, (url, elapsed)
        assert page.locator("h1").count() == 1, url
        assert page.title(), url
        body = page.locator("main").inner_text()
        assert not JARGON.search(body), (url, JARGON.search(body).group(0))      # aucune page n'y échappe
        if url.endswith("/comptes") or url.endswith("/soldes") or "/p/" in url:   # tableau des comptes et fiche
            assert "ce qu'il doit" in body.lower() and "ce qu'il a donné" in body.lower()   # des mots de tous les jours
        assert not unlabeled_fields(page), (url, unlabeled_fields(page))
        # boutons : tous ont un texte ou un titre
        empty = page.evaluate("""() => [...document.querySelectorAll('button')]
            .filter(b => b.checkVisibility() && !b.innerText.trim() && !b.title).length""")
        assert empty == 0, url
    assert not page.errors, page.errors


@pytest.mark.parametrize("width", [390, 768])
def test_every_page_mobile_no_horizontal_scroll(server, browser, width):
    page = new_page(browser, width=width, height=844)
    for url in urls(server):
        page.goto(url)
        sw = page.evaluate("document.documentElement.scrollWidth")
        assert sw <= width + 1, (url, sw)
        # les tableaux larges défilent dans leur carte, pas la page
        nav = page.locator("nav a").first
        if nav.count():
            assert nav.is_visible()
    assert not page.errors, page.errors


def test_mobile_no_horizontal_scroll_with_wide_font(server, browser):
    """Les polices de Linux et de Windows sont plus larges que celles du Mac : même essai avec Verdana."""
    page = new_page(browser, width=390, height=844)
    for url in urls(server):
        page.goto(url)
        page.add_style_tag(content="*{font-family:Verdana,'DejaVu Sans',sans-serif !important}")
        sw = page.evaluate("document.documentElement.scrollWidth")
        assert sw <= 391, (url, sw)


def test_destructive_actions_ask_confirmation(server, browser):
    page = new_page(browser)
    for url in urls(server):
        page.goto(url)
        bad = page.evaluate("""() => [...document.querySelectorAll('form')]
            .filter(f => /supprimer|annuler$|quitter/.test(f.getAttribute('action') || '')
                      && !/\\/annuler$/.test(f.getAttribute('action')))   // « Annuler » global = défaire, sans danger
            .filter(f => !f.dataset.confirm).map(f => f.getAttribute('action'))""")
        assert not bad, (url, bad)


def test_cancelled_confirmation_does_nothing(server, browser):
    base, tid, _ = server
    page = new_page(browser)
    page.on("dialog", lambda d: d.dismiss())
    page.goto(f"{base}/v/{tid}/depenses/1/modifier")
    page.click("button:has-text('Supprimer cette dépense')")
    page.goto(f"{base}/v/{tid}/depenses")
    pw.expect(page.locator("#e1")).to_be_visible()


def _luminance(rgb):
    def ch(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def _contrast(a, b):
    la, lb = sorted([_luminance(a), _luminance(b)], reverse=True)
    return (la + 0.05) / (lb + 0.05)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_text_contrast(server, browser, scheme):
    """Texte principal, texte secondaire et pastilles lisibles (WCAG AA : 4,5:1, 3:1 pour les grands chiffres)."""
    base, tid, _ = server
    page = new_page(browser, scheme=scheme)
    page.goto(f"{base}/v/{tid}")
    rgb = lambda s: tuple(int(x) for x in re.findall(r"\d+", s)[:3])
    checks = page.evaluate("""() => {
      const bg = el => { while (el) { const c = getComputedStyle(el).backgroundColor;
                          if (c && !c.endsWith(', 0)') && c !== 'transparent') return c; el = el.parentElement; }
                          return 'rgb(255,255,255)'; };
      const pick = sel => [...document.querySelectorAll(sel)].slice(0, 3).map(e => [sel, getComputedStyle(e).color, bg(e)]);
      return [...pick('h1'), ...pick('.muted'), ...pick('.pill.doit'), ...pick('.pill.du'), ...pick('.pill.ok'),
              ...pick('.kpi .value'), ...pick('.side nav a'), ...pick('.alerts li')];
    }""")
    assert checks
    for sel, fg, bg in checks:
        ratio = _contrast(rgb(fg), rgb(bg))
        minimum = 3.0 if sel == ".kpi .value" else 4.5
        assert ratio >= minimum, (scheme, sel, fg, bg, round(ratio, 2))


def test_keyboard_only_versement(server, browser):
    """Enregistrer un versement uniquement au clavier."""
    base, tid, _ = server
    page = new_page(browser)
    page.goto(f"{base}/v/{tid}/argent?k=versement")             # le formulaire s'ouvre directement
    page.focus("#participant")
    page.keyboard.type("Cam")                 # taper les premières lettres choisit la personne
    assert page.locator("#participant option:checked").inner_text().startswith("Camille")
    page.keyboard.press("Tab")
    page.keyboard.type("12,50")
    page.keyboard.press("Enter")
    pw.expect(page.locator(".flash")).to_contain_text("12,50")


def test_explanations_are_one_click_away(server, browser):
    """Sur une fiche, chaque catégorie se déplie d'un clic, et chaque montant s'explique d'un clic de plus."""
    base, tid, _ = server
    page = new_page(browser)
    page.goto(f"{base}/v/{tid}/p/7")
    toggles = page.locator(".person-block:not([hidden]) [data-toggle-group]")
    assert toggles.count() >= 3
    for i in range(toggles.count()):
        toggles.nth(i).click()
        assert toggles.nth(i).get_attribute("aria-expanded") == "true"
    details = page.locator(".person-block:not([hidden]) .statement details")
    n = details.count()
    assert n >= 5
    for i in range(n):
        details.nth(i).locator("summary").click()
        exp = details.nth(i).locator(".explain")
        pw.expect(exp).to_be_visible()
        assert "€" in exp.inner_text()


def test_preview_is_live_and_fast(server, browser):
    """La part de chacun se met à jour pendant la saisie, directement dans « Pour qui ? »."""
    base, tid, _ = server
    page = new_page(browser)
    page.goto(f"{base}/v/{tid}/depenses/nouvelle")
    page.fill("#label", "Test")
    page.fill("#actual", "120")
    first = page.locator(".who-amount >> nth=0")
    pw.expect(first).to_have_value("10,00", timeout=3000)          # 120 € / 12
    pw.expect(page.locator("#split-status")).to_contain_text("entre 12 personnes")
    page.fill("#actual", "240")
    pw.expect(first).to_have_value("20,00", timeout=3000)
    page.click("#who [data-reset=none]")
    pw.expect(first).to_have_value("", timeout=3000)
    pw.expect(page.locator("#split-status")).to_have_text("")


def test_dashboard_answers_where_am_i(server, browser):
    """Le tableau de bord répond en un coup d'œil : 4 chiffres, 4 actions, alertes cliquables."""
    base, tid, _ = server
    page = new_page(browser)
    page.goto(f"{base}/v/{tid}")
    labels = page.locator(".money-kpis .kpi .label").all_inner_texts()
    assert labels == ["Coût du voyage", "Déjà versé à l'asso", "Reste à recevoir", "Reste à rembourser"]
    assert page.locator(".action").count() == 0                     # plus de section « Que veux-tu faire ? »
    assert page.locator("h3:has-text('Participants')").count() == 1 and page.locator("h3:has-text('Dépenses par catégorie')").count() == 1
    first_alert = page.locator(".alerts a").first
    href = first_alert.get_attribute("href")
    first_alert.click()
    assert href.split("#")[0] in page.url


def test_first_time_user_only_clicks(server, browser):
    """Un nouvel organisateur, sans connaître les adresses : de zéro jusqu'au bilan PDF, en suivant l'interface."""
    base, _, _ = server
    page = new_page(browser)
    page.on("dialog", lambda d: d.accept())
    page.goto(base + "/")
    # 1. Créer le voyage depuis l'accueil : « + Nouveau voyage » déplie le formulaire (et replie l'import)
    assert not page.locator("#name").is_visible()
    page.click("button:has-text('⤓ Importer un voyage')")
    assert page.locator("#fichier").is_visible()
    pw.expect(page.locator("button:has-text('+ Nouveau voyage')")).to_be_hidden()   # formulaire ouvert : boutons masqués
    page.click("#importer-panel >> text=Fermer")
    page.click("button:has-text('+ Nouveau voyage')")
    assert not page.locator("#fichier").is_visible()
    page.fill("#name", "Ma première sortie")
    page.click("button:has-text('Créer le voyage')")
    pw.expect(page.locator(".flash")).to_contain_text("Ajoute maintenant les participants")
    # 2. L'appli m'a emmené sur les participants
    # (voyage vide : le formulaire est déjà ouvert ; après un ajout, il reste ouvert pour le suivant)
    for i, n in enumerate(("Alice", "Bruno", "Célia")):
        if i:                                                  # après un ajout, le formulaire est refermé
            assert not page.is_visible("#a-name")
            page.click("summary:has-text('Ajouter un participant')")
        page.fill("#a-name", n)
        page.click("#add-form button:text-is('Ajouter')")
        pw.expect(page.locator(".flash")).to_contain_text(f"{n} ajouté")
    # 3. Le tableau de bord me guide : « Aucune dépense saisie » -> lien
    page.click("nav >> text=Où j'en suis")
    page.click(".alerts >> text=Aucune dépense saisie")
    page.fill("#label", "Gîte")
    page.fill("#actual", "300")
    pw.expect(page.locator(".who-amount >> nth=0")).to_have_value("100,00")
    page.click("button:has-text('Enregistrer et en ajouter une autre')")
    pw.expect(page.locator(".flash")).to_contain_text("Dépense suivante")
    page.fill("#label", "Essence")
    page.fill("#actual", "60")
    page.check("input[name=payer_kind][value=p]")
    page.select_option("#payer_sel", label="Bruno")
    page.click("button:has-text('Enregistrer') >> nth=0")
    # 4. Retour au tableau de bord : 360 €, Bruno a avancé
    page.click("nav >> text=Où j'en suis")
    pw.expect(page.locator(".money-kpis .kpi .value").first).to_contain_text("360,00")
    pw.expect(page.locator("text=Alice n'a encore rien versé.")).to_be_visible()
    # 5. Alice paie : l'alerte « Alice n'a encore rien versé » mène à son compte -> « + Versement »
    page.click("text=Alice n'a encore rien versé.")
    page.click(".person-block:not([hidden]) a:has-text('+ Versement')")
    page.select_option("#participant", label=page.locator("#participant option:has-text('Alice')").inner_text())
    page.fill("#amount", "120")
    page.click("form#ajout button:has-text('Enregistrer')")
    pw.expect(page.locator(".flash")).to_contain_text("Versement de 120,00")
    # 6. « Reste à régler » : je règle tout (« ✓ Reçu » / « ✓ Remboursé », puis Enregistrer)
    page.click(".subtabs a:has-text('Reste à régler')")
    while page.locator(".settle-table .row-actions button").count():
        page.click(".settle-table .row-actions button >> nth=0")
        page.click("tr.edit-row:visible button:has-text('Enregistrer')")
    pw.expect(page.locator("text=Tout est réglé").first).to_be_visible()
    # 7. Fiche d'Alice : à jour
    page.click("nav >> text=Participants")
    page.click(".name-btn:has-text('Alice')")                       # le nom ouvre « Modifier »
    pw.expect(page.locator("tr.edit-row:visible input[name=name]")).to_have_value("Alice")
    page.click("tr:has(.name-btn:has-text('Alice')) a.status-link")    # la situation ouvre son compte
    pw.expect(page.locator(".person-block:not([hidden]) .bottom-line")).to_contain_text("Alice est à jour")
    # 8. Exporter le bilan
    page.click("nav >> text=Exporter le bilan")
    with page.expect_download() as d:
        page.click("text=Télécharger le PDF")
    assert d.value.suggested_filename.startswith("Bilan_Ma_premiere_sortie")
    # 9. Revenir à l'accueil : le voyage est « Tout est réglé »
    page.click("nav >> text=Tous les voyages")
    pw.expect(page.locator(".trip-card:has-text('Ma première sortie')")).to_contain_text("Tout est réglé")
    assert not page.errors, page.errors


# ---------------------------------------------------------------- lisibilité (contrastes étendus, taille du texte)
CONTRAST_PAGES = {
    "/participants": [".name-btn", ".btn.link.danger", ".pill.withdrawn-pill", ".pill.role", ".side nav .nav-group"],
    "/depenses/nouvelle": [".only-btn", ".hint", "label"],
    "/categories": [".cat-pill", ".muted"],
    "/depenses/1/modifier": [".btn.danger", ".only-btn.on"],
    "/participants/remises": [".btn.link.danger", ".muted"],
    "/envoyer": [".hint", ".pill.doit", ".linklike"],
    "/ia": [".ai-privacy", ".hint"],
}


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_text_contrast_everywhere_it_was_weak(server, browser, scheme):
    """Les textes turquoise, rouges et les boutons « allumés » : 4,5:1 au moins, dans les deux thèmes."""
    base, tid, _ = server
    page = new_page(browser, scheme=scheme)
    rgb = lambda s: tuple(int(x) for x in re.findall(r"\d+", s)[:3])
    seen = set()
    for path, sels in CONTRAST_PAGES.items():
        page.goto(f"{base}/v/{tid}{path}")
        if path == "/depenses/1/modifier":                       # allumer un bouton « Seulement pour »
            page.locator(".only-btn").first.click()
        checks = page.evaluate("""(sels) => {
          const bg = el => { while (el) { const c = getComputedStyle(el).backgroundColor;
                              if (c && !c.endsWith(', 0)') && c !== 'transparent') return c; el = el.parentElement; }
                              return 'rgb(255,255,255)'; };
          return sels.flatMap(sel => [...document.querySelectorAll(sel)].filter(e => e.checkVisibility()).slice(0, 4)
                                     .map(e => [sel, getComputedStyle(e).color, bg(e)]));
        }""", sels)
        for sel, fg, bg in checks:
            seen.add(sel)
            ratio = _contrast(rgb(fg), rgb(bg))
            assert ratio >= 4.5, (scheme, path, sel, fg, bg, round(ratio, 2))
    assert {".name-btn", ".only-btn", ".only-btn.on", ".btn.danger", ".btn.link.danger", ".cat-pill"} <= seen, seen


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_field_borders_are_visible(server, browser, scheme):
    """Le bord d'un champ se distingue du fond : 3:1 au moins (repère visuel d'un composant)."""
    base, tid, _ = server
    page = new_page(browser, scheme=scheme)
    rgb = lambda s: tuple(int(x) for x in re.findall(r"\d+", s)[:3])
    for path in ("/depenses/nouvelle", "/participants?ajout=1", "/envoyer"):
        page.goto(f"{base}/v/{tid}{path}")
        rows = page.evaluate("""() => {
          const bg = el => { while (el) { const c = getComputedStyle(el).backgroundColor;
                              if (c && !c.endsWith(', 0)') && c !== 'transparent') return c; el = el.parentElement; }
                              return 'rgb(255,255,255)'; };
          return [...document.querySelectorAll('input[type=text], input[type=date], select, textarea')]
            .filter(e => e.checkVisibility()).slice(0, 8)
            .map(e => [e.name || e.id, getComputedStyle(e).borderTopColor, bg(e.parentElement)]);
        }""")
        assert rows
        for name, border, bg in rows:
            assert _contrast(rgb(border), rgb(bg)) >= 3.0, (scheme, path, name, border, bg)


def test_base_text_is_16px_and_nothing_under_12px(server, browser):
    base, tid, _ = server
    page = new_page(browser)
    for path in ("", "/participants", "/depenses", "/depenses/nouvelle", "/argent", "/repartition", "/asso",
                 "/justificatifs", "/envoyer"):
        page.goto(f"{base}/v/{tid}{path}")
        assert page.evaluate("getComputedStyle(document.documentElement).fontSize") == "16px"
        small = page.evaluate("""() => {
          const out = [];
          const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
          while (walk.nextNode()) {
            const n = walk.currentNode, el = n.parentElement;
            if (!n.textContent.trim() || !el.checkVisibility() || el.closest('svg, script, style, template')) continue;
            const px = parseFloat(getComputedStyle(el).fontSize);
            if (px < 12) out.push([px, n.textContent.trim().slice(0, 40)]);
          }
          return out;
        }""")
        assert small == [], (path, small[:5])


def test_bigger_text_button_cycles_and_is_remembered(server, browser):
    """« Agrandir le texte » : 16 → 18 → 20 px → retour à 16 ; le choix est gardé d'une page à l'autre ; pas de
    défilement horizontal à la plus grande taille, même sur un petit écran."""
    base, tid, _ = server
    page = new_page(browser)
    page.goto(f"{base}/v/{tid}")
    size = lambda: page.evaluate("getComputedStyle(document.documentElement).fontSize")
    btn = page.locator("[data-text-size]")
    assert size() == "16px" and "Agrandir le texte" in btn.get_attribute("aria-label")
    assert "normale" in btn.get_attribute("aria-label")
    btn.click()
    assert size() == "18px" and "grande" in btn.get_attribute("aria-label")
    btn.click()
    assert size() == "20px" and "Remettre le texte à la taille normale" in btn.get_attribute("aria-label")
    page.goto(f"{base}/v/{tid}/depenses")                        # gardé en changeant de page
    assert size() == "20px"
    for path in ("", "/participants", "/depenses", "/depenses/nouvelle", "/argent", "/envoyer", "/asso"):
        page.goto(f"{base}/v/{tid}{path}")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), path
    page.locator("[data-text-size]").click()
    assert size() == "16px"
    page.reload()
    assert size() == "16px"
    assert page.errors == []
    # petit écran, très grand texte : toujours pas de défilement horizontal
    small = new_page(browser, width=390, height=800)
    small.goto(f"{base}/v/{tid}")
    small.evaluate("localStorage.setItem('texte', '2')")
    for path in ("", "/participants", "/depenses", "/depenses/nouvelle", "/argent", "/envoyer"):
        small.goto(f"{base}/v/{tid}{path}")
        assert small.evaluate("getComputedStyle(document.documentElement).fontSize") == "20px"
        assert small.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), path
    small.evaluate("localStorage.removeItem('texte')")
