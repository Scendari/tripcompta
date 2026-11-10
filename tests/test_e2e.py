"""Test de bout en bout dans un vrai navigateur (Playwright / Chromium).

Démarre l'application sur un port libre avec les données de démo, parcourt chaque écran,
fait les actions principales et télécharge le PDF et le ZIP.

    pytest tests/test_e2e.py                       # headless
    SCREENSHOTS=captures pytest tests/test_e2e.py  # + captures d'écran
"""
import io
import os
import socket
import threading
import time
import zipfile
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api")


SHOTS = os.environ.get("SCREENSHOTS")


def shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=f"{SHOTS}/{name}.png", full_page=True)


def expect_flash(page, text):
    pw.expect(page.locator(".flash")).to_contain_text(text)


def test_every_screen_and_main_flows(server, page):
    base, tid, data = server
    v = f"{base}/v/{tid}"

    # Accueil
    page.goto(base + "/")
    pw.expect(page.locator(".trip-card").first).to_contain_text("Port-Cros")
    shot(page, "01_accueil")

    # Tableau de bord
    page.click(".trip-card >> nth=0")
    pw.expect(page.locator("h1")).to_have_text("Où j'en suis ?")
    pw.expect(page.locator(".money-kpis .kpi")).to_have_count(4)
    pw.expect(page.locator(".trip-tiles .kpi")).to_have_count(4)
    pw.expect(page.locator(".alerts")).to_contain_text("sans justificatif")
    shot(page, "02_tableau_de_bord")

    # Participants : ajout de 2 personnes
    page.goto(v + "/participants")
    page.click("summary:has-text('Ajouter un participant')")
    page.fill("#a-name", "Paul")
    page.fill("#a-phone", "06 12 34 56 78")
    page.check('#add-form input[name="r_perso:DP"]')                  # « DP » : étiquette de ce voyage
    page.click("#add-form button:text-is('Ajouter')")
    expect_flash(page, "Paul ajouté")
    # proposition : l'ajouter aussi aux dépenses de tout le monde (rien de coché) ; « Tout cocher », puis Ajouter
    pw.expect(page.locator("h1")).to_have_text("L'ajouter aussi à ces dépenses ?")
    shot(page, "03a_rejoindre")
    page.check("[data-all]")
    page.click(".join-form button:has-text('Ajouter')")
    expect_flash(page, "Paul ajouté à")
    page.click("summary:has-text('Ajouter un participant')")         # refermé après l'ajout : on le rouvre
    page.fill("#a-name", "Zoé")
    page.check("#add-form input[name=r_accompagnant]")
    page.click("#add-form button:text-is('Ajouter')")
    expect_flash(page, "Zoé ajouté (accompagnant)")
    page.click("text=Pas maintenant")
    shot(page, "03_participants")

    # Fiche participant + explication d'un calcul
    page.goto(v + "/participants")
    page.click("tr:has(.name-btn:has-text('Camille')) a.status-link")
    pw.expect(page.locator(".person-block:not([hidden]) .bottom-line")).to_contain_text("Camille")
    page.click("[data-toggle-all=open]")
    page.click(".statement details >> nth=1")
    pw.expect(page.locator(".statement .explain").nth(1)).to_be_visible()
    shot(page, "04_fiche_participant")

    # Dépenses
    page.goto(v + "/depenses")
    pw.expect(page.locator("text=Forfait plongées club")).to_be_visible()
    shot(page, "05_depenses")

    # Nouvelle dépense avec aperçu en direct, avance d'un participant, pour 3 personnes, avec justificatif
    page.goto(v + "/depenses/nouvelle")
    page.fill("#label", "Apéro du dernier soir")
    page.select_option("#category", "Repas")
    page.fill("#actual", "45")
    page.click("#who [data-reset=none]")
    for n in ["Luc", "Marie", "Sophie"]:
        page.check(f".who label:has-text('{n}') input")
    pw.expect(page.locator(".who:has-text('Luc') .who-amount")).to_have_value("15,00")     # 45 € ÷ 3, en direct
    page.check("input[name=payer_kind][value=p]")
    page.select_option("#payer_sel", label="Marie (co-organisateur)")
    page.set_input_files("#files", files=[{"name": "ticket_apero.jpg", "mimeType": "image/jpeg",
                                            "buffer": b"\xff\xd8\xff\xe0fakejpeg"}])
    shot(page, "06_nouvelle_depense")
    page.click("button:has-text('Enregistrer') >> nth=0")
    expect_flash(page, "Apéro du dernier soir")
    pw.expect(page.locator("tr:has-text('Apéro du dernier soir')")).to_contain_text("Marie")

    # Qui paie quoi (matrice) → clic sur une case → la dépense s'ouvre ; on fixe le montant de Luc 🔒
    page.goto(v + "/repartition?vue=detail")
    pw.expect(page.locator("table.matrix")).to_be_visible()
    shot(page, "07_qui_paie_quoi")
    page.click("tr:has-text('Luc') td.cell a >> nth=0")
    pw.expect(page.locator("h1")).to_contain_text("Modifier")
    luc = page.locator(".who:has-text('Luc') .who-amount")
    before = float(luc.input_value().replace(",", "."))
    luc.fill(f"{before - 15:.2f}".replace(".", ","))
    pw.expect(page.locator(".who.is-fixed")).to_have_count(1)
    pw.expect(page.locator("#split-status")).to_contain_text("1 montant fixé")
    shot(page, "08_montant_fixe")
    page.click("form button.btn:has-text('Enregistrer') >> nth=0")
    expect_flash(page, "enregistrée")

    # Désistement de Paul (nouveau) : l'asso reprend sa part
    page.goto(v + "/desistement")
    page.select_option("#who", label="Paul")
    page.wait_for_url("**/desistement?p=*")
    page.check("input[name=absorber][value=asso]")
    pw.expect(page.locator("#preview")).to_contain_text("Paul")
    shot(page, "09_desistement")
    page.click("text=Valider le désistement")
    expect_flash(page, "Désistement de Paul")

    # Argent reçu et rendu : versement puis « C'est fait »
    page.goto(v + "/argent/mouvements")
    page.click("button:has-text('+ Enregistrer un mouvement')")
    page.select_option("#participant", index=2)
    page.fill("#amount", "50")
    page.click("form#ajout button:has-text('Enregistrer')")
    expect_flash(page, "Versement de 50,00")
    page.goto(v + "/argent")
    page.click(".settle-table .row-actions button >> nth=0")
    page.click("tr.edit-row:visible button:has-text('Enregistrer')")
    expect_flash(page, "enregistré")
    shot(page, "10_argent")

    # Justificatifs : upload d'un PDF lié à une dépense
    page.goto(v + "/justificatifs")
    page.set_input_files("input[name=files]", files=[{"name": "peage_thierry.pdf", "mimeType": "application/pdf",
                                                      "buffer": b"%PDF-1.4 fake"}])
    page.check("label:has-text('Péage A7 aller-retour — voiture de Thierry') input >> nth=0")
    page.click("form button.btn:has-text('Enregistrer')")
    expect_flash(page, "1 justificatif(s) ajouté(s)")
    shot(page, "11_justificatifs")

    # Entre nous : nouvelle dépense puis règlement
    page.goto(v + "/entre-nous")
    shot(page, "12_entre_nous")
    page.click(".trip-card:has-text('Resto du vendredi soir')")
    page.fill("#label", "Cafés")
    page.fill("#amount", "12,50")
    page.click("form#subexp button:has-text('Ajouter')")
    expect_flash(page, "Cafés")
    page.click("button:has-text(\"C'est réglé\") >> nth=0")
    expect_flash(page, "a remboursé")
    shot(page, "13_entre_nous_detail")

    # Plus de bouton prévu / réel
    page.goto(v)
    assert page.locator("text=Selon le prévu").count() == 0

    # Défaire : proposé dans le message juste après une action
    page.goto(v + "/argent?k=versement")
    page.fill("#amount", "1")
    page.select_option("#participant", index=1)
    page.click("#ajout button:has-text('Enregistrer')")
    page.click(".flash button:has-text('↶ Défaire')")
    expect_flash(page, "Défait")
    page.click(".flash button:has-text('↷ Rétablir')")
    expect_flash(page, "Rétabli")
    page.click(".flash button:has-text('↶ Défaire')") if page.locator(".flash button:has-text('↶ Défaire')").count() else None

    # Thème sombre
    page.click("[data-theme-toggle]")
    assert page.evaluate("document.documentElement.dataset.theme") in ("dark", "light")
    shot(page, "14_theme_sombre")
    page.click("[data-theme-toggle]")

    # Export : PDF et ZIP
    page.goto(v + "/export")
    shot(page, "15_export")
    with page.expect_download() as d:
        page.click("text=Télécharger le PDF")
    pdf = Path(d.value.path()).read_bytes()
    assert pdf[:4] == b"%PDF" and len(pdf) > 20000
    with page.expect_download() as d:
        page.click("text=Télécharger le ZIP")
    z = zipfile.ZipFile(io.BytesIO(Path(d.value.path()).read_bytes()))
    names = z.namelist()
    assert any(n.startswith("Bilan_") for n in names)
    assert sum(n.startswith("justificatifs/J") for n in names) >= 8
    assert "justificatifs/LISEZ-MOI.txt" in names
    if SHOTS:
        Path(SHOTS, "bilan.pdf").write_bytes(pdf)

    # Page « Faire remplir par une IA » + documentation de l'API
    page.goto(f"{base}/v/{tid}")
    page.click("nav >> text=🤖 IA")
    pw.expect(page.locator(".tabs a.on")).to_have_text("Mettre à jour le voyage")
    pw.expect(page.locator("#conf")).to_contain_text("tripcompta")
    pw.expect(page.locator("#cli")).to_contain_text("claude mcp add")
    shot(page, "17_ia")
    page.goto(base + "/api/docs")
    pw.expect(page.locator("body")).to_contain_text("Ajouter une dépense", timeout=15000)

    # Contrôle final : tout est équilibré et aucune erreur JS
    from compta.db import Database
    from compta import models as M
    from compta.services import compute_trip
    db = Database(data)
    s = db.Session()
    r = compute_trip(s.get(M.Trip, tid))
    s.close()
    assert r.is_balanced()
    page_errors = [e for e in page.errors if 'swagger' not in e.lower() and 'cdn' not in e.lower()]
    assert not page_errors, page_errors


def test_mobile_layout(server, page):
    base, tid, _ = server
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base}/v/{tid}")
    width = page.evaluate("document.documentElement.scrollWidth")
    assert width <= 390 + 1
    shot(page, "16_mobile")
    page.set_viewport_size({"width": 1366, "height": 900})
