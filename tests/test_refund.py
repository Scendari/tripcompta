"""« Dont remboursé » : une dépense en partie remboursée (réservation annulée, avoir) tient sur UNE ligne.
Cas réel anonymisé : l'asso paie 120,29 € un appartement puis l'annule ; l'hébergeur rend 90,29 € sur le compte de
l'organisateur (qui les reverse à l'asso dans son versement) : il reste 30 € de frais, soit 2 € par personne."""
import re

import pytest
from fastapi.testclient import TestClient

from compta import engine as E
from compta import models as M
from compta import services as S
from compta.db import Database
from compta.ops import Ops, OpsError
from compta.web import create_app

P = [E.Participant(i, f"P{i}") for i in range(1, 16)]
ALL = {i: 1 for i in range(1, 16)}


def test_engine_refund_one_line():
    e = E.Expense(1, "Appartement annulé", actual=12029, weights=ALL, refund=9029, refund_to=1)
    r = E.compute(P, [e], movements=[E.Movement(1, "versement", 27700 + 9029)])
    assert r.total_cost == 3000 and r.expense_totals[1] == 3000
    assert all(r.share(i, 1) == 200 for i in range(1, 16))                      # 2 € chacun, une seule ligne
    txt = r.lines_for(2)[0].components[0].text.replace(" ", " ").replace("\xa0", " ")
    assert txt == "Appartement annulé : 120,29 € − 90,29 € remboursés = 30,00 € ÷ 15 personnes"
    # P1 a reçu 90,29 pour l'asso puis les a reversés dans ses 367,29 : il est au même point que les autres
    assert r.people[1].balance == 200 - 27700 and r.is_balanced()


def test_refund_bigger_than_amount_warns():
    e = E.Expense(1, "X", actual=1000, weights=ALL, refund=2000)
    r = E.compute(P, [e])
    assert r.total_cost == 0 and any("dépasse le montant" in w for w in r.warnings)


@pytest.fixture()
def trip(tmp_path):
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.create_trip("x")["voyage_id"]
    ops.add_participants(tid, ["Anne", "Bob", "Chloé"])
    with app.state.db.Session() as s:
        Pp = {p.name: p.id for p in s.get(M.Trip, tid).participants}
    return app, TestClient(app), tid, Pp, ops


def test_web_form_refund(trip):
    app, c, tid, Pp, ops = trip
    data = {"label": "Appartement annulé", "category": "Logement", "actual": "120,29", "payer": "asso",
            "payer_kind": "asso", "has_refund": "1", "refund": "90,29", "refund_to": str(Pp["Anne"]),
            **{f"inc_{i}": "1" for i in Pp.values()}}
    r = c.post(f"/v/{tid}/depenses", data=data, follow_redirects=True)
    assert "enregistrée" in r.text
    d = ops.trip(tid)
    e = d["depenses"][0]
    assert (e["reel"], e["rembourse"], e["rembourse_a"]) == (120.29, 90.29, "Anne") and d["cout_total"] == 30.0
    p = {x["nom"]: x for x in d["participants"]}
    assert p["Bob"]["sa_part"] == 10.0 and p["Anne"]["reste"] == round(10 + 90.29, 2)   # Anne doit reverser 90,29
    page = re.sub(r"[ \xa0]", " ", c.get(f"/v/{tid}/depenses").text)
    assert "− 90,29 € remboursés (à Anne)" in page
    edit = c.get(f"/v/{tid}/depenses/{e['id']}/modifier").text
    assert 'name="has_refund" value="1" checked' in edit and 'value="90,29"' in edit
    # caisse : l'asso a payé 120,29 ; le remboursement est chez Anne (pas encore dans la caisse)
    with app.state.db.Session() as s:
        assert S.asso_cash(s.get(M.Trip, tid)) == (0, 12029)
    # décocher « remboursée » : plus de remboursement
    data.pop("has_refund")
    c.post(f"/v/{tid}/depenses/{e['id']}", data=data)
    assert ops.trip(tid)["depenses"][0]["rembourse"] is None and ops.trip(tid)["cout_total"] == 120.29
    # trop remboursé : refusé
    r = c.post(f"/v/{tid}/depenses", data={**data, "has_refund": "1", "refund": "200"}, follow_redirects=True)
    assert "dépasse le montant" in r.text


def test_refund_to_asso_goes_into_cash(trip):
    app, c, tid, Pp, ops = trip
    ops.add_expense(tid, "Gîte", montant_reel="300", rembourse="60")
    with app.state.db.Session() as s:
        assert S.asso_cash(s.get(M.Trip, tid)) == (6000, 30000)
    assert ops.trip(tid)["cout_total"] == 240.0


def test_ai_import_and_update_with_refund(trip):
    app, c, tid, Pp, ops = trip
    r = ops.import_trip({"voyage_id": tid, "depenses": [
        {"libelle": "Appartement annulé", "reel": "120,29", "rembourse": "90,29", "rembourse_a": "Anne"}]},
        str(tid), verifier_id=True)
    assert "120,29 € − 90,29 € remboursés (reçus par Anne)" in re.sub(r"[ \xa0]", " ", r["actions"][0])
    r = ops.import_trip({"voyage_id": tid, "depenses_a_modifier": [
        {"depense": "Appartement annulé", "rembourse": "100", "rembourse_a": "asso"}]}, str(tid), verifier_id=True)
    assert "remboursé 100,00 € (reçu par l’asso)" in re.sub(r"[ \xa0]", " ", r["actions"][0])
    with pytest.raises(OpsError, match="dépasse le montant"):
        ops.import_trip({"voyage_id": tid, "depenses_a_modifier": [
            {"depense": "Appartement annulé", "rembourse": "500"}]}, str(tid), verifier_id=True)


def test_ledger_shows_refund(trip):
    app, c, tid, Pp, ops = trip
    ops.add_expense(tid, "Appartement annulé", montant_reel="120,29", rembourse="90,29", rembourse_a="Anne")
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        L = S.ledger(t, S.compute_trip(t))
        acc = S.person_account(t, S.compute_trip(t), Pp["Anne"])
    e = L["expenses"][0]
    assert (e["amount"], e["refund"], e["refund_to"], e["ok"]) == (3000, 9029, "Anne", True)
    anne = {a["name"]: a for a in L["accounts"]}["Anne"]
    assert (anne["share"], anne["received"]) == (1000, 9029)
    items = [it["label"] for g in acc["groups"] for it in g["items"]]
    assert "Appartement annulé (remboursement)" in items
    page = re.sub(r"[ \xa0]", " ", c.get(f"/v/{tid}/comptes").text)
    assert "90,29 €" in page                                       # onglet Comptes : reçu pour la caisse
