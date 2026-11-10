"""Clôturer un voyage (comptes figés, réouvrable) et le dupliquer (préparer le suivant).

Voyage de test « Rando 2026 » (montants vérifiés à la main) : Anne, Bruno, Chloé ; la caisse est l'asso.
  Gîte : prévu 320 €, payé 300 €, pour tous                        → 100 € chacun
  Essence 90 € avancée par Anne, pour Anne et Bruno                 → 45 € chacun
  Bruno a versé 50 €.
  Parts : Anne 145, Bruno 145, Chloé 100 ; restes : Anne 55, Bruno 95, Chloé 100 (à recevoir : 250 €).
Copie « Rando 2027 » : mêmes personnes, Gîte prévu 300 € (le payé devient l'estimation), Essence prévue 90 € ;
  parts estimées identiques (145, 145, 100), aucun versement.
"""
import html
import re

import pytest
from fastapi.testclient import TestClient

from compta import models as M
from compta import services as S
from compta.ops import Ops, OpsError
from compta.web import create_app

SHARES = {"Anne": 14500, "Bruno": 14500, "Chloé": 10000}
LEFT = {"Anne": 5500, "Bruno": 9500, "Chloé": 10000}


def clean(t):
    return re.sub(r"\s+", " ", html.unescape(t).replace("\xa0", " "))


@pytest.fixture
def trip(tmp_path):
    app = create_app(tmp_path / "voyages")
    cl = TestClient(app)
    ops = Ops(app.state.db)
    tid = ops.create_trip("Rando 2026")["voyage_id"]
    ops.add_participants(tid, ["Anne", "Bruno", "Chloé"])
    ops.add_expense(tid, "Gîte", categorie="Logement", montant_prevu="320", montant_reel="300", date="2026-06-12")
    ops.add_expense(tid, "Essence", categorie="Transport", montant_reel="90", paye_par="Anne", pour=["Anne", "Bruno"])
    ops.add_movement(tid, "Bruno", "50")
    cl.post(f"/v/{tid}/justificatifs", files=[("files", ("facture.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf"))])
    cl.post(f"/v/{tid}/envoyer/comment-payer", data={"pay_info": "Virement sur le compte du club"})
    return app, cl, ops, tid


def figures(app, tid):
    with app.state.db.Session() as s:
        r = S.compute_trip(s.get(M.Trip, tid))
        return ({p.name: p.share for p in r.people.values()}, {p.name: p.balance for p in r.people.values()})


def count(app, model, tid):
    with app.state.db.Session() as s:
        return s.query(model).filter_by(trip_id=tid).count()


# ------------------------------------------------------------------ clôturer
def test_close_freezes_the_trip_on_screen(trip):
    app, cl, ops, tid = trip
    page = cl.get(f"/v/{tid}").text
    assert f'action="/v/{tid}/cloturer"' in page
    assert "tout n'est pas réglé (reste à recevoir 250,00 €)" in clean(page)      # prévenu avant de clôturer
    r = cl.post(f"/v/{tid}/cloturer", follow_redirects=False)
    assert r.status_code == 303 and "e=1" not in r.headers["location"]
    page = clean(cl.get(r.headers["location"]).text)
    assert "Voyage clôturé : ses comptes sont figés" in page and "Voyage clôturé le" in page
    assert "Rouvrir le voyage" in page and f'action="/v/{tid}/cloturer"' not in page
    assert "Modifier le voyage" not in page
    before = figures(app, tid)
    assert before == (SHARES, LEFT)

    def refused(path, **kw):
        r = cl.post(path, follow_redirects=False, **kw)
        assert r.status_code == 303 and "e=1" in r.headers["location"], path
        assert "Ce voyage est clôturé" in clean(cl.get(r.headers["location"]).text)

    refused(f"/v/{tid}/depenses", data={"label": "Péage", "actual": "12", "category": "Transport"})
    refused(f"/v/{tid}/argent", data={"participant": "1", "kind": "versement", "amount": "10"})
    refused(f"/v/{tid}/participants", data={"names": "Zoé"})
    refused(f"/v/{tid}/modifier", data={"name": "Autre nom"})
    refused(f"/v/{tid}/envoyer/comment-payer", data={"pay_info": "autre"})
    refused(f"/v/{tid}/justificatifs", files=[("files", ("x.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf"))])
    with app.state.db.Session() as s:
        eid = s.query(M.Expense).filter_by(label="Gîte").one().id
    refused(f"/v/{tid}/depenses/{eid}/supprimer")
    assert figures(app, tid) == before
    assert count(app, M.Expense, tid) == 2 and count(app, M.Movement, tid) == 1 and count(app, M.Receipt, tid) == 1
    with app.state.db.Session() as s:
        assert s.get(M.Trip, tid).name == "Rando 2026" and S.pay_info(s.get(M.Trip, tid)) == "Virement sur le compte du club"


def test_closed_trip_can_still_be_read_and_exported(trip):
    app, cl, ops, tid = trip
    cl.post(f"/v/{tid}/cloturer")
    for path in ("", "/participants", "/depenses", "/argent", "/justificatifs", "/repartition", "/en-images",
                 "/envoyer", "/export", "/transfert", "/asso", "/dupliquer"):
        r = cl.get(f"/v/{tid}{path}")
        assert r.status_code == 200, path
        assert 'id="voyage-cloture"' in r.text and 'data-closed="1"' in r.text, path
    assert cl.get(f"/v/{tid}/export.pdf").status_code == 200
    assert cl.get(f"/v/{tid}/export.xlsx").status_code == 200
    assert cl.get(f"/v/{tid}/export.zip").status_code == 200


def test_closed_trip_refuses_the_ai_and_the_api(trip):
    app, cl, ops, tid = trip
    cl.post(f"/v/{tid}/cloturer")
    assert ops.trip(tid)["cloture_le"] is not None
    for call in (lambda: ops.add_expense(tid, "Péage", montant_reel="12"),
                 lambda: ops.add_participants(tid, ["Zoé"]),
                 lambda: ops.add_movement(tid, "Bruno", "10")):
        with pytest.raises(OpsError, match="clôturé"):
            call()
    r = cl.post(f"/api/voyages/{tid}/depenses", json={"libelle": "Péage", "montant_reel": 12})
    assert r.status_code >= 400 and "clôturé" in r.text
    from compta import importer as I
    with app.state.db.Session() as s:
        with pytest.raises(I.ImportErrors) as err:
            I.run_import(s, {"voyage_id": tid, "depenses": [{"libelle": "Péage", "montant_reel": 12}]},
                         s.get(M.Trip, tid), expected_id=tid)
        assert "clôturé" in " ".join(err.value.errors)
    assert figures(app, tid) == (SHARES, LEFT) and count(app, M.Expense, tid) == 2


def test_reopen_and_undo(trip):
    app, cl, ops, tid = trip
    cl.post(f"/v/{tid}/cloturer")
    r = cl.post(f"/v/{tid}/rouvrir", follow_redirects=False)
    assert "Voyage rouvert" in clean(cl.get(r.headers["location"]).text)
    assert ops.trip(tid)["cloture_le"] is None
    ops.add_expense(tid, "Péage", montant_reel="12")                     # de nouveau modifiable
    assert count(app, M.Expense, tid) == 3
    cl.post(f"/v/{tid}/cloturer")
    cl.post("/annuler", data={})                                         # « Défaire » la clôture
    assert ops.trip(tid)["cloture_le"] is None
    assert "déjà ouvert" in clean(cl.post(f"/v/{tid}/rouvrir", follow_redirects=True).text)


def test_home_puts_closed_trips_aside(trip):
    app, cl, ops, tid = trip
    other = ops.create_trip("Week-end en cours")["voyage_id"]
    page = cl.get("/").text
    assert 'id="clotures"' not in page
    cl.post(f"/v/{tid}/cloturer")
    page = cl.get("/").text
    main, closed = page.split('id="clotures"')
    assert "Voyages clôturés (1)" in clean(closed) and "Rando 2026" in closed and "🔒 Clôturé" in closed
    assert "Week-end en cours" in main and "Rando 2026" not in main.split('class="trip-grid"')[-1]
    assert "<details class=\"closed-trips\" id=\"clotures\">" in page   # replié tant qu'il reste un voyage en cours
    cl.post(f"/v/{other}/supprimer")
    page = cl.get("/").text
    assert 'id="clotures" open' in page and "Aucun voyage en cours" in clean(page)


def test_closed_trip_can_be_deleted_and_demo_cannot_be_closed(trip):
    app, cl, ops, tid = trip
    cl.post(f"/v/{tid}/cloturer")
    r = cl.post(f"/v/{tid}/supprimer", follow_redirects=False)
    assert "e=1" not in r.headers["location"]
    with app.state.db.Session() as s:
        assert s.get(M.Trip, tid) is None
    cl.post("/exemple/ouvrir")
    with app.state.db.Session() as s:
        demo = s.query(M.Trip).filter_by(is_demo=True).one().id
    assert f'action="/v/{demo}/cloturer"' not in cl.get(f"/v/{demo}").text


# ------------------------------------------------------------------ dupliquer
def test_next_trip_name():
    assert S.next_trip_name("Rando 2026") == "Rando 2027"
    assert S.next_trip_name("Hendaye 2026 – mai 2026") == "Hendaye 2026 – mai 2027"
    assert S.next_trip_name("Week-end à la mer") == "Week-end à la mer (copie)"
    assert S.next_trip_name("Rando 2026", ["rando 2027"]) == "Rando 2027 (2)"
    assert S.next_trip_name("Sortie 12345") == "Sortie 12345 (copie)"           # pas une année


def test_duplicate_page_proposes_next_year(trip):
    app, cl, ops, tid = trip
    assert f'href="/v/{tid}/dupliquer"' in cl.get(f"/v/{tid}").text
    page = clean(cl.get(f"/v/{tid}/dupliquer").text)
    assert 'value="Rando 2027"' in page
    assert "Les participants (3)" in re.sub(r"<[^>]+>", "", page) and "Les dépenses (2)" in re.sub(r"<[^>]+>", "", page)
    assert "montants prévus" in page and "Jamais repris : l'argent reçu et rendu, les justificatifs" in page


def test_duplicate_copies_people_and_expenses_as_planned(trip):
    app, cl, ops, tid = trip
    r = cl.post(f"/v/{tid}/dupliquer", data={"name": "Rando 2027", "start": "2027-06-11", "end": "2027-06-13",
                                              "participants": "1", "depenses": "1"}, follow_redirects=False)
    assert r.status_code == 303
    new = int(re.match(r"/v/(\d+)", r.headers["location"]).group(1))
    assert new != tid
    page = clean(cl.get(r.headers["location"]).text)
    assert "Voyage « Rando 2027 » créé à partir de « Rando 2026 » : 3 participants, 2 dépenses reprises comme " \
           "montants prévus. Rien n'a changé dans l'ancien voyage." in page
    with app.state.db.Session() as s:
        t = s.get(M.Trip, new)
        assert (t.name, str(t.start), str(t.end), t.closed_on, t.is_demo) == ("Rando 2027", "2027-06-11", "2027-06-13",
                                                                              None, False)
        assert sorted(p.name for p in t.participants) == ["Anne", "Bruno", "Chloé"]
        exp = {e.label: e for e in t.expenses}
        assert (exp["Gîte"].planned, exp["Gîte"].actual, exp["Gîte"].date) == (30000, None, None)   # payé → prévu
        assert (exp["Essence"].planned, exp["Essence"].actual) == (9000, None)
        assert exp["Essence"].payer.name == "Anne" and exp["Gîte"].payer_id is None
        assert sorted(sh.participant_id for sh in exp["Essence"].shares) == sorted(
            p.id for p in t.participants if p.name in ("Anne", "Bruno"))
        assert exp["Gîte"].category.name == "Logement"
        assert not t.movements and not t.receipts and not t.withdrawals and not t.adjustments and not t.subaccounts
        assert S.pay_info(t) == "Virement sur le compte du club"
        assert [c.name for c in t.categories] == [c.name for c in s.get(M.Trip, tid).categories]
        r_ = S.compute_trip(t)
        assert {p.name: p.share for p in r_.people.values()} == SHARES            # estimation : mêmes parts
        assert all(p.paid == 0 and p.refunded == 0 for p in r_.people.values())
    assert figures(app, tid) == (SHARES, LEFT)                                    # l'ancien voyage n'a pas bougé
    assert count(app, M.Movement, tid) == 1 and count(app, M.Receipt, tid) == 1
    cl.post("/annuler", data={})                                                  # la copie se défait
    with app.state.db.Session() as s:
        assert s.get(M.Trip, new) is None and s.get(M.Trip, tid) is not None


def test_duplicate_without_people_or_expenses(trip):
    app, cl, ops, tid = trip
    r = cl.post(f"/v/{tid}/dupliquer", data={"name": "Rando sans personne", "depenses": "1"}, follow_redirects=False)
    new = int(re.match(r"/v/(\d+)", r.headers["location"]).group(1))
    with app.state.db.Session() as s:
        t = s.get(M.Trip, new)
        assert not t.participants and len(t.expenses) == 2
        assert all(not e.shares and e.payer_id is None and e.actual is None for e in t.expenses)
        assert sorted(e.planned for e in t.expenses) == [9000, 30000]
    assert cl.get(f"/v/{new}").status_code == 200 and cl.get(f"/v/{new}/depenses").status_code == 200
    r = cl.post(f"/v/{tid}/dupliquer", data={"name": "Rando réglages seuls"}, follow_redirects=False)
    new2 = int(re.match(r"/v/(\d+)", r.headers["location"]).group(1))
    with app.state.db.Session() as s:
        t = s.get(M.Trip, new2)
        assert not t.participants and not t.expenses and len(t.categories) >= 2
    assert "créé à partir de « Rando 2026 ». Rien n'a changé" in clean(cl.get(r.headers["location"]).text)


def test_duplicate_keeps_families_labels_and_cash_holder(tmp_path):
    app = create_app(tmp_path / "voyages")
    cl = TestClient(app)
    r = cl.post("/voyages", data={"name": "Famille 2026", "caisse": "personne", "cashier_name": "Camille"},
                follow_redirects=False)
    tid = int(re.match(r"/v/(\d+)", r.headers["location"]).group(1))
    ops = Ops(app.state.db)
    ops.add_participants(tid, ["Léa Lebrun", "Tom Lebrun", "Zoé"])
    with app.state.db.Session() as s:
        ids = {p.name: p.id for p in s.get(M.Trip, tid).participants}
    cl.post(f"/v/{tid}/familles", data={"name": "Lebrun", "members": [str(ids["Léa Lebrun"]), str(ids["Tom Lebrun"])],
                                        "referent": str(ids["Léa Lebrun"])})
    r = cl.post(f"/v/{tid}/dupliquer", data={"name": "Famille 2027", "participants": "1"}, follow_redirects=False)
    new = int(re.match(r"/v/(\d+)", r.headers["location"]).group(1))
    with app.state.db.Session() as s:
        t, old = s.get(M.Trip, new), s.get(M.Trip, tid)
        assert S.cashier(t).name == "Camille" and S.cashier(t).id != S.cashier(old).id
        assert len(t.families) == len(old.families)
        for fam in t.families:
            assert sorted(p.name for p in t.participants if p.family_id == fam.id) == ["Léa Lebrun", "Tom Lebrun"]
        assert t.roles_conf == old.roles_conf


def test_duplicate_refusals(trip):
    app, cl, ops, tid = trip

    def refused(data, words):
        r = cl.post(f"/v/{tid}/dupliquer", data=data, follow_redirects=False)
        assert "e=1" in r.headers["location"] and r.headers["location"].startswith(f"/v/{tid}/dupliquer")
        assert words in clean(cl.get(r.headers["location"]).text)

    refused({"name": "  "}, "Donne un nom au nouveau voyage")
    refused({"name": "rando 2026"}, "Un voyage s'appelle déjà")
    refused({"name": "Rando 2027", "start": "2027-06-13", "end": "2027-06-11"}, "La date de fin est avant")
    with app.state.db.Session() as s:
        assert s.query(M.Trip).count() == 1


def test_closed_trip_can_be_duplicated(trip):
    app, cl, ops, tid = trip
    cl.post(f"/v/{tid}/cloturer")
    r = cl.post(f"/v/{tid}/dupliquer", data={"name": "Rando 2027", "participants": "1", "depenses": "1"},
                follow_redirects=False)
    new = int(re.match(r"/v/(\d+)", r.headers["location"]).group(1))
    assert ops.trip(new)["cloture_le"] is None and ops.trip(tid)["cloture_le"] is not None
    ops.add_expense(new, "Péage", montant_prevu="12")                    # la copie, elle, se modifie


def test_browser_closed_trip_forms_are_off(server, page):
    base, tid, _ = server
    page.goto(f"{base}/v/{tid}")
    page.click("form[action$='/cloturer'] button")                       # (la boîte de confirmation est acceptée)
    page.wait_for_selector("#voyage-cloture")
    page.goto(f"{base}/v/{tid}/argent")
    assert page.locator("main form[method=post]:not([data-closed-ok]) button:not([disabled])").count() == 0
    assert page.locator("#voyage-cloture form[action$='/rouvrir'] button").is_enabled()
    page.click("#voyage-cloture form[action$='/rouvrir'] button")
    page.wait_for_selector(".flash")
    assert page.locator("#voyage-cloture").count() == 0
    page.goto(f"{base}/v/{tid}/argent")
    assert page.locator("main form[method=post] button:not([disabled])").count() > 0
    assert not page.errors, page.errors
