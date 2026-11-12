"""Préparer un voyage, tranche 4 : programme du séjour et enveloppe repas.

Montants de référence (brief de la tranche 4, calculés à la main) — projet de référence, retenues d'origine
(Transavia, Taxi, Hôtel X, Club B, Minibus), séjour de 7 jours, enveloppe 35,00 € ; jours 1 et 7 décochés ; jour 4 à
50,00 € :
- repas par personne 4 × 35,00 + 50,00 = 190,00 ; 5 jours comptés ; à 10 : 1 900,00 ;
- N = 10 : budget 8 930,00 + 1 900,00 = 10 830,00 ; par personne 1 083,00 ;
- N = 9 : repas 1 710,00 ; budget 10 050,00 ; par personne 1 116,67 ;
- N = 10, marge 5 % : 541,50 ; prix avec la marge 1 137,15 ; prix annoncé 1 150,00 : 670,00 de côté (67,00 par
  personne), 12,85 de plus que le prix avec la marge ;
- « si tu sélectionnes » le forfait à 10 : 10 580,00, −250,00, 1 058,00 ;
- 7 jours cochés : 245,00 / 2 450,00, budget 11 380,00, 1 138,00 ; jour 4 à 0,00 : 140,00 / 1 400,00 ;
- sans enveloppe : 8 930,00 / 893,00 ;
- durée ramenée à 5 (jour 1 décoché, jour 4 à 50,00) : 155,00 / 1 550,00 ;
- lancement : 6 dépenses dont « Repas (enveloppe) » 1 900,00 ; total 10 830,00 ; 1 083,00 chacun après 10 ajouts.
"""
import datetime as dt
import re
from urllib.parse import parse_qs, urlparse

import pytest

from compta import engine as E
from compta import models as M
from compta import preparation as P
from compta import services as S
from compta import writes as W
from compta.ops import Ops, OpsError
from test_preparation_ecran import FORBIDDEN, TEMPLATES, Screen, clean, message, refused


@pytest.fixture
def screen(tmp_path):
    sc = Screen(tmp_path)
    sc.app.state.aujourdhui = dt.date(2027, 1, 10)
    return sc


def ok(r):
    assert r.status_code == 303 and not refused(r), message(r)
    return r


def meals_ref(sc, days="7"):
    """Séjour de 7 jours, enveloppe 35,00 €, jours 1 et 7 sans repas, jour 4 à 50,00 €."""
    ok(sc.post(f"{sc.url}/duree", dict(days=days)))
    ok(sc.post(f"{sc.url}/repas", dict(meal_per_day="35")))
    ok(sc.post(f"{sc.url}/jours/1/repas", {}))
    ok(sc.post(f"{sc.url}/jours/7/repas", {}))
    ok(sc.post(f"{sc.url}/jours/4/repas", dict(on="1", amount="50")))
    return sc


def result(sc, people=None):
    with sc.db.Session() as s:
        return S.compute_plan(sc.plan(s), people)


def prog(sc):
    return sc.text(f"{sc.url}/programme")


# ---------------------------------------------------------------- calcul pur

def test_meals_pure():
    m = P.Meals(per_day=3500, days=7, off={1, 7}, amounts={4: 5000})
    assert P.meals_per_person(m) == (19000, 5)
    assert P.meals_per_person(P.Meals(per_day=3500, days=7)) == (24500, 7)
    assert P.meals_per_person(P.Meals(per_day=3500, days=7, off={1, 7}, amounts={4: 0})) == (14000, 5)
    assert P.meals_per_person(P.Meals(per_day=None, days=7, amounts={4: 5000})) == (0, 0)   # pas d'enveloppe
    assert P.meals_per_person(P.Meals(per_day=3500, days=None)) == (0, 0)                    # durée inconnue
    assert P.meals_per_person(None) == (0, 0)
    o = P.Offer(id=1, price=1000, counting=P.GROUP, items=[1], chosen=True)
    r = P.compute(10, [1], [o], meals=m)
    assert (r.meals_per_person, r.meals_total, r.meals_days, r.budget) == (19000, 190000, 5, 191000)


# ---------------------------------------------------------------- montants de référence (écran)

def test_reference_amounts_with_meals(screen):
    sc = meals_ref(screen.reference())
    r = result(sc)
    assert (r.meals_per_person, r.meals_days, r.meals_total, r.budget, r.per_person) == (19000, 5, 190000, 1083000, 108300)
    t = prog(sc)
    assert "5 jours comptés : 190,00 € par personne, 1 900,00 € pour 10 personnes. Compris dans le budget." in t
    t = sc.text()
    assert "Budget total 10 830,00 € dont repas 1 900,00 €" in t
    assert "Prix par personne 1 083,00 €" in t
    assert f'dont repas <a href="{sc.url}/programme#repas">' in sc.cl.get(sc.url).text      # le montant est le lien
    r9 = result(sc, 9)
    assert (r9.meals_total, r9.budget, r9.per_person) == (171000, 1005000, 111667)
    w = r.what_if[sc.offer("Forfait Club A")]
    assert (w.budget, w.delta, w.per_person) == (1058000, -25000, 105800)
    sc.post(f"{sc.url}/marge", dict(margin_kind="pourcent", margin_value="5"))
    sc.post(f"{sc.url}/prix-annonce", dict(announced="1150"))
    r = result(sc)
    assert (r.margin, r.price_with_margin, r.reserve, r.reserve_per_person, r.above_margin) == (54150, 113715, 67000, 6700, 1285)
    t = sc.text()
    assert "Avec ta marge (5 %) : 1 137,15 € par personne 541,50 € au total" in t
    assert "Il te reste 67,00 € par personne (670,00 € en tout)." in t
    # le home : même budget
    row = re.search(r'<tr class="home-row plan-row">(.*?)</tr>', sc.cl.get("/").text, re.S).group(1)
    assert " ".join(clean(row).split()).endswith(" 1 083,00 €")


def test_meal_variants(screen):
    sc = screen.reference()
    assert (result(sc).budget, result(sc).per_person) == (893000, 89300)          # sans enveloppe : rien ne change
    ok(sc.post(f"{sc.url}/repas", dict(meal_per_day="35")))
    assert result(sc).meals_total == 0 and "Indique d'abord la durée du séjour" in prog(sc)
    ok(sc.post(f"{sc.url}/duree", dict(days="7")))
    r = result(sc)
    assert (r.meals_per_person, r.meals_total, r.budget, r.per_person) == (24500, 245000, 1138000, 113800)
    sc.post(f"{sc.url}/jours/1/repas", {})
    sc.post(f"{sc.url}/jours/7/repas", {})
    sc.post(f"{sc.url}/jours/4/repas", dict(on="1", amount="0"))
    assert (result(sc).meals_per_person, result(sc).meals_total) == (14000, 140000)
    sc.post(f"{sc.url}/jours/4/repas", dict(on="1", amount=""))             # retour à l'enveloppe
    assert result(sc).meals_per_person == 17500
    with sc.db.Session() as s:
        assert sorted(d.day for d in sc.plan(s).day_meals) == [1, 7]           # un jour par défaut : pas de ligne
    r = sc.post(f"{sc.url}/repas", dict(meal_per_day=""))
    assert message(r) == "Enveloppe repas retirée" and result(sc).meals_total == 0


def test_shortening_the_stay(screen):
    sc = meals_ref(screen.reference())
    ok(sc.post(f"{sc.url}/jours/1/repas", {}))
    r = ok(sc.post(f"{sc.url}/duree", dict(days="5")))
    assert message(r) == "Le séjour dure 5 jours"
    r = result(sc)
    assert (r.meals_per_person, r.meals_total) == (15500, 155000)
    with sc.db.Session() as s:
        assert sorted(d.day for d in sc.plan(s).day_meals) == [1, 4]           # le réglage du jour 7 a disparu
    ok(sc.post(f"{sc.url}/duree", dict(days="7")))
    ok(sc.post(f"{sc.url}/programme", dict(day="6", text="Sortie bateau")))
    r = sc.post(f"{sc.url}/duree", dict(days="5"))
    assert refused(r) and "jour 6" in message(r)
    with sc.db.Session() as s:
        assert sc.plan(s).duration == 7
        line = sc.plan(s).lines[0]
    sc.post(f"{sc.url}/programme/{line.id}", dict(day="2", until="6", text="Plongées"))
    r = sc.post(f"{sc.url}/duree", dict(days="5"))
    assert refused(r) and "jour 6" in message(r) and parse_qs(urlparse(r.headers["location"]).query)["f"] == ["days"]
    # avec les dates : refusé de même, en nommant le jour
    r = sc.post(f"{sc.url}/modifier", dict(name="Mer Rouge 2027", place="Hurghada", start="2027-05-01", end="2027-05-05"))
    assert refused(r) and "jour 6" in message(r)


# ---------------------------------------------------------------- dates et durée

def test_days_and_dates(screen):
    sc = screen.reference()
    assert "Indique la durée du séjour pour voir les jours." in prog(sc)
    ok(sc.post(f"{sc.url}/duree", dict(days="7")))
    page = sc.cl.get(f"{sc.url}/programme").text
    heads = [clean(h).strip() for h in re.findall(r'<h3 id="jour-\d+-h">(.*?)</h3>', page, re.S)]
    assert heads == [f"Jour {d}" for d in range(1, 8)]                         # sans date : « Jour 1 » seul
    sc.post(f"{sc.url}/modifier", dict(name="Mer Rouge 2027", place="Hurghada", start="2027-05-01"))
    page = sc.cl.get(f"{sc.url}/programme").text
    heads = [clean(h).strip() for h in re.findall(r'<h3 id="jour-\d+-h">(.*?)</h3>', page, re.S)]
    assert heads[0] == "Jour 1 · samedi 1er mai 2027" and heads[3] == "Jour 4 · mardi 4 mai 2027"
    assert heads[6] == "Jour 7 · vendredi 7 mai 2027"
    sc.post(f"{sc.url}/programme", dict(day="2", text="Plongée"))
    sc.post(f"{sc.url}/modifier", dict(name="Mer Rouge 2027", place="Hurghada", start="2027-05-08"))
    page = sc.cl.get(f"{sc.url}/programme").text
    heads = [clean(h).strip() for h in re.findall(r'<h3 id="jour-\d+-h">(.*?)</h3>', page, re.S)]
    assert heads[0] == "Jour 1 · samedi 8 mai 2027" and heads[6] == "Jour 7 · vendredi 14 mai 2027"
    with sc.db.Session() as s:
        assert [(ln.day, ln.text) for ln in sc.plan(s).lines] == [(2, "Plongée")]   # les lignes ne bougent pas
    # départ et retour : la durée est calculée
    sc.post(f"{sc.url}/modifier", dict(name="Mer Rouge 2027", place="Hurghada", start="2027-05-01", end="2027-05-07"))
    t = prog(sc)
    assert "Du samedi 1er mai 2027 au vendredi 7 mai 2027 : 7 jours (calculé avec les dates" in t
    assert 'name="days"' not in sc.cl.get(f"{sc.url}/programme").text
    r = sc.post(f"{sc.url}/duree", dict(days="9"))
    assert refused(r) and "La durée vient des dates" in message(r)
    sc.post(f"{sc.url}/modifier", dict(name="Mer Rouge 2027", place="Hurghada", start="2027-05-01", end="2027-05-10"))
    with sc.db.Session() as s:
        assert sc.plan(s).duration == 10


# ---------------------------------------------------------------- le programme

def test_lines_order_period_and_item(screen):
    sc = screen.reference()
    ok(sc.post(f"{sc.url}/duree", dict(days="7")))
    r = ok(sc.post(f"{sc.url}/programme", dict(day="1", text="Arrivée à l'hôtel")))
    assert message(r) == "Jour 1 : « Arrivée à l'hôtel » ajouté" and r.headers["location"].endswith("#jour-1")
    ok(sc.post(f"{sc.url}/programme", dict(day="1", text="Vol aller", time="7h30", items=str(sc.item("Avion")))))
    ok(sc.post(f"{sc.url}/programme", dict(day="1", text="Dîner", time="20:00")))
    ok(sc.post(f"{sc.url}/programme", dict(day="1", text="Courses")))
    ok(sc.post(f"{sc.url}/programme", dict(day="1", text="Taxi pour l'hôtel", time="11 h", items=str(sc.item("Taxi")))))
    ok(sc.post(f"{sc.url}/programme", dict(day="2", until="6", text="Plongées le matin", items=str(sc.item("Plongées")),
                                           note="2 plongées par jour")))
    ok(sc.post(f"{sc.url}/programme", dict(day="3", text="Visite", items=str(sc.item("Hôtel")))))
    sc.choose("Forfait Club A")
    sc.post(f"{sc.url}/offres/{sc.offer('Taxi')}/deselectionner")
    page = sc.cl.get(f"{sc.url}/programme").text
    day1 = clean(re.search(r'id="jour-1"(.*?)</section>', page, re.S).group(1))
    order = [day1.index(x) for x in ("Arrivée à l'hôtel", "Courses", "7 h 30 Vol aller", "11 h Taxi pour l'hôtel",
                                     "20 h Dîner")]
    assert order == sorted(order)                                             # sans heure d'abord, puis par heure
    assert "Avion : Transavia" in day1 and "Taxi : pas encore choisi" in day1
    day2 = clean(re.search(r'id="jour-2"(.*?)</section>', page, re.S).group(1))
    assert "Plongées le matin Jours 2 à 6 · Plongées : Forfait Club A" in day2 and "2 plongées par jour" in day2
    for d in (3, 4, 5, 6):
        block = clean(re.search(rf'id="jour-{d}"(.*?)</section>', page, re.S).group(1))
        assert "↳ suite : Plongées le matin (jours 2 à 6)" in block
    day7 = clean(re.search(r'id="jour-7"(.*?)</section>', page, re.S).group(1))
    assert "suite" not in day7
    assert "Hôtel : Forfait Club A" in clean(re.search(r'id="jour-3"(.*?)</section>', page, re.S).group(1))
    # un poste supprimé détache la ligne, sans la supprimer
    sc.post(f"{sc.url}/postes/{sc.item('Taxi')}/supprimer")
    with sc.db.Session() as s:
        line = next(ln for ln in sc.plan(s).lines if ln.text == "Taxi pour l'hôtel")
        assert line.item_ids() == []
    assert "Taxi :" not in clean(re.search(r'id="jour-1"(.*?)</section>', sc.cl.get(f"{sc.url}/programme").text, re.S).group(1))
    # modifier, supprimer
    r = ok(sc.post(f"{sc.url}/programme/{line.id}", dict(day="2", text="Transfert", time="9h", note="")))
    assert message(r) == "« Transfert » modifié"
    r = ok(sc.post(f"{sc.url}/programme/{line.id}/supprimer"))
    assert message(r) == "« Transfert » supprimé du programme"
    # aucun calcul ne dépend du programme
    assert result(sc).budget == 850000                    # forfait sélectionné, Taxi sans offre : 8 680 − 180


def test_line_refusals(screen):
    sc = screen.reference()
    r = sc.post(f"{sc.url}/programme", dict(day="1", text="Vol"))
    assert refused(r) and "Indique d'abord la durée du séjour" in message(r)
    ok(sc.post(f"{sc.url}/duree", dict(days="7")))
    before = sc.db.undo_token()
    for data, field, words in [
            (dict(day="8", text="X"), "day", "le séjour dure 7 jours"), (dict(day="0", text="X"), "day", "entre 1 et 7"),
            (dict(day="3", until="2", text="X"), "until", "avant le jour"),
            (dict(day="3", until="9", text="X"), "until", "au plus au jour 7"),
            (dict(day="3", time="midi", text="X"), "time", "Heure illisible"),
            (dict(day="3", time="25h", text="X"), "time", "Heure illisible"),
            (dict(day="3", text="  "), "text", "Écris ce qu'on fait"),
            (dict(day="abc", text="X"), "day", "nombre entier")]:
        r = sc.post(f"{sc.url}/programme", data)
        assert refused(r), data
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q["f"] == [field] and words in q["msg"][0], (data, q)
    assert sc.db.undo_token() == before
    for bad in ("0", "-2", "x"):
        assert refused(sc.post(f"{sc.url}/duree", dict(days=bad)))
    assert refused(sc.post(f"{sc.url}/repas", dict(meal_per_day="-5")))
    assert refused(sc.post(f"{sc.url}/jours/9/repas", dict(on="1")))
    assert refused(sc.post(f"{sc.url}/jours/2/repas", dict(on="1", amount="-1")))
    assert W.clean_time("9h30") == "09:30" and W.clean_time("14:05") == "14:05" and W.clean_time("9") == "09:00"
    assert W.clean_time("") == ""


def test_undo_redo_programme_and_meals(screen):
    sc = meals_ref(screen.reference())
    ok(sc.post(f"{sc.url}/programme", dict(day="2", text="Plongée")))
    assert sc.db.last_action() == "programme, jour 2 : « Plongée » ajouté"
    r = sc.post("/annuler", {}, headers={"referer": f"http://testserver{sc.url}/programme"})
    assert message(r) == "Défait : programme, jour 2 : « Plongée » ajouté."
    assert urlparse(r.headers["location"]).path == f"{sc.url}/programme"
    with sc.db.Session() as s:
        assert not sc.plan(s).lines
    sc.post("/annuler", {}, headers={"referer": f"http://testserver{sc.url}/programme"})   # jour 4 à 50 défait
    assert result(sc).meals_per_person == 17500
    q = parse_qs(urlparse(sc.post("/annuler", {}, headers={"referer": f"http://testserver{sc.url}/programme"})
                          .headers["location"]).query)
    assert result(sc).meals_per_person == 21000                              # jour 7 de nouveau compté
    sc.post("/retablir", dict(r=q["r"][0]), headers={"referer": f"http://testserver{sc.url}/programme"})
    assert result(sc).meals_per_person == 17500


# ---------------------------------------------------------------- lancement avec les repas

def test_launch_with_meals(screen):
    sc = meals_ref(screen.reference())
    t = sc.text(f"{sc.url}/lancer")
    assert "Repas (enveloppe) Repas 1 900,00 €" in t and "Total prévu 10 830,00 €" in t
    r = ok(sc.post(f"{sc.url}/lancer", dict(caisse="asso")))
    tid = int(re.match(r"/v/(\d+)", r.headers["location"]).group(1))
    with sc.db.Session() as s:
        trip = s.get(M.Trip, tid)
        cats = {c.id: c.name for c in trip.categories}
        rows = [(e.label, cats[e.category_id], e.planned) for e in trip.expenses]
        assert len(rows) == 6 and rows[-1] == ("Repas (enveloppe)", "Repas", 190000)
        assert sum(x[2] for x in rows) == 1083000
        note = trip.expenses[-1].note.replace("\xa0", " ").replace(" ", " ")
        assert note == "35,00 € par personne et par jour, 5 jours sur 7 (jour 4 : 50,00 €), pour 10 personnes"
    sc.post(f"/v/{tid}/participants", dict(names="\n".join(f"Personne{i} Test" for i in range(10))))
    with sc.db.Session() as s:
        res = S.compute_trip(s.get(M.Trip, tid), E.PLANNED)
        assert [x.share for x in res.people.values()] == [108300] * 10
    # enveloppe à 0 : seul le jour changé (50,00 €) compte encore ; sans enveloppe : pas de dépense Repas
    sc.post(f"/v/{tid}/supprimer")
    sc.post(f"{sc.url}/repas", dict(meal_per_day="0"))
    assert "Repas (enveloppe) Repas 500,00 €" in sc.text(f"{sc.url}/lancer")
    sc.post(f"{sc.url}/repas", dict(meal_per_day=""))
    assert "Repas (enveloppe)" not in sc.text(f"{sc.url}/lancer")


def test_meals_item_and_launched_project(screen):
    sc = meals_ref(screen.reference())
    assert "Le poste « Repas »" not in prog(sc)
    sc.add_item("Repas")
    sc.add_offer("Restaurant de l'hôtel", "15", "personne", ["Repas"])
    sc.choose("Restaurant de l'hôtel")
    assert "Le poste « Repas » et ses offres comptent aussi, en plus de cette enveloppe." in prog(sc)
    assert result(sc).budget == 1083000 + 15000                               # les deux comptent
    ok(sc.post(f"{sc.url}/lancer", dict(caisse="asso")))
    assert "Voyage lancé : modifier le programme ne change pas les comptes du voyage." in prog(sc)
    ok(sc.post(f"{sc.url}/programme", dict(day="3", text="Encore modifiable")))


def test_line_linked_to_several_items(screen):
    """Une ligne liée à plusieurs postes (pastilles cochées sous « Lié à ») : « · Avion, Taxi » dans la vue Par jour,
    le prestataire de chaque poste dans la vue Tableau ; décocher tout : liée à rien ; un poste supprimé : il quitte
    la ligne, les autres restent ; montants inchangés (affichage seulement)."""
    sc = screen.reference()
    ok(sc.post(f"{sc.url}/duree", dict(days="7")))
    avion, taxi, hotel = sc.item("Avion"), sc.item("Taxi"), sc.item("Hôtel")
    ok(sc.post(f"{sc.url}/programme", dict(day="1", text="Arrivée", time="7h30", items=[str(taxi), str(avion)])))
    page = sc.cl.get(f"{sc.url}/programme").text
    day1 = clean(re.search(r'id="jour-1"(.*?)</section>', page, re.S).group(1))
    assert "7 h 30 Arrivée · Avion , Taxi" in day1 or "7 h 30 Arrivée · Avion, Taxi" in day1   # dans l'ordre des postes
    assert "Transavia" not in day1.split("Modifier")[0]
    form = re.search(r'<form[^>]*class="plan-line-form".*?</form>', page, re.S).group(0)
    assert f'name="items" value="{avion}" checked' in form and f'name="items" value="{taxi}" checked' in form
    assert f'name="items" value="{hotel}" >' in form                                     # pas cochée
    assert '<legend>Lié à</legend>' in form
    table = sc.cl.get(f"{sc.url}/programme?vue=tableau").text
    who = clean(re.search(r'id="t-jour-1".*?</tbody>', table, re.S).group(0))
    assert "Avion : Transavia" in who and "Taxi" in who
    with sc.db.Session() as s:
        line = next(ln for ln in sc.plan(s).lines if ln.text == "Arrivée")
        assert line.item_ids() == [avion, taxi]
        assert S.compute_plan(sc.plan(s)).budget == 893000                                 # rien ne change au budget
    ok(sc.post(f"{sc.url}/programme/{line.id}", dict(day="1", text="Arrivée", time="7h30", items=[str(hotel)])))
    ok(sc.post(f"{sc.url}/postes/{hotel}/supprimer"))
    with sc.db.Session() as s:
        assert next(ln for ln in sc.plan(s).lines if ln.text == "Arrivée").item_ids() == []
        assert s.query(M.PlanLineItem).count() == 0
    ok(sc.post(f"{sc.url}/programme/{line.id}", dict(day="1", text="Arrivée", time="7h30", items=[str(avion), str(taxi)])))
    ok(sc.post(f"{sc.url}/programme/{line.id}", dict(day="1", text="Arrivée", time="7h30")))   # rien de coché
    with sc.db.Session() as s:
        assert next(ln for ln in sc.plan(s).lines if ln.text == "Arrivée").item_ids() == []
    r = sc.post(f"{sc.url}/programme/{line.id}", dict(day="1", text="Arrivée", items=["999"]))
    assert refused(r)


# ---------------------------------------------------------------- IA

def test_programme_and_meals_by_ai_same_as_screen(screen):
    sc = meals_ref(screen.reference())
    sc.post(f"{sc.url}/programme", dict(day="1", text="Vol aller", time="7h30", items=str(sc.item("Avion"))))
    sc.post(f"{sc.url}/programme", dict(day="2", until="6", text="Plongées le matin"))
    ops = Ops(sc.db)
    from test_preparation_lancer import REF_JSON
    pid = ops.create_plan("Mer Rouge 2027", 10, lieu="Hurghada")["projet_id"]
    ops.plan_import(pid, REF_JSON)
    res = ops.plan_import(pid, {"duree": 7, "repas": {"par_jour": 35, "jours_sans": [1, 7], "montants": {"4": 50}},
                                "programme": [{"jour": 1, "heure": "7h30", "texte": "Vol aller", "postes": ["Avion"]},
                                              {"jour": 2, "jusqu_au": 6, "texte": "Plongées le matin"}]})
    a, b = ops.plan(sc.pid), ops.plan(pid)
    for k in ("budget", "par_personne", "duree", "programme", "repas"):
        assert a[k] == b[k], k
    assert (b["budget"], b["par_personne"]) == (10830.0, 1083.0)
    assert b["repas"] == dict(par_jour=35.0, jours_sans=[1, 7], montants={"4": 50.0}, jours_comptes=5,
                              par_personne=190.0, total=1900.0)
    assert b["programme"][0] == dict(jour=1, jusqu_au=None, heure="07:30", texte="Vol aller", note=None, postes=["Avion"])
    assert "Programme, jour 2 à 6 : « Plongées le matin »" in res["actions"]
    # refus comme à l'écran
    with pytest.raises(OpsError) as e:
        ops.plan_import(pid, {"duree": 5, "programme": [{"jour": 9, "texte": "X"}, {"jour": 2, "texte": "Y", "heure": "midi"},
                                                        {"jour": 2, "texte": "Z", "postes": ["Lune"]}],
                              "repas": {"par_jour": -1, "montants": {"12": 5}}})
    errs = e.value.errors
    assert any("Heure illisible" in x for x in errs)
    assert any("pas de poste « Lune »" in x for x in errs) and any("négative" in x for x in errs)
    assert not [m for m in errs if FORBIDDEN.search(m)]
    with pytest.raises(OpsError, match="jour 6"):
        ops.plan_import(pid, {"duree": 5})
    # déjà là : pas en double
    ops.plan_import(pid, {"programme": [{"jour": 1, "texte": "vol aller"}]})
    assert len(ops.plan(pid)["programme"]) == 2


def test_ai_page_preview_with_programme(screen):
    import json
    sc = screen.reference()
    sc.post(f"{sc.url}/duree", dict(days="7"))
    answer = json.dumps({"projet_id": sc.pid, "programme": [{"jour": 1, "texte": "Vol aller", "heure": "7h30"},
                                                            {"jour": 7, "texte": "Retour"}],
                         "repas": {"par_jour": 35}})
    t = clean(sc.cl.post(f"{sc.url}/ia/apercu", data=dict(texte=answer)).text)
    assert "Programme, jour 1, 7 h 30 : « Vol aller »" in t and "Programme, jour 7 : « Retour »" in t
    assert "Budget : 8 930,00 € → 11 380,00 €" in t


def test_detail_without_programme_keeps_amounts(screen):
    """Un projet sans programme ni enveloppe garde exactement ses montants (tranches 1 à 3)."""
    sc = screen.reference()
    d = Ops(sc.db).plan(sc.pid)
    assert (d["budget"], d["par_personne"], d["duree"], d["programme"]) == (8930.0, 893.0, None, [])
    assert d["repas"]["total"] == 0.0 and "dont repas" not in sc.text()


# ---------------------------------------------------------------- vocabulaire

def test_no_forbidden_words(screen):
    sc = meals_ref(screen.reference())
    sc.post(f"{sc.url}/programme", dict(day="2", until="6", text="Plongées"))
    src = (TEMPLATES / "plan_program.html").read_text(encoding="utf-8") + (TEMPLATES / "_plan_tabs.html").read_text(encoding="utf-8") \
        + (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert not FORBIDDEN.search(src)
    for url in (f"{sc.url}/programme", sc.url, f"{sc.url}/lancer"):
        t = sc.text(url)
        assert not FORBIDDEN.search(t), (url, FORBIDDEN.search(t).group(0))


def test_same_header_on_both_pages_and_edit_from_programme(screen):
    """« Postes et offres » et « Programme » : même en-tête (Modifier, Supprimer le projet) ; IA et « Tous les voyages »
    sont dans le menu de gauche, pas répétés ; « Modifier » revient sur la page d'où l'on vient."""
    sc = screen.reference()
    for url in (sc.url, f"{sc.url}/programme"):
        page = sc.cl.get(url).text
        head = clean(page[page.index('<div class="page-head">'):page.index('id="modifier"')])
        assert "Modifier" in head and "Supprimer le projet" in head
        assert "IA" not in head and "Tous les voyages" not in head
        menu = clean(page[page.index('<aside class="side">'):page.index("</aside>")])
        assert "🤖 IA" in menu and "← Tous les voyages" in menu
        assert f'action="{sc.url}/modifier"' in page
    r = ok(sc.post(f"{sc.url}/modifier", dict(name="Mer Rouge 2027", place="Hurghada", start="2027-05-01",
                                               end="2027-05-07", retour="programme")))
    assert urlparse(r.headers["location"]).path == f"{sc.url}/programme" and message(r) == "Projet modifié"
    r = ok(sc.post(f"{sc.url}/modifier", dict(name="Mer Rouge 2027", place="Hurghada", retour="postes")))
    assert urlparse(r.headers["location"]).path == sc.url
    r = sc.post(f"{sc.url}/modifier", dict(name=" ", retour="programme"))
    assert refused(r) and urlparse(r.headers["location"]).path == f"{sc.url}/programme"


def test_meal_field_label_and_autosave_on_every_day(screen):
    sc = meals_ref(screen.reference())
    page = sc.cl.get(f"{sc.url}/programme").text
    assert '<label for="meal_per_day">Montant :</label>' in page
    assert page.count('<h2 id="repas-h" class="section-title">Enveloppe repas</h2>') == 1
    for d in range(1, 8):
        block = re.search(rf'id="jour-{d}"(.*?)</form>', page, re.S).group(1)
        assert "data-autosave" in block and "<button" not in block         # enregistré en quittant le champ
        assert '<span class="euro" aria-hidden="true">€</span></span>' in block   # le « € » dans le champ
        assert "disabled" not in block


def test_unticking_a_day_keeps_its_own_amount(screen):
    """Décocher un jour garde son montant à lui ; le recocher le retrouve (il ne revient pas à l'enveloppe)."""
    sc = meals_ref(screen.reference())
    ok(sc.post(f"{sc.url}/jours/4/repas", dict(amount="50")))              # décoché (case absente), montant gardé
    assert result(sc).meals_per_person == 14000                              # jours 2, 3, 5, 6 à 35,00
    ok(sc.post(f"{sc.url}/jours/4/repas", dict(on="1", amount="50")))
    assert result(sc).meals_per_person == 19000
    with sc.db.Session() as s:
        d4 = next(d for d in sc.plan(s).day_meals if d.day == 4)
        assert (d4.meal_on, d4.meal_amount) == (True, 5000)
