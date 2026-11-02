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
    assert re.search(r"› Péages — sous-total \(2\) 20,00 €", t)
    assert re.search(r"› Essence — sous-total \(1\) 15,00 €", t)
    assert re.search(r"› Autres \(sans sous-catégorie\) — sous-total \(1\) 50,00 €", t)
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
