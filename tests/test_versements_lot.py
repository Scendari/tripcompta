"""Versements cochés en lot, moyen de paiement, chèques à déposer, alerte de doublon, filtre et tri des dépenses.

Voyage de test (montants vérifiés à la main) : Anne, Bruno, Chloé, David.
- Gîte 400 € payé par l'asso, pour tous : 100 € chacun.
- Essence 60 € payée par Bruno, pour tous : 15 € chacun.
Part de chacun : 115 €. Bruno a avancé 60 € : il doit encore 55 €. David a versé 200 € : on lui doit 85 €.
Reste à régler : à recevoir Anne 115 €, Chloé 115 €, Bruno 55 € (285 €) ; à rendre David 85 €.
"""
import datetime as dt
import html
import io
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

from compta import models as M
from compta import services as S
from compta import transfer as TR
from compta.ops import Ops, OpsError
from compta.web import create_app


def clean(t):
    return re.sub(r"\s+", " ", html.unescape(t).replace("\xa0", " "))


def visible(t):
    return clean(re.sub(r"<(script|style)\b.*?</\1>|<[^>]+>", " ", t, flags=re.S))


def setup(tmp_path):
    app = create_app(tmp_path / "voyages")
    cl = TestClient(app)
    ops = Ops(app.state.db)
    tid = ops.create_trip("Sortie test")["voyage_id"]
    ops.add_participants(tid, ["Anne", "Bruno", "Chloé", "David"])
    ops.add_expense(tid, "Gîte", categorie="Logement", montant_reel="400")
    ops.add_expense(tid, "Essence", categorie="Transport", montant_reel="60", paye_par="Bruno")
    ops.add_movement(tid, "David", "200", date="2027-03-01")
    with app.state.db.Session() as s:
        ids = {p.name: p.id for p in s.query(M.Participant).filter_by(trip_id=tid)}
    return app, cl, tid, ids


def balances(app, tid):
    with app.state.db.Session() as s:
        r = S.compute_trip(s.get(M.Trip, tid))
        return {ps.name: ps.balance for ps in r.people.values()}


def moves(app, tid):
    with app.state.db.Session() as s:
        return [(m.participant.name, m.kind, m.amount, m.date, m.method, m.deposited, m.note)
                for m in s.query(M.Movement).filter_by(trip_id=tid).order_by(M.Movement.id)]


def follow(cl, r):
    assert r.status_code == 303, r.text[:300]
    return r.headers["location"], clean(cl.get(r.headers["location"]).text)


# ------------------------------------------------------------------ situation de départ
def test_starting_point_is_what_the_docstring_says(tmp_path):
    app, cl, tid, ids = setup(tmp_path)
    assert balances(app, tid) == {"Anne": 11500, "Bruno": 5500, "Chloé": 11500, "David": -8500}
    page = cl.get(f"/v/{tid}/argent").text
    assert page.count('name="qui"') == 3                          # trois lignes à recevoir : cases à cocher
    assert 'action="/v/%d/argent/lot" id="lot-versement"' % tid in page
    assert 'id="lot-remboursement"' not in page                     # une seule ligne à rendre : pas de lot
    assert "285,00 €" in clean(page) and "85,00 €" in clean(page)


# ------------------------------------------------------------------ versements cochés en lot
def test_batch_notes_each_ticked_line_for_its_exact_amount(tmp_path):
    app, cl, tid, ids = setup(tmp_path)
    r = cl.post(f"/v/{tid}/argent/lot", data={"kind": "versement", "qui": [str(ids["Anne"]), str(ids["Bruno"])],
                                              "date": "2027-04-02", "method": "cheque"}, follow_redirects=False)
    url, page = follow(cl, r)
    assert "2 versements reçus enregistrés, 170,00 € en tout : Anne 115,00 €, Bruno 55,00 €" in page
    assert "&d=" in url                                               # « Défaire » proposé : un seul geste à défaire
    new = moves(app, tid)[1:]
    assert new == [("Anne", "versement", 11500, dt.date(2027, 4, 2), "cheque", False, ""),
                   ("Bruno", "versement", 5500, dt.date(2027, 4, 2), "cheque", False, "")]
    assert balances(app, tid) == {"Anne": 0, "Bruno": 0, "Chloé": 11500, "David": -8500}
    # une seule étape dans l'historique : la défaire retire les deux versements
    hist = app.state.db.history()
    assert clean(hist[0]["label"]) == "2 versements notés d'un coup (170,00 €)"
    app.state.db.undo_to(hist[0]["token"])
    assert len(moves(app, tid)) == 1 and balances(app, tid)["Anne"] == 11500


def test_batch_refund_and_single_line(tmp_path):
    app, cl, tid, ids = setup(tmp_path)
    url, page = follow(cl, cl.post(f"/v/{tid}/argent/lot", data={"kind": "remboursement", "qui": str(ids["David"])},
                                   follow_redirects=False))
    assert "Remboursement de 85,00 € à David enregistré" in page
    assert moves(app, tid)[-1] == ("David", "remboursement", 8500, None, "", False, "")
    assert balances(app, tid)["David"] == 0


def test_batch_amounts_come_from_the_accounts_not_from_the_form(tmp_path):
    app, cl, tid, ids = setup(tmp_path)
    # un montant glissé dans le formulaire est ignoré ; David (à qui l'on doit) n'est pas « à recevoir »
    r = cl.post(f"/v/{tid}/argent/lot", data={"kind": "versement", "qui": [str(ids["Chloé"]), str(ids["David"]), "999"],
                                              "amount": "1", "montant": "1"}, follow_redirects=False)
    url, page = follow(cl, r)
    assert "Versement de 115,00 € par Chloé enregistré" in page
    assert [m[:3] for m in moves(app, tid)[1:]] == [("Chloé", "versement", 11500)]
    # la même page renvoyée une seconde fois (double clic, page restée ouverte) : plus rien à noter
    before = moves(app, tid)
    url, page = follow(cl, cl.post(f"/v/{tid}/argent/lot", data={"kind": "versement", "qui": str(ids["Chloé"])},
                                   follow_redirects=False))
    assert "e=1" in url and "déjà réglées" in page and moves(app, tid) == before
    url, page = follow(cl, cl.post(f"/v/{tid}/argent/lot", data={"kind": "versement"}, follow_redirects=False))
    assert "e=1" in url and "Coche d'abord au moins une ligne" in page and moves(app, tid) == before
    assert cl.post(f"/v/{tid}/argent/lot", data={"kind": "cadeau", "qui": "1"}).status_code == 400


def test_batch_for_a_family_goes_to_the_one_who_pays(tmp_path):
    """Anne et Chloé forment une famille (Anne paie) : une seule ligne de 230 €, un seul versement au nom d'Anne."""
    app, cl, tid, ids = setup(tmp_path)
    ops = Ops(app.state.db)
    res = ops.import_trip({"participants_a_modifier": [{"participant": "Anne", "famille": "Martin"},
                                                       {"participant": "Chloé", "famille": "Martin"}]}, voyage=tid)
    assert "erreur" not in res, res
    with app.state.db.Session() as s:
        r = S.compute_trip(s.get(M.Trip, tid))
        owed = {a.name: (a.referent, c) for a, b, c in r.settlements() if b is S.E.ASSO}
    fam = next(k for k in owed if "Martin" in k)
    referent, cents = owed[fam]
    assert cents == 23000 and referent in (ids["Anne"], ids["Chloé"])
    cl.post(f"/v/{tid}/argent/lot", data={"kind": "versement", "qui": [str(referent), str(ids["Bruno"])],
                                          "method": "virement"})
    got = {m[0]: m[2] for m in moves(app, tid)[1:]}
    assert sorted(got.values()) == [5500, 23000] and len(got) == 2
    b = balances(app, tid)
    assert b["Anne"] + b["Chloé"] == 0 and b["Bruno"] == 0


def test_closed_trip_refuses_batch_and_cheque_changes(tmp_path):
    app, cl, tid, ids = setup(tmp_path)
    cl.post(f"/v/{tid}/argent", data={"participant": str(ids["Anne"]), "kind": "versement", "amount": "15",
                                      "method": "cheque"})
    mid = len(moves(app, tid))
    cl.post(f"/v/{tid}/cloturer")
    before = moves(app, tid)
    for url, data in ((f"/v/{tid}/argent/lot", {"kind": "versement", "qui": str(ids["Bruno"])}),
                      (f"/v/{tid}/argent/{mid}/depose", {"etat": "1"}), (f"/v/{tid}/argent/cheques-deposes", {})):
        r = cl.post(url, data=data, follow_redirects=False)
        assert r.status_code == 303 and "e=1" in r.headers["location"], url
    assert moves(app, tid) == before


# ------------------------------------------------------------------ moyen de paiement, chèques à déposer
def test_method_is_saved_shown_and_changed(tmp_path):
    app, cl, tid, ids = setup(tmp_path)
    cl.post(f"/v/{tid}/argent", data={"participant": str(ids["Anne"]), "kind": "versement", "amount": "50",
                                      "date": "2027-03-10", "method": "cheque", "note": "n° 1234"})
    cl.post(f"/v/{tid}/argent", data={"participant": str(ids["Bruno"]), "kind": "versement", "amount": "30",
                                      "date": "2027-03-11", "method": "virement"})
    cl.post(f"/v/{tid}/argent", data={"participant": str(ids["Chloé"]), "kind": "versement", "amount": "40",
                                      "date": "2027-03-12", "method": "n'importe quoi"})          # inconnu : pas précisé
    got = moves(app, tid)
    assert [(m[0], m[2], m[4]) for m in got[1:]] == [("Anne", 5000, "cheque"), ("Bruno", 3000, "virement"),
                                                     ("Chloé", 4000, "")]
    raw = cl.get(f"/v/{tid}/argent/mouvements").text
    page = visible(raw)
    assert "Versement de Anne chèque à déposer · n° 1234" in page
    assert "Versement de Bruno virement" in page
    assert "Chèques à déposer à la banque" in page and 'id="cheques"' in raw
    # sur « Reste à régler » : un rappel discret, avec le total des chèques à déposer
    assert "1 chèque reçu, pas encore déposé à la banque (50,00 €)" in visible(cl.get(f"/v/{tid}/argent").text)
    # les trois formulaires proposent les mêmes moyens, en mots simples
    for label in ("Virement", "Chèque", "Espèces", "Carte bancaire", "Paiement en ligne", "Autre"):
        assert f">{label}</option>" in raw
    # modifier : le chèque devient un virement → plus rien à déposer
    with app.state.db.Session() as s:
        mid = s.query(M.Movement).filter_by(participant_id=ids["Anne"]).one().id
    cl.post(f"/v/{tid}/argent/{mid}", data={"participant": str(ids["Anne"]), "kind": "versement", "amount": "50",
                                            "method": "virement", "note": "n° 1234"})
    assert moves(app, tid)[1][4:6] == ("virement", False)
    assert "Chèques à déposer" not in visible(cl.get(f"/v/{tid}/argent/mouvements").text)
    # les comptes ne dépendent pas du moyen de paiement
    assert balances(app, tid) == {"Anne": 6500, "Bruno": 2500, "Chloé": 7500, "David": -8500}


def test_cheque_deposited_is_a_reminder_only(tmp_path):
    app, cl, tid, ids = setup(tmp_path)
    for name, amount in (("Anne", "50"), ("Bruno", "30"), ("Chloé", "40")):
        cl.post(f"/v/{tid}/argent", data={"participant": str(ids[name]), "kind": "versement", "amount": amount,
                                          "method": "cheque"})
    before = balances(app, tid)
    with app.state.db.Session() as s:
        mids = {m.participant.name: m.id for m in s.query(M.Movement).filter_by(method="cheque")}
    page = visible(cl.get(f"/v/{tid}/argent/mouvements").text)
    assert "Chèques à déposer à la banque 120,00 €" in page and "✓ Tous déposés" in page
    url, page = follow(cl, cl.post(f"/v/{tid}/argent/{mids['Anne']}/depose", data={"etat": "1"}, follow_redirects=False))
    assert "Chèque de Anne (50,00 €) noté comme déposé" in page and "&d=" in url
    assert "Chèques à déposer à la banque 70,00 €" in visible(cl.get(url).text)
    assert "Versement de Anne chèque déposé" in visible(cl.get(url).text)
    # retour en arrière depuis la fiche du mouvement (case décochée)
    cl.post(f"/v/{tid}/argent/{mids['Anne']}", data={"participant": str(ids["Anne"]), "kind": "versement",
                                                       "amount": "50", "method": "cheque", "deposited_shown": "1"})
    assert [m[5] for m in moves(app, tid)[1:]] == [False, False, False]
    # tous déposés d'un coup
    url, page = follow(cl, cl.post(f"/v/{tid}/argent/cheques-deposes", follow_redirects=False))
    assert "3 chèques notés comme déposés" in page
    assert [m[5] for m in moves(app, tid)[1:]] == [True, True, True]
    assert "Chèques à déposer" not in visible(cl.get(f"/v/{tid}/argent/mouvements").text)
    assert balances(app, tid) == before                               # un pense-bête : les comptes ne bougent pas
    # un virement n'est pas un chèque : refusé poliment
    cl.post(f"/v/{tid}/argent", data={"participant": str(ids["Anne"]), "kind": "versement", "amount": "5",
                                      "method": "virement"})
    with app.state.db.Session() as s:
        vid = s.query(M.Movement).filter_by(method="virement").one().id
    url, page = follow(cl, cl.post(f"/v/{tid}/argent/{vid}/depose", data={"etat": "1"}, follow_redirects=False))
    assert "e=1" in url and "n'est pas un chèque reçu" in page
    assert cl.post(f"/v/{tid}/argent/99999/depose").status_code == 404


def test_method_words_are_understood():
    for said, key in (("Chèque", "cheque"), ("cheque", "cheque"), ("CB", "carte"), ("Carte bancaire", "carte"),
                      ("espèces", "especes"), ("liquide", "especes"), ("Virement", "virement"),
                      ("HelloAsso", "en_ligne"), ("paiement en ligne", "en_ligne"), ("autre", "autre"),
                      ("", ""), (None, "")):
        assert S.pay_method(said) == key, said
    assert S.pay_method("troc") is None
    assert S.pay_method_label("cheque") == "Chèque" and S.pay_method_label("") == ""


# ------------------------------------------------------------------ alerte de doublon
def test_same_payment_twice_is_flagged_but_not_blocked(tmp_path):
    app, cl, tid, ids = setup(tmp_path)
    form = {"participant": str(ids["Bruno"]), "kind": "versement", "amount": "20", "date": "2027-03-10"}
    url, page = follow(cl, cl.post(f"/v/{tid}/argent", data=form, follow_redirects=False))
    assert "Versement de 20,00 € par Bruno enregistré" in page and "Attention" not in page
    url, page = follow(cl, cl.post(f"/v/{tid}/argent", data=form, follow_redirects=False))
    assert ("Versement de 20,00 € par Bruno enregistré. ⚠️ Attention : un versement de 20,00 € était déjà noté pour "
            "Bruno (le 10/03/2027). Si c'est le même, clique sur « Défaire »") in page
    assert "&d=" in url and 'action="/annuler"' in cl.get(url).text  # « Défaire » est là, sur le message
    assert balances(app, tid)["Bruno"] == 1500                        # enregistré quand même : 55 − 20 − 20
    # même montant deux semaines plus tard : un vrai deuxième versement, pas d'alerte
    url, page = follow(cl, cl.post(f"/v/{tid}/argent", data=dict(form, date="2027-03-24", amount="20"),
                                   follow_redirects=False))
    assert "Attention" not in page
    # autre montant, autre personne, autre sens : pas d'alerte
    for change in ({"amount": "21"}, {"participant": str(ids["Anne"])}, {"kind": "remboursement"}):
        url, page = follow(cl, cl.post(f"/v/{tid}/argent", data=dict(form, **change), follow_redirects=False))
        assert "Attention" not in page, change
    # sans date : comparé quand même (on ne sait pas, donc on prévient)
    url, page = follow(cl, cl.post(f"/v/{tid}/argent", data=dict(form, date=""), follow_redirects=False))
    assert "Attention : un versement de 20,00 € était déjà noté pour Bruno" in page


def test_duplicate_movement_rule(tmp_path):
    app, cl, tid, ids = setup(tmp_path)
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        d = dt.date
        david = ids["David"]                                           # il a versé 200 € le 1er mars 2027
        assert S.duplicate_movement(t, david, "versement", 20000, d(2027, 3, 1)) is not None
        assert S.duplicate_movement(t, david, "versement", 20000, d(2027, 3, 4)) is not None      # à 3 jours près
        assert S.duplicate_movement(t, david, "versement", 20000, d(2027, 3, 5)) is None
        assert S.duplicate_movement(t, david, "versement", 20000, None) is not None
        assert S.duplicate_movement(t, david, "versement", 20001, d(2027, 3, 1)) is None
        assert S.duplicate_movement(t, david, "remboursement", 20000, d(2027, 3, 1)) is None
        assert S.duplicate_movement(t, ids["Anne"], "versement", 20000, d(2027, 3, 1)) is None
        only = t.movements[0].id
        assert S.duplicate_movement(t, david, "versement", 20000, d(2027, 3, 1), ignore_id=only) is None


def expense_form(ids, label, amount, **more):
    data = {"label": label, "actual": amount, "payer": "asso", "category": "Divers"}
    data.update({f"inc_{pid}": "1" for pid in ids.values()})
    data.update({f"w_{pid}": "1" for pid in ids.values()})
    data.update(more)
    return data


def test_same_expense_twice_is_flagged(tmp_path):
    app, cl, tid, ids = setup(tmp_path)
    with app.state.db.Session() as s:
        before = s.query(M.Expense).count()
    r = cl.post(f"/v/{tid}/depenses", data=expense_form(ids, "gîte", "400"), follow_redirects=False)
    url, page = follow(cl, r)
    with app.state.db.Session() as s:
        assert s.query(M.Expense).count() == before + 1, page          # enregistrée quand même
    assert ("« gîte » enregistrée. ⚠️ Attention : une dépense « Gîte » de 400,00 € existait déjà. Si c'est la même, "
            "clique sur « Défaire »") in page
    assert "&d=" in url
    # même nom, autre montant : rien à signaler ; modifier une dépense ne se compare pas à elle-même
    url, page = follow(cl, cl.post(f"/v/{tid}/depenses", data=expense_form(ids, "Gîte", "380"), follow_redirects=False))
    assert "Attention" not in page
    with app.state.db.Session() as s:
        eid = s.query(M.Expense).filter_by(label="Essence").one().id
    url, page = follow(cl, cl.post(f"/v/{tid}/depenses/{eid}", data=expense_form(ids, "Essence", "60"),
                                   follow_redirects=False))
    assert "Attention" not in page and "« Essence » enregistrée" in page


# ------------------------------------------------------------------ IA : fichier, API, outils
def test_method_through_the_ai_paths(tmp_path):
    app, cl, tid, ids = setup(tmp_path)
    ops = Ops(app.state.db)
    out = ops.add_movement(tid, "Anne", "50", date="2027-03-10", moyen="CB")
    assert "Versement de 50,00 € (Anne) enregistré." in clean(out["message"]) and "Attention" not in out["message"]
    twice = ops.add_movement(tid, "Anne", "50", date="2027-03-11", moyen="chèque")
    assert "Attention : un versement identique était déjà noté le 10/03/2027 (peut-être un doublon)" in twice["message"]
    with pytest.raises(OpsError) as err:
        ops.add_movement(tid, "Anne", "50", moyen="troc")
    assert "Moyen de paiement inconnu : « troc »" in str(err.value) and "chèque" in str(err.value)
    detail = ops.trip(tid)
    assert [(m["participant"], m["moyen"]) for m in detail["mouvements"]] == [
        ("David", None), ("Anne", "carte bancaire"), ("Anne", "chèque")]
    # fichier préparé par une IA
    res = ops.import_trip({"versements": [
        {"participant": "Bruno", "montant": "55", "date": "2027-04-01", "moyen": "Espèces"},
        {"participant": "Chloé", "montant": "15", "date": "2027-04-01"}]}, voyage=tid)
    assert "erreur" not in res, res
    assert [(m[0], m[2], m[4]) for m in moves(app, tid)[-2:]] == [("Bruno", 5500, "especes"), ("Chloé", 1500, "")]
    n = len(moves(app, tid))
    try:
        res = ops.import_trip({"versements": [{"participant": "Chloé", "montant": "5", "moyen": "coquillages"}]},
                              voyage=tid)
    except Exception as err:                                           # refusé : par une erreur ou par « erreur »
        res = {"erreur": str(err) + str(getattr(err, "errors", ""))}
    assert "Moyen de paiement inconnu" in str(res) and len(moves(app, tid)) == n
    # API locale
    r = cl.post(f"/api/voyages/{tid}/mouvements", json={"participant": "Chloé", "montant": "10", "moyen": "virement"})
    assert r.status_code == 200, r.text
    assert moves(app, tid)[-1][4] == "virement"


def test_method_survives_trip_export_and_import(tmp_path):
    app, cl, tid, ids = setup(tmp_path)
    cl.post(f"/v/{tid}/argent", data={"participant": str(ids["Anne"]), "kind": "versement", "amount": "50",
                                      "method": "cheque"})
    with app.state.db.Session() as s:
        mid = s.query(M.Movement).filter_by(method="cheque").one().id
    cl.post(f"/v/{tid}/argent/{mid}/depose", data={"etat": "1"})
    r = cl.get(f"/v/{tid}/voyage.zip")
    assert r.status_code == 200 and zipfile.is_zipfile(io.BytesIO(r.content))
    app2 = create_app(tmp_path / "ailleurs")
    with app2.state.db.Session() as s:
        trips, _ = TR.import_zip(s, r.content, app2.state.db.receipts_dir)
        s.commit()
        got = [(m.participant.name, m.amount, m.method, m.deposited) for m in trips[0].movements]
    assert ("Anne", 5000, "cheque", True) in got and ("David", 20000, "", False) in got


# ------------------------------------------------------------------ dépenses : ranger, chercher
def many_expenses(tmp_path):
    app, cl, tid, ids = setup(tmp_path)
    ops = Ops(app.state.db)
    ops.add_expense(tid, "Péage", categorie="Transport", montant_reel="24,50", paye_par="Anne", date="2027-05-02")
    ops.add_expense(tid, "Courses", categorie="Repas", montant_prevu="150")
    ops.add_expense(tid, "Bateau", categorie="Activités", montant_reel="900", date="2027-05-03")
    return app, cl, tid, ids


def order(page):
    return re.findall(r'<tr id="e\d+"[^>]*>\s*<td><a href="[^"]+"><b>([^<]+)</b>', page)


def test_expenses_can_be_listed_by_amount_date_or_name(tmp_path):
    app, cl, tid, ids = many_expenses(tmp_path)
    page = cl.get(f"/v/{tid}/depenses").text
    assert 'id="outils"' in page and page.count("data-exp-group") == 4       # quatre catégories, comme d'habitude
    assert order(cl.get(f"/v/{tid}/depenses?tri=montant").text) == ["Bateau", "Gîte", "Courses", "Essence", "Péage"]
    assert order(cl.get(f"/v/{tid}/depenses?tri=nom").text) == ["Bateau", "Courses", "Essence", "Gîte", "Péage"]
    by_date = cl.get(f"/v/{tid}/depenses?tri=date").text
    assert order(by_date)[:2] == ["Bateau", "Péage"]                           # les datées d'abord, la plus récente en haut
    flat = clean(by_date)
    assert "Toutes les dépenses, les plus récentes en haut" in flat and by_date.count("data-exp-group") == 1
    assert "1 534,50 €" in flat                                                # 400 + 60 + 24,50 + 150 + 900
    assert '<div class="muted small exp-cat">Activités</div>' in by_date and 'value="date" selected' in by_date
    # un tri inconnu : la liste habituelle
    assert cl.get(f"/v/{tid}/depenses?tri=n'importe").text.count("data-exp-group") == 4


def test_filter_toolbar_only_when_useful_and_rows_carry_their_facts(tmp_path):
    app, cl, tid, ids = setup(tmp_path)
    assert 'id="outils"' not in cl.get(f"/v/{tid}/depenses").text           # deux dépenses : pas de barre
    app, cl, tid, ids = many_expenses(tmp_path / "b")
    page = cl.get(f"/v/{tid}/depenses").text
    rows = {m.group(2): m.group(1) for m in re.finditer(r'<tr id="e\d+"([^>]*)>\s*<td><a href="[^"]+"><b>([^<]+)</b>', page)}
    assert 'data-amount="40000"' in rows["Gîte"] and 'data-state="sansjustif"' in rows["Gîte"]
    assert 'data-payers="asso "' in rows["Gîte"]
    assert f'data-payers="{ids["Bruno"]} "' in rows["Essence"]
    assert 'data-state="prevu"' in rows["Courses"] and 'data-amount="15000"' in rows["Courses"]
    opts = re.search(r'<select id="exp-payer">(.*?)</select>', page, re.S).group(1)
    assert [clean(x) for x in re.findall(r">([^<]+)</option>", opts)] == ["Tout le monde", "L'asso", "Anne", "Bruno"]


def test_expense_filters_in_the_browser(tmp_path):
    import threading
    import time

    import uvicorn
    from playwright.sync_api import sync_playwright

    from conftest import _free_port
    app, cl, tid, ids = many_expenses(tmp_path)
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_context(viewport={"width": 1366, "height": 900}, locale="fr-FR").new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("dialog", lambda d: d.accept())
            page.goto(f"{base}/v/{tid}/depenses")
            shown = lambda: [clean(x) for x in page.locator("tr[data-exp]:visible b").all_inner_texts()
                             if x.strip() in ("Gîte", "Essence", "Péage", "Courses", "Bateau")]
            assert len(shown()) == 5 and page.locator("#exp-count").is_hidden()
            # chercher : sans accent ni majuscule ; le total de la catégorie suit ce qui est visible
            page.fill("#exp-q", "peage")
            assert shown() == ["Péage"]
            assert clean(page.locator("#exp-count").inner_text()).startswith("1 dépense affichée sur 5 · 24,50 €")
            assert page.locator("details[data-exp-group]:visible").count() == 1
            assert clean(page.locator("details[data-exp-group]:visible summary [data-exp-total]").inner_text()) == "24,50 €"
            # par le nom de la catégorie
            page.fill("#exp-q", "transport")
            assert sorted(shown()) == ["Essence", "Péage"]
            assert clean(page.locator("#exp-count").inner_text()).startswith("2 dépenses affichées sur 5 · 84,50 €")
            page.click("[data-exp-reset]")
            assert len(shown()) == 5 and page.locator("#exp-count").is_hidden()
            assert clean(page.locator("details[data-exp-group] summary [data-exp-total]").first.inner_text()) == "400,00 €"
            # payé par Bruno, par la caisse
            page.select_option("#exp-payer", str(ids["Bruno"]))
            assert shown() == ["Essence"]
            page.select_option("#exp-payer", "asso")
            assert sorted(shown()) == ["Bateau", "Courses", "Gîte"]
            assert "3 dépenses affichées sur 5 · 1 450,00 €" in clean(page.locator("#exp-count").inner_text())
            # … dont le montant n'est encore que prévu
            page.select_option("#exp-state", "prevu")
            assert shown() == ["Courses"]
            page.select_option("#exp-payer", "")
            page.select_option("#exp-state", "sansjustif")
            assert sorted(shown()) == ["Bateau", "Essence", "Gîte", "Péage"]
            page.fill("#exp-q", "zzz")
            assert shown() == [] and "Aucune dépense ne correspond" in page.locator("#exp-count").inner_text()
            # ranger par montant : la page se recharge, en une seule liste
            page.click("[data-exp-reset]")
            page.select_option("#exp-sort", "montant")
            page.wait_for_url("**/depenses?tri=montant")
            assert shown() == ["Bateau", "Gîte", "Courses", "Essence", "Péage"]

            # --- « Reste à régler » : cocher, voir le total, tout noter d'un coup
            page.goto(f"{base}/v/{tid}/argent")
            go = page.locator("#lot-versement [data-batch-go]")
            assert go.is_hidden() and page.locator("#lot-versement [data-batch-none]").is_visible()
            # parts : Gîte 100 + Essence 15 + Péage 6,125 + Courses 37,50 + Bateau 225 = 383,625 € chacun (au centime près)
            page.check(f'input[name="qui"][value="{ids["Chloé"]}"]')
            assert go.is_visible() and clean(go.inner_text()).startswith("✓ Reçu : 1 ligne, 383,6")
            page.check("#lot-versement [data-batch-all]")
            n = page.locator('input[name="qui"]').count()
            assert page.locator('input[name="qui"]:checked').count() == n == 4      # David doit encore 183,63 €
            with app.state.db.Session() as s:
                r = S.compute_trip(s.get(M.Trip, tid))
                due = {a.name: c for a, b, c in r.settlements() if b is S.E.ASSO}
            assert set(due) == {"Anne", "Bruno", "Chloé", "David"}
            total = sum(due.values())
            assert total == 153450 - 6000 - 2450 - 20000                # tout le voyage, moins les avances et l'acompte
            assert clean(go.inner_text()) == "✓ Reçu : 4 lignes, 1 250,00 €"
            page.select_option("#lot-m-versement", "cheque")
            go.click()
            page.wait_for_url("**/argent?msg=*")
            assert "4 versements reçus enregistrés, 1 250,00 € en tout" in clean(page.locator("body").inner_text())
            assert {m[0]: m[2] for m in moves(app, tid)[1:]} == due
            assert all(m[4] == "cheque" for m in moves(app, tid)[1:])
            b = balances(app, tid)
            assert b["Anne"] == b["Bruno"] == b["Chloé"] == b["David"] == 0
            assert "Tout est réglé" in clean(page.locator("body").inner_text())
            assert "4 chèques reçus, pas encore déposés à la banque (1 250,00 €)" in clean(page.locator("body").inner_text())
            assert not errors, errors
            browser.close()
    finally:
        srv.should_exit = True
        th.join(5)
