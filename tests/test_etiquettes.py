"""Rôles = étiquettes : elles aident à cocher vite, la répartition ne suit que les personnes cochées.
Quand quelqu'un rejoint le voyage, une étiquette, un groupe ou une famille, on PROPOSE (pré-coché) de l'ajouter aux
dépenses qui concernaient exactement cet ensemble. L'ancien rôle « DP » devient une étiquette ordinaire.
Entrées et sorties de caisse : sans date d'abord, puis la plus récente en haut."""
import json
import re
import sqlite3
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from compta import models as M
from compta import services as S
from compta.db import Database
from compta.ops import Ops
from compta.web import create_app


def clean(t):
    import html
    return re.sub(r"\s+", " ", html.unescape(t).replace(" ", " ").replace("\xa0", " "))


@pytest.fixture
def env(tmp_path):
    """Anne, Bob, Chloé plongeurs ; Zoé accompagnante. Gîte (tous) 400, Bateau (plongeurs) 300,
    Musée (accompagnants) 20, Resto 90 pour Anne, Bob, Chloé mais Anne ×2 (parts inégales)."""
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.import_trip({"voyage": {"nom": "Sortie"},
                           "participants": ["Anne", "Bob", "Chloé", {"nom": "Zoé", "accompagnant": True}],
                           "depenses": [{"libelle": "Gîte", "reel": "400", "paye_par": "asso", "pour": "tous"},
                                        {"libelle": "Bateau", "reel": "300", "paye_par": "asso", "pour": "plongeurs"},
                                        {"libelle": "Musée", "reel": "20", "paye_par": "asso", "pour": "accompagnants"},
                                        {"libelle": "Resto", "reel": "90", "paye_par": "asso",
                                         "parts": {"Anne": 2, "Bob": 1, "Chloé": 1}}]})["voyage_id"]
    c = TestClient(app)
    P = {p["nom"]: p["id"] for p in ops.trip(tid)["participants"]}
    E = {d["libelle"]: d["id"] for d in ops.trip(tid)["depenses"]}
    return app, ops, tid, c, P, E


def proposal(r):
    """La page « L'ajouter aussi… » : {(pid, eid)} proposés (tous pré-cochés)."""
    assert "L'ajouter aussi à ces dépenses ?" in clean(r.text)
    items = re.findall(r'name="add" value="(\d+)\.(\d+)" checked', r.text)
    assert len(items) == r.text.count('name="add"')                          # tout est pré-coché
    return {(int(a), int(b)) for a, b in items}


def shares(app, tid):
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        names = {p.id: p.name for p in t.participants}
        return {e.label: sorted(names[x.participant_id] for x in e.shares) for e in t.expenses}


def test_newcomer_diver_gets_everyone_and_divers_expenses(env):
    app, ops, tid, c, P, E = env
    r = c.post(f"/v/{tid}/participants", data={"name": "Julie", "roles_form": "1", "r_plongeur": "1"},
               follow_redirects=True)
    jid = next(p["id"] for p in ops.trip(tid)["participants"] if p["nom"] == "Julie")
    assert proposal(r) == {(jid, E["Gîte"]), (jid, E["Bateau"])}           # pas le Musée, pas le Resto (inégal)
    page = clean(re.sub(r"<[^>]+>", " ", r.text))
    assert "Julie · maintenant dans « tout le monde », « Plongeurs »" in page
    assert "Gîte" in page and "400,00 € pour 4 pers. (tout le monde)" in page
    assert "Bateau" in page and "300,00 € pour 3 pers. (Plongeurs)" in page
    before = shares(app, tid)
    assert before["Gîte"] == ["Anne", "Bob", "Chloé", "Zoé"]                # rien n'est ajouté avant de valider
    # décocher le bateau (sortie avant son arrivée) : seul le gîte
    nxt = re.search(r'name="next" value="([^"]+)"', r.text).group(1)
    r = c.post(f"/v/{tid}/rejoindre", data={"next": nxt, "add": [f"{jid}.{E['Gîte']}"]}, follow_redirects=True)
    assert "Julie ajouté à 1 dépense" in clean(r.text)
    after = shares(app, tid)
    assert after["Gîte"] == ["Anne", "Bob", "Chloé", "Julie", "Zoé"] and after["Bateau"] == ["Anne", "Bob", "Chloé"]
    d = {p["nom"]: p for p in ops.trip(tid)["participants"]}
    assert d["Julie"]["sa_part"] == 80.0 and d["Zoé"]["sa_part"] == 80.0 + 20.0      # gîte 400 / 5 ; musée 20
    # « Annuler » défait l'ajout aux dépenses
    c.post("/annuler")
    assert shares(app, tid)["Gîte"] == ["Anne", "Bob", "Chloé", "Zoé"]


def test_newcomer_companion_gets_companion_expenses(env):
    app, ops, tid, c, P, E = env
    r = c.post(f"/v/{tid}/participants", data={"name": "Léa", "roles_form": "1", "r_accompagnant": "1"},
               follow_redirects=True)
    lid = next(p["id"] for p in ops.trip(tid)["participants"] if p["nom"] == "Léa")
    assert proposal(r) == {(lid, E["Gîte"]), (lid, E["Musée"])}


def test_not_now_changes_nothing(env):
    app, ops, tid, c, P, E = env
    before = shares(app, tid)
    r = c.post(f"/v/{tid}/participants", data={"name": "Julie", "roles_form": "1", "r_plongeur": "1"},
               follow_redirects=False)
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert r.headers["location"].startswith(f"/v/{tid}/rejoindre?") and q["next"][0].startswith(f"/v/{tid}/participants")
    page = c.get(r.headers["location"]).text
    href = re.search(r'<a class="btn ghost" href="([^"]+)">Pas maintenant</a>', page).group(1)
    assert href == q["next"][0]
    assert shares(app, tid) == before


def test_any_label_group_or_family_works(env):
    """Une étiquette du voyage (Moniteur), un groupe (Minibus), une famille (Martin) : même logique."""
    app, ops, tid, c, P, E = env
    c.post(f"/v/{tid}/roles-du-voyage", data={"name": "Moniteur"})
    c.post(f"/v/{tid}/roles/perso%3AMoniteur", data={"members": [P["Anne"], P["Bob"]]})
    c.post(f"/v/{tid}/groupes", data={"name": "Minibus", "members": [P["Bob"], P["Chloé"]]})
    c.post(f"/v/{tid}/familles", data={"name": "Martin", "members": [P["Anne"], P["Zoé"]], "referent": P["Anne"]})
    for label, who in (("Cadeau moniteurs", ["Anne", "Bob"]), ("Péage", ["Bob", "Chloé"]), ("Glaces", ["Anne", "Zoé"])):
        ops.add_expense(tid, label, montant_reel="10", pour=who)
    E = {d["libelle"]: d["id"] for d in ops.trip(tid)["depenses"]}
    # Chloé devient monitrice (onglet Organisation) : le cadeau des moniteurs lui est proposé
    r = c.post(f"/v/{tid}/roles/perso%3AMoniteur", data={"members": [P["Anne"], P["Bob"], P["Chloé"]]},
               follow_redirects=True)
    assert proposal(r) == {(P["Chloé"], E["Cadeau moniteurs"])} and "« Moniteur »" in clean(r.text)
    # Anne monte dans le minibus (depuis sa fiche) : le péage
    r = c.post(f"/v/{tid}/participants/{P['Anne']}", data={"name": "Anne", "groups_form": "1", "groups": ["Minibus"]},
               follow_redirects=True)
    assert proposal(r) == {(P["Anne"], E["Péage"])} and "« groupe Minibus »" in clean(r.text)
    # Bob rejoint la famille Martin : les glaces
    fid = re.search(r'action="/v/\d+/familles/(\d+)"', c.get(f"/v/{tid}/participants/organisation").text).group(1)
    r = c.post(f"/v/{tid}/familles/{fid}", data={"name": "Martin", "members": [P["Anne"], P["Zoé"], P["Bob"]],
                                                 "referent": P["Anne"]}, follow_redirects=True)
    assert proposal(r) == {(P["Bob"], E["Glaces"])} and "« famille Martin »" in clean(r.text)
    # sans rien de concerné : pas de page, retour direct
    r = c.post(f"/v/{tid}/participants/{P['Bob']}", data={"name": "Bob", "phone": "06"}, follow_redirects=False)
    assert "/rejoindre" not in r.headers["location"]


def test_ai_adds_newcomers_the_same_way(env):
    """L'IA (et l'API) : ajoutés d'office, selon la même règle (tout le monde + leurs étiquettes)."""
    app, ops, tid, c, P, E = env
    ops.add_participants(tid, ["Julie", {"nom": "Léa", "accompagnant": True}])
    sh = shares(app, tid)
    assert "Julie" in sh["Gîte"] and "Julie" in sh["Bateau"] and "Julie" not in sh["Musée"]
    assert "Léa" in sh["Gîte"] and "Léa" in sh["Musée"] and "Léa" not in sh["Bateau"]
    assert "Julie" not in sh["Resto"] and "Léa" not in sh["Resto"]


def test_join_page_ignores_bad_or_stale_input(env):
    app, ops, tid, c, P, E = env
    r = c.get(f"/v/{tid}/rejoindre?s=abc,{P['Anne']}.{E['Gîte']}.0&w=x&next=https://evil.example/",
              follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/v/{tid}/participants"   # déjà dedans ; adresse sûre
    r = c.post(f"/v/{tid}/rejoindre", data={"next": "//evil", "add": ["x.y", f"{P['Anne']}.{E['Gîte']}"]},
               follow_redirects=False)
    assert r.headers["location"].startswith(f"/v/{tid}/participants?msg=Aucune")


def test_labels_are_independent_and_removable(env):
    app, ops, tid, c, P, E = env
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        p = next(x for x in t.participants if x.name == "Zoé")
        S.set_roles(t, p, ["plongeur", "accompagnant"])                    # cumulables
        s.commit()
        assert p.role_list == ["plongeur", "accompagnant"]
    before = {d["libelle"]: (d["pour"], d["reel"]) for d in ops.trip(tid)["depenses"]}
    sold = {p["nom"]: p["sa_part"] for p in ops.trip(tid)["participants"]}
    for role in ("plongeur", "accompagnant"):
        c.post(f"/v/{tid}/roles-du-voyage/supprimer", data={"role": role})
    assert {d["libelle"]: (d["pour"], d["reel"]) for d in ops.trip(tid)["depenses"]} == before   # aucun montant
    assert {p["nom"]: p["sa_part"] for p in ops.trip(tid)["participants"]} == sold
    page = c.get(f"/v/{tid}/participants").text
    assert 'name="r_plongeur"' not in page and 'name="r_accompagnant"' not in page
    # un nouveau venu n'a alors aucune étiquette d'office
    ops.add_participants(tid, ["Julie"])
    assert next(p for p in ops.trip(tid)["participants"] if p["nom"] == "Julie")["roles"] == []


def test_new_trip_takes_labels_of_the_last_trip(env):
    app, ops, tid, c, P, E = env
    c.post(f"/v/{tid}/roles-du-voyage", data={"name": "DP"})
    c.post(f"/v/{tid}/roles-du-voyage/supprimer", data={"role": "accompagnant"})
    c.post("/voyages", data={"name": "Suivant"})
    with app.state.db.Session() as s:
        t = s.query(M.Trip).filter_by(name="Suivant").one()
        assert list(S.trip_roles(t).values()) == ["Plongeur", "Organisateur", "Co-organisateur", "DP"]


# ---------------------------------------------------------------- l'ancien rôle DP

def test_old_database_dp_becomes_a_label(tmp_path):
    d = tmp_path / "old"
    db = Database(d)
    with db.Session() as s:
        a, b = M.Trip(name="Avec DP"), M.Trip(name="Sans DP", roles_conf='{"perso": [], "masques": ["dp"]}')
        a.participants = [M.Participant(name="Luc"), M.Participant(name="Marie")]
        b.participants = [M.Participant(name="Hugo")]
        s.add_all([a, b])
        s.commit()
    con = sqlite3.connect(d / "compta.db")                                  # comme écrit par une version d'avant
    con.execute("UPDATE participants SET roles='plongeur,dp' WHERE name='Luc'")
    con.execute("UPDATE participants SET roles='organisateur,dp' WHERE name='Marie'")
    con.commit()
    con.close()
    for _ in range(2):                                                      # deux lancements : même résultat
        db = Database(d)
        with db.Session() as s:
            a = s.query(M.Trip).filter_by(name="Avec DP").one()
            b = s.query(M.Trip).filter_by(name="Sans DP").one()
            assert {p.name: p.role_list for p in a.participants} == {"Luc": ["plongeur", "perso:DP"],
                                                                     "Marie": ["organisateur", "perso:DP"]}
            assert json.loads(a.roles_conf)["perso"] == ["DP"] and "DP" not in S.trip_roles(b).values()
            assert json.loads(b.roles_conf)["masques"] == []


def test_example_trip_keeps_its_dp_as_a_label(tmp_path):
    app = create_app(tmp_path / "d")
    c = TestClient(app)
    c.post("/exemple/ouvrir")
    with app.state.db.Session() as s:
        t = s.query(M.Trip).filter_by(is_demo=True).one()
        assert "DP" in S.trip_roles(t).values() and S.with_role(t, "perso:DP")
        assert all("dp" not in p.role_list for p in t.participants)
        tid = t.id
    dash = c.get(f"/v/{tid}").text
    tile = dash.split('<div class="label">Équipe</div>')[1].split('<div class="card kpi">')[0].split("</div></div>")[0]
    assert "DP" not in tile and "organisateur" in tile                      # l'Équipe : organisation seulement


def test_old_export_with_dp_is_converted(tmp_path):
    from compta import transfer
    app = create_app(tmp_path / "a")
    ops = Ops(app.state.db)
    tid = ops.import_trip({"voyage": {"nom": "Exporté"}, "participants": ["Luc", "Anne"]})["voyage_id"]
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        next(p for p in t.participants if p.name == "Luc").roles = "plongeur,dp"     # comme une ancienne version
        s.commit()
    data = TestClient(app).get(f"/v/{tid}/voyage.zip").content
    app2 = create_app(tmp_path / "b")
    with app2.state.db.Session() as s:
        trips, _ = transfer.import_zip(s, data, app2.state.db.receipts_dir)
        s.commit()
        t = s.get(M.Trip, trips[0].id)
        assert next(p for p in t.participants if p.name == "Luc").role_list == ["plongeur", "perso:DP"]
        assert "DP" in S.trip_roles(t).values()


# ---------------------------------------------------------------- caisse

def test_cash_lines_undated_first_then_newest(env):
    app, ops, tid, c, P, E = env
    for who, amt, date in (("Anne", "10", "2027-05-01"), ("Bob", "11", ""), ("Chloé", "12", "2027-05-20"),
                           ("Zoé", "13", "2027-05-10")):
        c.post(f"/v/{tid}/argent", data={"kind": "versement", "participant": P[who], "amount": amt, "date": date})
    page = c.get(f"/v/{tid}/argent/mouvements").text
    table = page.split("Entrées et sorties de la caisse")[1].split("</table>")[0]
    order = re.findall(r"Versement de (\w+)", table)
    assert order == ["Bob", "Chloé", "Zoé", "Anne"]
    # les dépenses sans date (payées par l'asso) sont aussi en haut, avant toutes les lignes datées
    first_dated = table.index("Versement de Chloé")
    assert table.index("Paiement : Gîte") < first_dated
