"""Tests d'intégration : parcours complets via l'application web."""
import io
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

from compta import models as M
from compta.demo import DEMO_NAME, seed_demo
from compta.services import compute_trip
from compta.web import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(tmp_path / "data")
    c = TestClient(app)
    c.app_ = app
    return c


def tid_from(resp):
    m = re.search(r"/v/(\d+)", str(resp.url))
    return int(m.group(1))


def trip(client, tid):
    s = client.app_.state.db.Session()
    t = s.get(M.Trip, tid)
    return s, t


def new_trip(client, names="Anne\nBob\nChloé\nDavid"):
    r = client.post("/voyages", data={"name": "Test", "start": "2027-05-01"})
    assert r.status_code == 200
    tid = tid_from(r)
    client.post(f"/v/{tid}/participants", data={"names": names})
    s, t = trip(client, tid)
    ids = {p.name: p.id for p in t.participants}
    s.close()
    return tid, ids


def test_full_flow(client):
    tid, P = new_trip(client)
    assert len(P) == 4

    # Dépense partagée par tout le monde, payée par l'asso
    data = {"label": "Gîte", "category": "Logement", "planned": "400", "actual": "360", "payer": "asso"}
    data.update({f"inc_{i}": "1" for i in P.values()})
    data.update({f"w_{i}": "1" for i in P.values()})
    r = client.post(f"/v/{tid}/depenses", data=data)
    assert "enregistrée" in r.text

    # Avance de Bob, seulement pour Anne et Bob, nouvelle catégorie créée à la volée
    data = {"label": "Péage", "category": "Autoroute", "actual": "30,50", "payer": str(P["Bob"]),
            f"inc_{P['Anne']}": "1", f"w_{P['Anne']}": "1", f"inc_{P['Bob']}": "1", f"w_{P['Bob']}": "1"}
    client.post(f"/v/{tid}/depenses", data=data,
                files={"files": ("ticket.jpg", b"\xff\xd8fake", "image/jpeg")})

    # Aperçu d'une dépense
    pv = client.post(f"/v/{tid}/depenses/apercu", data={"label": "x", "actual": "10",
                                                        **{f"inc_{i}": "1" for i in list(P.values())[:3]}})
    assert "3,33" in pv.text and "+ 1 centime pour l&#39;un d&#39;eux (tiré au sort)" in pv.text   # 10 € pour 3

    # Versements
    client.post(f"/v/{tid}/argent", data={"participant": P["Anne"], "amount": "50", "kind": "versement"})

    # Remise : Anne −20 € sur son voyage, offerte par l'asso (fiche du participant)
    r = client.post(f"/v/{tid}/remises", data={"participant": P["Anne"], "kind": "reduce", "value": "20",
                    "by": "asso", "reason": "étudiante"}, follow_redirects=True)
    assert "Remise de 20,00" in r.text.replace("\u202f", " ").replace("\xa0", " ")

    # Désistement de David : le groupe reprend sa part
    pv = client.post(f"/v/{tid}/desistement/apercu", data={"participant": P["David"], "absorber": "group"})
    assert "David" in pv.text
    client.post(f"/v/{tid}/desistement", data={"participant": P["David"], "absorber": "group",
                                               "date": "2027-04-01"})

    s, t = trip(client, tid)
    r_ = compute_trip(t)
    assert r_.is_balanced()
    assert r_.people[P["David"]].share == 0
    assert r_.people[P["Anne"]].share == 12000 - 2000 + 1525        # 360/3 - 20 + péage/2
    assert r_.people[P["Bob"]].balance == 12000 + 1525 - 3050
    assert r_.asso_total == 2000
    assert len(t.receipts) == 1 and t.receipts[0].links
    s.close()

    # Toutes les pages s'affichent
    for url in ["", "/participants", f"/p/{P['Anne']}", "/depenses", "/depenses/nouvelle", "/repartition",
                "/ajuster", "/desistement", f"/desistement?p={P['Bob']}", "/argent", "/justificatifs",
                "/entre-nous", "/export"]:
        assert client.get(f"/v/{tid}{url}").status_code == 200, url

    # Exports
    pdf = client.get(f"/v/{tid}/export.pdf")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/v/{tid}/export.zip").content))
    names = z.namelist()
    assert any(n.startswith("justificatifs/J01_") for n in names)
    assert any(n.endswith(".pdf") and n.startswith("Bilan_") for n in names)


def test_undo(client):
    tid, P = new_trip(client)
    client.post(f"/v/{tid}/argent", data={"participant": P["Anne"], "amount": "50", "kind": "versement"})
    s, t = trip(client, tid)
    assert len(t.movements) == 1
    s.close()
    r = client.post("/annuler", headers={"referer": f"http://testserver/v/{tid}/argent"})
    assert "Défait" in r.text
    s, t = trip(client, tid)
    assert len(t.movements) == 0
    s.close()


def test_undo_trip_creation_goes_home(client):
    tid, _ = new_trip(client)
    client.post("/annuler")   # annule l'ajout des participants
    r = client.post("/annuler", headers={"referer": f"http://testserver/v/{tid}/participants"})
    assert r.url.path == "/"


def test_new_participant_joins_everyone_expenses(client):
    tid, P = new_trip(client, "Anne\nBob")
    data = {"label": "Gîte", "actual": "100", **{f"inc_{i}": "1" for i in P.values()}}
    client.post(f"/v/{tid}/depenses", data=data)
    client.post(f"/v/{tid}/participants", data={"names": "Chloé", "join_all": "1"})
    s, t = trip(client, tid)
    assert len(t.expenses[0].shares) == 3
    s.close()


def test_delete_participant_with_payments_refused(client):
    tid, P = new_trip(client)
    client.post(f"/v/{tid}/argent", data={"participant": P["Anne"], "amount": "50", "kind": "versement"})
    r = client.post(f"/v/{tid}/participants/{P['Anne']}/supprimer")
    assert "désiste" in r.text
    r = client.post(f"/v/{tid}/participants/{P['Bob']}/supprimer")
    assert "retiré" in r.text


def test_subaccount_flow(client):
    tid, P = new_trip(client)
    r = client.post(f"/v/{tid}/entre-nous", data={"name": "Resto", "members": [P["Anne"], P["Bob"], P["Chloé"]]})
    sid = int(re.search(r"/entre-nous/(\d+)", str(r.url)).group(1))
    client.post(f"/v/{tid}/entre-nous/{sid}/depense",
                data={"label": "Pizza", "amount": "30", "payer": P["Anne"], "for": [P["Anne"], P["Bob"], P["Chloé"]]})
    page = client.get(f"/v/{tid}/entre-nous/{sid}").text
    assert "doit" in page and "10,00" in page
    client.post(f"/v/{tid}/entre-nous/{sid}/regler", data={"sender": P["Bob"], "receiver": P["Anne"], "amount": "10"})
    assert client.get(f"/v/{tid}/entre-nous/{sid}/export.pdf").content[:4] == b"%PDF"
    # n'affecte pas les comptes de l'asso
    s, t = trip(client, tid)
    assert compute_trip(t).people[P["Anne"]].balance == 0
    s.close()


def test_demo_data(client):
    db = client.app_.state.db
    tid = seed_demo(db)
    s, t = trip(client, tid)
    assert t.name == DEMO_NAME
    assert len(t.participants) == 12
    r = compute_trip(t)
    assert r.is_balanced()
    assert any(p.withdrawn for p in r.people.values())
    assert t.adjustments and t.subaccounts and t.receipts
    cats = {e.category.name for e in t.expenses}
    assert {"Logement", "Transport / Covoit", "Plongée"} <= cats
    assert any(e.payer_id for e in t.expenses)        # des avances
    s.close()
    # ré-import : remplace sans dupliquer
    seed_demo(db)
    s = db.Session()
    assert s.query(M.Trip).filter_by(name=DEMO_NAME).count() == 1
    s.close()


def test_real_if_known_else_planned(client):
    """Plus de bouton prévu / réel : le réel s'il est saisi, sinon le prévu, signalé (≈, en couleur)."""
    tid, P = new_trip(client, "Anne\nBob")
    client.post(f"/v/{tid}/depenses", data={"label": "Gîte", "planned": "100", "actual": "80",
                                            **{f"inc_{i}": "1" for i in P.values()}})
    client.post(f"/v/{tid}/depenses", data={"label": "Bateau", "planned": "60",
                                            **{f"inc_{i}": "1" for i in P.values()}})
    page = client.get(f"/v/{tid}").text
    assert "Selon le prévu" not in page and "140,00" in page          # 80 (réel) + 60 (prévu)
    assert "1 dépense est comptée sur son" in page
    dep = client.get(f"/v/{tid}/depenses").text
    assert '<span class="est"' in dep and "≈ 60,00" in dep.replace("\u202f", " ").replace("\xa0", " ")
    client.get(f"/mode?m=prevu&next=/v/{tid}")                         # ancien lien : sans effet
    assert "140,00" in client.get(f"/v/{tid}").text


def test_404(client):
    r = client.get("/v/12345")
    assert r.status_code == 404 and "introuvable" in r.text


def test_undo_is_offered_right_after_the_action(tmp_path):
    """« ↶ Défaire » n'est plus dans la barre du haut : il apparaît dans le message qui suit une action, ne vaut que
    pour cette action, et « ↷ Rétablir » répare un clic malheureux."""
    from compta.ops import Ops
    app = create_app(tmp_path / "d")
    c = TestClient(app)
    ops = Ops(app.state.db)
    tid = ops.create_trip("Voyage")["voyage_id"]
    ops.add_participants(tid, ["Anne", "Bob"])
    page = c.get(f"/v/{tid}").text
    assert "Défaire" not in page.split("</header>")[0] and 'action="/annuler"' not in page
    # une action à l'écran -> le message propose de la défaire
    r = c.post(f"/v/{tid}/argent", data={"kind": "versement", "participant": "1", "amount": "50"},
               follow_redirects=True)
    assert "Versement de 50,00" in r.text and "↶ Défaire" in r.text
    token = re.search(r'name="d" value="([^"]+)"', r.text).group(1)
    # une simple visite (sans action) ne propose rien
    assert "↶ Défaire" not in c.get(f"/v/{tid}/argent?msg=Bonjour").text
    # défaire -> « Rétablir » proposé dans le message
    r = c.post("/annuler", data={"d": token}, headers={"referer": f"http://testserver/v/{tid}/argent"},
               follow_redirects=True)
    assert "Défait : versement" in r.text and "↷ Rétablir" in r.text and ops.trip(tid)["mouvements"] == []
    rtok = re.search(r'name="r" value="([^"]+)"', r.text).group(1)
    r = c.post("/retablir", data={"r": rtok}, headers={"referer": f"http://testserver/v/{tid}/argent"},
               follow_redirects=True)
    assert "Rétabli : versement" in r.text and len(ops.trip(tid)["mouvements"]) == 1
    # un vieux bouton « Défaire » (action suivie d'une autre) ne défait PAS la mauvaise action
    ops.add_expense(tid, "Gîte", montant_reel="100")
    r = c.post("/annuler", data={"d": token}, follow_redirects=True)
    assert "ne peut plus être défaite" in r.text and len(ops.trip(tid)["depenses"]) == 1
    # « Rétablir » disparaît après une nouvelle action
    r = c.post("/retablir", data={"r": rtok}, follow_redirects=True)
    assert "plus rien à rétablir" in r.text


def test_stop_button_always_in_top_bar(tmp_path):
    from compta.ops import Ops
    app = create_app(tmp_path / "d")
    c = TestClient(app)
    tid = Ops(app.state.db).create_trip("Voyage")["voyage_id"]
    for url in ["/", f"/v/{tid}", f"/v/{tid}/depenses", f"/v/{tid}/ia"]:
        page = c.get(url).text
        top = page.split("</header>")[0]
        assert 'action="/quitter"' in top and "⏻ Arrêter" in top, url
        assert 'data-confirm="Arrêter Trip Compta ?' in top
        assert top.index("⏻ Arrêter") > top.index("data-theme-toggle")      # tout à droite, après le thème


def test_ping_identifies_the_app(tmp_path):
    from fastapi.testclient import TestClient
    from compta.web import create_app
    j = TestClient(create_app(tmp_path / "d")).get("/ping").json()
    assert j["app"] == "tripcompta" and len(j["boot"]) == 12
