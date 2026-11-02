"""Exporter la liste des participants (tableau à coller, texte, CSV pour Excel). Données anonymisées."""
from fastapi.testclient import TestClient

from compta import models as M
from compta.ops import Ops
from compta.web import create_app


def setup(tmp_path):
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.create_trip("Sortie test")["voyage_id"]
    ops.import_trip({"voyage_id": tid, "participants": [
        {"nom": "Anne Martin", "telephone": "06 11 22 33 44", "email": "anne@example.org", "famille": "Martin"},
        {"nom": "Paul Martin", "famille": "Martin"},
        {"nom": "Bob Durand", "note": "niveau 2; nitrox"},
        {"nom": "Chloé Petit"}]}, voyage=tid, verifier_id=tid)
    ops.add_expense(tid, "Gîte", montant_reel="400")
    ops.add_movement(tid, "Bob Durand", "150")
    ops.withdraw(tid, "Chloé Petit", parts_conservees=["Gîte"])
    return app, TestClient(app), tid


def test_csv_for_excel(tmp_path):
    app, c, tid = setup(tmp_path)
    r = c.get(f"/v/{tid}/participants/export?format=csv&cols=nom,telephone,note,famille,desiste,part,verse,solde")
    assert r.headers["content-type"].startswith("text/csv") and "attachment" in r.headers["content-disposition"]
    assert "participants_Sortie_test.csv" in r.headers["content-disposition"]
    body = r.content.decode("utf-8")
    assert body.startswith("﻿")                                   # accents corrects dans Excel
    lines = body[1:].split("\r\n")
    assert lines[0] == ("Nom;Téléphone;Note;Famille;Désisté;Sa part;Déjà versé ou avancé;"
                        "Reste à régler (négatif = à lui rendre)")
    # Gîte 400 € ÷ 4 = 100 € chacun (Chloé garde sa part) ; famille Martin : le reste sur la ligne d'Anne
    assert lines[1] == "Anne Martin;06 11 22 33 44;;Martin;;100,00;0,00;200,00"
    assert lines[2] == 'Bob Durand;;"niveau 2; nitrox";;;100,00;150,00;-50,00'
    assert lines[3] == "Chloé Petit;;;;oui;100,00;0,00;100,00"
    assert lines[4] == "Paul Martin;;;Martin;;100,00;0,00;en famille"


def test_paste_table_and_text_and_options(tmp_path):
    app, c, tid = setup(tmp_path)
    tsv = c.get(f"/v/{tid}/participants/export?format=tsv&cols=nom,email&desistes=0").text
    assert tsv.split("\r\n")[:2] == ["Nom\tE-mail", "Anne Martin\tanne@example.org"]
    assert "Chloé" not in tsv                                          # désistés exclus à la demande
    txt = c.get(f"/v/{tid}/participants/export?format=txt&cols=nom,telephone,solde").text.replace(" ", " ").replace("\xa0", " ")
    assert txt.split("\n") == ["Anne Martin · 06 11 22 33 44 · doit 200,00 €", "Bob Durand · on lui doit 50,00 €",
                               "Chloé Petit · doit 100,00 €", "Paul Martin · en famille"]
    # colonnes inconnues ignorées ; sans colonne : les colonnes par défaut
    assert c.get(f"/v/{tid}/participants/export?format=tsv&cols=pirate").text.startswith("Nom\r\n")
    assert c.get(f"/v/{tid}/participants/export?format=tsv").text.startswith("Nom\tTéléphone\tE-mail\tRôles")


def test_button_and_panel_on_list(tmp_path):
    app, c, tid = setup(tmp_path)
    page = c.get(f"/v/{tid}/participants").text
    assert "⤓ Exporter" in page and 'id="export" hidden' in page
    assert "Copier en tableau" in page and "Copier en texte" in page and "Télécharger (CSV)" in page
    assert "Inclure les désistés (1)" in page
