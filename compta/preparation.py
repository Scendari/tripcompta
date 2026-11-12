"""Préparer un voyage — calcul, Python pur, sans base de données.

Avant que le voyage existe en comptabilité : des postes (Avion, Hôtel, Plongées…), plusieurs offres par poste, une
offre sélectionnée par poste, et ce que ça donne : le budget, le prix par personne, la marge, la réserve par rapport au
prix annoncé, et « si tu sélectionnes cette offre ».

Tous les montants sont en centimes entiers. Les pourcentages (marge, répartition d'un forfait) sont en centièmes
de pour cent : 500 = 5 %, 3333 = 33,33 %, 10000 = 100 %.

Règles :
- total d'une offre pour N participants : « par personne » prix × quantité × N ; « pour le groupe » prix × quantité ;
  « par lot » (taxi de 4 places, chambre de 2) prix × quantité × plafond(N / capacité) ;
- budget = somme des totaux des offres sélectionnées, plus l'enveloppe repas (par personne et par jour, sur les jours où
  elle compte, × participants) ; les postes sans offre sélectionnée sont « pas encore choisis » ;
- prix par personne = budget / N, arrondi au centime supérieur ;
- marge totale = budget × pourcentage arrondi au centime le plus proche (un demi-centime : au-dessus), ou euros par
  personne × N ; prix avec marge = (budget + marge totale) / N, arrondi au centime supérieur ;
- réserve = prix annoncé × N − budget (négative : à ce prix, il manque de l'argent) ;
- répartition d'un forfait entre ses postes : chaque poste sauf le dernier reçoit sa part arrondie au centime le plus
  proche, le dernier reçoit le reste : la somme fait exactement le total.

Les règles qui refusent une saisie (N < 1, quantité < 1…) sont dans compta/writes.py ; ici, on suppose des données
déjà vérifiées (une entrée impossible lève ValueError).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

PER_PERSON = "personne"
GROUP = "groupe"
LOT = "lot"
COUNTINGS = (PER_PERSON, GROUP, LOT)

PERCENT = "pourcent"          # marge en pourcentage du budget / répartition en pourcentages
EUROS = "euros"               # marge en euros par personne
AMOUNTS = "montant"           # répartition en montants (parts du prix saisi)
MARGIN_KINDS = (PERCENT, EUROS)
SPLITS = ("", AMOUNTS, PERCENT)
FULL = 10000                  # 100 %, en centièmes de pour cent


@dataclass
class Offer:
    """Une offre : `items` = les postes qu'elle couvre (dans l'ordre des postes du projet) ; `split` : "" (un seul
    bloc), AMOUNTS (`shares` = parts du prix, même unité que `price`) ou PERCENT (`shares` en centièmes de %)."""
    id: int
    price: int
    counting: str = GROUP
    items: list = field(default_factory=list)
    quantity: int = 1
    capacity: Optional[int] = None
    chosen: bool = False
    split: str = ""
    shares: Optional[list] = None


@dataclass
class Meals:
    """Enveloppe repas : `per_day` centimes par personne et par jour (None : pas d'enveloppe), sur `days` jours (None :
    durée inconnue) ; `off` : les jours où elle ne compte pas ; `amounts` : {jour: centimes} pour un jour changé."""
    per_day: Optional[int] = None
    days: Optional[int] = None
    off: set = field(default_factory=set)
    amounts: dict = field(default_factory=dict)


def meals_per_person(m: Optional[Meals]) -> tuple:
    """(repas par personne, jours comptés) : la somme, sur les jours comptés, du montant du jour (le sien s'il a été
    changé, sinon celui de l'enveloppe). Sans enveloppe ou sans durée : (0, 0)."""
    if m is None or m.per_day is None or not m.days:
        return 0, 0
    counted = [d for d in range(1, m.days + 1) if d not in m.off]
    return sum(m.amounts.get(d, m.per_day) for d in counted), len(counted)


@dataclass
class WhatIf:
    """Ce que donnerait le choix d'une offre pas encore sélectionnée."""
    offer: int
    budget: int
    delta: int                 # budget obtenu − budget actuel
    per_person: int
    delta_per_person: int      # prix par personne obtenu − prix par personne actuel
    replaces: list             # offres sélectionnées qu'elle écarterait
    empty: list                # postes sans offre sélectionnée après ce choix
    lost: list                 # parmi eux, ceux qui avaient une offre sélectionnée avant


@dataclass
class Result:
    people: int
    totals: dict               # offre -> total pour N participants
    offer_per_person: dict     # offre -> son total par personne (au centime supérieur)
    chosen: list               # offres sélectionnées
    budget: int
    per_person: int
    empty: list                # postes « pas encore choisi »
    by_item: dict              # poste -> {"offer": id|None, "amount": centimes|None, "with": [autres postes du bloc]}
    margin: int                # marge totale
    price_with_margin: int
    announced: Optional[int]
    reserve: Optional[int]     # None sans prix annoncé
    reserve_per_person: Optional[int]   # prix annoncé − prix par personne
    above_margin: Optional[int]         # prix annoncé − prix avec la marge (None sans prix annoncé ou sans marge)
    what_if: dict              # offre non sélectionnée -> WhatIf
    meals_per_person: int = 0  # enveloppe repas, par personne (comprise dans le budget)
    meals_total: int = 0       # … pour tout le groupe
    meals_days: int = 0        # jours où elle compte


# ---------------------------------------------------------------- arrondis

def ceil_div(a: int, b: int) -> int:
    """a / b arrondi à l'entier supérieur (b > 0)."""
    return -(-a // b)


def round_div(a: int, b: int) -> int:
    """a / b arrondi à l'entier le plus proche, un demi vers le haut (b > 0)."""
    return (2 * a + b) // (2 * b)


# ---------------------------------------------------------------- une offre

def offer_total(o: Offer, people: int) -> int:
    """Le total de l'offre pour `people` participants."""
    if people < 1:
        raise ValueError("Il faut au moins une personne.")
    if o.quantity < 1:
        raise ValueError("La quantité doit être d'au moins 1.")
    base = o.price * o.quantity
    if o.counting == PER_PERSON:
        return base * people
    if o.counting == GROUP:
        return base
    if o.counting == LOT:
        if not o.capacity or o.capacity < 1:
            raise ValueError("Un prix pour un nombre de personnes vaut pour au moins une personne.")
        return base * ceil_div(people, o.capacity)
    raise ValueError(f"Façon de compter inconnue : {o.counting}")


def split_total(o: Offer, total: int) -> dict:
    """{poste: montant} : le total de l'offre réparti entre ses postes ({} sans répartition). Chaque poste sauf le
    dernier reçoit sa part arrondie au centime le plus proche, le dernier reçoit le reste."""
    if not o.split or len(o.items) < 2 or not o.shares:
        return {}
    weights, whole = list(o.shares), (o.price if o.split == AMOUNTS else FULL)
    out, given = {}, 0
    for item, w in zip(o.items[:-1], weights[:-1]):
        part = round_div(total * w, whole) if whole else 0
        out[item] = part
        given += part
    out[o.items[-1]] = total - given
    return out


# ---------------------------------------------------------------- un projet

def _budget(chosen, totals) -> int:
    return sum(totals[o] for o in chosen)


def _empty(items, offers, chosen) -> list:
    covered = {i for o in offers if o.id in chosen for i in o.items}
    return [i for i in items if i not in covered]


def replaced_by(o: Offer, offers, chosen) -> list:
    """Les offres sélectionnées qu'écarterait le choix de `o` : celles qui partagent au moins un poste avec elle."""
    mine = set(o.items)
    return [x.id for x in offers if x.id in chosen and x.id != o.id and mine & set(x.items)]


def compute(people: int, items, offers, margin_kind: str = PERCENT, margin_value: int = 0,
            announced: Optional[int] = None, meals: Optional[Meals] = None) -> Result:
    """`items` : les postes du projet, dans l'ordre (identifiants) ; `offers` : [Offer]. Les offres sélectionnées sont
    celles marquées `chosen` (une seule par poste : c'est le chemin d'écriture qui y veille). `meals` : l'enveloppe
    repas, qui entre dans le budget (budget = offres sélectionnées + repas × participants)."""
    if people < 1:
        raise ValueError("Il faut au moins une personne.")
    items, offers = list(items), list(offers)
    totals = {o.id: offer_total(o, people) for o in offers}
    chosen = [o.id for o in offers if o.chosen]
    meal_each, meal_days = meals_per_person(meals)
    meal_total = meal_each * people
    budget = _budget(chosen, totals) + meal_total
    empty = _empty(items, offers, chosen)

    by_item = {i: {"offer": None, "amount": None, "with": []} for i in items}
    for o in offers:
        if not o.chosen:
            continue
        parts = split_total(o, totals[o.id])
        for i in o.items:
            if i in by_item:
                by_item[i] = {"offer": o.id, "amount": totals[o.id] if len(o.items) == 1 else parts.get(i),
                              "with": [x for x in o.items if x != i]}

    if margin_kind == PERCENT:
        margin = round_div(budget * margin_value, FULL)
    elif margin_kind == EUROS:
        margin = margin_value * people
    else:
        raise ValueError(f"Type de marge inconnu : {margin_kind}")
    with_margin = ceil_div(budget + margin, people)
    reserve = None if announced is None else announced * people - budget

    what_if = {}
    for o in offers:
        if o.chosen:
            continue
        gone = replaced_by(o, offers, chosen)
        after = [x for x in chosen if x not in gone] + [o.id]
        b = _budget(after, totals) + meal_total
        left = _empty(items, offers, after)
        pp = ceil_div(b, people)
        what_if[o.id] = WhatIf(offer=o.id, budget=b, delta=b - budget, per_person=pp,
                               delta_per_person=pp - ceil_div(budget, people),
                               replaces=gone, empty=left, lost=[i for i in left if i not in empty])

    return Result(people=people, totals=totals, offer_per_person={k: ceil_div(v, people) for k, v in totals.items()},
                  chosen=chosen, budget=budget, per_person=ceil_div(budget, people),
                  empty=empty, by_item=by_item, margin=margin, price_with_margin=with_margin, announced=announced,
                  reserve=reserve, reserve_per_person=None if announced is None else announced - ceil_div(budget, people),
                  above_margin=None if announced is None or not margin else announced - with_margin,
                  what_if=what_if, meals_per_person=meal_each, meals_total=meal_total, meals_days=meal_days)


def live_offers(offers) -> set:
    """Les offres encore « en course » : sélectionnées, ou dont au moins un poste n'a encore aucune offre sélectionnée. Leurs
    échéances comptent ; celles des offres écartées ne comptent plus."""
    offers = list(offers)
    covered = {i for o in offers if o.chosen for i in o.items}
    return {o.id for o in offers if o.chosen or any(i not in covered for i in o.items)}
