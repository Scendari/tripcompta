"""Un seul chemin d'écriture : les formulaires et l'API / l'IA créent et modifient les voyages par le même module
(compta/writes.py). Le même scénario, saisi à l'écran ou donné par l'IA, laisse exactement les mêmes lignes et
les mêmes comptes ; et les règles qui protègent les comptes valent pour les deux.

Le scénario (montants vérifiés à la main, voir test_same_scenario_same_rows_and_hand_checked_amounts) :
Anne, Bruno, Chloé, David.
- Gîte 400 € payé par l'asso, pour tous ;
- Essence 60 € payée par Bruno, pour Anne, Bruno et Chloé ;
- Restaurant : deux tickets 80,12 + 69,10 = 149,22 €, Anne a payé 100 € et l'asso le reste, 19,22 € remboursés à
  Anne par le restaurant (coût : 130,00 €), pour tous ;
- Bateau : 200 € prévus, pour tous ;
- David a versé 200 € par chèque ;
- Chloé a une remise de 20 € offerte par l'asso ;
- David se désiste : il paie quand même le gîte, le groupe reprend le reste, on lui rend 50 €.
"""
import datetime as dt
import html
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from compta import engine as E
from compta import models as M
from compta import services as S
from compta import writes as W
from compta.ops import Ops, OpsError
from compta.web import create_app

ROOT = Path(__file__).resolve().parent.parent
PEOPLE = ["Anne", "Bruno", "Chloé", "David"]


def clean(t):
    return re.sub(r"\s+", " ", html.unescape(t).replace("\xa0", " "))


def message(r):
    """Le message d'un retour de formulaire (redirection avec ?msg=)."""
    from urllib.parse import parse_qs, urlparse
    return parse_qs(urlparse(r.headers["location"]).query).get("msg", [""])[0]


def refused(r):
    return r.status_code == 303 and "e=1" in r.headers["location"]


def post(cl, url, data, referer=None):
    return cl.post(url, data=data, follow_redirects=False, headers={"referer": referer} if referer else {})


# ---------------------------------------------------------------- le même scénario, par les deux chemins
class ByForms:
    """Le scénario saisi à l'écran."""

    def __init__(self, tmp_path):
        self.app = create_app(tmp_path / "ecran")
        self.cl = TestClient(self.app)
        r = post(self.cl, "/voyages", {"name": "Week-end", "plongee": "1", "place": "Hyères", "start": "2027-05-14",
                                       "end": "2027-05-16"})
        self.tid = int(re.search(r"/v/(\d+)/", r.headers["location"]).group(1))
        assert post(self.cl, f"/v/{self.tid}/participants", {"names": "\n".join(PEOPLE)}).status_code == 303
        with self.app.state.db.Session() as s:
            self.ids = {p.name: p.id for p in s.get(M.Trip, self.tid).participants}

    def expense(self, eid=None, **f):
        url = f"/v/{self.tid}/depenses" + (f"/{eid}" if eid else "")
        r = post(self.cl, url, f, referer=f"http://testserver/v/{self.tid}/depenses/nouvelle")
        assert r.status_code == 303 and not refused(r), message(r)
        return r

    def everyone(self, *names):
        return {f"inc_{self.ids[n]}": "1" for n in (names or PEOPLE)}

    def eid(self, label):
        with self.app.state.db.Session() as s:
            return next(e.id for e in s.get(M.Trip, self.tid).expenses if e.label == label)

    def fill(self):
        i = self.ids
        self.expense(label="Gîte", category="__new__", category_new="Gîte rural", subcategory="__new__",
                     subcategory_new="Nuitées", actual="400", date="2027-05-14", payer="asso", **self.everyone())
        self.expense(label="Essence", category="Transport", actual="60", date="2027-05-15", payer=str(i["Bruno"]),
                     **self.everyone("Anne", "Bruno", "Chloé"))
        self.expense(label="Restaurant", category="Repas", ticket_amount=["80,12", "69,10"],
                     ticket_date=["2027-05-15", "2027-05-16"], payer_kind="multi", mp_0=str(i["Anne"]), ma_0="100",
                     mp_1="asso", ma_1="49,22", has_refund="1", refund="19,22", refund_to=str(i["Anne"]),
                     note="  deux services  ", **self.everyone())
        self.expense(label="Bateau", category="Plongée", planned="200", payer="asso", **self.everyone())
        r = post(self.cl, f"/v/{self.tid}/argent", {"participant": str(i["David"]), "kind": "versement",
                                                     "amount": "200", "date": "2027-05-02", "note": "acompte",
                                                     "method": "cheque"})
        assert r.status_code == 303 and not refused(r)
        r = post(self.cl, f"/v/{self.tid}/remises", {"participant": str(i["Chloé"]), "value": "20", "by": "asso",
                                                      "reason": "étudiante"})
        assert r.status_code == 303 and not refused(r)
        r = post(self.cl, f"/v/{self.tid}/desistement", {"participant": str(i["David"]), "absorber": E.GROUP,
                                                          "kept": [str(self.eid("Gîte"))], "date": "2027-05-10",
                                                          "refund": "50"})
        assert r.status_code == 303 and not refused(r)

    def change(self):
        """Essence passe à 75 €, payée par Chloé ; le restaurant est supprimé ; le versement de David aussi."""
        i = self.ids
        self.expense(self.eid("Essence"), label="Essence", category="Transport", actual="75", date="2027-05-15",
                     payer=str(i["Chloé"]), **self.everyone("Anne", "Bruno", "Chloé"))
        assert post(self.cl, f"/v/{self.tid}/depenses/{self.eid('Restaurant')}/supprimer", {}).status_code == 303
        with self.app.state.db.Session() as s:
            mid = next(m.id for m in s.get(M.Trip, self.tid).movements if m.kind == "versement")
        assert post(self.cl, f"/v/{self.tid}/argent/{mid}/supprimer", {}).status_code == 303


class ByApi:
    """Le même scénario, donné par l'IA (ou un programme)."""

    def __init__(self, tmp_path):
        self.app = create_app(tmp_path / "ia")
        self.ops = Ops(self.app.state.db)
        self.tid = self.ops.create_trip("Week-end", lieu="Hyères", debut="2027-05-14", fin="2027-05-16")["voyage_id"]
        self.ops.add_participants(self.tid, PEOPLE)

    def fill(self):
        o, t = self.ops, self.tid
        o.add_expense(t, "Gîte", categorie="Gîte rural", sous_categorie="Nuitées", montant_reel="400",
                      date="2027-05-14")
        o.add_expense(t, "Essence", categorie="Transport", montant_reel="60", date="2027-05-15", paye_par="Bruno",
                      pour=["Anne", "Bruno", "Chloé"])
        o.add_expense(t, "Restaurant", categorie="Repas",
                      montant_reel=[{"montant": "80,12", "date": "2027-05-15"}, {"montant": "69,10", "date": "2027-05-16"}],
                      paye_par={"Anne": "100", "asso": "49,22"}, rembourse="19,22", rembourse_a="Anne",
                      note="deux services")
        o.add_expense(t, "Bateau", categorie="Plongée", montant_prevu="200")
        o.add_movement(t, "David", "200", date="2027-05-02", note="acompte", moyen="cheque")
        o.adjust(t, "Chloé", "20", type="reduction", sur="total", compense_par="asso", motif="étudiante")
        o.withdraw(t, "David", pris_en_charge_par="groupe", parts_conservees=["Gîte"], date="2027-05-10",
                   remboursement="50")

    def change(self):
        o, t = self.ops, self.tid
        o.update_expense(t, "Essence", montant_reel="75", paye_par="Chloé")
        o.delete_expense(t, "Restaurant")
        with self.app.state.db.Session() as s:
            from compta.ops import do_delete_movement
            do_delete_movement(s, s.get(M.Trip, t), "David", "200", "versement")
            s.commit()


def dump(app, tid):
    """Tout ce qui compte dans un voyage, par les noms (les numéros de ligne n'ont pas à être les mêmes)."""
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        n = {p.id: p.name for p in t.participants}
        n[None] = "asso"
        cats = {c.id: c.name for c in t.categories}
        labels = {e.id: e.label for e in t.expenses}
        r = S.compute_trip(t)
        return dict(
            trip=(t.name, t.place, t.organizer, t.start, t.end, t.roles_conf, S.is_diving(t)),
            categories=sorted(cats.values()),
            library=sorted(S.category_library(s)),
            subs={k: sorted(v) for k, v in S.sub_library(s).items() if v},
            people=sorted((p.name, tuple(sorted(p.role_list)), p.companion, p.phone, p.email, p.note)
                          for p in t.participants),
            expenses=sorted((e.label, cats.get(e.category_id), e.subcategory, e.kind, e.planned, e.actual,
                             tuple((x["cents"], str(x.get("date") or "")) for x in e.ticket_list), e.date,
                             n[e.payer_id], tuple(sorted((n[x.payer_id], x.amount) for x in e.payments)),
                             tuple(sorted((n[x.participant_id], x.weight, x.fixed) for x in e.shares)),
                             e.refund, n[e.refund_to_id] if e.refund else None, e.note,
                             len(e.checks)) for e in t.expenses),
            movements=sorted((n[m.participant_id], m.kind, m.amount, m.date, m.note, m.method) for m in t.movements),
            discounts=sorted((n[a.participant_id], a.value, a.fixed, a.compensation, n[a.compensator_id] if a.compensator_id else None, a.reason,
                              a.expense_id) for a in t.adjustments),
            withdrawals=sorted((n[w.participant_id], w.date, w.absorber, tuple(sorted(labels[x] for x in w.kept_ids)),
                                w.note) for w in t.withdrawals),
            accounts={p.name: (p.share, p.paid, p.advanced, p.refunded) for p in r.people.values()},
            settlements=sorted((getattr(a, "name", "asso"), getattr(b, "name", "asso"), c) for a, b, c in r.settlements()),
        )


@pytest.fixture
def both(tmp_path):
    return ByForms(tmp_path), ByApi(tmp_path)


def same(a, b):
    da, db_ = dump(a.app, a.tid), dump(b.app, b.tid)
    for key in da:
        assert da[key] == db_[key], f"« {key} » diffère entre l'écran et l'IA :\n{da[key]}\n{db_[key]}"
    return da


def test_new_trip_is_the_same(both):
    """Un voyage tout neuf et ses participants : mêmes catégories, mêmes rôles de départ, mêmes personnes."""
    d = same(*both)
    assert d["trip"][0] == "Week-end" and d["trip"][-1] is True
    assert [p[0] for p in d["people"]] == PEOPLE and all(p[1] == (S.DIVER,) for p in d["people"])


def test_same_scenario_same_rows_and_hand_checked_amounts(both):
    a, b = both
    a.fill()
    b.fill()
    d = same(a, b)
    # ---- à la main. Parts :
    #   Gîte 400 / 4 = 100,00 chacun (David le paie quand même)
    #   Essence 60 / 3 = 20,00 (Anne, Bruno, Chloé)
    #   Restaurant : 149,22 − 19,22 remboursés = 130,00 ; David désisté, repris par le groupe : 130 / 3 = 43,33 et un
    #     centime de plus pour l'un des trois
    #   Bateau prévu 200 : David désisté : 200 / 3 = 66,66 et deux centimes à placer
    #   Remise Chloé : −20,00
    #   Total des parts : 400 + 60 + 130 + 200 − 20 = 770,00
    acc = d["accounts"]
    assert sum(v[0] for v in acc.values()) == 77000
    assert acc["David"][0] == 10000
    assert acc["Anne"][0] + acc["Bruno"][0] + acc["Chloé"][0] == 67000
    for who, expected in (("Anne", 23000), ("Bruno", 23000), ("Chloé", 21000)):
        assert abs(acc[who][0] - expected) <= 2, (who, acc[who])           # au centime de l'arrondi près
    # Argent : David a versé 200,00 et on lui a rendu 50,00 ; Bruno a avancé 60,00 ; Anne a avancé 100,00 et reçu
    # 19,22 du restaurant, soit 80,78 d'avance au bout du compte
    assert acc["David"][1:] == (20000, 0, 5000)
    assert acc["Bruno"][1:] == (0, 6000, 0)
    assert acc["Anne"][1] == 0 and acc["Anne"][2] == 8078
    assert acc["Chloé"][1:] == (0, 0, 0)
    # Ce qu'il reste à régler : Anne 230 − 80,78 = 149,22 ; Bruno 230 − 60 = 170,00 ; Chloé 210,00 ; on doit encore
    # 50,00 à David (100 de part, 150 gardés)
    owed = {x[0]: x[2] for x in d["settlements"] if x[1] == "asso"}
    back = {x[1]: x[2] for x in d["settlements"] if x[0] == "asso"}
    assert back == {"David": 5000}
    assert abs(owed["Anne"] - 14922) <= 2 and abs(owed["Bruno"] - 17000) <= 2 and abs(owed["Chloé"] - 21000) <= 2
    assert sum(owed.values()) == 77000 - 10000 - 8078 - 6000              # 52 922 : tout se retrouve, au centime
    # Les lignes : la catégorie et la sous-catégorie créées au passage sont retenues, des deux côtés
    assert "Gîte rural" in d["categories"] and "Gîte rural" in d["library"]
    assert d["subs"] == {"Gîte rural": ["Nuitées"]}
    resto = next(e for e in d["expenses"] if e[0] == "Restaurant")
    assert resto[5] == 14922 and resto[6] == ((8012, "2027-05-15"), (6910, "2027-05-16"))
    assert resto[7] == dt.date(2027, 5, 15) and resto[8] == "asso"            # date : celle du premier ticket
    assert resto[9] == (("Anne", 10000), ("asso", 4922)) and resto[11:14] == (1922, "Anne", "deux services")
    assert d["movements"] == [("David", "remboursement", 5000, dt.date(2027, 5, 10), W.WITHDRAWAL_REFUND, ""),
                              ("David", "versement", 20000, dt.date(2027, 5, 2), "acompte", "cheque")]
    assert d["discounts"] == [("Chloé", -2000, False, E.ASSO, None, "étudiante", None)]
    assert d["withdrawals"] == [("David", dt.date(2027, 5, 10), E.GROUP, ("Gîte",), "")]


def test_changes_and_deletions_are_the_same(both):
    a, b = both
    a.fill()
    b.fill()
    a.change()
    b.change()
    d = same(a, b)
    assert [e[0] for e in d["expenses"]] == ["Bateau", "Essence", "Gîte"]
    essence = next(e for e in d["expenses"] if e[0] == "Essence")
    assert essence[5] == 7500 and essence[8] == "Chloé" and essence[9] == ()
    # à la main : Essence 75 / 3 = 25,00 ; plus de restaurant. Parts : 400 + 75 + 200 − 20 = 655,00
    acc = d["accounts"]
    assert sum(v[0] for v in acc.values()) == 65500 and acc["David"][0] == 10000
    assert acc["Chloé"][2] == 7500 and acc["Bruno"][2] == 0 and acc["Anne"][2] == 0
    assert d["movements"] == [("David", "remboursement", 5000, dt.date(2027, 5, 10), W.WITHDRAWAL_REFUND, "")]


def test_one_full_payer_is_simply_the_payer(both):
    """« Plusieurs payeurs » avec une seule personne qui a tout payé : c'est simplement « payé par », des deux côtés."""
    a, b = both
    a.expense(label="Péage", actual="30", payer_kind="multi", mp_0=str(a.ids["Bruno"]), ma_0="30", **a.everyone())
    b.ops.add_expense(b.tid, "Péage", montant_reel="30", paye_par={"Bruno": "30"})
    d = same(a, b)
    assert d["expenses"][0][8:10] == ("Bruno", ())


# ---------------------------------------------------------------- les divergences corrigées
def test_deleting_a_kept_expense_forgets_it(tmp_path):
    """Une dépense qu'un désisté payait quand même est supprimée à l'écran : son numéro quitte le désistement.
    Sinon une dépense créée ensuite pouvait reprendre ce numéro, et le désisté se retrouver à la payer."""
    a = ByForms(tmp_path)
    a.fill()
    a.expense(label="Dernière", actual="90", payer="asso", **a.everyone())
    last = a.eid("Dernière")
    post(a.cl, f"/v/{a.tid}/desistement", {"participant": str(a.ids["David"]), "absorber": E.GROUP,
                                            "kept": [str(a.eid("Gîte")), str(last)], "date": "2027-05-10"})
    assert dump(a.app, a.tid)["withdrawals"][0][3] == ("Dernière", "Gîte")
    assert post(a.cl, f"/v/{a.tid}/depenses/{last}/supprimer", {}).status_code == 303
    with a.app.state.db.Session() as s:
        w = s.get(M.Trip, a.tid).withdrawals[0]
        assert w.kept_ids == {a.eid("Gîte")}
    a.expense(label="Nouvelle", actual="90", payer="asso", **a.everyone())      # reprend souvent le numéro libéré
    with a.app.state.db.Session() as s:
        t = s.get(M.Trip, a.tid)
        r = S.compute_trip(t)
        new = next(e for e in t.expenses if e.label == "Nouvelle")
        assert r.share(a.ids["David"], new.id) == 0                              # David, désisté, ne la paie pas
        assert sum(r.share(a.ids[n], new.id) for n in ("Anne", "Bruno", "Chloé")) == 9000


def test_ai_categories_are_remembered_like_typed_ones(tmp_path):
    b = ByApi(tmp_path)
    b.ops.add_expense(b.tid, "Parking", categorie="  Stationnement   port ", sous_categorie="Port", montant_reel="12")
    with b.app.state.db.Session() as s:
        assert "Stationnement port" in S.category_library(s)                    # proposée aux prochains voyages
        assert S.sub_library(s).get("Stationnement port") == ["Port"]
    # en simulation, rien n'est retenu
    b.ops.add_expense(b.tid, "Essai", categorie="Jamais vue", montant_reel="5", simulation=True)
    with b.app.state.db.Session() as s:
        assert "Jamais vue" not in S.category_library(s)


def test_people_and_expenses_of_another_trip_are_refused(tmp_path):
    """Un formulaire trafiqué (ou une vieille page) qui désigne une personne ou une dépense d'un autre voyage."""
    a = ByForms(tmp_path)
    a.fill()
    r = post(a.cl, "/voyages", {"name": "Autre voyage", "plongee": "1"})
    other = int(re.search(r"/v/(\d+)/", r.headers["location"]).group(1))
    post(a.cl, f"/v/{other}/participants", {"names": "Zoé"})
    post(a.cl, f"/v/{other}/depenses", {"label": "Ailleurs", "actual": "10", "payer": "asso",
                                        **{f"inc_{pid}": "1" for pid in range(1, 12)}})
    with a.app.state.db.Session() as s:
        zoe = s.get(M.Trip, other).participants[0].id
        elsewhere = s.get(M.Trip, other).expenses[0].id
    before = dump(a.app, a.tid)
    ref = f"http://testserver/v/{a.tid}/depenses/nouvelle"
    url = f"/v/{a.tid}/depenses"
    r = post(a.cl, url, {"label": "Intrus", "actual": "50", "payer": str(zoe), **a.everyone()}, ref)
    assert refused(r) and "ne fait pas partie de ce voyage" in message(r)
    r = post(a.cl, url, {"label": "Intrus", "actual": "50", "payer": "n'importe quoi", **a.everyone()}, ref)
    assert refused(r)
    r = post(a.cl, url, {"label": "Intrus", "actual": "50", "payer": "asso", "has_refund": "1", "refund": "5",
                         "refund_to": str(zoe), **a.everyone()}, ref)
    assert refused(r) and "remboursement" in message(r)
    r = post(a.cl, url, {"label": "Intrus", "actual": "50", "payer_kind": "multi", "mp_0": str(a.ids["Anne"]),
                         "ma_0": "20", "mp_1": "asso", "ma_1": "40", **a.everyone()}, ref)
    assert refused(r) and "plus que" in message(r)
    assert dump(a.app, a.tid) == before                                         # rien d'écrit, même pas à moitié
    assert a.cl.get(f"/v/{a.tid}/depenses").status_code == 200
    # désistement : une dépense d'un autre voyage dans « parts conservées » est ignorée
    post(a.cl, f"/v/{a.tid}/desistement", {"participant": str(a.ids["David"]), "absorber": E.GROUP,
                                            "kept": [str(a.eid("Gîte")), str(elsewhere), "999999"],
                                            "date": "2027-05-10"})
    with a.app.state.db.Session() as s:
        assert s.get(M.Trip, a.tid).withdrawals[0].kept_ids == {a.eid("Gîte")}
    # un versement pour quelqu'un d'un autre voyage : refusé
    r = post(a.cl, f"/v/{a.tid}/argent", {"participant": str(zoe), "kind": "versement", "amount": "10"})
    assert refused(r) and dump(a.app, a.tid)["movements"] == before["movements"]


def test_refusal_is_a_page_for_a_form_and_json_for_a_program(tmp_path):
    """La même règle refusée : un retour au formulaire en clair à l'écran, un message JSON pour un programme."""
    a = ByForms(tmp_path)
    r = post(a.cl, f"/v/{a.tid}/depenses", {"label": "Trop", "actual": "50", "payer_kind": "multi",
                                             "mp_0": str(a.ids["Anne"]), "ma_0": "80", **a.everyone()},
             f"http://testserver/v/{a.tid}/depenses/nouvelle")
    assert refused(r) and f"/v/{a.tid}/depenses/nouvelle?msg=" in r.headers["location"]
    r = a.cl.post(f"/api/voyages/{a.tid}/depenses", json={"libelle": "Trop", "montant_reel": "50",
                                                           "paye_par": {"Anne": "80"}})
    assert r.status_code == 400 and "plus que son montant" in r.json()["erreur"]
    assert dump(a.app, a.tid)["expenses"] == []


def test_rules_hold_whoever_writes(tmp_path):
    """Les règles sont dans le module d'écriture : même appelé directement, il refuse ce qui fausserait les comptes."""
    b = ByApi(tmp_path)
    with b.app.state.db.Session() as s:
        t = s.get(M.Trip, b.tid)
        ids = {p.name: p.id for p in t.participants}
        everyone = {pid: 1.0 for pid in ids.values()}
        ok = dict(label="Gîte", actual=40000, weights=everyone)
        for bad, words in (
            (dict(label="  "), "nom"),
            (dict(actual=None), "montant"),
            (dict(actual=-5), "positif"),
            (dict(kind="cadeau"), "Type"),
            (dict(payer_id=9999), "ne fait pas partie"),
            (dict(weights={}), "Pour qui"),
            (dict(weights={9999: 1.0}), "ne fait pas partie"),
            (dict(refund=50000), "dépasse"),
            (dict(refund=100, refund_to_id=9999), "ne fait pas partie"),
            (dict(payments=[(ids["Anne"], 30000), (None, 20000)]), "plus que"),
            (dict(payments=[(ids["Anne"], 0)]), "positif"),
            (dict(fixed={ids["Anne"]: 50000}), "plus que"),
            (dict(fixed={pid: 5000 for pid in ids.values()}), "reste"),
            (dict(fixed={9999: 100}), "Pour qui"),
        ):
            with pytest.raises(W.WriteError) as err:
                W.save_expense(s, t, None, **{**ok, **bad})
            assert words in str(err.value), (bad, str(err.value))
        assert t.expenses == []
        for call, words in (
            (lambda: W.add_movement(t, ids["Anne"], "don", 100), "type"),
            (lambda: W.add_movement(t, ids["Anne"], "versement", 0), "positif"),
            (lambda: W.add_movement(t, 9999, "versement", 100), "ne fait pas partie"),
            (lambda: W.add_movement(t, ids["Anne"], "versement", 100, method="troc"), "Moyen de paiement"),
            (lambda: W.save_discount(s, t, 9999, -100), "ne fait pas partie"),
            (lambda: W.save_discount(s, t, ids["Anne"], -100, E.PERSON, ids["Anne"]), "différente"),
            (lambda: W.save_discount(s, t, ids["Anne"], -100, E.PERSON, None), "compense"),
            (lambda: W.save_withdrawal(s, t, 9999), "ne fait pas partie"),
            (lambda: W.save_withdrawal(s, t, ids["Anne"], absorber="personne"), "reprend"),
            (lambda: W.add_participant(s, t, "anne"), "déjà"),
            (lambda: W.add_participant(s, t, "   "), "nom"),
            (lambda: W.create_trip(s, "  "), "nom"),
        ):
            with pytest.raises(W.WriteError) as err:
                call()
            assert words in str(err.value), str(err.value)
        assert not t.movements and not t.adjustments and not t.withdrawals and len(t.participants) == 4
    assert OpsError is W.WriteError


def test_discount_edited_on_screen_is_a_plain_discount(tmp_path):
    """Un « total fixé » posé par l'IA, puis corrigé à l'écran comme une remise de 30 € : c'est une remise de 30 €,
    pas un total fixé à −30 €."""
    b = ByApi(tmp_path)
    b.ops.add_expense(b.tid, "Gîte", montant_reel="400")
    b.ops.adjust(b.tid, "Anne", "80", type="nouveau_montant", compense_par="asso", verrouiller=True)
    cl = TestClient(b.app)
    with b.app.state.db.Session() as s:
        t = s.get(M.Trip, b.tid)
        a = t.adjustments[0]
        assert a.fixed and a.value == 8000
        aid, anne = a.id, a.participant_id
    r = post(cl, f"/v/{b.tid}/remises", {"participant": str(anne), "value": "30", "by": "asso", "aid": str(aid)})
    assert r.status_code == 303 and not refused(r)
    with b.app.state.db.Session() as s:
        t = s.get(M.Trip, b.tid)
        assert [(x.value, x.fixed) for x in t.adjustments] == [(-3000, False)]
        assert S.compute_trip(t).people[anne].share == 10000 - 3000            # 400 / 4, moins 30


# ---------------------------------------------------------------- personne n'écrit à côté
def test_nobody_creates_rows_on_the_side():
    """Les lignes d'un voyage ne sont créées que dans writes.py (le voyage d'exemple, lui, est une donnée toute
    faite). Un nouveau formulaire ou un nouvel outil pour l'IA passe par ce module."""
    made = re.compile(r"M\.(Trip|Participant|Expense|ExpenseShare|ExpensePayment|Movement|Adjustment|Withdrawal)\(")
    pages = sorted(f"pages/{p.name}" for p in (ROOT / "compta" / "pages").glob("*.py"))
    assert len(pages) > 10                                    # les formulaires sont là (voir test_pages.py)
    for name in ["web.py", "ops.py", "api.py", "importer.py", "mcp_server.py"] + pages:
        src = (ROOT / "compta" / name).read_text(encoding="utf-8")
        assert not made.search(src), f"{name} crée des lignes sans passer par writes.py : {made.findall(src)}"
    src = (ROOT / "compta" / "writes.py").read_text(encoding="utf-8")
    assert len(set(made.findall(src))) == 8
