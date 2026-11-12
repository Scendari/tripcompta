"""Préparer un voyage (tranche 1 : données, calcul, chemin d'écriture ; pas d'écran).

Montants de référence calculés à la main (brief de la tranche 1), retrouvés ici au centime :
projet à 5 postes : Avion, Taxi, Hôtel, Plongées, Minibus.
O1 Avion Transavia 210,00 par personne ; O2 Avion Air France 320,00 par personne ; O3 Taxi 60,00 par lot de 4 ;
O4 Hôtel X 90,00 par lot de 2, 5 nuits ; O5 Plongées Club B 38,00 par personne, 10 plongées ; O6 Minibus 600,00
pour le groupe ; O7 Forfait Club A (Hôtel + Plongées) 580,00 par personne. Retenues : O1, O3, O4, O5, O6.
"""
import datetime as dt
import sqlite3

import pytest

from compta import models as M
from compta import preparation as P
from compta import services as S
from compta import writes as W
from compta.db import SCHEMA, Database
from compta.engine import fmt

AVION, TAXI, HOTEL, PLONGEES, MINIBUS = "Avion", "Taxi", "Hôtel", "Plongées", "Minibus"


# ---------------------------------------------------------------- le projet de référence, par le chemin d'écriture

@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "donnees", app_version="1.0.0")


class Ref:
    """Le projet de référence dans une vraie base. `o["O1"]`, `it["Avion"]` : ses offres et ses postes."""

    def __init__(self, s, people=10):
        self.s = s
        self.plan = W.save_plan(s, name="Mer Rouge 2027", people=people, place="Hurghada")
        self.it = {n: W.add_plan_item(s, self.plan, n) for n in (AVION, TAXI, HOTEL, PLONGEES, MINIBUS)}
        it = self.it
        self.o = {
            "O1": W.save_offer(s, self.plan, label="Transavia", price=21000, counting=P.PER_PERSON,
                               items=[it[AVION].id], chosen=True),
            "O2": W.save_offer(s, self.plan, label="Air France", price=32000, counting=P.PER_PERSON,
                               items=[it[AVION].id]),
            "O3": W.save_offer(s, self.plan, label="Taxi", price=6000, counting=P.LOT, capacity=4,
                               items=[it[TAXI].id], chosen=True),
            "O4": W.save_offer(s, self.plan, label="Hôtel X", price=9000, counting=P.LOT, capacity=2, quantity=5,
                               quantity_label="nuits", items=[it[HOTEL].id], chosen=True),
            "O5": W.save_offer(s, self.plan, label="Club B", price=3800, counting=P.PER_PERSON, quantity=10,
                               quantity_label="plongées", items=[it[PLONGEES].id], chosen=True),
            "O6": W.save_offer(s, self.plan, label="Minibus", price=60000, counting=P.GROUP,
                               items=[it[MINIBUS].id], chosen=True),
            "O7": W.save_offer(s, self.plan, label="Forfait Club A", price=58000, counting=P.PER_PERSON,
                               items=[it[HOTEL].id, it[PLONGEES].id]),
        }
        s.commit()

    def result(self, people=None):
        return S.compute_plan(self.plan, people)

    def totals(self, r, *names):
        return [r.totals[self.o[n].id] for n in names]

    def names(self, item_ids):
        by_id = {v.id: k for k, v in self.it.items()}
        return [by_id[i] for i in item_ids]

    def chosen(self):
        return sorted(k for k, v in self.o.items() if v.chosen)


@pytest.fixture
def ref(db):
    with db.Session() as s:
        yield Ref(s)


CHOSEN = ("O1", "O3", "O4", "O5", "O6")


@pytest.mark.parametrize("n, amounts, budget, per_person", [
    (10, [210000, 18000, 225000, 380000, 60000], 893000, 89300),
    (8, [168000, 12000, 180000, 304000, 60000], 724000, 90500),
    (9, [189000, 18000, 225000, 342000, 60000], 834000, 92667),     # 926,666… arrondi au-dessus
    (12, [252000, 18000, 270000, 456000, 60000], 1056000, 88000),
    (1, [21000, 6000, 45000, 38000, 60000], 170000, 170000),
])
def test_reference_amounts(ref, n, amounts, budget, per_person):
    ref.plan.people = n
    r = ref.result()
    assert ref.totals(r, *CHOSEN) == amounts
    assert r.budget == budget and r.per_person == per_person
    assert r.empty == []
    assert ref.result(n).budget == budget          # le même calcul en passant le nombre à part


def test_lot_counts_whole_lots(ref):
    """N = 10 : 3 taxis de 4 places, 5 chambres de 2 ; N = 9 : toujours 3 taxis et 5 chambres."""
    for n, taxis, rooms in ((10, 3, 5), (9, 3, 5), (8, 2, 4), (4, 1, 2), (5, 2, 3), (1, 1, 1)):
        r = ref.result(n)
        assert r.totals[ref.o["O3"].id] == 6000 * taxis
        assert r.totals[ref.o["O4"].id] == 9000 * 5 * rooms


def test_what_if_at_ten(ref):
    r = ref.result()
    o2 = r.what_if[ref.o["O2"].id]
    assert (o2.budget, o2.delta, o2.empty, o2.lost) == (1003000, 110000, [], [])
    assert (o2.per_person, o2.delta_per_person) == (100300, 11000)
    assert o2.replaces == [ref.o["O1"].id]
    o7 = r.what_if[ref.o["O7"].id]
    assert (o7.budget, o7.delta, o7.per_person, o7.empty) == (868000, -25000, 86800, [])
    assert o7.delta_per_person == -2500
    assert r.offer_per_person[ref.o["O3"].id] == 1800 and r.offer_per_person[ref.o["O7"].id] == 58000
    assert sorted(o7.replaces) == sorted([ref.o["O4"].id, ref.o["O5"].id])
    assert set(r.what_if) == {ref.o["O2"].id, ref.o["O7"].id}    # seulement les offres pas encore retenues


def test_what_if_o7_at_nine(ref):
    o7 = ref.result(9).what_if[ref.o["O7"].id]
    assert (o7.budget, o7.delta, o7.per_person) == (789000, -45000, 87667)
    assert o7.delta_per_person == 87667 - 92667
    assert ref.result(9).offer_per_person[ref.o["O3"].id] == 2000      # 180,00 / 9


def test_choosing_a_package_unchooses_what_it_replaces_and_nothing_comes_back(ref):
    s, o = ref.s, ref.o
    gone = W.choose_offer(s, ref.plan, o["O7"])
    s.commit()
    assert sorted(gone) == ["Club B", "Hôtel X"]
    assert ref.chosen() == ["O1", "O3", "O6", "O7"]
    r = ref.result()
    assert r.budget == 868000 and r.per_person == 86800 and r.empty == []
    # « si tu sélectionnes O4 » : O7 serait écartée, et Plongées se retrouverait sans offre
    w = r.what_if[o["O4"].id]
    assert w.budget == 513000 and ref.names(w.empty) == [PLONGEES] and ref.names(w.lost) == [PLONGEES]
    assert w.replaces == [o["O7"].id]
    # on retient vraiment O4 : O7 ne l'est plus, O5 ne revient pas toute seule
    assert W.choose_offer(s, ref.plan, o["O4"]) == ["Forfait Club A"]
    s.commit()
    assert ref.chosen() == ["O1", "O3", "O4", "O6"]
    r = ref.result()
    assert r.budget == 513000 and ref.names(r.empty) == [PLONGEES]
    assert r.by_item[ref.it[PLONGEES].id] == {"offer": None, "amount": None, "with": []}


def test_one_chosen_offer_per_item_always(ref):
    """Quelle que soit la suite de choix, chaque poste a au plus une offre sélectionnée."""
    s, o = ref.s, ref.o
    for name in ("O2", "O7", "O1", "O5", "O4", "O7", "O2", "O5"):
        W.choose_offer(s, ref.plan, o[name])
        covered = [ln.item_id for x in ref.plan.offers if x.chosen for ln in x.links]
        assert len(covered) == len(set(covered)), name
        assert o[name].chosen
    # désélectionner une offre ne re-sélectionne rien
    W.choose_offer(s, ref.plan, o["O5"], chosen=False)
    assert ref.chosen() == ["O2", "O3", "O6"]


def test_saving_a_chosen_offer_on_a_new_item_unchooses_the_other(ref):
    """Une offre sélectionnée qu'on étend à un autre poste écarte l'offre sélectionnée de ce poste (règle dans l'écriture)."""
    s, o, it = ref.s, ref.o, ref.it
    W.save_offer(s, ref.plan, o["O6"], label="Minibus + taxi", price=60000, counting=P.GROUP,
                 items=[it[MINIBUS].id, it[TAXI].id])
    assert o["O6"].chosen and not o["O3"].chosen
    # une offre non sélectionnée peut couvrir un poste déjà pris sans rien changer
    W.save_offer(s, ref.plan, o["O2"], label="Air France", price=32000, counting=P.PER_PERSON,
                 items=[it[AVION].id, it[TAXI].id])
    assert o["O1"].chosen and o["O6"].chosen and not o["O2"].chosen
    # créer une offre déjà retenue
    o8 = W.save_offer(s, ref.plan, label="Hôtel Y", price=8000, counting=P.LOT, capacity=2, quantity=5,
                      items=[it[HOTEL].id], chosen=True)
    assert o8.chosen and not o["O4"].chosen


# ---------------------------------------------------------------- répartition d'un forfait

def _split_o7(ref, split, shares):
    it = ref.it
    W.save_offer(ref.s, ref.plan, ref.o["O7"], label="Forfait Club A", price=58000, counting=P.PER_PERSON,
                 items=[it[HOTEL].id, it[PLONGEES].id], split=split,
                 shares={it[HOTEL].id: shares[0], it[PLONGEES].id: shares[1]}, chosen=True)
    return ref.result()


def test_package_without_split_stays_one_block(ref):
    W.choose_offer(ref.s, ref.plan, ref.o["O7"])
    r = ref.result()
    o7 = ref.o["O7"].id
    assert r.by_item[ref.it[HOTEL].id] == {"offer": o7, "amount": None, "with": [ref.it[PLONGEES].id]}
    assert r.by_item[ref.it[PLONGEES].id] == {"offer": o7, "amount": None, "with": [ref.it[HOTEL].id]}
    assert r.by_item[ref.it[AVION].id] == {"offer": ref.o["O1"].id, "amount": 210000, "with": []}


def test_package_split_in_amounts(ref):
    r = _split_o7(ref, P.AMOUNTS, (30000, 28000))
    assert r.totals[ref.o["O7"].id] == 580000
    assert r.by_item[ref.it[HOTEL].id]["amount"] == 300000
    assert r.by_item[ref.it[PLONGEES].id]["amount"] == 280000
    assert r.budget == 868000                       # la répartition ne change pas le budget


def test_package_split_in_percent(ref):
    r = _split_o7(ref, P.PERCENT, (5500, 4500))
    assert r.by_item[ref.it[HOTEL].id]["amount"] == 319000
    assert r.by_item[ref.it[PLONGEES].id]["amount"] == 261000


def test_split_that_does_not_add_up_is_refused(ref):
    with pytest.raises(W.WriteError, match="580,00"):
        _split_o7(ref, P.AMOUNTS, (30000, 27000))
    with pytest.raises(W.WriteError, match="100 %"):
        _split_o7(ref, P.PERCENT, (6000, 3000))
    with pytest.raises(W.WriteError, match="chaque poste"):
        W.save_offer(ref.s, ref.plan, ref.o["O7"], label="Forfait Club A", price=58000, counting=P.PER_PERSON,
                     items=[ref.it[HOTEL].id, ref.it[PLONGEES].id], split=P.PERCENT,
                     shares={ref.it[HOTEL].id: 10000})
    ref.s.rollback()
    assert ref.o["O7"].split == "" and not ref.o["O7"].chosen      # rien n'a bougé


def test_split_rounding_last_item_takes_the_rest():
    a, b, c = 1, 2, 3
    o = P.Offer(id=1, price=33333, counting=P.PER_PERSON, items=[a, b], split=P.PERCENT, shares=[3300, 6700],
                chosen=True)
    r = P.compute(10, [a, b], [o])
    assert r.totals[1] == 333330
    assert (r.by_item[a]["amount"], r.by_item[b]["amount"]) == (109999, 223331)
    o = P.Offer(id=2, price=10000, counting=P.GROUP, items=[a, b, c], split=P.PERCENT, shares=[3333, 3333, 3334],
                chosen=True)
    r = P.compute(10, [a, b, c], [o])
    assert [r.by_item[i]["amount"] for i in (a, b, c)] == [3333, 3333, 3334]
    # trois tiers d'un euro : 0,33 + 0,33 + 0,34, la somme tombe juste
    o = P.Offer(id=3, price=100, counting=P.GROUP, items=[a, b, c], split=P.PERCENT, shares=[3333, 3333, 3334],
                chosen=True)
    parts = P.split_total(o, 100)
    assert parts == {a: 33, b: 33, c: 34} and sum(parts.values()) == 100


def test_split_follows_the_order_of_items_in_the_project(ref):
    """Le « dernier » poste d'une répartition est le dernier dans la liste du projet, quel que soit l'ordre saisi."""
    it = ref.it
    W.save_offer(ref.s, ref.plan, ref.o["O7"], label="Forfait Club A", price=33333, counting=P.PER_PERSON,
                 items=[it[PLONGEES].id, it[HOTEL].id], split=P.PERCENT,
                 shares={it[PLONGEES].id: 6700, it[HOTEL].id: 3300}, chosen=True)
    r = ref.result()
    assert r.by_item[it[HOTEL].id]["amount"] == 109999 and r.by_item[it[PLONGEES].id]["amount"] == 223331


def test_split_of_a_free_package():
    o = P.Offer(id=1, price=0, counting=P.GROUP, items=[1, 2], split=P.AMOUNTS, shares=[0, 0], chosen=True)
    assert P.split_total(o, 0) == {1: 0, 2: 0}


# ---------------------------------------------------------------- marge et prix annoncé

@pytest.mark.parametrize("n, kind, value, margin, price", [
    (10, P.PERCENT, 500, 44650, 93765),
    (10, P.EUROS, 2000, 20000, 91300),
    (9, P.PERCENT, 500, 41700, 97300),
    (9, P.EUROS, 2000, 18000, 94667),       # 946,666… arrondi au-dessus
    (10, P.PERCENT, 0, 0, 89300),
])
def test_margin(ref, n, kind, value, margin, price):
    W.update_plan(ref.s, ref.plan, people=n, margin_kind=kind, margin_value=value)
    r = ref.result()
    assert (r.margin, r.price_with_margin) == (margin, price)


@pytest.mark.parametrize("n, announced, reserve, each", [(10, 95000, 57000, 5700), (10, 88000, -13000, -1300),
                                                    (9, 95000, 21000, 95000 - 92667)])
def test_announced_price_and_reserve(ref, n, announced, reserve, each):
    W.update_plan(ref.s, ref.plan, people=n, announced=announced)
    r = ref.result()
    assert r.reserve == reserve and r.announced == announced and r.reserve_per_person == each


def test_changing_one_field_keeps_the_others(ref):
    """« Et si on était 9 ? » : changer le nombre de participants ne touche ni la marge, ni le prix annoncé, ni le
    lieu, ni les dates, ni la note ; et inversement."""
    s, plan = ref.s, ref.plan
    W.update_plan(s, plan, start=dt.date(2027, 5, 1), end=dt.date(2027, 5, 9), note="Club de Lyon",
                  margin_kind=P.EUROS, margin_value=2000, announced=95000)
    s.commit()
    before = {k: getattr(plan, k) for k in W.PLAN_FIELDS}
    assert before["place"] == "Hurghada" and before["name"] == "Mer Rouge 2027"
    W.update_plan(s, plan, people=9)
    s.commit()
    assert {k: getattr(plan, k) for k in W.PLAN_FIELDS} == {**before, "people": 9}
    r = ref.result()
    assert (r.price_with_margin, r.reserve) == (94667, 21000)
    W.update_plan(s, plan, announced=None)                       # retirer le prix annoncé seulement
    W.update_plan(s, plan, margin_kind=P.PERCENT, margin_value=500)
    assert {k: getattr(plan, k) for k in W.PLAN_FIELDS} == {**before, "people": 9, "announced": None,
                                                           "margin_kind": P.PERCENT, "margin_value": 500}
    # mêmes refus que save_plan, et rien ne bouge
    _refused(W.update_plan, s, plan, people=0, match="au moins une personne")
    _refused(W.update_plan, s, plan, margin_value=-5, match="négative")
    _refused(W.update_plan, s, plan, end=dt.date(2027, 4, 1), match="avant")
    _refused(W.update_plan, s, plan, name=" ", match="nom au projet")
    assert plan.people == 9 and plan.end == dt.date(2027, 5, 9)
    with pytest.raises(TypeError):
        W.update_plan(s, plan, peple=9)                          # une faute de frappe ne passe pas sans bruit


def test_ordering_items(ref):
    s, plan, it = ref.s, ref.plan, ref.it
    names = lambda: [i.name for i in plan.items]                 # noqa: E731
    W.move_plan_item(s, plan, it[MINIBUS], 0)
    assert names() == [MINIBUS, AVION, TAXI, HOTEL, PLONGEES]
    W.move_plan_item(s, plan, it[MINIBUS], 99)
    assert names() == [AVION, TAXI, HOTEL, PLONGEES, MINIBUS]
    W.order_plan_items(s, plan, [it[n].id for n in (PLONGEES, AVION, HOTEL, MINIBUS, TAXI)])
    s.commit()
    s.expire_all()
    assert names() == [PLONGEES, AVION, HOTEL, MINIBUS, TAXI]   # relu de la base
    # un nouveau poste arrive à la fin
    W.add_plan_item(s, plan, "Assurance")
    assert names()[-1] == "Assurance"
    _refused(W.order_plan_items, s, plan, [it[AVION].id], match="chacun des postes")
    _refused(W.order_plan_items, s, plan, [i.id for i in plan.items] + [it[AVION].id], match="chacun des postes")
    _refused(W.order_plan_items, s, plan, [i.id for i in plan.items][:-1] + [999999], match="chacun des postes")
    other = W.save_plan(s, name="Autre projet", people=2)
    _refused(W.move_plan_item, s, plan, W.add_plan_item(s, other, "Bateau"), 0, match="ce projet")


def test_item_order_decides_who_takes_the_rest_of_a_split(ref):
    """Le dernier poste d'un forfait réparti, dans l'ordre de la liste, reçoit le reste ; les autres, leur part
    arrondie au centime le plus proche.
    - forfait à 333,33 par personne, N = 10 (3 333,30), 33 % Hôtel / 67 % Plongées, Plongées rangé avant Hôtel :
      Plongées 2 233,311 -> 2 233,31, Hôtel le reste 1 099,99 ;
    - forfait de 1,00 pour le groupe à 33,33 / 33,33 / 33,34 % : 0,33 ; 0,33 ; et 0,34 pour le dernier de la liste,
      qui change quand on déplace un poste."""
    s, plan, it = ref.s, ref.plan, ref.it
    W.save_offer(s, plan, ref.o["O7"], label="Forfait Club A", price=33333, counting=P.PER_PERSON,
                 items=[it[HOTEL].id, it[PLONGEES].id], split=P.PERCENT,
                 shares={it[HOTEL].id: 3300, it[PLONGEES].id: 6700}, chosen=True)
    W.move_plan_item(s, plan, it[HOTEL], 99)                     # Hôtel passe après Plongées
    r = ref.result()
    assert ref.o["O7"].item_ids() == [it[PLONGEES].id, it[HOTEL].id]
    assert r.by_item[it[PLONGEES].id]["amount"] == 223331 and r.by_item[it[HOTEL].id]["amount"] == 109999
    # forfait de 1,00 pour le groupe sur Avion, Taxi, Minibus : 33,33 / 33,33 / 33,34 %
    shares = {it[AVION].id: 3333, it[TAXI].id: 3333, it[MINIBUS].id: 3334}
    o = W.save_offer(s, plan, label="Petit forfait", price=100, counting=P.GROUP,
                     items=[it[AVION].id, it[TAXI].id, it[MINIBUS].id], split=P.PERCENT, shares=shares,
                     chosen=True)
    amounts = lambda: {ref.names([i])[0]: ref.result().by_item[i]["amount"] for i in o.item_ids()}   # noqa: E731
    assert amounts() == {AVION: 33, TAXI: 33, MINIBUS: 34}
    W.move_plan_item(s, plan, it[MINIBUS], 0)                    # Minibus en tête : c'est Taxi le dernier
    assert ref.names(o.item_ids()) == [MINIBUS, AVION, TAXI]
    assert amounts() == {MINIBUS: 33, AVION: 33, TAXI: 34}
    # 33,34 % du Minibus arrondi : 0,33 ; Taxi (33,33 %) reçoit le reste, 0,34 : la somme fait toujours 1,00
    assert sum(amounts().values()) == 100


def test_no_announced_price_no_reserve(ref):
    assert ref.result().reserve is None


def test_margin_rounds_to_nearest_cent():
    o = P.Offer(id=1, price=1001, counting=P.GROUP, items=[1], chosen=True)
    assert P.compute(1, [1], [o], P.PERCENT, 250).margin == 25        # 25,025 -> 25
    o.price = 1002
    assert P.compute(1, [1], [o], P.PERCENT, 250).margin == 25        # 25,05 -> 25
    o.price = 1020
    assert P.compute(1, [1], [o], P.PERCENT, 250).margin == 26        # 25,5 -> 26 (le demi : au-dessus)
    o.price = 1019
    assert P.compute(1, [1], [o], P.PERCENT, 250).margin == 25        # 25,475 -> 25


def test_free_offer_is_accepted(ref):
    o = W.save_offer(ref.s, ref.plan, label="Transfert inclus", price=0, counting=P.GROUP,
                     items=[ref.it[TAXI].id], chosen=True)
    r = ref.result()
    assert r.totals[o.id] == 0 and r.budget == 893000 - 18000 and not ref.o["O3"].chosen


# ---------------------------------------------------------------- refus

def _refused(fn, *a, match=None, **kw):
    with pytest.raises(W.WriteError, match=match) as e:
        fn(*a, **kw)
    msg = str(e.value)
    for jargon in ("NULL", "None", "id", "Error", "integer", "centime"):
        assert jargon not in msg.split(), msg
    return msg


def test_refusals(ref):
    s, plan, it = ref.s, ref.plan, ref.it
    _refused(W.save_plan, s, plan, name="X", people=0, match="au moins une personne")
    _refused(W.save_plan, s, None, name="X", people=-3, match="au moins une personne")
    _refused(W.save_plan, s, None, name="  ", people=3, match="nom au projet")
    _refused(W.save_plan, s, plan, name="X", people=3, margin_value=-1, match="négative")
    _refused(W.save_plan, s, plan, name="X", people=3, announced=-100, match="négatif")
    _refused(W.save_plan, s, plan, name="X", people=3, margin_kind="beaucoup", match="Marge")
    _refused(W.save_plan, s, plan, name="X", people=3, start=dt.date(2027, 5, 10), end=dt.date(2027, 5, 1),
             match="avant")
    base = dict(label="Taxi", price=6000, counting=P.LOT, capacity=4, items=[it[TAXI].id])
    _refused(W.save_offer, s, plan, **{**base, "capacity": 0}, match="Au moins 1")
    _refused(W.save_offer, s, plan, **{**base, "capacity": None}, match="Au moins 1")
    _refused(W.save_offer, s, plan, **{**base, "quantity": 0}, match="quantité")
    _refused(W.save_offer, s, plan, **{**base, "price": -1}, match="négatif")
    _refused(W.save_offer, s, plan, **{**base, "items": []}, match="au moins un poste")
    _refused(W.save_offer, s, plan, **{**base, "label": ""}, match="nom à l'offre")
    _refused(W.save_offer, s, plan, **{**base, "counting": "au hasard"}, match="par personne")
    _refused(W.add_plan_item, s, plan, "", match="nom au poste")
    _refused(W.add_plan_item, s, plan, "  hotel ", match="existe déjà")           # accents et majuscules ignorés
    _refused(W.rename_plan_item, s, plan, it[TAXI], "AVION", match="existe déjà")
    W.rename_plan_item(s, plan, it[TAXI], "Taxis")                              # renommer, oui
    W.rename_plan_item(s, plan, it[TAXI], "taxis")                              # changer la casse de son nom, oui
    s.rollback()
    assert plan.people == 10 and len(plan.offers) == 7 and len(plan.items) == 5
    # capacité ignorée hors lot ; doublon de poste ignoré
    o = W.save_offer(s, plan, label="Navette", price=500, counting=P.PER_PERSON, capacity=0,
                     items=[it[TAXI].id, it[TAXI].id])
    assert o.capacity is None and o.item_ids() == [it[TAXI].id]


def test_items_and_offers_of_another_project_are_refused(ref):
    s = ref.s
    other = W.save_plan(s, name="Autre projet", people=4)
    foreign = W.add_plan_item(s, other, "Bateau")
    foreign_offer = W.save_offer(s, other, label="Bateau", price=1000, items=[foreign.id])
    _refused(W.save_offer, s, ref.plan, label="X", price=10, items=[foreign.id], match="ce projet")
    _refused(W.save_offer, s, ref.plan, label="X", price=10, items=[ref.it[AVION].id, foreign.id], match="ce projet")
    _refused(W.save_offer, s, ref.plan, foreign_offer, label="X", price=10, items=[ref.it[AVION].id],
             match="ce projet")
    _refused(W.choose_offer, s, ref.plan, foreign_offer, match="ce projet")
    _refused(W.delete_offer, s, ref.plan, foreign_offer, match="ce projet")
    _refused(W.delete_plan_item, s, ref.plan, foreign, match="ce projet")
    _refused(W.rename_plan_item, s, ref.plan, foreign, "Y", match="ce projet")
    assert not foreign_offer.chosen and len(other.items) == 1


def test_calculation_refuses_impossible_inputs():
    with pytest.raises(ValueError):
        P.compute(0, [], [])
    with pytest.raises(ValueError):
        P.offer_total(P.Offer(id=1, price=100, counting=P.LOT, capacity=0), 4)
    with pytest.raises(ValueError):
        P.offer_total(P.Offer(id=1, price=100, quantity=0), 4)


# ---------------------------------------------------------------- suppression

def test_deleting_an_item(ref):
    s, o, it = ref.s, ref.o, ref.it
    W.save_offer(s, ref.plan, o["O7"], label="Forfait Club A", price=58000, counting=P.PER_PERSON,
                 items=[it[HOTEL].id, it[PLONGEES].id], split=P.AMOUNTS,
                 shares={it[HOTEL].id: 30000, it[PLONGEES].id: 28000})
    s.commit()
    gone = W.delete_plan_item(s, ref.plan, it[HOTEL])
    s.commit()
    assert gone == ["Hôtel X"]                                   # ne couvrait que l'Hôtel
    ids = {x.id for x in ref.plan.offers}
    assert o["O4"].id not in ids and o["O7"].id in ids
    assert o["O7"].item_ids() == [it[PLONGEES].id]               # le forfait garde les Plongées…
    assert o["O7"].split == "" and [ln.share for ln in o["O7"].links] == [None]   # … et perd sa répartition
    assert [i.name for i in ref.plan.items] == [AVION, TAXI, PLONGEES, MINIBUS]
    r = ref.result()
    assert r.budget == 893000 - 225000 and r.empty == []
    # le forfait couvre maintenant seulement les Plongées : le retenir écarte Club B
    assert W.choose_offer(s, ref.plan, o["O7"]) == ["Club B"]
    # rien ne reste en base qui désigne le poste supprimé
    assert s.query(M.PlanOfferItem).filter_by(item_id=it[HOTEL].id).count() == 0


def test_deleting_offers_and_projects(ref, db):
    s = ref.s
    assert W.delete_offer(s, ref.plan, ref.o["O1"]) == "Transavia"
    assert ref.names(ref.result().empty) == [AVION]
    other = W.save_plan(s, name="Autre projet", people=4)
    W.add_plan_item(s, other, "Bateau")
    s.commit()
    assert W.delete_plan(s, ref.plan) == "Mer Rouge 2027"
    s.commit()
    assert s.query(M.Plan).count() == 1
    assert s.query(M.PlanItem).count() == 1 and s.query(M.PlanOffer).count() == 0
    assert s.query(M.PlanOfferItem).count() == 0


def test_project_lives_apart_from_trips(ref):
    """Un projet n'apparaît pas parmi les voyages ; supprimer un voyage ne touche pas aux projets."""
    s = ref.s
    t = W.create_trip(s, "Voyage déjà en comptabilité")
    ref.plan.trip_id = t.id
    s.commit()
    assert [x.name for x in s.query(M.Trip).all()] == ["Voyage déjà en comptabilité"]
    s.delete(t)
    s.commit()
    assert s.get(M.Plan, ref.plan.id).trip_id == t.id and len(ref.plan.offers) == 7


# ---------------------------------------------------------------- défaire / rétablir, sauvegardes, structure

def test_undo_and_redo_on_a_project(db):
    with db.Session() as s:
        ref = Ref(s)
        pid, o7 = ref.plan.id, ref.o["O7"].id
    db.snapshot("Retenir le forfait Club A")
    with db.Session() as s:
        plan = s.get(M.Plan, pid)
        W.choose_offer(s, plan, s.get(M.PlanOffer, o7))
        s.commit()
        assert S.compute_plan(plan).budget == 868000
    db.snapshot("Supprimer le poste Hôtel")
    with db.Session() as s:
        plan = s.get(M.Plan, pid)
        W.delete_plan_item(s, plan, next(i for i in plan.items if i.name == HOTEL))
        s.commit()
    db.snapshot("Passer à 9 personnes")
    with db.Session() as s:
        plan = s.get(M.Plan, pid)
        W.update_plan(s, plan, people=9, margin_value=500)
        s.commit()

    def state():
        with db.Session() as s:
            plan = s.get(M.Plan, pid)
            r = S.compute_plan(plan)
            return plan.people, [i.name for i in plan.items], sorted(o.label for o in plan.offers if o.chosen), r.budget

    assert state() == (9, [AVION, TAXI, PLONGEES, MINIBUS], ["Forfait Club A", "Minibus", "Taxi", "Transavia"],
                       189000 + 18000 + 58000 * 9 + 60000)
    assert db.undo() == "Passer à 9 personnes"
    assert state() == (10, [AVION, TAXI, PLONGEES, MINIBUS], ["Forfait Club A", "Minibus", "Taxi", "Transavia"],
                       868000)
    assert db.undo() == "Supprimer le poste Hôtel"
    assert state() == (10, [AVION, TAXI, HOTEL, PLONGEES, MINIBUS],
                       ["Forfait Club A", "Minibus", "Taxi", "Transavia"], 868000)
    assert db.undo() == "Retenir le forfait Club A"
    assert state() == (10, [AVION, TAXI, HOTEL, PLONGEES, MINIBUS],
                       ["Club B", "Hôtel X", "Minibus", "Taxi", "Transavia"], 893000)
    assert db.redo() == "Retenir le forfait Club A"
    assert state()[3] == 868000
    assert db.redo() == "Supprimer le poste Hôtel"
    assert state()[1] == [AVION, TAXI, PLONGEES, MINIBUS]
    assert [h["label"] for h in db.history()][:2] == ["Supprimer le poste Hôtel", "Retenir le forfait Club A"]


def test_projects_are_in_the_daily_backup_and_come_back_with_it(db):
    with db.Session() as s:
        Ref(s)
    db.daily_backup()
    with db.Session() as s:
        W.delete_plan(s, s.query(M.Plan).one())
        s.commit()
    name = db.backups()[0]["name"]
    db.restore_backup(name)
    with db.Session() as s:
        plan = s.query(M.Plan).one()
        assert S.compute_plan(plan).budget == 893000 and len(plan.offers) == 7


def test_structure_number_and_tables(db):
    con = sqlite3.connect(db.path)
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA == 5
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    assert {"plans", "plan_items", "plan_offers", "plan_offer_items"} <= names


def test_amounts_read_back_in_euros(ref):
    """Les montants de référence, tels qu'ils seront affichés."""
    def euros(c):
        return fmt(c).replace("\u202f", " ").replace("\xa0", " ")

    r = ref.result(9)
    assert euros(r.budget) == "8 340,00 €" and euros(r.per_person) == "926,67 €"
    assert euros(ref.result().what_if[ref.o["O7"].id].delta) == "−250,00 €"
