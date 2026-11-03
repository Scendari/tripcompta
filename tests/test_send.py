"""Envoyer à chacun son compte : PDF détaillé (catégories, sous-catégories), message WhatsApp, ZIP."""
import html
import io
import re
import zipfile
from urllib.parse import unquote

import pypdfium2 as pdfium
import pytest
from fastapi.testclient import TestClient

from compta import services as S
from compta.ops import Ops
from compta.web import create_app


def clean(t):
    return re.sub(r"\s+", " ", html.unescape(t).replace(" ", " ").replace("\xa0", " "))


def pdf_text(data):
    doc = pdfium.PdfDocument(io.BytesIO(data))
    return clean(" ".join(doc[i].get_textpage().get_text_range() for i in range(len(doc))))


@pytest.mark.parametrize("raw,out", [("06 12 34 56 78", "33612345678"), ("+33 6 12 34 56 78", "33612345678"),
                                     ("0033612345678", "33612345678"), ("+41 79 123 45 67", "41791234567"),
                                     ("", ""), ("pas de tel", "")])
def test_whatsapp_number(raw, out):
    assert S.whatsapp_number(raw) == out


@pytest.fixture
def trip(tmp_path):
    """Anne (06…), Bob, famille Lebrun (Paul paie, Léa accompagnante). Transport : 2 péages + 1 essence + location."""
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.import_trip({
        "voyage": {"nom": "Week-end Port-Cros"},
        "participants": [{"nom": "Anne Martin", "telephone": "06 12 34 56 78"}, "Bob",
                         {"nom": "Paul Lebrun", "famille": "Lebrun"},
                         {"nom": "Léa Lebrun", "famille": "Lebrun", "accompagnant": True}],
        "familles": [{"nom": "Lebrun", "paie": "Paul Lebrun"}],
        "depenses": [
            {"libelle": "Gîte", "categorie": "Logement", "reel": "400", "paye_par": "asso", "pour": "tous"},
            {"libelle": "Péage aller", "categorie": "Transport / Covoit", "sous_categorie": "Péages", "reel": "40",
             "paye_par": "Bob", "pour": "tous"},
            {"libelle": "Péage retour", "categorie": "Transport / Covoit", "sous_categorie": "Péages", "reel": "40",
             "paye_par": "asso", "pour": "tous"},
            {"libelle": "Essence", "categorie": "Transport / Covoit", "sous_categorie": "Essence", "reel": "60",
             "paye_par": "asso", "pour": "tous"},
            {"libelle": "Location minibus", "categorie": "Transport / Covoit", "reel": "200", "paye_par": "asso",
             "pour": "tous"}],
        "versements": [{"participant": "Anne Martin", "montant": "150"}, {"participant": "Famille Lebrun",
                                                                         "montant": "300"}]})["voyage_id"]
    return app, ops, tid


def test_message_by_hand(trip):
    app, ops, tid = trip
    page = clean(TestClient(app).get(f"/v/{tid}/envoyer").text)
    # chacun : (400 + 40 + 40 + 60 + 200) / 4 = 185 €
    assert "Bonjour Anne, voici ton compte pour le voyage « Week-end Port-Cros »" in page
    assert "ta part : 185,00 €" in page and "déjà versé : 150,00 €" in page
    assert "Il te reste 35,00 € à verser à l'asso." in page
    # Bob : 185 − 40 avancés = 145
    assert "dépenses payées pour le groupe : 40,00 €" in page and "Il te reste 145,00 € à verser" in page
    # famille : 370 − 300 = 70, message à Paul
    assert "Bonjour Paul, voici le compte de la famille Lebrun" in page and "Il vous reste 70,00 € à verser" in page
    # lien WhatsApp vers le bon numéro, avec le message
    m = re.search(r'href="(https://wa\.me/33612345678\?text=[^"]+)"', TestClient(app).get(f"/v/{tid}/envoyer").text)
    assert m and "Bonjour Anne" in unquote(html.unescape(m.group(1)))
    assert 'href="https://wa.me/?text=Bonjour%20Bob' in TestClient(app).get(f"/v/{tid}/envoyer").text


def test_person_pdf_details_by_subcategory(trip):
    app, ops, tid = trip
    c = TestClient(app)
    anne = next(p["id"] for p in ops.trip(tid)["participants"] if p["nom"] == "Anne Martin")
    page = c.get(f"/v/{tid}/p/{anne}").text
    url = re.search(r'href="(/v/\d+/compte/[^"]+\.pdf)"', page).group(1)
    r = c.get(url)
    assert r.status_code == 200 and "Compte_Anne_Martin" in r.headers["content-disposition"]
    t = pdf_text(r.content)
    assert "Compte de Anne Martin" in t and "Reste à verser à l'asso : 35,00 €" in t
    # catégorie Transport : 85 € ; sous-totaux Péages 20 €, Essence 15 €, Autres 50 € (location)
    assert "TRANSPORT / COVOIT" in t and "85,00 €" in t
    assert re.search(r"› Péages — sous-total 20,00 €", t)
    assert re.search(r"› Essence — sous-total 15,00 €", t)
    assert re.search(r"› Autres \(sans sous-catégorie\) — sous-total 50,00 €", t)
    assert "Péage aller" in t and "Location minibus" in t and "LOGEMENT" in t


def test_family_pdf_has_each_member(trip):
    app, ops, tid = trip
    c = TestClient(app)
    with app.state.db.Session() as s:
        from compta import models as M
        r = S.compute_trip(s.get(M.Trip, tid))
        key = next(a.key for a in r.accounts if a.is_family)
    t = pdf_text(c.get(f"/v/{tid}/compte/{key}.pdf").content)
    assert "Compte de Famille Lebrun" in t and "Détail : Paul Lebrun" in t and "Détail : Léa Lebrun" in t
    assert "Reste à verser à l'asso : 70,00 €" in t
    assert c.get(f"/v/{tid}/compte/zzz.pdf").status_code == 404


def test_zip_has_one_pdf_per_account(trip):
    app, ops, tid = trip
    z = zipfile.ZipFile(io.BytesIO(TestClient(app).get(f"/v/{tid}/comptes-personnels.zip").content))
    names = sorted(z.namelist())
    assert len(names) == 3 and any("Famille_Lebrun" in n for n in names) and any("Bob" in n for n in names)


def test_cashier_message(tmp_path):
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.import_trip({"voyage": {"nom": "Amis", "caisse": "Paul"}, "participants": ["Anne", "Paul"],
                           "depenses": [{"libelle": "Gîte", "reel": "100", "paye_par": "asso", "pour": "tous"}]})["voyage_id"]
    page = clean(TestClient(app).get(f"/v/{tid}/envoyer").text)
    assert "Il te reste 50,00 € à verser à la caisse (Paul)." in page
    assert "Tu tiens la caisse" in page


def test_solde_column_on_categories_and_total(trip):
    """« Son compte » : colonne Solde sur les catégories, sous-catégories et le total (pas sur chaque dépense)."""
    app, ops, tid = trip
    c = TestClient(app)
    bob = next(p["id"] for p in ops.trip(tid)["participants"] if p["nom"] == "Bob")
    page = clean(c.get(f"/v/{tid}/participants/comptes?p={bob}").text)
    flat = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))
    # Bob : transport 85 € de part, a payé le péage aller 40 € -> doit 45 € ; péages : 20 - 40 -> on lui doit 20 €
    assert ">Solde</th>" in page
    assert re.search(r"Transport / Covoit \(4\) 85,00 € 40,00 € doit 45,00 €", flat)
    assert re.search(r"Péages \(2\) 20,00 € 40,00 € on lui doit 20,00 €", flat)
    assert re.search(r"Total 185,00 € 40,00 € doit 145,00 €", flat)
    with app.state.db.Session() as s:
        from compta import models as M
        r = S.compute_trip(s.get(M.Trip, tid))
        k = r.account_of(bob).key
    t = pdf_text(c.get(f"/v/{tid}/compte/{k}.pdf").content)
    assert "Solde" in t and "doit 45,00 €" in t and "on lui doit 20,00 €" in t and "doit 145,00 €" in t


def test_share_menu_whatsapp_and_email(trip):
    """« Partager » : WhatsApp, e-mail (à l'adresse de la fiche, avec objet et message), PDF, copier."""
    app, ops, tid = trip
    ops.import_trip({"voyage_id": tid, "participants_a_modifier": [{"participant": "Anne Martin",
                                                                    "email": "anne@example.org"}]},
                    voyage=tid, verifier_id=tid)
    raw = TestClient(app).get(f"/v/{tid}/envoyer").text
    assert raw.count('class="share" data-share') == 3 and "📲 WhatsApp" not in raw
    m = re.search(r'href="(mailto:anne@example\.org\?subject=[^"]+)"', raw)
    mail = unquote(html.unescape(m.group(1)))
    assert "subject=Ton compte — voyage « Week-end Port-Cros »" in mail and "body=Bonjour Anne, voici ton compte" in mail
    assert "Il te reste 35,00 € à verser" in mail.replace("\xa0", " ")
    m = re.search(r'href="(mailto:\?subject=[^"]+)"', raw)                      # Bob : pas d'e-mail
    assert "Compte" in unquote(html.unescape(m.group(1))) or "Ton compte" in unquote(html.unescape(m.group(1)))
    assert "subject=Compte de la famille Lebrun" in unquote(html.unescape(raw))
    page = clean(raw)
    assert "anne@example.org" in page and "pas d'e-mail" in page and "Télécharger le PDF" in page
    # même menu dans le détail des comptes
    assert 'class="share" data-share' in TestClient(app).get(f"/v/{tid}/participants/comptes").text


def test_browser_share_menu(trip):
    import socket, threading, time
    import uvicorn
    from playwright.sync_api import sync_playwright
    app, ops, tid = trip
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
            ctx = b.new_context(permissions=["clipboard-read", "clipboard-write"])
            pg = ctx.new_page()
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(f"http://127.0.0.1:{port}/v/{tid}/envoyer")
            row = pg.locator("[data-send-row]").first
            assert not row.locator(".share-menu").is_visible()
            row.locator(".share-btn").click()
            assert row.locator(".share-menu").is_visible()
            assert row.locator(".share-btn").get_attribute("aria-expanded") == "true"
            row.locator("[data-copy]").click()
            assert pg.evaluate("navigator.clipboard.readText()").startswith("Bonjour Anne")
            assert "✓ Copié" in row.locator("[data-copy]").inner_text()
            pg.keyboard.press("Escape")
            assert not row.locator(".share-menu").is_visible()
            row.locator(".share-btn").click()
            pg.locator("h1").click()                                          # clic ailleurs : se referme
            assert not row.locator(".share-menu").is_visible()
            # e-mail : le PDF se télécharge et la ligne est marquée « préparé »
            row.locator(".share-btn").click()
            with pg.expect_download() as d:
                row.locator("a[href^='mailto:']").evaluate("a => { a.removeAttribute('href'); a.click(); }")
            assert d.value.suggested_filename.endswith(".pdf")
            assert "sent" in row.get_attribute("class")
            assert errors == []
            b.close()
    finally:
        srv.should_exit = True
        th.join(5)


def test_treasurer_mail(trip):
    """Mail au trésorier (page « Exporter le bilan »). Vérifié à la main : 740 € pour 4 → 185 € chacun.
    Anne doit 185 − 150 = 35 ; Bob 185 − 40 avancés = 145 ; famille Lebrun 370 − 300 = 70 → 250 à demander.
    Caisse : 450 versés − 700 payés par l'asso = l'asso a avancé 250."""
    app, ops, tid = trip
    raw = TestClient(app).get(f"/v/{tid}/export").text
    body = clean(re.search(r'<pre id="mail-body" class="msg">(.*?)</pre>', raw, re.S).group(1))
    assert "Objet proposé" in raw and "Voyage « Week-end Port-Cros » : bilan et règlements" in clean(raw)
    assert body.index("À DEMANDER AUX PARTICIPANTS") < body.index("Voici le résumé du voyage") < body.index("LE COÛT")
    for line in ("Pour clôturer le voyage « Week-end Port-Cros » (4 participants), pouvez-vous procéder aux demandes "
                 "de paiement et aux remboursements ci-dessous ?",
                 "Je me tiens à votre disposition pour toute question.",
                 "- Coût total : 740,00 €",
                 "dont payé par l'asso : 700,00 €, et avancé directement par des participants : 40,00 €",
                 "+ Reçu des participants : 450,00 €", "− Payé aux prestataires (hébergement, club…) : 700,00 €",
                 "= L'asso a avancé 250,00 € de sa trésorerie",
                 "UNE FOIS TOUT RÉGLÉ − 250,00 € avancés par l'asso + 250,00 € à recevoir = 0,00 € : "
                 "le voyage ne coûte rien à l'asso.",
                 "À DEMANDER AUX PARTICIPANTS : 250,00 €",
                 "- Bob : 145,00 € (part 185,00 € − avancé 40,00 €) "
                 "- Famille Lebrun (à Paul Lebrun) : 70,00 € (parts 370,00 € − versé 300,00 €) "
                 "- Anne Martin : 35,00 € (part 185,00 € − versé 150,00 €)",
                 "À LEUR REMBOURSER : rien", "AVANCÉ PAR DES PARTICIPANTS : 40,00 € - Bob : 40,00 € · Péage aller : 40,00 €",
                 "Le bilan détaillé (PDF) et les justificatifs (ZIP) sont en pièces jointes."):
        assert line in body, line
    assert "ENCORE À VÉRIFIER" not in body and "justificatif" not in body.replace("justificatifs (ZIP)", "")
    m = re.search(r'href="(mailto:\?subject=[^"]+)"', raw)
    assert "body=Bonjour" in unquote(html.unescape(m.group(1)))
    assert 'data-copy="#mail-body"' in raw and 'data-copy="#mail-subject"' in raw


def test_pdf_summary_matches_mail_and_dashboard(trip):
    """Bilan PDF, page 1 : les trois blocs du mail, mêmes chiffres (vérifiés à la main, voir test_treasurer_mail)."""
    app, ops, tid = trip
    c = TestClient(app)
    txt = pdf_text(c.get(f"/v/{tid}/export.pdf").content)
    for part in ("Le coût du voyage", "Payé par l'asso 700,00 €", "Avancé directement par des participants 40,00 €",
                 "Coût total du voyage 740,00 €", "La caisse de l'asso", "+ Reçu des participants 450,00 €",
                 "− Payé aux prestataires (hébergement, club…) 700,00 €",
                 "= L'asso a avancé de sa trésorerie 250,00 €", "Une fois tout réglé",
                 "+ À recevoir des participants 250,00 €", "Le voyage ne coûte rien à l'asso 0,00 €"):
        assert part in txt, part
    assert "Le voyage en un coup d'œil" not in txt and "dépenses payées + remboursements" not in txt
    dash = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", clean(c.get(f"/v/{tid}").text)))
    assert "dont 700,00 € payés par l'asso et 40,00 € avancés par des participants" in dash


def test_treasurer_mail_withdrawals(tmp_path):
    """Désistement dans le mail. À la main : Gîte 300 € pour A, B, C → 100 chacun ; Bateau 90 € → C se désiste
    (garde le gîte), le groupe reprend le reste : A et B 45 chacun. C a versé 150 → à lui rembourser 50."""
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.create_trip("Sortie")["voyage_id"]
    ops.add_participants(tid, ["Anne", "Bob", "Chloé"])
    ops.add_expense(tid, "Gîte", montant_reel="300")
    ops.add_expense(tid, "Bateau", montant_reel="90")
    ops.add_movement(tid, "Chloé", "150")
    ops.withdraw(tid, "Chloé", parts_conservees=["Gîte"], date="2026-05-02")
    raw = TestClient(app).get(f"/v/{tid}/export").text
    body = clean(re.search(r'<pre id="mail-body" class="msg">(.*?)</pre>', raw, re.S).group(1))
    for part in ("À LEUR REMBOURSER : 50,00 € - Chloé (désistement) : 50,00 € (versé 150,00 € − part 100,00 €)",
                 "DÉSISTEMENTS : 1 sur 3 - Chloé ·",
                 "· paie quand même 100,00 € : Gîte 100,00 €",
                 "· le reste de sa part est repris par le groupe", "· a versé 150,00 €, à lui rembourser 50,00 €",
                 "- Anne : 145,00 € (part 145,00 €)"):
        assert part in body, part
    assert body.index("DÉSISTEMENTS") < body.index("LA CAISSE") and "02/05/2026" not in body
