"""Un grand voyage reste rapide à calculer : le temps de calcul suit le nombre de parts (pas son carré)."""
import time

from compta import models as M
from compta import services as S
from compta.db import Database


def build(tmp_path, people, expenses):
    db = Database(tmp_path / f"d{people}")
    with db.Session() as s:
        t = M.Trip(name="Grand")
        s.add(t)
        s.flush()
        ps = [M.Participant(trip_id=t.id, name=f"Personne {i:03d}", roles="plongeur") for i in range(people)]
        s.add_all(ps)
        s.flush()
        for j in range(expenses):
            e = M.Expense(trip_id=t.id, label=f"Dépense {j}", actual=100_00 + j)
            s.add(e)
            s.flush()
            # une sur trois en parts inégales (le cas qui déclenchait la boucle lente)
            s.add_all([M.ExpenseShare(expense_id=e.id, participant_id=p.id, weight=(1 + i % 3) if j % 3 == 0 else 1)
                       for i, p in enumerate(ps)])
        s.commit()
        return db, t.id


def timed(db, tid):
    with db.Session() as s:
        t = s.get(M.Trip, tid)
        t0 = time.perf_counter()
        r = S.compute_trip(t)
        return time.perf_counter() - t0, r


def test_big_trip_is_computed_quickly_and_exactly(tmp_path):
    db, tid = build(tmp_path, 150, 120)
    secs, r = timed(db, tid)
    # total à la main : 120 dépenses de 100,00 € + (0 + 1 + … + 119) centimes = 12 000,00 + 71,40
    assert r.total_cost == 120 * 100_00 + sum(range(120)) == 1_207_140
    assert sum(p.share for p in r.people.values()) == r.total_cost       # au centime près, rien ne se perd
    assert secs < 4, f"calcul trop lent : {secs:.1f} s pour 150 personnes × 120 dépenses"


def test_time_grows_with_the_number_of_shares_not_its_square(tmp_path):
    """Deux fois plus de personnes : à peu près deux fois plus long (et sûrement pas quatre fois)."""
    small, big = build(tmp_path, 60, 40), build(tmp_path, 120, 40)
    t_small = min(timed(*small)[0] for _ in range(3))
    t_big = min(timed(*big)[0] for _ in range(3))
    assert t_big < t_small * 3.2, f"{t_small:.3f} s → {t_big:.3f} s"
