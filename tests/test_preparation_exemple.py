"""Le projet d'exemple (10 jours de plongée à Marsa Alam) : il s'ouvre depuis l'accueil, se calcule comme prévu, se
remet à neuf, se retire, et ne se mélange pas aux vrais projets (ni dans le nombre de l'onglet, ni dans « À faire
bientôt »).

Montants vérifiés à la main, pour 14 personnes :
  Vols (Air Horizon) 610 × 14 = 8 540 ; Transferts : 2 minibus de 8 places × 2 trajets × 140 = 560 ;
  Forfait Shams Alam (lodge + plongées) 690 × 14 = 9 660 ; Assurance 42 × 14 = 588 ; Visa 25 × 14 = 350 ;
  repas : jours 1 à 8 à 25 € + jour 9 à 45 € (jour 10 sans) = 245 € × 14 = 3 430.
  Budget 23 128 € ; par personne 1 652 € ; avec 5 % de marge 1 734,60 €.
"""
import datetime as dt
import json
import re

from compta import demo
from compta import models as M
from compta import services as S
from test_preparation_ecran import FORBIDDEN, Screen, clean, message

TODAY = dt.date(2026, 11, 12)


def opened(tmp_path):
    sc = Screen(tmp_path)
    sc.app.state.aujourdhui = TODAY
    r = sc.post("/preparer/exemple/ouvrir")
    assert message(r) == "Voici un projet d'exemple, entièrement fictif : explore-le et modifie-le librement."
    sc.url = r.headers["location"].split("?")[0]
    sc.pid = int(sc.url.rsplit("/", 1)[1])
    return sc


def test_example_data_reads_plainly():
    data = demo.plan_demo_data(TODAY)
    words = json.dumps(data, ensure_ascii=False)
    assert not FORBIDDEN.search(words) and "solde" not in words.lower()
    start, end = dt.date.fromisoformat(data["debut"]), dt.date.fromisoformat(data["fin"])
    assert start.weekday() == 5 and (end - start).days == 9 and start > TODAY + dt.timedelta(days=140)
    assert len([o for o in data["offres"] if "Hébergement" in o["postes"]]) == 3 + 1     # 3 logements + le forfait
    assert len([o for o in data["offres"] if "Plongées" in o["postes"]]) == 2 + 1        # 2 centres + le forfait


def test_open_from_home_and_amounts(tmp_path):
    sc = Screen(tmp_path)
    sc.app.state.aujourdhui = TODAY
    sc.post("/voyages", dict(name="Hendaye", caisse="asso"))
    home = sc.cl.get("/").text
    assert 'action="/preparer/exemple/ouvrir"' in home and "Ouvrir un projet d'exemple" in clean(home)
    sc = opened(tmp_path / "b")
    with sc.db.Session() as s:
        plan = s.get(M.Plan, sc.pid)
        r = S.compute_plan(plan)
        assert plan.is_demo and plan.people == 14 and plan.duration == 10 and len(plan.offers) == 15
        assert (r.budget, r.per_person, r.meals_total) == (2312800, 165200, 343000)
        assert [i.name for i in plan.items if i.id in r.empty] == ["Sortie dauphins"]
        assert len(plan.lines) == 11
    t = sc.text()
    assert "Projet d'exemple , entièrement fictif" in t and "Recommencer l'exemple" in t
    assert "1 652,00 €" in t
    assert "Projet d'exemple" in sc.text(f"{sc.url}/programme")             # la bannière sur chaque page du projet
    r = sc.post("/preparer/exemple/ouvrir")                                  # une seule fois
    assert message(r) == "Le projet d'exemple est déjà là" and r.headers["location"].startswith(sc.url)


def test_kept_apart_on_home(tmp_path):
    sc = opened(tmp_path)
    home = sc.cl.get("/").text
    assert 'Préparer <span class="count">0</span>' in home                  # l'exemple ne compte pas
    assert 'id="onglet-preparer" aria-labelledby="tab-preparer" hidden' in home   # ni pour choisir l'onglet
    row = re.search(r'<tr class="home-row plan-row">(.*?)</tr>', home, re.S).group(1)
    assert "Exemple</span>" in row
    assert 'id="a-faire"' not in home                                        # ses échéances restent dans l'exemple
    assert "Ouvrir un projet d'exemple" not in clean(home)                   # déjà ouvert
    sc.create(name="Mon vrai projet", people="8", place="")
    home = sc.cl.get("/").text
    assert 'Préparer <span class="count">1</span>' in home
    assert home.index("Mon vrai projet") < home.index(demo.PLAN_DEMO_NAME)  # l'exemple en dernier


def test_reset_and_remove(tmp_path):
    sc = opened(tmp_path)
    sc.post(f"{sc.url}/participants", dict(people="20"))
    r = sc.post(f"{sc.url}/exemple/recommencer")
    assert message(r) == "Projet d'exemple remis à neuf."
    url = r.headers["location"].split("?")[0]
    with sc.db.Session() as s:
        plans = s.query(M.Plan).all()
        assert len(plans) == 1 and plans[0].is_demo and plans[0].people == 14
    r = sc.post(f"{url}/supprimer")
    assert "supprimé" in message(r)
    with sc.db.Session() as s:
        assert s.query(M.Plan).count() == 0
    sc.create(name="Vrai", people="4", place="")
    with sc.db.Session() as s:
        real = s.query(M.Plan).one()
    assert sc.post(f"/preparer/{real.id}/exemple/recommencer").status_code == 404   # pas sur un vrai projet
