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
    """La page « L'ajouter aussi… » : {(pid, eid)} proposés (rien n'est coché d'avance)."""
    assert "L'ajouter aussi à ces dépenses ?" in clean(r.text)
    items = re.findall(r'name="add" value="(\d+)\.(\d+)"', r.text)
    assert "checked" not in "".join(re.findall(r'<input type="checkbox" name="add"[^>]*>', r.text))
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
    assert "Gîte" in page and "400,00 € · 4 → 5 personnes (tout le monde)" in page
    assert "Bateau" in page and "300,00 € · 3 → 4 personnes (Plongeurs)" in page
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
    c.post("/voyages", data={"name": "Suivant", "plongee": "1"})
    c.post("/voyages", data={"name": "Sans plongée"})
    with app.state.db.Session() as s:
        t = s.query(M.Trip).filter_by(name="Suivant").one()
        assert list(S.trip_roles(t).values()) == ["Plongeur", "Organisateur", "Co-organisateur", "DP"]
        # un voyage ordinaire garde les étiquettes ajoutées (« DP »), sans Plongeur ni Accompagnant
        t = s.query(M.Trip).filter_by(name="Sans plongée").one()
        assert list(S.trip_roles(t).values()) == ["Organisateur", "Co-organisateur", "DP"]


# ---------------------------------------------------------------- l'ancien rôle DP

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


# ---------------------------------------------------------------- groupes allumés, réunions, « à vérifier »
def _gisele_env(env):
    """Groupe Minibus = Bob, Chloé. Dépenses : Péage (Minibus), Ferry (Minibus + Accompagnants : Bob, Chloé, Zoé),
    Café (Minibus + Anne, ajoutée à la main), Pique-nique (Minibus, parts inégales). Gisèle, déjà au voyage."""
    app, ops, tid, c, P, E = env
    c.post(f"/v/{tid}/groupes", data={"name": "Minibus", "members": [P["Bob"], P["Chloé"]]})
    ops.add_participants(tid, ["Gisèle"], inclure_dans_depenses_communes=False)
    ops.add_expense(tid, "Péage", montant_reel="12", pour=["Bob", "Chloé"])
    ops.add_expense(tid, "Ferry", montant_reel="30", pour=["Bob", "Chloé", "Zoé"])
    ops.add_expense(tid, "Café", montant_reel="9", pour=["Anne", "Bob", "Chloé"])
    ops.add_expense(tid, "Pique-nique", montant_reel="40", parts={"Bob": 2, "Chloé": 1})
    d = ops.trip(tid)
    P = {p["nom"]: p["id"] for p in d["participants"]}
    E = {x["libelle"]: x["id"] for x in d["depenses"]}
    return app, ops, tid, c, P, E


def test_joining_a_group_proposes_every_expense_where_it_is_lit(env):
    """Gisèle rejoint le Minibus : toute dépense où le Minibus est allumé est proposée, RIEN n'est coché ; l'origine
    est indiquée (réunion de groupes comprise), « contient aussi Anne » pour un ajout fait à la main."""
    app, ops, tid, c, P, E = _gisele_env(env)
    r = c.post(f"/v/{tid}/groupes", data={"name": "Minibus", "old_name": "Minibus",
                                         "members": [P["Bob"], P["Chloé"], P["Gisèle"]]}, follow_redirects=True)
    g = P["Gisèle"]
    # le Minibus est allumé partout où Bob et Chloé sont : aussi dans le Gîte, le Bateau et le Café (avec Anne)
    assert proposal(r) == {(g, E["Péage"]), (g, E["Ferry"]), (g, E["Café"]), (g, E["Gîte"]), (g, E["Bateau"])}
    page = clean(re.sub(r"<[^>]+>", " ", r.text))
    assert "Gisèle · maintenant dans « groupe Minibus »" in page
    assert "12,00 € · 2 → 3 personnes (groupe Minibus)" in page
    assert "30,00 € · 3 → 4 personnes (groupe Minibus + Accompagnants)" in page
    assert "9,00 € · 3 → 4 personnes (groupe Minibus) contient aussi Anne" in page   # Café : Anne en plus
    assert "Tout cocher" in page and 'data-go disabled' in r.text                # rien de coché : Ajouter grisé
    # le pique-nique et le resto (parts inégales) : pas de case, mais « à vérifier »
    assert "2 dépenses à vérifier" in page and "À vérifier plus tard" in page and "Pique-nique" in page
    assert not re.search(rf'value="{g}\.{E["Pique-nique"]}"', r.text)


def test_group_plus_someone_added_by_hand(env):
    """Une dépense « Minibus + Paul » (Paul ajouté à la main) : le Minibus y est allumé, elle est proposée avec
    « contient aussi Paul »."""
    app, ops, tid, c, P, E = _gisele_env(env)
    ops.add_participants(tid, ["Paul"], inclure_dans_depenses_communes=False)
    ops.add_expense(tid, "Parking", montant_reel="8", pour=["Bob", "Chloé", "Paul"])
    E = {x["libelle"]: x["id"] for x in ops.trip(tid)["depenses"]}
    r = c.post(f"/v/{tid}/groupes", data={"name": "Minibus", "old_name": "Minibus",
                                         "members": [P["Bob"], P["Chloé"], P["Gisèle"]]}, follow_redirects=True)
    assert (P["Gisèle"], E["Parking"]) in proposal(r)
    assert "contient aussi Paul" in clean(re.sub(r"<[^>]+>", " ", r.text))


def test_custom_shares_are_flagged_until_reviewed(env):
    """Parts personnalisées : signalées « à vérifier » (base), en rouge dans Dépenses, bandeau sur les pages du voyage,
    alerte du tableau de bord ; « C'est bon » ou un nouvel enregistrement de la dépense les retire. Jamais ajoutées
    d'office, même par un envoi trafiqué."""
    app, ops, tid, c, P, E = _gisele_env(env)
    g = P["Gisèle"]
    c.post(f"/v/{tid}/groupes", data={"name": "Minibus", "old_name": "Minibus", "members": [P["Bob"], P["Chloé"], g]})
    lst = c.get(f"/v/{tid}/depenses").text
    assert re.search(r'<tr id="e%d" class="[^"]*to-check' % E["Pique-nique"], lst)
    t = clean(re.sub(r"<[^>]+>", " ", lst))
    assert "2 dépenses à vérifier" in t and "Gisèle a rejoint « groupe Minibus » : ajoute sa part" in t
    assert "2 dépenses à vérifier" in clean(re.sub(r"<[^>]+>", " ", c.get(f"/v/{tid}").text))
    assert any("à vérifier" in a["text"] for a in _alerts(app, tid))
    # un envoi trafiqué ne l'ajoute pas aux parts personnalisées
    c.post(f"/v/{tid}/rejoindre", data={"next": f"/v/{tid}/participants", "add": [f"{g}.{E['Pique-nique']}"]})
    assert "Gisèle" not in shares(app, tid)["Pique-nique"]
    # « C'est bon »
    r = c.post(f"/v/{tid}/depenses/{E['Pique-nique']}/verifiee", follow_redirects=True)
    assert "« Pique-nique » vérifiée" in clean(r.text)
    assert "1 dépense à vérifier" in clean(re.sub(r"<[^>]+>", " ", c.get(f"/v/{tid}/depenses").text))   # le resto
    # de nouveau signalée (Gisèle sort puis revient), puis la dépense est revue (enregistrée) : retirée aussi
    c.post(f"/v/{tid}/groupes", data={"name": "Minibus", "old_name": "Minibus", "members": [P["Bob"], P["Chloé"]]})
    c.post(f"/v/{tid}/groupes", data={"name": "Minibus", "old_name": "Minibus", "members": [P["Bob"], P["Chloé"], g]})
    assert "2 dépenses à vérifier" in clean(re.sub(r"<[^>]+>", " ", c.get(f"/v/{tid}/depenses").text))
    with app.state.db.Session() as s_:
        assert len(s_.get(M.Expense, E["Pique-nique"]).checks) == 1
    # « Modifier » : on ajoute Gisèle à la main (une part) et on enregistre
    c.post(f"/v/{tid}/depenses/{E['Pique-nique']}", data={
        "label": "Pique-nique", "actual": "40", f"inc_{P['Bob']}": "1", f"w_{P['Bob']}": "2",
        f"inc_{P['Chloé']}": "1", f"w_{P['Chloé']}": "1", f"inc_{g}": "1", f"w_{g}": "1"})
    with app.state.db.Session() as s_:
        assert s_.get(M.Expense, E["Pique-nique"]).checks == []
    assert "Gisèle" in shares(app, tid)["Pique-nique"]
    assert "1 dépense à vérifier" in clean(re.sub(r"<[^>]+>", " ", c.get(f"/v/{tid}/depenses").text))   # le resto


def test_ai_adds_only_whole_group_expenses_and_flags_the_rest(env):
    """L'IA (API) : ajoutée d'office seulement aux dépenses partagées exactement entre des groupes entiers ; les
    parts personnalisées sont signalées « à vérifier » ; « contient aussi » : rien d'office."""
    app, ops, tid, c, P, E = env
    ops.add_participants(tid, ["Julie"])                  # plongeuse par défaut
    sh = shares(app, tid)
    assert "Julie" in sh["Gîte"] and "Julie" in sh["Bateau"] and "Julie" not in sh["Resto"]
    with app.state.db.Session() as s_:
        resto = s_.get(M.Expense, E["Resto"])
        assert [c_.reason for c_ in resto.checks] in (["Plongeurs"], ["tout le monde"])


def _alerts(app, tid):
    with app.state.db.Session() as s_:
        t = s_.get(M.Trip, tid)
        return S.alerts(t, S.compute_trip(t))


def test_organizer_is_a_label_like_the_others(env):
    """Organisateur, co-organisateur : des étiquettes comme les autres (l'organisateur peut ne pas partir). Anne
    co-organisatrice ; Bob le devient aussi : les dépenses où l'étiquette est allumée lui sont proposées."""
    app, ops, tid, c, P, E = env
    c.post(f"/v/{tid}/roles/co-organisateur", data={"members": [P["Anne"]]})
    ops.add_expense(tid, "Repérage", montant_reel="50", pour=["Anne"])
    E = {x["libelle"]: x["id"] for x in ops.trip(tid)["depenses"]}
    r = c.post(f"/v/{tid}/roles/co-organisateur", data={"members": [P["Anne"], P["Bob"]]}, follow_redirects=True)
    assert (P["Bob"], E["Repérage"]) in proposal(r)
    assert "Co-organisateurs" in clean(r.text)


def test_check_texts_and_see_button(env):
    """Textes courts (la part à ajouter à la main) ; « Voir » mène à la page Dépenses, et disparaît sur elle."""
    app, ops, tid, c, P, E = _gisele_env(env)
    ops.add_expense(tid, "Autre", montant_reel="1", pour=["Anne"])     # (sans effet)
    # une seule dépense à vérifier : on retire le resto (parts inégales, aussi concerné)
    with app.state.db.Session() as s_:
        s_.delete(s_.get(M.Expense, E["Resto"]))
        s_.commit()
    c.post(f"/v/{tid}/groupes", data={"name": "Minibus", "old_name": "Minibus",
                                     "members": [P["Bob"], P["Chloé"], P["Gisèle"]]})
    page = clean(re.sub(r"<[^>]+>", " ", c.get(f"/v/{tid}").text))
    assert ("1 dépense à vérifier : Gisèle a rejoint « groupe Minibus » ; ajoute sa part à la main si besoin.") in page
    assert f'href="/v/{tid}/depenses">Voir</a>' in c.get(f"/v/{tid}").text
    lst = c.get(f"/v/{tid}/depenses").text
    assert ">Voir</a>" not in lst                                           # déjà sur la page Dépenses
    assert ("Gisèle a rejoint « groupe Minibus » : ajoute sa part à la main si besoin."
            in clean(re.sub(r"<[^>]+>", " ", lst)))


def test_withdrawal_in_expense_with_fixed_amounts_is_flagged(env):
    """Désistement repris par le groupe : dans une dépense avec des montants fixés 🔒 (le resto : Anne 45 €), sa part
    va aux montants non fixés, les montants fixés ne bougent pas : la dépense est « à vérifier ». Pas les dépenses à
    parts égales (le gîte), ni celles qu'il garde, ni si l'asso reprend sa part."""
    app, ops, tid, c, P, E = env
    with app.state.db.Session() as s_:                       # le resto : Anne 45 € fixés 🔒, Bob et Chloé libres
        e = s_.get(M.Expense, E["Resto"])
        for x in e.shares:
            x.weight = 1.0
            x.fixed = 4500 if x.participant_id == P["Anne"] else None
        s_.commit()
    ops.withdraw(tid, "Bob", pris_en_charge_par="groupe")
    page = clean(re.sub(r"<[^>]+>", " ", c.get(f"/v/{tid}/depenses").text))
    assert "1 dépense à vérifier : désistement de Bob ; vérifie les montants fixés 🔒 de cette dépense." in page
    assert "Désistement de Bob : sa part est répartie entre les montants non fixés" in page
    assert "Tous les montants sont fixés" not in page
    # Chloé aussi, mais l'asso reprend sa part : rien de plus à vérifier
    ops.withdraw(tid, "Chloé", pris_en_charge_par="asso")
    assert "1 dépense à vérifier" in clean(re.sub(r"<[^>]+>", " ", c.get(f"/v/{tid}/depenses").text))
    r = c.post(f"/v/{tid}/depenses/{E['Resto']}/verifiee", follow_redirects=True)
    assert "dépense à vérifier" not in clean(re.sub(r"<[^>]+>", " ", c.get(f"/v/{tid}/depenses").text))
