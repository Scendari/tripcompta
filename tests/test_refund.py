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


# ------------------------------------------------------------- coût partagé = payé − remboursé, PARTOUT
# (retour d'un organisateur : l'aperçu du formulaire répartissait 120,29 € au lieu de 30 € ; le calcul était juste)

def test_table_shows_shared_cost_first(trip):
    app, c, tid, Pp, ops = trip
    data = {"label": "Appartement annulé", "category": "Logement", "actual": "120,29", "payer": "asso",
            "payer_kind": "asso", "has_refund": "1", "refund": "90,29", "refund_to": str(Pp["Anne"]),
            **{f"inc_{i}": "1" for i in Pp.values()}}
    c.post(f"/v/{tid}/depenses", data=data)
    page = re.sub(r"[  \xa0]", " ", c.get(f"/v/{tid}/depenses").text)
    cell = re.search(r'<td class="num">(30,00 €)<div class="small muted"[^>]*>(payé 120,29 € − 90,29 € remboursés '
                     r'\(à Anne\))</div>', page)
    assert cell, "le montant affiché doit être le coût partagé (30 €), le détail en dessous"


def test_fixed_share_checked_against_shared_cost(trip):
    app, c, tid, Pp, ops = trip
    base = {"label": "Appartement annulé", "category": "Logement", "actual": "120", "payer": "asso",
            "payer_kind": "asso", "has_refund": "1", "refund": "90", "refund_to": "asso",
            **{f"inc_{i}": "1" for i in Pp.values()}}
    # 40 € fixés pour Anne : plus que les 30 € à partager → refusé, avec le bon montant
    r = c.post(f"/v/{tid}/depenses", data={**base, f"fx_{Pp['Anne']}": "1", f"m_{Pp['Anne']}": "40"},
               follow_redirects=True)
    txt = re.sub(r"[  \xa0]", " ", r.text)
    assert "Les montants fixés font 40,00 €, plus que le coût à partager (30,00 €)" in txt
    assert ops.trip(tid)["depenses"] == []
    # 10 € fixés : accepté ; Bob et Chloé se partagent les 20 € restants (à la main : 30 − 10 = 20 ÷ 2 = 10)
    r = c.post(f"/v/{tid}/depenses", data={**base, f"fx_{Pp['Anne']}": "1", f"m_{Pp['Anne']}": "10"},
               follow_redirects=True)
    p = {x["nom"]: x["sa_part"] for x in ops.trip(tid)["participants"]}
    assert p == {"Anne": 10.0, "Bob": 10.0, "Chloé": 10.0}
    # tous fixés pour 30 € pile : accepté (et pas « il reste 90 € »)
    eid = ops.trip(tid)["depenses"][0]["id"]
    allfx = {**base, **{f"fx_{i}": "1" for i in Pp.values()}, f"m_{Pp['Anne']}": "5", f"m_{Pp['Bob']}": "10",
             f"m_{Pp['Chloé']}": "15"}
    r = c.post(f"/v/{tid}/depenses/{eid}", data=allfx, follow_redirects=True)
    p = {x["nom"]: x["sa_part"] for x in ops.trip(tid)["participants"]}
    assert p == {"Anne": 5.0, "Bob": 10.0, "Chloé": 15.0}


def test_ai_fixed_share_checked_against_shared_cost(trip):
    app, c, tid, Pp, ops = trip
    ops.import_trip({"voyage_id": tid, "depenses": [{"libelle": "Appart", "reel": "120", "rembourse": "90",
                                                     "pour": "tous"}]}, voyage=tid, verifier_id=tid)
    with pytest.raises(Exception) as e:
        ops.import_trip({"voyage_id": tid, "depenses_a_modifier": [{"depense": "Appart", "fixe": {"Anne": "40"}}]},
                        voyage=tid, verifier_id=tid)
    assert "plus que la dépense (30,00" in str(e.value).replace(" ", " ").replace("\xa0", " ")
    r = ops.import_trip({"voyage_id": tid, "depenses_a_modifier": [{"depense": "Appart", "fixe": {"Anne": "6"}}]},
                        voyage=tid, verifier_id=tid)
    assert {p["nom"]: p["sa_part"] for p in r["apercu"]["participants"]} == {"Anne": 6.0, "Bob": 12.0, "Chloé": 12.0}


def test_form_preview_deducts_refund(trip):
    """Dans le navigateur : l'aperçu « Pour qui ? » répartit le coût partagé, et suit la case et le montant."""
    import threading
    import time

    import uvicorn
    from playwright.sync_api import sync_playwright
    from tests.conftest import _free_port
    app, c, tid, Pp, ops = trip
    ops.add_participants(tid, [f"P{i}" for i in range(4, 16)])            # 15 personnes, comme le cas réel
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    while not srv.started:
        time.sleep(0.05)
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page()
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(f"http://127.0.0.1:{port}/v/{tid}/depenses/nouvelle")
            pg.fill("#label", "Appartement annulé")
            pg.fill("#actual", "120,29")
            pg.locator("[data-who-chips] button", has_text="Tout le monde").click()
            status = pg.locator("#split-status")
            norm = lambda s: s.replace(" ", " ").replace("\xa0", " ")
            assert norm(status.inner_text()).startswith("120,29 € réparti entre 15 personnes.")
            pg.check("input[name=has_refund]")
            pg.fill("#refund", "90,29")
            assert norm(status.inner_text()) == "30,00 € (120,29 € − 90,29 € remboursés) réparti entre 15 personnes."
            amounts = pg.eval_on_selector_all(".who-amount", "els => els.map(e => e.value).filter(v => v)")
            assert amounts == ["2,00"] * 15                                    # 30 € ÷ 15, comme le calcul
            pg.uncheck("input[name=has_refund]")                               # décoché : de nouveau 120,29
            assert norm(status.inner_text()).startswith("120,29 € réparti entre 15 personnes.")
            pg.check("input[name=has_refund]")
            assert "30,00 €" in norm(status.inner_text())
            # un montant fixé plus grand que le coût partagé : averti avant d'enregistrer
            first = pg.locator(".who-amount").first
            first.fill("40")
            assert "plus que le coût à partager (30,00 € (120,29 € − 90,29 € remboursés))" in norm(status.inner_text())
            assert errors == []
            b.close()
    finally:
        srv.should_exit = True
        th.join(5)


def test_advances_page_shows_refunds_already_given(trip):
    """« Payé / reçu » : avancé 120 € par Anne, puis l'asso lui rend 100 € (deux fois) : on voit les deux."""
    app, c, tid, Pp, ops = trip
    data = {"label": "Gîte", "category": "Logement", "actual": "120", "payer": str(Pp["Anne"]),
            "payer_kind": "participant", **{f"inc_{i}": "1" for i in Pp.values()}}
    c.post(f"/v/{tid}/depenses", data=data)
    ops.add_movement(tid, "Anne", "60", type="remboursement", date="2026-05-15", note="Avance du gîte")
    ops.add_movement(tid, "Anne", "40", type="remboursement", date="2026-06-01")
    ops.add_movement(tid, "Bob", "40", type="versement", date="2026-04-01")      # un versement : pas ici
    page = re.sub(r"[  \xa0]", " ", c.get(f"/v/{tid}/participants/avances").text)
    assert "Payé par des participants (avances)" in page
    i = page.index("Déjà remboursé par")
    block = page[i:]
    assert "100,00 €" in block[:400]                                            # total du bloc
    assert "15/05/2026" in block and "Avance du gîte" in block and "60,00 €" in block
    assert "01/06/2026" in block and "Remboursement</td>" in block               # sans motif : libellé par défaut
    assert "Total Anne" in block and "Bob" not in block                          # pas les versements
    mids = re.findall(r"/argent/mouvements#edit-m(\d+)", block)
    assert len(mids) == 2
    # le lien ouvre bien la ligne de modification de ce mouvement
    money = c.get(f"/v/{tid}/argent/mouvements").text
    assert all(f'id="edit-m{m}"' in money for m in mids)


def test_advances_page_only_refunds(trip):
    app, c, tid, Pp, ops = trip
    ops.add_movement(tid, "Bob", "25", type="remboursement")
    page = c.get(f"/v/{tid}/participants/avances").text
    assert "Déjà remboursé par" in page and "Personne n&#39;a avancé" not in page and "Personne n'a avancé" not in page
