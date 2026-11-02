"""L'asso, acteur à part entière : son compte (entrées / sorties), son nom, et le contrôle « tout tombe juste »."""
import re

import pytest
from fastapi.testclient import TestClient

from compta import models as M
from compta import services as S
from compta.db import Database
from compta.demo import seed_demo, seed_family_demo
from compta.ops import Ops, OpsError
from compta.web import create_app


def clean(text):
    import html
    return re.sub(r"\s+", " ", html.unescape(text).replace("\u202f", " ").replace("\xa0", " "))


@pytest.mark.parametrize("seed", [seed_demo, seed_family_demo])
def test_asso_account_matches_cash_and_balances(tmp_path, seed):
    db = Database(tmp_path / "d")
    tid = seed(db)
    with db.Session() as s:
        t = s.get(M.Trip, tid)
        r = S.compute_trip(t)
        A = S.asso_account(t, r)
        assert (A["cash_in"], A["cash_out"]) == S.asso_cash(t)
        # une fois tout réglé, il reste à l'asso les arrondis moins ce qu'elle prend à sa charge
        assert A["final"] == r.rounding_gain - r.asso_total == A["expected_final"]


def test_cancelled_booking_case_by_hand(tmp_path):
    """Cas réel (anonymisé) : l'organisateur avance 481,16 € d'acompte + 120,29 € (appartement annulé, dont 90,29 €
    remboursés sur son compte) ; l'asso paie le solde 1 443,48 € et lui rend 601,45 €."""
    ops = Ops(Database(tmp_path / "d"))
    people = ["Org", "B", "C"]
    r = ops.import_trip({"voyage": {"nom": "x"}, "participants": people, "depenses": [
        {"libelle": "Logement", "reel": "1924,64", "paye_par": {"Org": "481,16", "asso": "1443,48"}, "pour": "tous"},
        {"libelle": "Appartement annulé", "reel": "120,29", "paye_par": "Org", "rembourse": "90,29",
         "rembourse_a": "Org", "pour": "tous"}],
        "versements": [{"participant": "Org", "montant": "367,29"}, {"participant": "B", "montant": "277"},
                       {"participant": "Org", "montant": "601,45", "type": "remboursement", "date": "2026-05-15"}]})
    with Database(tmp_path / "d").Session() as s:
        t = s.get(M.Trip, r["voyage_id"])
        A = S.asso_account(t, S.compute_trip(t))
    g = {x["name"]: x for x in A["groups"]}
    assert g["Versements des participants"]["cash_in"] == 36729 + 27700
    assert g["Argent rendu aux participants"]["cash_out"] == 60145
    assert g["Divers"]["cash_out"] == 144348                     # seul le solde du logement payé par l'asso
    assert A["cash"] == 36729 + 27700 - 144348 - 60145
    assert A["final"] == A["expected_final"]


def test_asso_page_rename_and_alias(tmp_path):
    app = create_app(tmp_path / "d")
    c = TestClient(app)
    ops = Ops(app.state.db)
    tid = ops.import_trip({"voyage": {"nom": "x"}, "participants": ["Anne", "Bob"], "depenses": [
        {"libelle": "Gîte", "reel": "100", "paye_par": "asso", "pour": "tous"}],
        "versements": [{"participant": "Anne", "montant": "80"}]})["voyage_id"]
    page = clean(c.get(f"/v/{tid}/asso").text)
    assert "Caisse du voyage" in page and "Versements des participants" in page and "Donner un nom" in page
    assert "Il reste" not in page and "avancé 20,00 €" in page
    assert "Une fois tout réglé, le voyage ne coûte rien" in page and "tombent juste" in page
    r = c.post(f"/v/{tid}/asso/nom", data={"name": "Club Subaqua"}, follow_redirects=True)
    page = clean(r.text)
    assert "🏛️ Club Subaqua" in page and "Renommer" in page
    assert "Reçu pour Club Subaqua" in clean(c.get(f"/v/{tid}/comptes").text)
    # l'IA peut écrire le nom de l'asso comme payeur
    ops.import_trip({"voyage_id": tid, "depenses": [{"libelle": "Bateau", "reel": "50", "paye_par": "club subaqua",
                                                      "pour": "tous"}]}, voyage=tid, verifier_id=tid)
    assert ops.trip(tid)["asso"] == "Club Subaqua"
    assert next(d for d in ops.trip(tid)["depenses"] if d["libelle"] == "Bateau")["paye_par"] == "asso"
    with pytest.raises(OpsError):
        ops.import_trip({"voyage_id": tid, "depenses": [{"libelle": "X", "reel": "5", "paye_par": "Club Inconnu",
                                                          "pour": "tous"}]}, voyage=tid, verifier_id=tid)
    # nom effacé -> « l'asso »
    rr = c.post(f"/v/{tid}/asso/nom", data={"name": ""}, follow_redirects=False)
    assert rr.status_code == 303
    assert "🏛️ L'asso" in clean(c.get(f"/v/{tid}/asso").text)


def test_asso_in_matrix_and_pdf(tmp_path):
    app = create_app(tmp_path / "d")
    tid = seed_demo(app.state.db)
    c = TestClient(app)
    m = clean(c.get(f"/v/{tid}/repartition").text)
    assert "Pris en charge par l'asso" in m
    pdf = c.get(f"/v/{tid}/export.pdf")
    assert pdf.status_code == 200
    import io
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(io.BytesIO(pdf.content))
    text = " ".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))
    assert "Compte de l'asso" in text and "Pour terminer le voyage" in text


# ---------------------------------------------------------------- voyage sans asso : un participant tient la caisse

def _friends(tmp_path):
    """Anne, Bob et Paul partent entre amis ; Paul tient la caisse. Gîte 300 € payé avec la caisse ;
    Bob a avancé 60 € d'essence. Versements à Paul : Anne 100 €, Bob 50 €. Parts : 120 € chacun."""
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.import_trip({"voyage": {"nom": "Entre amis", "caisse": "Paul"}, "participants": ["Anne", "Bob", "Paul"],
                           "depenses": [{"libelle": "Gîte", "reel": "300", "paye_par": "asso", "pour": "tous"},
                                        {"libelle": "Essence", "reel": "60", "paye_par": "Bob", "pour": "tous"}],
                           "versements": [{"participant": "Anne", "montant": "100"},
                                          {"participant": "Bob", "montant": "50"}]})["voyage_id"]
    return app, ops, tid


def test_cashier_settlements_by_hand(tmp_path):
    app, ops, tid = _friends(tmp_path)
    assert ops.trip(tid)["caisse"] == "Paul"
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        r = S.compute_trip(t)
        A = S.asso_account(t, r)
        names = {p.id: p.name for p in t.participants}
    # Paul ne se paie pas lui-même : seuls Anne (120 − 100) et Bob (120 − 50 − 60) règlent avec lui
    st = sorted((a.name, c) for a, b, c in r.settlements())
    assert st == [("Anne", 2000), ("Bob", 1000)] and r.to_collect == 3000 and r.to_refund == 0
    assert A["holder"].name == "Paul" and A["holder_balance"] == 12000
    assert (A["cash_in"], A["cash_out"]) == (15000, 30000)
    # Paul a payé 300, reçu 150 + 30 : il en est de 120 €, sa part ; la caisse finit à 0
    assert A["final"] == 0 == A["expected_final"]
    assert 15000 + 3000 - 30000 == -12000


def test_cashier_screens(tmp_path):
    app, ops, tid = _friends(tmp_path)
    c = TestClient(app)
    paul = next(p["id"] for p in ops.trip(tid)["participants"] if p["nom"] == "Paul")
    money = re.sub(r"</?b>", "", clean(c.get(f"/v/{tid}/argent").text))
    flat = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", money))
    assert "À recevoir 2 personnes ou familles 30,00 €" in flat and "Anne 20,00 € ✓ Reçu" in flat
    assert "Paul 120" not in flat                              # Paul tient la caisse : rien à se verser
    page = clean(c.get(f"/v/{tid}/p/{paul}").text)
    assert "Paul tient la caisse" in page and "rien à verser" in page
    asso = re.sub(r"</?b>", "", clean(c.get(f"/v/{tid}/asso").text))
    assert "🏛️ La caisse (Paul)" in asso and "c'est Paul qui tient la caisse" in asso
    assert "Le compte de Paul lui-même" in asso and "ne coûte rien" in asso
    assert "tient la caisse" in clean(c.get(f"/v/{tid}/comptes").text)
    # retour à l'asso, puis de nouveau Paul, via l'écran
    c.post(f"/v/{tid}/asso/caisse", data={"cashier": ""})
    assert ops.trip(tid)["caisse"] == "asso"
    flat = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", clean(c.get(f"/v/{tid}/argent").text)))
    assert "Paul 120,00 € ✓ Reçu" in flat
    c.post(f"/v/{tid}/asso/caisse", data={"cashier": "p", "participant": str(paul)})
    assert ops.trip(tid)["caisse"] == "Paul"
    # PDF : la caisse porte le nom de Paul
    import io
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(io.BytesIO(c.get(f"/v/{tid}/export.pdf").content))
    text = " ".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))
    assert "Compte de la caisse (Paul)" in text and "À recevoir —" in text and "l'asso" not in text


def test_cashier_alerts_skip_holder(tmp_path):
    app, ops, tid = _friends(tmp_path)
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        texts = [a["text"] for a in S.alerts(t, S.compute_trip(t))]
    assert not any(x.startswith("Paul n'a encore rien versé") for x in texts)


def test_cashier_unknown_person_is_an_error(tmp_path):
    app, ops, tid = _friends(tmp_path)
    from compta.importer import ImportErrors
    with pytest.raises(ImportErrors, match="caisse"):
        ops.import_trip({"voyage_id": tid, "voyage": {"caisse": "Zorro"}}, voyage=tid, verifier_id=tid)
    assert ops.trip(tid)["caisse"] == "Paul"


def test_cashier_wording_never_doubles(tmp_path):
    """Jamais « caisse de la caisse (Paul) », ni « l'asso » quand il n'y a pas d'asso (pages principales)."""
    app, ops, tid = _friends(tmp_path)
    c = TestClient(app)
    for url in ("", "/argent", "/comptes", "/asso", "/repartition", "/depenses"):
        page = clean(c.get(f"/v/{tid}{url}").text).lower()
        assert "caisse de la caisse" not in page, url
        body = page.split("<main", 1)[1]
        assert "l'asso" not in body.replace("donner un nom à l'asso", "").replace("revenir à « l'asso »", "") \
            .replace("organisé par l'asso", "").replace('value="" > l\'asso', ""), url    # le choix « L'asso » reste proposé


def test_asso_page_details_who_owes(tmp_path):
    """« Encore à recevoir / à rendre » : le détail par personne ou famille, avec un lien pour enregistrer."""
    app, ops, tid = _friends(tmp_path)
    c = TestClient(app)
    c.post(f"/v/{tid}/asso/caisse", data={"cashier": ""})        # l'asso tient la caisse : Paul doit aussi
    page = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", clean(c.get(f"/v/{tid}/asso").text)))
    assert "Encore à recevoir de 3 personnes ou familles" in page
    assert "Paul 120,00 € Enregistrer" in page
    assert "Encore à rendre — personne" in page
    raw = c.get(f"/v/{tid}/asso").text
    assert "/argent?k=versement&p=" in raw and "a=120#ajout" in raw


@pytest.mark.parametrize("seed", [seed_demo, seed_family_demo])
def test_charges_explain_the_final_result(tmp_path, seed):
    """Plus de tuile à part « Ce que l'asso prend à sa charge » : c'est dans « D'où vient ce résultat ? »,
    sous le résultat final, avec les arrondis."""
    app = create_app(tmp_path / "d")
    tid = seed(app.state.db)
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        r = S.compute_trip(t)
        A = S.asso_account(t, r)
    page = clean(TestClient(app).get(f"/v/{tid}/asso").text)
    assert "<h3>Ce que" not in page
    if not (A["rounding"] or A["charges"]):
        assert "D'où vient ce résultat ?" not in page          # rien à expliquer : pas de bloc vide
    else:
        why = page.split("D'où vient ce résultat ?")[1].split("</table>")[0]
        assert ("+ Les arrondis au centime" in why) == bool(A["rounding"])
        if A["charges"]:
            assert "prend à sa charge</summary>" in why
            assert all(c.label in why for c in A["charges"])
    assert "✓ Les comptes tombent juste." in page
