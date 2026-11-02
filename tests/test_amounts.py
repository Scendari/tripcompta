"""Vérification des montants.

1. Les chiffres du voyage de démo sont recalculés « à la main » (formules indépendantes du moteur).
2. Des centaines de voyages aléatoires vérifient les invariants (équilibre, arrondis, verrous…).
3. Le PDF affiche bien les mêmes montants que l'application.
"""
import math
import random
from fractions import Fraction as F

import pytest

from compta import engine as E
from compta import models as M
from compta.db import Database
from compta.demo import seed_demo
from compta.services import compute_trip


def near(x):
    """Centime le plus proche (affichage d'une ligne)."""
    return math.floor(F(x) + F(1, 2))


def ceil(x):
    return math.ceil(F(x))


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    db = Database(tmp_path_factory.mktemp("demo"))
    tid = seed_demo(db)
    s = db.Session()
    t = s.get(M.Trip, tid)
    yield t, compute_trip(t, E.ACTUAL), compute_trip(t, E.PLANNED), {p.name: p.id for p in t.participants}
    s.close()


# ---------------------------------------------------------------- 1. démo vérifiée à la main

def test_demo_total_cost(demo):
    t, r, rp, P = demo
    # réel quand il existe, sinon prévu (navette 240 €, restaurant 420 €)
    expected = 174000 + 231000 + 28500 + 9000 + 18640 + 6480 + 17210 + 6480 + 24000 + 41235 + 42000
    assert r.total_cost == expected == 598545
    # selon le prévu (le snorkeling n'a pas de prévu : on prend son réel)
    assert rp.total_cost == 180000 + 240000 + 30000 + 9000 + 18000 + 6500 + 18000 + 6500 + 24000 + 40000 + 42000


def test_demo_withdrawn_antoine(demo):
    t, r, _, P = demo
    a = r.people[P["Antoine"]]
    assert a.withdrawn
    assert a.share == 174000 // 12 == 14500      # il garde seulement sa part du gîte
    assert a.paid == 20000
    assert a.balance == -5500                    # l'asso lui doit 55 €


def test_demo_car_shares_and_advances(demo):
    t, r, _, P = demo
    fuel_luc = next(e for e in t.expenses if e.label.startswith("Carburant — voiture de Luc"))
    for n in ["Luc", "Marie", "Sophie", "Julien"]:
        assert r.share(P[n], fuel_luc.id) == 18640 // 4 == 4660
    assert r.people[P["Luc"]].advanced == 18640 + 6480
    assert r.people[P["Stéphane"]].advanced == 6480 + 41235
    assert r.people[P["Thierry"]].advanced == 17210
    assert r.people[P["Claire"]].advanced == 9000


def test_demo_dives_with_locked_baptism(demo):
    """Forfait 2 310 € : 8 plongeurs × 6 + Julien 4 + Camille 3 = 55 parts ; Antoine (6) se désiste -> 49.
    Camille est verrouillée à 120 € ; l'écart est réparti sur les 46 autres parts."""
    t, r, _, P = demo
    club = next(e for e in t.expenses if e.label.startswith("Forfait plongées"))
    camille_base = F(231000) * 3 / 49
    gap = camille_base - 12000
    julien = F(231000) * 4 / 49 + gap * 4 / 46
    marie = F(231000) * 6 / 49 + gap * 6 / 46
    assert r.share(P["Camille"], club.id) == 12000
    assert r.share(P["Julien"], club.id) == near(julien) == 19043      # 190,4347… affiché au centime le plus proche
    assert r.share(P["Marie"], club.id) == near(marie)
    assert r.share(P["Antoine"], club.id) == 0
    assert r.share(P["Hugo"], club.id) == 0          # non plongeur
    assert r.lines[(P["Camille"], club.id)].locked


def test_demo_hugo_line_by_line(demo):
    """Hugo : gîte −40 € (étudiant, pris en charge par l'asso), snorkeling avec Claire, parts communes à 11,
    et sa part de compensation de la réduction de Thierry (−25 € sur le total, au prorata des totaux)."""
    t, r, _, P = demo
    E_ = {e.label: e for e in t.expenses}
    gite = E_["Gîte « Les Pins » — 3 nuits"]
    assert r.share(P["Hugo"], gite.id) == 14500 - 4000
    assert r.share(P["Hugo"], E_["Sortie palmes-masque-tuba"].id) == 4500
    assert r.share(P["Hugo"], E_["Navette bateau Hyères ↔ Port-Cros"].id) == near(F(24000, 11)) == 2182
    assert r.share(P["Hugo"], E_["Courses communes"].id) == near(F(41235, 11)) == 3749
    assert r.share(P["Hugo"], E_["Restaurant de clôture"].id) == near(F(42000, 11)) == 3818

    # compensation de Thierry : au prorata des totaux EXACTS des non-désistés, hors Thierry
    totals = {}
    for (pid, eid), l in r.lines.items():
        if eid != "global":
            totals[pid] = totals.get(pid, 0) + l.exact
    eligible = {p: v for p, v in totals.items() if p not in (P["Thierry"], P["Antoine"]) and v > 0}
    hugo_comp = F(2500) * eligible[P["Hugo"]] / sum(eligible.values())
    assert r.share(P["Hugo"], "global") == near(hugo_comp)
    assert r.share(P["Thierry"], "global") == -2500
    # le total de Hugo est arrondi UNE fois, au centime supérieur, sur la somme exacte
    exact = 10500 + 4500 + F(24000, 11) + F(41235, 11) + F(42000, 11) + hugo_comp
    assert r.people[P["Hugo"]].share == ceil(exact)
    assert r.people[P["Hugo"]].share - r.people[P["Hugo"]].rounding == sum(
        l.cents for (pid, _), l in r.lines.items() if pid == P["Hugo"])
    assert r.people[P["Hugo"]].paid == 10000


def test_demo_asso_and_rounding(demo):
    t, r, _, P = demo
    assert [(c.cents, "Hugo" in c.reason) for c in r.asso_charges] == [(4000, True)]
    assert r.is_balanced()
    assert 0 <= r.rounding_gain < len(r.lines)        # moins d'un centime par ligne arrondie


def test_demo_balance_identity(demo):
    t, r, _, P = demo
    paid = sum(m.amount for m in t.movements if m.kind == "versement")
    refunded = sum(m.amount for m in t.movements if m.kind == "remboursement")
    advanced = sum(e.best_known() for e in t.expenses if e.payer_id)
    assert sum(p.balance for p in r.people.values()) == r.total_shares - paid - advanced + refunded
    assert r.to_collect - r.to_refund == sum(p.balance for p in r.people.values())


def test_demo_planned_vs_actual_regularisation(demo):
    t, r, rp, P = demo
    # Gîte : prévu 1 800, réel 1 740 -> chacun paie 5 € de moins sur le gîte (12 personnes)
    gite = next(e for e in t.expenses if e.label.startswith("Gîte"))
    assert rp.share(P["Marie"], gite.id) - r.share(P["Marie"], gite.id) == 500
    assert rp.is_balanced() and r.is_balanced()


def test_demo_subaccount(demo):
    from compta.services import sub_view
    t, *_ , P = demo
    v = sub_view(t.subaccounts[0])
    # Pizzas 96,50 € / 5 = 19,30 ; Glaces 22 € / 4 = 5,50 ; Sophie a déjà remboursé 19,30 à Marie
    assert v["shares"][P["Camille"]] == 1930
    assert v["shares"][P["Luc"]] == 1930 + 550
    assert v["balances"][P["Marie"]] == 9650 - 1930 - 550 - 1930
    assert v["balances"][P["Sophie"]] == -550          # 19,30 + 5,50 de glaces − 19,30 déjà remboursés
    assert sum(v["balances"].values()) == 0


# ---------------------------------------------------------------- 2. invariants sur voyages aléatoires

def random_trip(rng: random.Random):
    n = rng.randint(2, 16)
    people = [E.Participant(i, f"P{i}") for i in range(1, n + 1)]
    ids = [p.id for p in people]
    expenses = []
    for eid in range(1, rng.randint(1, 10) + 1):
        who = rng.sample(ids, rng.randint(1, n))
        weights = {p: rng.choice([1, 1, 1, 2, 3, "0,5", "1,5"]) for p in who}
        planned = rng.choice([None, rng.randint(1, 300000)])
        actual = rng.choice([None, rng.randint(1, 300000)]) if planned else rng.randint(1, 300000)
        payer = rng.choice([None, None, rng.choice(ids)])
        expenses.append(E.Expense(eid, f"D{eid}", "C", planned, actual, payer, weights))
    adjustments = []
    for aid in range(1, rng.randint(0, 5) + 1):
        e = rng.choice(expenses + [None])
        pool = list(e.weights) if e else ids
        p = rng.choice(pool)
        fixed = rng.random() < 0.3
        value = rng.randint(0, 20000) if fixed else rng.randint(-5000, 5000)
        comp = rng.choice([E.OTHERS, E.ASSO, E.PERSON])
        compensator = rng.choice([x for x in ids if x != p]) if comp == E.PERSON else None
        adjustments.append(E.Adjustment(aid, p, value, e.id if e else None, fixed, comp, compensator))
    withdrawals = []
    for p in rng.sample(ids, rng.randint(0, max(0, n // 4))):
        kept = {e.id for e in expenses if rng.random() < 0.3}
        withdrawals.append(E.Withdrawal(p, rng.choice([E.GROUP, E.ASSO]), kept))
    movements = [E.Movement(rng.choice(ids), rng.choice(["versement", "remboursement"]), rng.randint(1, 50000))
                 for _ in range(rng.randint(0, 10))]
    return people, expenses, adjustments, withdrawals, movements


@pytest.mark.parametrize("seed", range(400))
def test_random_trip_invariants(seed):
    rng = random.Random(seed)
    people, expenses, adjustments, withdrawals, movements = random_trip(rng)
    for mode in (E.ACTUAL, E.PLANNED):
        r = E.compute(people, expenses, adjustments, withdrawals, movements, mode=mode)
        # Le total tombe toujours juste, et les arrondis profitent toujours à l'asso
        assert r.is_balanced(), (seed, mode)
        assert r.rounding_gain >= 0
        assert r.rounding_gain < len(r.lines) + len(r.asso_charges) + 1
        # Tout est en centimes entiers
        assert all(isinstance(l.cents, int) for l in r.lines.values())
        assert all(isinstance(c.cents, int) for c in r.asso_charges)
        # Coût total = somme des montants retenus
        assert r.total_cost == sum(e.amount(mode) for e in expenses)
        # Identité des soldes
        tot_bal = sum(p.balance for p in r.people.values())
        paid = sum(m.amount for m in movements if m.kind == "versement")
        refunded = sum(m.amount for m in movements if m.kind == "remboursement")
        adv = sum(e.best_known() for e in expenses if e.payer)
        assert tot_bal == r.total_shares - paid - adv + refunded
        # Un participant désisté n'a plus de part sur ce qu'il n'a pas gardé (sauf compensation explicite)
        explicit = {(a.compensator, a.expense) for a in adjustments if a.compensation == E.PERSON}
        for w in withdrawals:
            for e in expenses:
                if e.id not in w.kept and (w.participant, e.id) not in explicit:
                    assert r.share(w.participant, e.id) == 0, (seed, w, e.id)
        # Sans ajustement ni désistement : parts égales -> écart d'au plus 1 centime
        if not adjustments and not withdrawals:
            for e in expenses:
                ws = {E.to_weight(w) for w in e.weights.values()}
                if len(ws) == 1:
                    vals = [r.share(p, e.id) for p in e.weights]
                    assert max(vals) - min(vals) <= 1


@pytest.mark.parametrize("seed", range(150))
def test_random_locked_amounts_are_respected(seed):
    """Un montant verrouillé reste exactement à sa valeur, quels que soient les autres ajustements
    (sauf si on désigne explicitement cette personne comme compensatrice)."""
    rng = random.Random(10_000 + seed)
    n = rng.randint(3, 10)
    people = [E.Participant(i, f"P{i}") for i in range(1, n + 1)]
    ids = [p.id for p in people]
    e = E.Expense(1, "Gîte", actual=rng.randint(10000, 500000), weights={p: rng.choice([1, 2, 3]) for p in ids})
    locked_p = rng.choice(ids)
    locked_v = rng.randint(0, 30000)
    adjs = [E.Adjustment(1, locked_p, locked_v, 1, fixed=True)]
    for k in range(rng.randint(1, 4)):
        p = rng.choice([x for x in ids if x != locked_p])
        adjs.append(E.Adjustment(2 + k, p, rng.randint(-3000, 3000), 1, False, rng.choice([E.OTHERS, E.ASSO])))
    rng.shuffle(adjs)
    r = E.compute(people, [e], adjs)
    assert r.share(locked_p, 1) == locked_v
    assert r.is_balanced()


def test_determinism():
    rng = random.Random(42)
    args = random_trip(rng)
    a = E.compute(*args)
    b = E.compute(*args)
    assert {k: l.cents for k, l in a.lines.items()} == {k: l.cents for k, l in b.lines.items()}


# ---------------------------------------------------------------- 3. le PDF dit la même chose que l'appli

def pdf_text(data: bytes) -> str:
    pdfium = pytest.importorskip("pypdfium2")
    doc = pdfium.PdfDocument(data)
    return "\n".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))


def norm(s: str) -> str:
    return s.replace(" ", " ").replace(" ", " ").replace("\r", "")


def test_pdf_amounts_match_app(demo):
    from compta.export import trip_pdf
    t, r, _, P = demo
    txt = norm(pdf_text(trip_pdf(t, E.ACTUAL)))
    assert norm(E.fmt(r.total_cost)) in txt
    assert norm(E.fmt(r.total_shares)) in txt
    assert norm(E.fmt(r.to_collect)) in txt and norm(E.fmt(r.to_refund)) in txt
    # « Ce qu'il reste à régler » : deux tableaux, comme à l'écran (plus de section « Contrôle »)
    recv, back = txt.split("À recevoir —")[1].split("À rendre —")[0], txt.split("À rendre —")[1]
    for p in r.people.values():
        if p.balance > 0:
            assert f"{p.name} {norm(E.fmt(p.balance))}" in recv
        elif p.balance < 0:
            assert f"{p.name} {norm(E.fmt(-p.balance))}" in back
    assert "Contrôle" not in txt and "✓ juste" not in txt
    # Les comptes « Entre nous » ne sont pas dans le bilan de l'asso
    assert "Resto du vendredi soir" not in txt
    # Les numéros de justificatifs sont repris
    assert "J01" in txt and "J06" in txt


def test_subaccount_pdf_amounts(demo):
    from compta.export import subaccount_pdf
    t, *_ = demo
    txt = norm(pdf_text(subaccount_pdf(t, t.subaccounts[0])))
    assert "Pizzas" in txt and "96,50 €" in txt
    assert "doit" in txt
