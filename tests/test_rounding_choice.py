"""Qui paie les centimes d'arrondi : les participants (par défaut, au centime supérieur) ou l'asso (au centime
inférieur). Montants vérifiés à la main : 100 € pour 3 personnes = 33,333… €."""
import html
import re

import pytest
from fastapi.testclient import TestClient

from compta import engine as E
from compta import models as M
from compta import services as S
from compta.ops import Ops
from compta.web import create_app

P3 = [E.Participant(i, f"P{i}") for i in (1, 2, 3)]
ALL3 = {1: 1, 2: 1, 3: 1}


def norm(t):
    return re.sub(r"[  \xa0]", " ", html.unescape(t))


def test_engine_participants_pay_cents_by_default():
    r = E.compute(P3, [E.Expense(1, "Gîte", actual=10000, weights=ALL3)])
    assert [r.people[i].share for i in (1, 2, 3)] == [3334, 3334, 3334]   # 33,333… → 33,34
    assert r.rounding_gain == 2 and r.rounding_mode == "participants" and r.is_balanced()


def test_engine_asso_takes_cents():
    r = E.compute(P3, [E.Expense(1, "Gîte", actual=10000, weights=ALL3)], rounding=E.ROUND_ASSO)
    assert [r.people[i].share for i in (1, 2, 3)] == [3333, 3333, 3333]   # 33,333… → 33,33
    assert r.rounding_gain == -1 and r.is_balanced() and not r.warnings    # 1 centime pour l'asso
    # montants ronds : aucun écart, quel que soit le choix
    r = E.compute(P3, [E.Expense(1, "Gîte", actual=9000, weights=ALL3)], rounding=E.ROUND_ASSO)
    assert [r.people[i].share for i in (1, 2, 3)] == [3000] * 3 and r.rounding_gain == 0
    # cas du voyage : 189,22 € ÷ 11 = 17,2018… → 17,20 chacun, 0,02 € pour l'asso (au lieu de 17,21 et +0,09)
    p11 = [E.Participant(i, f"P{i}") for i in range(1, 12)]
    e = E.Expense(1, "Essence", actual=18922, weights={i: 1 for i in range(1, 12)})
    a, b = E.compute(p11, [e], rounding=E.ROUND_ASSO), E.compute(p11, [e])
    assert {p.share for p in a.people.values()} == {1720} and a.rounding_gain == 18920 - 18922
    assert {p.share for p in b.people.values()} == {1721} and b.rounding_gain == 18931 - 18922


def test_engine_asso_mode_with_asso_charges_balances():
    """Une part prise en charge par l'asso (désistement) + l'arrondi pour l'asso : l'équation reste juste."""
    w = E.Withdrawal(3, E.ASSO, [])
    r = E.compute(P3, [E.Expense(1, "Gîte", actual=10000, weights=ALL3)], withdrawals=[w], rounding=E.ROUND_ASSO)
    assert r.total_shares + r.asso_total - r.rounding_gain == r.total_cost and r.is_balanced()


@pytest.fixture()
def trip(tmp_path):
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.create_trip("x")["voyage_id"]
    ops.add_participants(tid, ["Anne", "Bob", "Chloé"])
    ops.import_trip({"voyage_id": tid, "depenses": [{"libelle": "Gîte", "reel": "100", "pour": "tous"}],
                     "versements": [{"participant": n, "montant": "33,34"} for n in ("Anne", "Bob", "Chloé")]},
                    voyage=tid, verifier_id=tid)
    return app, TestClient(app), tid, ops


def test_web_choice_changes_everything(trip):
    app, c, tid, ops = trip
    page = norm(c.get(f"/v/{tid}/asso").text)
    assert "Centimes d'arrondi : participants" in page and 'value="participants" checked' in page
    assert "Aujourd'hui : 0,02 € au profit de" in page
    assert "Arrondis au centime : 0,02 € au profit de" in norm(c.get(f"/v/{tid}").text)

    r = c.post(f"/v/{tid}/asso/arrondi", data={"rounding": "asso"}, follow_redirects=True)
    page = norm(r.text)
    assert "Les centimes d'arrondi sont pris en charge par" in page and "↶ Défaire" in page
    assert 'value="asso" checked' in page and "Aujourd'hui : 0,01 € pris en charge par" in page
    assert "− Les arrondis au centime" in page and "arrondi au centime inférieur" in page
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        r_ = S.compute_trip(t)
        assert {p.share for p in r_.people.values()} == {3333}
        m = S.money_summary(t, r_)
        # chacun a versé 33,34 € pour une part de 33,33 € : 0,01 € à rendre à chacun ; la caisse a 100,02 − 100 = 0,02
        assert (m["to_refund"], m["cash"], m["final"], m["rounding_only"]) == (3, 2, -1, True)
        assert norm(S.final_sentence(m, "l'asso", E.fmt)) == "Le voyage coûte 0,01 € à l'asso (arrondis au centime)."
        subject, body = S.treasurer_mail(t, r_)
        assert "0,01 € à l'asso (arrondis au centime)" in norm(body)
    assert "Arrondis au centime : 0,01 € pris en charge par" in norm(c.get(f"/v/{tid}").text)
    mx = norm(c.get(f"/v/{tid}/repartition").text)
    assert "+ Arrondis (pris en charge par" in mx and "✓" in mx
    pdf = c.get(f"/v/{tid}/export.pdf")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"

    # Défaire : retour aux participants
    c.post("/annuler", headers={"referer": f"http://testserver/v/{tid}/asso"})
    with app.state.db.Session() as s:
        assert s.get(M.Trip, tid).rounding == "participants"
    r = c.post(f"/v/{tid}/asso/arrondi", data={"rounding": "n'importe quoi"}, follow_redirects=True)
    assert "Choix d'arrondi inconnu" in norm(r.text)


def test_choice_kept_by_export_import(trip, tmp_path):
    from compta import transfer as T
    app, c, tid, ops = trip
    c.post(f"/v/{tid}/asso/arrondi", data={"rounding": "asso"})
    with app.state.db.Session() as s:
        z = T.export_zip(s, [s.get(M.Trip, tid)], app.state.db.receipts_dir)
    app2 = create_app(tmp_path / "autre")
    with app2.state.db.Session() as s:
        trips, _ = T.import_zip(s, z, app2.state.db.receipts_dir)
        s.commit()
        assert trips[0].rounding == "asso"


def test_expense_preview_follows_choice(trip):
    app, c, tid, ops = trip
    pids = [p["id"] for p in ops.trip(tid)["participants"]]
    form = {"label": "Bateau", "actual": "10", **{f"inc_{i}": "1" for i in pids}}
    assert "0,02 € pour" in norm(c.post(f"/v/{tid}/depenses/apercu", data=form).text)
    c.post(f"/v/{tid}/asso/arrondi", data={"rounding": "asso"})
    assert "0,01 € pris en charge par" in norm(c.post(f"/v/{tid}/depenses/apercu", data=form).text)
