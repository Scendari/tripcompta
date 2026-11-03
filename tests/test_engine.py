from fractions import Fraction

import pytest

from compta.engine import (
    ROUND_ASSO, ACTUAL, ASSO, GROUP, OTHERS, PERSON, PLANNED, Adjustment, Expense, Movement,
    Participant, SubExpense, SubTransfer, Withdrawal, compute, fmt, minimal_settlements,
    parse_amount, split_exact, sub_balances,
)

P = [Participant(i, n) for i, n in enumerate(["Anne", "Bob", "Chloé", "David"], start=1)]
ALL = {1: 1, 2: 1, 3: 1, 4: 1}


def check(r):
    assert r.is_balanced(), (r.total_shares, r.asso_total, r.rounding_gain, r.total_cost)
    assert r.rounding_gain >= 0


# ---------------------------------------------------------------- formatage

def test_fmt_and_parse():
    assert fmt(150000) == "1 500,00 €"
    assert fmt(-3050) == "−30,50 €"
    assert parse_amount("1 500,50") == 150050
    assert parse_amount("1500.5") == 150050
    assert parse_amount("12") == 1200
    assert parse_amount("12,345") == 1235
    assert parse_amount("-30") == -3000
    assert parse_amount("") is None
    with pytest.raises(ValueError):
        parse_amount("abc")


# ---------------------------------------------------------------- répartition

def test_equal_split():
    r = compute(P, [Expense(1, "Gîte", planned=40000, weights=ALL)])
    assert [r.share(p.id, 1) for p in P] == [10000] * 4
    assert r.rounding_gain == 0
    check(r)


def test_weighted_split_nights():
    # Anne 5 nuits, Bob 3 nuits, Chloé 2 nuits -> 10 parts
    r = compute(P, [Expense(1, "Gîte", actual=100000, weights={1: 5, 2: 3, 3: 2})])
    assert r.share(1, 1) == 50000
    assert r.share(2, 1) == 30000
    assert r.share(3, 1) == 20000
    assert r.share(4, 1) == 0
    check(r)


def test_half_share():
    r = compute(P, [Expense(1, "Repas", actual=3000, weights={1: 1, 2: "0,5"})])
    assert r.share(1, 1) == 2000 and r.share(2, 1) == 1000
    check(r)


def test_subset_only_divers():
    r = compute(P, [Expense(1, "Plongée", actual=24000, weights={1: 1, 3: 1})])
    assert r.share(1, 1) == 12000 and r.share(3, 1) == 12000
    assert r.share(2, 1) == 0
    check(r)


# ---------------------------------------------------------------- arrondis

def test_rounding_up_goes_to_asso():
    """10,00 € pour 3 = 3,333… : chacun 3,33 € et le centime restant est payé par UNE personne tirée au sort
    (la dépense tombe juste : 3,33 + 3,33 + 3,34 = 10,00) ; ou par l'asso si elle prend les centimes."""
    exp = lambda **kw: [Expense(1, "Péage", actual=1000, weights={1: 1, 2: 1, 3: 1}, **kw)]
    r = compute(P[:3], exp())
    assert sorted(r.share(i, 1) for i in (1, 2, 3)) == [333, 333, 334]
    assert [r.people[i].share for i in (1, 2, 3)] == [r.share(i, 1) for i in (1, 2, 3)]
    assert r.rounding_gain == 0 and r.cent_info[1]["need"] == 1 and not r.cent_info[1]["drawn"]
    check(r)
    r = compute(P[:3], exp(cents_who=(2,)))                       # tirage enregistré : Bob
    assert [r.share(i, 1) for i in (1, 2, 3)] == [333, 334, 333] and r.cent_info[1]["drawn"]
    assert r.cent_winners(1) == [2]
    r = compute(P[:3], exp(cents_who=(9,)))                       # tirage qui ne vaut plus : tirage de secours
    assert not r.cent_info[1]["drawn"] and sum(r.share(i, 1) for i in (1, 2, 3)) == 1000
    for r in (compute(P[:3], exp(), rounding=ROUND_ASSO), compute(P[:3], exp(cents_mode="asso"))):
        assert [r.share(i, 1) for i in (1, 2, 3)] == [333, 333, 333]
        assert r.asso_total == 1 and r.asso_charges[-1].reason == "centimes d'arrondi"
        assert r.total_shares + r.asso_total == 1000 and r.is_balanced()
    # le voyage est réglé sur l'asso, mais CETTE dépense sur les participants
    r = compute(P[:3], exp(cents_mode="participants", cents_who=(3,)), rounding=ROUND_ASSO)
    assert [r.share(i, 1) for i in (1, 2, 3)] == [333, 333, 334] and r.asso_total == 0


def test_rounding_many_expenses_never_negative():
    exps = [Expense(i, f"D{i}", actual=1000 + i * 7, weights={1: 1, 2: 2, 3: 3}) for i in range(1, 30)]
    r = compute(P, exps)
    check(r)
    assert r.rounding_gain < 3 * 29


# ---------------------------------------------------------------- prévu / réel

def test_planned_vs_actual():
    e = [Expense(1, "Gîte", planned=40000, actual=36000, weights=ALL),
         Expense(2, "Bateau", planned=20000, weights=ALL)]       # réel pas encore connu
    rp = compute(P, e, mode=PLANNED)
    ra = compute(P, e, mode=ACTUAL)
    assert rp.total_cost == 60000
    assert ra.total_cost == 56000                                # bateau retombe sur le prévu
    assert rp.people[1].share - ra.people[1].share == 1000
    check(rp); check(ra)


def test_planned_falls_back_on_actual():
    e = [Expense(1, "Péage", actual=2000, weights=ALL)]
    assert compute(P, e, mode=PLANNED).total_cost == 2000


# ---------------------------------------------------------------- versements / avances / soldes

def test_balances_payments_and_advances():
    exps = [Expense(1, "Gîte", actual=40000, weights=ALL),
            Expense(2, "Péage", actual=4000, payer=2, weights=ALL)]      # Bob avance le péage
    mv = [Movement(1, "versement", 11000), Movement(2, "versement", 5000),
          Movement(3, "versement", 5000)]
    r = compute(P, exps, movements=mv)
    # part de chacun : 100 + 10 = 110 €
    assert r.people[1].balance == 0
    assert r.people[2].balance == 11000 - 5000 - 4000           # doit encore 20 €
    assert r.people[3].balance == 6000
    assert r.people[4].balance == 11000
    assert r.to_collect == 2000 + 6000 + 11000
    check(r)


def test_refund_reduces_credit():
    exps = [Expense(1, "Gîte", actual=40000, payer=1, weights=ALL)]
    r = compute(P, exps, movements=[Movement(1, "remboursement", 30000)])
    assert r.people[1].balance == 10000 - 40000 + 30000 == 0


def test_settlements_through_asso():
    exps = [Expense(1, "Gîte", actual=40000, payer=1, weights=ALL)]
    r = compute(P, exps)
    s = {(a if a == ASSO else a.referent, b if b == ASSO else b.referent, c) for a, b, c in r.settlements()}
    assert (ASSO, 1, 30000) in s
    assert (2, ASSO, 10000) in s and (4, ASSO, 10000) in s


# ---------------------------------------------------------------- ajustements

def test_adjustment_compensated_by_others():
    exps = [Expense(1, "Gîte", actual=40000, weights=ALL)]
    adj = [Adjustment(1, participant=1, expense=1, value=-3000, compensation=OTHERS, reason="étudiant")]
    r = compute(P, exps, adj)
    assert r.share(1, 1) == 7000
    assert [r.share(i, 1) for i in (2, 3, 4)] == [11000] * 3
    assert r.asso_total == 0
    check(r)


def test_adjustment_compensated_prorata_of_weights():
    exps = [Expense(1, "Gîte", actual=60000, weights={1: 1, 2: 1, 3: 2})]  # 150 / 150 / 300
    adj = [Adjustment(1, participant=1, expense=1, value=-6000, compensation=OTHERS)]
    r = compute(P, exps, adj)
    assert r.share(1, 1) == 9000
    assert r.share(2, 1) == 17000            # +20
    assert r.share(3, 1) == 34000            # +40
    check(r)


def test_adjustment_compensated_by_person():
    exps = [Expense(1, "Gîte", actual=40000, weights=ALL)]
    adj = [Adjustment(1, participant=1, expense=1, value=-2000, compensation=PERSON, compensator=4)]
    r = compute(P, exps, adj)
    assert r.share(1, 1) == 8000 and r.share(4, 1) == 12000
    assert r.share(2, 1) == 10000
    check(r)


def test_adjustment_compensated_by_asso():
    exps = [Expense(1, "Gîte", actual=40000, weights=ALL)]
    adj = [Adjustment(1, participant=1, expense=1, value=-2500, compensation=ASSO, reason="encadrant")]
    r = compute(P, exps, adj)
    assert r.share(1, 1) == 7500 and r.share(2, 1) == 10000
    assert r.asso_total == 2500
    assert "encadrant" in r.asso_charges[0].reason
    check(r)


def test_locked_amount_excluded_from_compensation():
    exps = [Expense(1, "Gîte", actual=40000, weights=ALL)]
    adj = [
        Adjustment(1, participant=2, expense=1, value=10000, fixed=True),   # Bob verrouillé à 100 €
        Adjustment(2, participant=1, expense=1, value=-3000, compensation=OTHERS),
    ]
    r = compute(P, exps, adj)
    assert r.share(2, 1) == 10000                    # Bob ne bouge pas
    assert r.share(1, 1) == 7000
    assert r.share(3, 1) == 11500 and r.share(4, 1) == 11500
    assert r.lines[(2, 1)].locked
    check(r)


def test_lock_after_adjustment():
    """Bob est verrouillé à 100 € APRÈS la réduction d'Anne : il reste à 100 €, et l'écart créé par son
    verrou (110 -> 100) est réparti sur les autres, Anne comprise."""
    exps = [Expense(1, "Gîte", actual=40000, weights=ALL, cents_who=(3,))]
    adj = [
        Adjustment(1, participant=1, expense=1, value=-3000, compensation=OTHERS),
        Adjustment(2, participant=2, expense=1, value=10000, fixed=True),
    ]
    r = compute(P, exps, adj)
    assert r.share(2, 1) == 10000
    # exacts : Anne 73,333…, Chloé et David 113,333… ; le centime restant va à Chloé (tirée au sort)
    assert (r.share(1, 1), r.share(3, 1), r.share(4, 1)) == (7333, 11334, 11333)
    assert r.people[1].share == 7333 and r.people[3].share == 11334
    assert sum(r.people[i].share for i in (1, 2, 3, 4)) == 40000
    check(r)


def test_fixed_new_amount_follows_actual():
    """Montant fixé : reste fixe même si le réel change ; l'écart est recompensé."""
    adj = [Adjustment(1, participant=1, expense=1, value=5000, fixed=True, compensation=OTHERS)]
    for total in (40000, 48000):
        r = compute(P, [Expense(1, "Gîte", actual=total, weights=ALL)], adj)
        assert r.share(1, 1) == 5000
        check(r)


def test_all_locked_falls_back_to_asso():
    exps = [Expense(1, "Gîte", actual=20000, weights={1: 1, 2: 1})]
    adj = [Adjustment(1, participant=2, expense=1, value=10000, fixed=True),
           Adjustment(2, participant=1, expense=1, value=-4000, compensation=OTHERS)]
    r = compute(P, exps, adj)
    assert r.share(1, 1) == 6000
    assert r.asso_total == 4000
    assert any("verrouillés" in w for w in r.warnings)
    check(r)


def test_global_adjustment_on_total():
    exps = [Expense(1, "Gîte", actual=40000, weights=ALL), Expense(2, "Bateau", actual=20000, weights=ALL)]
    adj = [Adjustment(1, participant=1, value=-3000, compensation=OTHERS, reason="petit budget")]
    r = compute(P, exps, adj)
    assert r.people[1].share == 15000 - 3000
    assert r.people[2].share == 15000 + 1000
    check(r)


def test_global_fixed_total_with_rounding():
    exps = [Expense(1, "Gîte", actual=40000, weights=ALL)]
    adj = [Adjustment(1, participant=1, value=9000, fixed=True, compensation=OTHERS)]
    r = compute(P[:4], exps, adj)
    assert r.people[1].share == 9000
    # 10 € répartis sur 3 : 3,33 chacun + 1 centime pour l'un d'eux : 103,33 / 103,33 / 103,34
    assert sorted(r.people[i].share for i in (2, 3, 4)) == [10333, 10333, 10334]
    assert r.rounding_gain == 0 and r.total_shares == 40000
    check(r)


def test_adjustment_increase_compensated_by_others():
    exps = [Expense(1, "Covoit", actual=12000, weights={1: 1, 2: 1, 3: 1})]
    adj = [Adjustment(1, participant=3, expense=1, value=1500, compensation=OTHERS, reason="2 passagers")]
    r = compute(P, exps, adj)
    assert r.share(3, 1) == 5500 and r.share(1, 1) == 3250
    check(r)


def test_adjustment_on_non_participant_warns():
    exps = [Expense(1, "Plongée", actual=10000, weights={1: 1, 2: 1})]
    r = compute(P, exps, [Adjustment(1, participant=4, expense=1, value=-1000)])
    assert any("ne participe pas" in w for w in r.warnings)
    check(r)


# ---------------------------------------------------------------- désistements

def test_withdrawal_group_absorbs():
    exps = [Expense(1, "Gîte", actual=40000, weights=ALL)]
    r = compute(P, exps, withdrawals=[Withdrawal(4, absorber=GROUP)])
    assert r.share(4, 1) == 0
    # 400 / 3 = 133,333… : 133,33 chacun, le centime restant pour l'un des trois
    assert sorted(r.share(i, 1) for i in (1, 2, 3)) == [13333, 13333, 13334]
    assert [r.people[i].share for i in (1, 2, 3)] == [r.share(i, 1) for i in (1, 2, 3)]
    assert r.people[4].withdrawn
    check(r)


def test_withdrawal_asso_absorbs():
    exps = [Expense(1, "Gîte", actual=40000, weights=ALL)]
    r = compute(P, exps, withdrawals=[Withdrawal(4, absorber=ASSO)])
    assert r.share(4, 1) == 0
    assert r.share(1, 1) == 10000
    assert r.asso_total == 10000
    assert "désistement" in r.asso_charges[0].reason
    check(r)


def test_withdrawal_keeps_non_refundable_part_and_gets_refund():
    exps = [Expense(1, "Acompte gîte", actual=20000, weights=ALL),     # non remboursable
            Expense(2, "Bateau", actual=40000, weights=ALL, cents_who=(1,))]
    mv = [Movement(4, "versement", 15000)]
    r = compute(P, exps, withdrawals=[Withdrawal(4, absorber=GROUP, kept={1})], movements=mv)
    assert r.share(4, 1) == 5000          # garde sa part de l'acompte
    assert r.share(4, 2) == 0
    assert r.share(1, 2) == 13334 and r.people[1].share == 5000 + 13334   # 133,33 + le centime tiré au sort
    assert r.share(2, 2) == 13333
    assert r.people[4].balance == 5000 - 15000      # on lui doit 100 €
    # remboursement partiel effectué
    r2 = compute(P, exps, withdrawals=[Withdrawal(4, absorber=GROUP, kept={1})],
                 movements=mv + [Movement(4, "remboursement", 10000)])
    assert r2.people[4].balance == 0
    check(r); check(r2)


def test_withdrawn_keeping_share_pays_like_the_others():
    """Cas réel (sortie plongée 2026) : logement à parts égales sauf l'enfant (part fixe) ; les désistés gardent
    leur part de logement -> ils paient la même part que ceux qui sont venus, réduction de l'enfant comprise."""
    exps = [Expense(1, "Logement", actual=40000, weights=ALL)]
    w = [Withdrawal(4, absorber=GROUP, kept={1})]
    adj = [Adjustment(1, participant=1, expense=1, value=4000, fixed=True, compensation=OTHERS)]
    r = compute(P, exps, adj, w)
    assert r.share(1, 1) == 4000
    assert r.share(4, 1) == r.share(2, 1) == r.share(3, 1) == 12000      # (400 - 40) / 3
    check(r)


def test_withdrawn_released_to_asso_does_not_compensate():
    exps = [Expense(1, "Acompte", actual=40000, weights=ALL)]
    w = [Withdrawal(4, absorber=ASSO)]
    adj = [Adjustment(1, participant=1, expense=1, value=-3000, compensation=OTHERS)]
    r = compute(P, exps, adj, w)
    assert r.share(4, 1) == 0 and r.share(2, 1) == r.share(3, 1) == 11500
    check(r)


def test_everyone_withdrawn_asso_pays():
    exps = [Expense(1, "Gîte", actual=10000, weights={1: 1})]
    r = compute(P, exps, withdrawals=[Withdrawal(1, absorber=GROUP)])
    assert r.asso_total == 10000
    check(r)


# ---------------------------------------------------------------- explications

def test_explanations_present():
    exps = [Expense(1, "Gîte", actual=150000, weights={1: 5, 2: 5, 3: 40})]
    r = compute(P, exps)
    txt = r.lines[(1, 1)].components[0].text
    assert "5 sur 50" in txt and "150,00" in txt and "part(s)" not in txt


# ---------------------------------------------------------------- Entre nous

def test_split_exact_sums():
    s = split_exact(1000, {1: 1, 2: 1, 3: 1})
    assert sum(s.values()) == 1000 and sorted(s.values()) == [333, 333, 334]


def test_sub_account_settlement():
    exps = [SubExpense(1, "Resto", 9000, payer=1, weights={1: 1, 2: 1, 3: 1}),
            SubExpense(2, "Courses", 3000, payer=2, weights={1: 1, 2: 1, 3: 1})]
    bal = sub_balances([1, 2, 3], exps, [])
    assert bal == {1: 5000, 2: -1000, 3: -4000}
    s = minimal_settlements(bal)
    assert sorted(s) == [(2, 1, 1000), (3, 1, 4000)]
    bal2 = sub_balances([1, 2, 3], exps, [SubTransfer(3, 1, 4000)])
    assert bal2[3] == 0 and bal2[1] == 1000
    assert sum(bal2.values()) == 0


def test_negative_share_warns():
    exps = [Expense(1, "Péage", actual=2000, weights=ALL)]          # 5 € chacun
    r = compute(P, exps, [Adjustment(1, participant=1, expense=1, value=-800, compensation=ASSO)])
    assert any("négative" in w for w in r.warnings)
    check(r)


def test_lock_freezes_what_you_see():
    """Verrouiller un montant à sa valeur actuelle ne change rien pour les autres."""
    exps = [Expense(1, "Gîte", actual=40000, weights=ALL)]
    a1 = Adjustment(1, participant=1, expense=1, value=-3000, compensation=OTHERS)
    before = compute(P, exps, [a1])
    lock = Adjustment(2, participant=2, expense=1, value=before.share(2, 1), fixed=True)
    after = compute(P, exps, [a1, lock])
    assert {p: after.share(p, 1) for p in ALL} == {p: before.share(p, 1) for p in ALL}
    # et un ajustement ultérieur ne le touche plus
    a3 = Adjustment(3, participant=3, expense=1, value=-2000, compensation=OTHERS)
    later = compute(P, exps, [a1, lock, a3])
    assert later.share(2, 1) == before.share(2, 1)
    check(later)



def test_rounding_once_per_person_matches_a_calculator():
    """Cas réel : 8 dépenses de transport partagées entre 11 personnes, 1 522,29 € au total.
    Avant : chaque part arrondie au centime supérieur -> 138,42 €. Maintenant : 1 522,29 / 11 = 138,39 €."""
    P11 = [Participant(i, f"P{i}") for i in range(1, 12)]
    W = {i: 1 for i in range(1, 12)}
    amounts = [60300, 4130, 10490, 3540, 12712, 14922, 14135, 32000]
    r = compute(P11, [Expense(i, f"D{i}", actual=a, weights=W) for i, a in enumerate(amounts, 1)])
    assert sum(amounts) == 152229
    assert all(r.people[i].share == 13839 for i in range(1, 12))
    assert r.rounding_gain == 13839 * 11 - 152229 == 0
    check(r)


def test_rounding_line_makes_displayed_lines_add_up():
    """3 × 10 € partagés à 3 : chaque dépense tombe juste (3,33 + 3,33 + 3,34) et le centime en plus tombe
    chaque fois sur quelqu'un d'autre : chacun paie 10,00 € pile, et son total est la somme de ses lignes."""
    r = compute(P[:3], [Expense(i, f"D{i}", actual=1000, weights={1: 1, 2: 1, 3: 1}) for i in (1, 2, 3)])
    assert [r.people[i].share for i in (1, 2, 3)] == [1000] * 3
    assert all(ps.rounding == 0 for ps in r.people.values()) and r.rounding_gain == 0
    for eid in (1, 2, 3):
        assert sum(r.share(i, eid) for i in (1, 2, 3)) == 1000
    assert sorted(w for eid in (1, 2, 3) for w in r.cent_winners(eid)) == [1, 2, 3]
    for pid, ps in r.people.items():
        assert ps.share == sum(l.cents for (p, _), l in r.lines.items() if p == pid)


