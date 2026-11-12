"""Préparer un voyage, tranche 3 : échéances, « À faire bientôt », « Lancer le voyage », IA.

« Aujourd'hui » est toujours fixé (app.state.aujourdhui ou un paramètre), jamais la date du jour réelle.

Montants de référence (brief de la tranche 3, projet de référence, N = 10) :
- retenues d'origine : Transavia 2 100,00 (Avion) ; Taxi 180,00 ; Hôtel X 2 250,00 ; Club B 3 800,00 (Plongées) ;
  Minibus 600,00 ; total prévu 8 930,00 ;
- forfait sélectionné sans répartition : Transavia 2 100,00 ; Taxi 180,00 ; Forfait Club A 5 800,00 (catégorie Hôtel, note
  « Hôtel + Plongées ») ; Minibus 600,00 ; total 8 680,00 ;
- forfait réparti 300,00 + 280,00 : Forfait Club A (Hôtel) 3 000,00 ; Forfait Club A (Plongées) 2 800,00 ; total 8 680,00 ;
- N = 9 : 1 890,00 ; 180,00 ; 2 250,00 ; 3 420,00 ; 600,00 ; total 8 340,00 ;
- sans le Taxi : 4 dépenses, total 8 750,00 ;
- 10 participants ajoutés après le lancement : chacun doit 893,00 € au prévu ;
- marge 5 %, forfait sélectionné : prix avec la marge 911,40 € ; prix annoncé 950,00 € : 38,60 € de plus ; 900,00 € :
  11,40 € de moins.
"""
import datetime as dt
import json
import re
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from compta import engine as E
from compta import models as M
from compta import preparation as P
from compta import services as S
from compta import writes as W
from compta.ops import Ops, OpsError, plan_detail
from test_preparation_ecran import FORBIDDEN, Screen, clean, message, refused

TODAY = dt.date(2027, 1, 10)


@pytest.fixture
def screen(tmp_path):
    sc = Screen(tmp_path)
    sc.app.state.aujourdhui = TODAY
    return sc


def lines_of(sc, tid):
    with sc.db.Session() as s:
        t = s.get(M.Trip, tid)
        cats = {c.id: c.name for c in t.categories}
        return [(e.label, cats.get(e.category_id), e.planned) for e in sorted(t.expenses, key=lambda e: e.id)]


def sp(t):
    return str(t).replace("\xa0", " ").replace("\u202f", " ")


def launch(sc, **data):
    r = sc.post(f"{sc.url}/lancer", {"caisse": "asso", **data})
    assert r.status_code == 303 and not refused(r), message(r)
    return int(re.match(r"/v/(\d+)", r.headers["location"]).group(1)), r


# ---------------------------------------------------------------- calcul pur

def test_live_offers_rule():
    a, b, c = 1, 2, 3
    offers = [P.Offer(id=10, price=1, items=[a], chosen=True), P.Offer(id=11, price=1, items=[a]),
              P.Offer(id=12, price=1, items=[b]), P.Offer(id=13, price=1, items=[a, b]),
              P.Offer(id=14, price=1, items=[a, c])]
    # 10 retenue ; 11 écartée (a pris) ; 12, 13, 14 en course (b et c n'ont rien de sélectionné)
    assert P.live_offers(offers) == {10, 12, 13, 14}
    offers[2].chosen = True
    assert P.live_offers(offers) == {10, 12, 14}


def test_when_words():
    assert S.when_words(dt.date(2027, 1, 7), TODAY) == "en retard de 3 jours"
    assert S.when_words(dt.date(2027, 1, 9), TODAY) == "en retard d'un jour"
    assert S.when_words(TODAY, TODAY) == "aujourd'hui"
    assert S.when_words(dt.date(2027, 1, 11), TODAY) == "demain"
    assert S.when_words(dt.date(2027, 1, 22), TODAY) == "dans 12 jours (22/01/2027)"


def test_announced_price_against_price_with_margin(screen):
    sc = screen.reference()
    sc.choose("Forfait Club A")
    sc.post(f"{sc.url}/marge", dict(margin_kind="pourcent", margin_value="5"))
    with sc.db.Session() as s:
        r = S.compute_plan(sc.plan(s))
        assert (r.budget, r.margin, r.price_with_margin, r.above_margin) == (868000, 43400, 91140, None)
    sc.post(f"{sc.url}/prix-annonce", dict(announced="950"))
    t = sc.text()                         # 950 × 10 − 8 680 = 820 ; le prix avec la marge n'est dit qu'une fois
    assert "Il te reste 82,00 € par personne (820,00 € en tout)." in t
    assert "Avec ta marge (5 %) : 911,40 € par personne" in t and t.count("911,40") == 1
    assert "de plus que le prix avec ta marge" not in t
    with sc.db.Session() as s:
        assert S.compute_plan(sc.plan(s)).above_margin == 3860
    sc.post(f"{sc.url}/prix-annonce", dict(announced="800"))   # 8 000 − 8 680 = −680
    assert "Ce prix ne couvre pas le coût : il manque 68,00 € par personne (680,00 € en tout)." in sc.text()
    sc.post(f"{sc.url}/marge", dict(margin_kind="pourcent", margin_value="0"))
    assert "Avec ta marge" not in sc.text()                  # sans marge : pas de phrase


def test_package_without_split_shows_its_total(screen):
    sc = screen.reference()
    sc.choose("Forfait Club A")
    page = sc.cl.get(sc.url).text
    for name in ("Hôtel", "Plongées"):
        block = clean(re.search(rf'id="poste-{sc.item(name)}"(.*?)</section>', page, re.S).group(1))
        assert "compris dans Forfait Club A · 5 800,00 € pour Hôtel et Plongées" in block


# ---------------------------------------------------------------- échéances

def deadline(sc, offer, label, day, amount=""):
    r = sc.post(f"{sc.url}/offres/{sc.offer(offer)}/echeances", dict(label=label, date=day, amount=amount))
    assert not refused(r), message(r)
    return r


def test_deadlines_written_counted_and_shown(screen):
    sc = screen.reference()
    r = deadline(sc, "Hôtel X", "Acompte à verser", "2027-01-07", "500")
    assert message(r) == "Échéance « Acompte à verser » ajoutée (07/01/2027)"
    deadline(sc, "Transavia", "Option à confirmer", "2027-01-10")
    deadline(sc, "Club B", "Reste à payer", "2027-01-11", "1 900")
    deadline(sc, "Minibus", "Prix garanti jusqu'à cette date", "2027-01-22")
    deadline(sc, "Air France", "Prix garanti jusqu'à cette date", "2027-01-12")      # offre écartée : ne compte pas
    deadline(sc, "Forfait Club A", "Option à confirmer", "2027-01-15")             # en course ? non : Hôtel et Plongées pris
    page = sc.cl.get(sc.url).text
    soon = clean(re.search(r'<section class="card plan-soon" id="a-faire".*?</section>', page, re.S).group(0))
    assert soon.index("En retard de 3 jours") < soon.index("Aujourd'hui") < soon.index("Demain") < soon.index("Dans 12 jours (22/01/2027)")
    assert "Hôtel X · Acompte à verser · 500,00 €" in soon and "Club B · Reste à payer · 1 900,00 €" in soon
    assert "Air France" not in soon and "Forfait Club A" not in soon
    af = clean(page.split(f'id="offre-{sc.offer("Air France")}"')[1].split('<li class="plan-offer')[0])
    assert "Prix garanti jusqu'à cette date" in af and "ne compte plus : l'offre est écartée" in af
    # retenir le forfait : les échéances de Hôtel X et Club B ne comptent plus, celle du forfait compte
    sc.choose("Forfait Club A")
    soon = clean(re.search(r'<section class="card plan-soon".*?</section>', sc.cl.get(sc.url).text, re.S).group(0))
    assert "Hôtel X" not in soon and "Club B" not in soon and "Forfait Club A · Option à confirmer" in soon
    # « Fait » : barrée dans l'offre, sortie de la liste
    with sc.db.Session() as s:
        did = next(d.id for o in sc.plan(s).offers for d in o.deadlines if d.label == "Option à confirmer"
                   and o.label == "Transavia")
    r = sc.post(f"{sc.url}/echeances/{did}/fait?retour=liste", dict(fait="1"))
    assert message(r) == "« Option à confirmer » (Transavia) : fait ✓"
    assert urlparse(r.headers["location"]).fragment == "a-faire"
    page = sc.cl.get(sc.url).text
    assert "Transavia" not in clean(re.search(r'<section class="card plan-soon".*?</section>', page, re.S).group(0))
    assert re.search(r'<li class="plan-dl-row done">.*?Option à confirmer', page, re.S)
    sc.post(f"{sc.url}/echeances/{did}/fait", dict(fait="0"))
    assert "Transavia · Option à confirmer" in clean(sc.cl.get(sc.url).text)


def test_deadline_edit_delete_and_refusals(screen):
    sc = screen.reference()
    deadline(sc, "Taxi", "Acompte", "2027-02-01", "50")
    with sc.db.Session() as s:
        did = sc.plan(s).offers[2].deadlines[0].id
    r = sc.post(f"{sc.url}/echeances/{did}", dict(label="Acompte à verser", date="2027-02-03", amount="60"))
    assert message(r) == "Échéance « Acompte à verser » modifiée"
    with sc.db.Session() as s:
        d = s.get(M.PlanDeadline, did)
        assert (d.label, d.date, d.amount, d.done) == ("Acompte à verser", dt.date(2027, 2, 3), 6000, False)
    before = sc.db.undo_token()
    oid = sc.offer("Taxi")
    for data, field in [(dict(label="", date="2027-02-01"), "label"), (dict(label="X", date=""), "date"),
                        (dict(label="X", date="2027-02-01", amount="-5"), "amount"),
                        (dict(label="X", date="2027-02-01", amount="abc"), "amount")]:
        r = sc.post(f"{sc.url}/offres/{oid}/echeances", data)
        assert refused(r) and parse_qs(urlparse(r.headers["location"]).query)["f"] == [field], data
    assert sc.db.undo_token() == before
    r = sc.post(f"{sc.url}/echeances/{did}/supprimer")
    assert message(r) == "Échéance « Acompte à verser » supprimée"
    # une échéance d'un autre projet : introuvable par l'adresse de celui-ci
    with sc.db.Session() as s:
        other = W.save_plan(s, name="Autre", people=2)
        it = W.add_plan_item(s, other, "Bateau")
        o = W.save_offer(s, other, label="Bateau", price=100, items=[it.id])
        d = W.save_deadline(s, other, o, label="Acompte", date=TODAY)
        s.commit()
        with pytest.raises(W.WriteError):
            W.set_deadline_done(s, sc.plan(s), d)
        with pytest.raises(W.WriteError):
            W.save_deadline(s, sc.plan(s), o, label="X", date=TODAY)
        foreign = d.id
    assert sc.post(f"{sc.url}/echeances/{foreign}/supprimer").status_code == 404


def test_deleting_an_offer_takes_its_deadlines(screen):
    sc = screen.reference()
    deadline(sc, "Taxi", "Acompte", "2027-02-01")
    page = sc.cl.get(sc.url).text
    assert "Supprimer l&#39;offre « Taxi » et ses échéances ?" in page or "Supprimer l'offre « Taxi » et ses échéances ?" in page
    sc.post(f"{sc.url}/offres/{sc.offer('Taxi')}/supprimer")
    with sc.db.Session() as s:
        assert s.query(M.PlanDeadline).count() == 0


def test_upcoming_one_list_for_home(screen):
    sc = screen.reference()
    deadline(sc, "Hôtel X", "Acompte à verser", "2027-01-07", "500")
    deadline(sc, "Minibus", "Reste à payer", "2027-02-09")          # dans 30 jours : oui
    deadline(sc, "Transavia", "Reste à payer", "2027-02-10")        # dans 31 jours : pas encore
    # un voyage en comptabilité avec des dates de paiement dépassées
    with sc.db.Session() as s:
        t = W.create_trip(s, "Hendaye")
        a = W.add_participant(s, t, "Anne Martin")
        b = W.add_participant(s, t, "Bruno Petit")
        W.save_expense(s, t, label="Gîte", planned=40000, weights={a.id: 1, b.id: 1})
        W.set_pay_dates(t, [("Acompte", dt.date(2027, 1, 5), 5000)])
        t.pay_dates = S.dump_pay_dates(S.pay_dates(t)) if not t.pay_dates else t.pay_dates
        s.commit()
        got = S.upcoming(s, TODAY)
        assert [(x["kind"], x.get("words")) for x in got] == [
            ("echeance", "en retard de 3 jours"), ("relance", "2 personnes à relancer"),
            ("echeance", "dans 30 jours (09/02/2027)")]
        assert got[1]["url"] == f"/v/{t.id}/argent#relances"
        assert len(S.upcoming(s, TODAY, days=31)) == 4
        tid = t.id
    page = sc.cl.get("/").text
    card = clean(re.search(r'<section class="card soon-card" id="a-faire".*?</section>', page, re.S).group(0))
    assert "À faire bientôt" in card
    assert "En retard de 3 jours" in card and "Mer Rouge 2027 · Hôtel X · Acompte à verser · 500,00 €" in card
    assert "Voyage Hendaye : 2 personnes à relancer" in card and f'href="/v/{tid}/argent#relances"' in page
    assert "Dans 30 jours (09/02/2027)" in card and "10/02/2027" not in card
    # « Fait » depuis l'accueil : retour à l'accueil
    with sc.db.Session() as s:
        did = next(x["d"].id for x in S.upcoming(s, TODAY) if x["kind"] == "echeance")
    r = sc.post(f"{sc.url}/echeances/{did}/fait?retour=accueil", dict(fait="1"))
    assert r.headers["location"].startswith("/?") and r.headers["location"].endswith("#a-faire")
    # rien : pas de carte
    with sc.db.Session() as s:
        s.get(M.Trip, tid).pay_dates = ""
        for o in sc.plan(s).offers:
            for d in o.deadlines:
                d.done = True
        s.commit()
    assert 'id="a-faire"' not in sc.cl.get("/").text


# ---------------------------------------------------------------- lancer le voyage

REF_LINES = [("Transavia", "Avion", 210000), ("Taxi", "Taxi", 18000), ("Hôtel X", "Hôtel", 225000),
             ("Club B", "Plongées", 380000), ("Minibus", "Minibus", 60000)]


def test_launch_reference(screen):
    sc = screen.reference()
    page = sc.cl.get(sc.url).text
    assert f'href="{sc.url}/lancer"' in page and "Lancer le voyage" in clean(page)
    conf = sc.cl.get(f"{sc.url}/lancer").text
    t = clean(conf)
    for label, cat, cents in REF_LINES:
        assert f"{label} {cat} {E.fmt(cents)}".replace("\xa0", " ").replace(" ", " ") in t
    assert "Total prévu 8 930,00 €" in t and "Qui tient la caisse ?" in t and "C'est un voyage de plongée" in t
    assert "n'a pas d'offre sélectionnée" not in t
    tid, r = launch(sc)
    assert message(r) == ("Voyage « Mer Rouge 2027 » lancé : 5 dépenses prévues, pour un total de 8 930,00 €. "
                          "Ajoute maintenant les participants : ils seront comptés dans chaque dépense.")
    assert lines_of(sc, tid) == REF_LINES
    with sc.db.Session() as s:
        trip = s.get(M.Trip, tid)
        assert (trip.name, trip.place, trip.note) == ("Mer Rouge 2027", "Hurghada", "Préparé pour 10 personnes.")
        assert all(e.actual is None and e.for_all and not e.shares and e.payer_id is None for e in trip.expenses)
        assert sp(trip.expenses[0].note) == "210,00 € par personne, pour 10 personnes"
        assert sp(trip.expenses[1].note) == "60,00 € pour 4 personnes (il en faut 3 pour 10 personnes)"
        plan = sc.plan(s)
        assert (plan.trip_id, plan.launched_on) == (tid, TODAY)


def test_launch_variants(screen):
    sc = screen.reference()
    sc.choose("Forfait Club A")
    sc.post(f"{sc.url}/prix-annonce", dict(announced="950"))
    tid, _ = launch(sc)
    assert lines_of(sc, tid) == [("Transavia", "Avion", 210000), ("Taxi", "Taxi", 18000),
                                 ("Forfait Club A", "Hôtel", 580000), ("Minibus", "Minibus", 60000)]
    with sc.db.Session() as s:
        trip = s.get(M.Trip, tid)
        assert sp(trip.note) == "Préparé pour 10 personnes. Prix annoncé : 950,00 € par personne."
        assert "Couvre Hôtel + Plongées." in trip.expenses[2].note
        assert sum(e.planned for e in trip.expenses) == 868000
    # répartition 300 + 280
    sc.post(f"/v/{tid}/supprimer")                       # le voyage supprimé : on peut relancer
    h, d = sc.item("Hôtel"), sc.item("Plongées")
    sc.post(f"{sc.url}/offres/{sc.offer('Forfait Club A')}", dict(
        label="Forfait Club A", price="580", counting="personne", quantity="1", items=[str(h), str(d)],
        split="montant", note="10 plongées encadrées", **{f"share_{h}": "300", f"share_{d}": "280"}))
    tid, _ = launch(sc)
    assert lines_of(sc, tid) == [("Transavia", "Avion", 210000), ("Taxi", "Taxi", 18000),
                                 ("Forfait Club A (Hôtel)", "Hôtel", 300000),
                                 ("Forfait Club A (Plongées)", "Plongées", 280000), ("Minibus", "Minibus", 60000)]
    with sc.db.Session() as s:
        assert "10 plongées encadrées" in s.get(M.Trip, tid).expenses[2].note


def test_launch_at_nine_and_without_taxi(screen):
    sc = screen.reference()
    sc.post(f"{sc.url}/participants", dict(people="9"))
    tid, _ = launch(sc)
    assert [x[2] for x in lines_of(sc, tid)] == [189000, 18000, 225000, 342000, 60000]
    sc.post(f"/v/{tid}/supprimer")
    sc.post(f"{sc.url}/participants", dict(people="10"))
    sc.post(f"{sc.url}/offres/{sc.offer('Taxi')}/deselectionner")
    t = sc.text(f"{sc.url}/lancer")
    assert "Taxi n'a pas d'offre sélectionnée : rien ne sera créé pour ce poste." in t
    assert "Total prévu 8 750,00 €" in t
    tid, _ = launch(sc)
    assert len(lines_of(sc, tid)) == 4 and sum(x[2] for x in lines_of(sc, tid)) == 875000


def test_launched_trip_is_a_normal_trip(screen):
    sc = screen.reference()
    tid, _ = launch(sc)
    r = sc.post(f"/v/{tid}/participants", dict(names="\n".join(f"Personne {i} Test" for i in range(10))))
    assert "10 participants ajoutés" in message(r)
    with sc.db.Session() as s:
        trip = s.get(M.Trip, tid)
        res = S.compute_trip(trip, E.PLANNED)
        assert [a.share for a in res.accounts] == [89300] * 10
        assert all(len(e.shares) == 10 and e.for_all for e in trip.expenses)
    deps = sc.text(f"/v/{tid}/depenses")
    assert "Transavia 2 100,00 € ≈ 2 100,00 €" in deps                       # le prévu, comme partout
    viz = sc.text(f"/v/{tid}/en-images")
    assert "5 dépenses sont comptées sur leur montant prévu" in viz
    assert "Coût du voyage 8 930,00 € 10 participants Part la plus courante 893,00 €" in viz
    # un nouveau venu plus tard entre aussi dans chaque dépense
    sc.post(f"/v/{tid}/participants", dict(name="Zoé Retard"))
    with sc.db.Session() as s:
        res = S.compute_trip(s.get(M.Trip, tid), E.PLANNED)
        assert sum(a.share for a in res.accounts) == 893000 and len(res.accounts) == 11
    # changer « Pour qui ? » d'une dépense : elle n'est plus « pour tout le monde »
    with sc.db.Session() as s:
        trip = s.get(M.Trip, tid)
        e = trip.expenses[0]
        W.save_expense(s, trip, e, label=e.label, planned=e.planned, weights={trip.participants[0].id: 1})
        s.commit()
        assert not e.for_all and [x.for_all for x in trip.expenses[1:]] == [True] * 4


def test_launch_with_a_person_holding_the_cash_and_a_free_offer(screen):
    sc = screen.reference()
    sc.add_offer("Transfert offert", "0", "groupe", ["Taxi"])
    sc.choose("Transfert offert")
    t_conf = sc.text(f"{sc.url}/lancer")
    assert "Transfert offert : inclus, rien à payer (aucune dépense créée)." in t_conf
    assert "Total prévu 8 750,00 €" in t_conf and "Transfert offert Taxi" not in t_conf
    r = sc.post(f"{sc.url}/lancer", dict(caisse="personne", cashier_name=""))
    assert refused(r) and parse_qs(urlparse(r.headers["location"]).query)["f"] == ["cashier_name"]
    tid, _ = launch(sc, **{"caisse": "personne", "cashier_name": "Camille Durand", "plongee": "1"})
    with sc.db.Session() as s:
        trip = s.get(M.Trip, tid)
        assert [p.name for p in trip.participants] == ["Camille Durand"] and trip.cashier_id == trip.participants[0].id
        assert all(len(e.shares) == 1 for e in trip.expenses)                 # elle compte dans chaque dépense
        assert "Transfert offert" not in [e.label for e in trip.expenses]    # inclus : rien à payer, pas de dépense
        assert len(trip.expenses) == 4 and S.is_diving(trip)


def test_after_launch(screen):
    sc = screen.reference()
    deadline(sc, "Minibus", "Reste à payer", "2027-01-20")
    tid, _ = launch(sc)
    t = sc.text()
    assert "✓ Voyage lancé le 10/01/2027. Modifier ce projet ne change plus les comptes du voyage." in t
    assert "Ouvrir le voyage" in t and "🚀 Lancer le voyage" not in t
    assert f'href="/v/{tid}"' in sc.cl.get(sc.url).text
    r = sc.cl.get(f"{sc.url}/lancer", follow_redirects=False)
    assert r.headers["location"].startswith(f"/v/{tid}?")                 # déjà lancé : on va au voyage
    assert "Ce projet a déjà été lancé" in sc.post(f"{sc.url}/lancer", dict(caisse="asso")).headers["location"] \
        or refused(sc.post(f"{sc.url}/lancer", dict(caisse="asso")))
    # le projet reste modifiable ; rien ne change dans le voyage
    sc.post(f"{sc.url}/participants", dict(people="12"))
    sc.choose("Air France")
    assert lines_of(sc, tid) == REF_LINES
    home = sc.cl.get("/").text          # accueil : le projet lancé quitte le tableau « Préparer », un lien reste
    assert "Déjà lancés, leurs comptes sont dans « Faire la compta » : Mer Rouge 2027 ." in clean(home)   # (le
    # nom est un lien vers la préparation)
    assert f'<a href="/preparer/{sc.pid}">Mer Rouge 2027</a>.' in home
    assert 'id="projets"' not in home and 'class="home-row trip-row"' in home
    dash = sc.cl.get(f"/v/{tid}").text
    assert f'href="/preparer/{sc.pid}" id="voir-preparation"' in dash and "Voir la préparation de ce voyage" in dash
    # le voyage supprimé : le lien disparaît, on peut relancer
    sc.post(f"/v/{tid}/supprimer")
    t = sc.text()
    assert "Voyage lancé" not in t and "🚀 Lancer le voyage" in t
    with sc.db.Session() as s:
        assert sc.plan(s).trip_id is None
    # un voyage créé ensuite (même numéro possible) n'est pas pris pour le sien
    sc.post("/voyages", dict(name="Autre voyage", caisse="asso"))
    assert "Voyage lancé" not in sc.text() and "Voir la préparation" not in sc.text(f"/v/{tid}")


def test_launch_is_undone_in_one_step(screen):
    sc = screen.reference()
    before = sc.db.undo_token()
    tid, r = launch(sc)
    assert sc.db.last_action() == "lancement du voyage « Mer Rouge 2027 » (depuis sa préparation)"
    assert len([h for h in sc.db.history() if h["token"] > (before or "")]) == 1
    d = parse_qs(urlparse(r.headers["location"]).query)["d"][0]
    r = sc.post("/annuler", dict(d=d), headers={"referer": f"http://testserver/v/{tid}"})
    assert message(r) == "Défait : lancement du voyage « Mer Rouge 2027 » (depuis sa préparation)."
    with sc.db.Session() as s:
        assert s.get(M.Trip, tid) is None and sc.plan(s).trip_id is None
    assert "🚀 Lancer le voyage" in sc.text()


def test_launch_needs_a_chosen_offer(screen):
    sc = screen
    sc.create(name="Vide", people="4")
    sc.add_item("Gîte")
    assert 'id="lancer"' not in sc.cl.get(sc.url).text          # pas de bouton en haut ; le menu y mène, et la page refuse
    r = sc.cl.get(f"{sc.url}/lancer", follow_redirects=False)
    assert refused(r) and "Sélectionne au moins une offre" in message(r)
    with sc.db.Session() as s:
        with pytest.raises(W.WriteError, match="Sélectionne au moins une offre"):
            W.launch_plan(s, sc.plan(s))


# ---------------------------------------------------------------- IA

REF_JSON = {
    "postes": ["Avion", "Taxi", "Hôtel", "Plongées", "Minibus"],
    "offres": [
        {"prestataire": "Transavia", "prix": 210, "compte": "par personne", "postes": ["Avion"], "selectionnee": True},
        {"prestataire": "Air France", "prix": "320,00", "compte": "par personne", "postes": ["Avion"]},
        {"prestataire": "Taxi", "prix": 60, "compte": "pour un nombre de personnes", "pour_combien": 4,
         "postes": ["Taxi"], "selectionnee": True},
        {"prestataire": "Hôtel X", "prix": 90, "compte": "pour un nombre de personnes", "pour_combien": 2,
         "quantite": 5, "de_quoi": "nuits", "postes": ["Hôtel"], "selectionnee": True},
        {"prestataire": "Club B", "prix": 38, "compte": "par personne", "quantite": 10, "de_quoi": "plongées",
         "postes": ["Plongées"], "selectionnee": True},
        {"prestataire": "Minibus", "prix": 600, "compte": "pour le groupe", "postes": ["Minibus"], "selectionnee": True},
        {"prestataire": "Forfait Club A", "prix": 580, "compte": "par personne", "postes": ["Hôtel", "Plongées"]},
    ]}


def rows(db, pid):
    """Les lignes d'un projet, sans les numéros."""
    with db.Session() as s:
        p = s.get(M.Plan, pid)
        names = {i.id: i.name for i in p.items}
        return dict(
            plan=(p.name, p.people, p.place, p.margin_kind, p.margin_value, p.announced),
            items=[(i.name, i.position) for i in p.items],
            offers=[(o.label, o.price, o.counting, o.capacity, o.quantity, o.quantity_label, o.chosen, o.split,
                     [names[i] for i in o.item_ids()], sorted((names[ln.item_id], ln.share) for ln in o.links))
                    for o in p.offers])


def test_same_project_by_ai_and_on_screen(screen, tmp_path):
    sc = screen.reference()
    ops = Ops(sc.db)
    made = ops.create_plan("Mer Rouge 2027", 10, lieu="Hurghada")
    pid = made["projet_id"]
    res = ops.plan_import(pid, dict(REF_JSON, projet_id=pid))
    assert res["message"] == "12 changements pour « Mer Rouge 2027 »."
    by_screen, by_ai = rows(sc.db, sc.pid), rows(sc.db, pid)
    assert by_screen == by_ai
    d = ops.plan(pid)
    assert (d["budget"], d["par_personne"]) == (8930.0, 893.0)
    fa = next(o for o in d["offres"] if o["prestataire"] == "Forfait Club A")
    assert fa["si_selectionnee"] == dict(budget=8680.0, ecart=-250.0, ecart_par_personne=-25.0,
                                    remplace=["Hôtel X", "Club B"], postes_sans_offre=[])
    assert sp(fa["prix_en_clair"]) == "580,00 € par personne"
    # le même forfait sélectionné, réparti et avec une échéance : mêmes montants à l'écran
    ops.plan_import(pid, {"offres": [{"prestataire": "Forfait Club A", "repartition": {
        "type": "euros", "parts": {"Hôtel": 300, "Plongées": 280}}, "selectionnee": True,
        "echeances": [{"libelle": "Option à confirmer", "date": "15/01/2027"}]}],
        "marge": {"type": "pourcent", "valeur": 5}, "prix_annonce": 950})
    d = ops.plan(pid)
    assert (d["budget"], d["prix_avec_marge"], d["reserve"]) == (8680.0, 911.4, 820.0)
    with sc.db.Session() as s:
        r = S.compute_plan(s.get(M.Plan, pid))
        assert [r.by_item[i.id]["amount"] for i in s.get(M.Plan, pid).items][2:4] == [300000, 280000]
        assert [d.label for o in s.get(M.Plan, pid).offers for d in o.deadlines] == ["Option à confirmer"]


def test_ai_refuses_like_the_screen(screen):
    sc = screen.reference()
    ops = Ops(sc.db)
    before = sc.db.undo_token()
    bad = {"participants": 0, "offres": [
        {"prestataire": "Navette", "prix": -1, "postes": ["Taxi"]},
        {"prestataire": "Bateau", "prix": 10},
        {"prestataire": "Taxi 8", "prix": 10, "compte": "pour un nombre de personnes", "pour_combien": 0, "postes": ["Taxi"]},
        {"prestataire": "Forfait B", "prix": 580, "postes": ["Hôtel", "Plongées"],
         "repartition": {"type": "euros", "parts": {"Hôtel": 300, "Plongées": 270}}},
        {"prestataire": "Forfait C", "prix": 580, "postes": ["Hôtel", "Plongées"],
         "repartition": {"type": "pourcent", "parts": {"Hôtel": 60, "Plongées": 30}}},
        {"prestataire": "X", "prix": 1, "postes": ["Avion"], "couleur": "bleu"}],
        "selectionner": ["Inconnue"], "echeances": [{"offre": "Taxi", "libelle": "", "date": "2027-01-01"}],
        "bizarre": 1}
    with pytest.raises(OpsError) as e:
        ops.plan_import(sc.pid, bad)
    errs = e.value.errors
    assert errs[0] == "Champs inconnus : bizarre. Voir le guide."
    assert "Projet : Il faut au moins une personne pour calculer un prix par personne." in errs
    assert "Offre 1 (Navette) : Le prix ne peut pas être négatif (0 si c'est inclus)." in errs
    assert "Offre 2 (Bateau) : Choisis au moins un poste pour cette offre." in errs
    assert any(x.startswith("Offre 3 (Taxi 8) : Ce prix vaut pour combien de personnes ?") for x in errs)
    assert any(x.startswith("Offre 4 (Forfait B) : Les montants par poste font 570,00") for x in errs)
    assert "Offre 5 (Forfait C) : Les pourcentages par poste font 90 %, il faut 100 %." in errs
    assert "Offre 6 (X) : champs inconnus : couleur" in errs
    assert "Sélectionner « Inconnue » : pas d'offre « Inconnue » dans ce projet" in errs
    assert any(x.startswith("Échéance 1 : Dis ce qu'il faut faire") for x in errs)
    assert sc.db.undo_token() == before and rows(sc.db, sc.pid)["offers"][0][0] == "Transavia"
    with sc.db.Session() as s:
        assert len(sc.plan(s).offers) == 7 and sc.plan(s).people == 10
    with pytest.raises(OpsError, match="pour le projet n°99"):
        ops.plan_import(sc.pid, {"projet_id": 99})
    with pytest.raises(OpsError, match="Projet introuvable"):
        ops.plan("Lune")
    assert not [m for m in errs if FORBIDDEN.search(m)]


def test_ai_page_preview_and_save(screen):
    sc = screen.reference()
    page = sc.cl.get(f"{sc.url}/ia").text
    t = clean(page)
    assert "Ajouter des offres avec une IA" in t and "Copier le message pour l'IA" in t
    assert '"projet_id": 1' in page.replace("&#34;", '"') or "projet_id" in page
    assert "Colle ici le devis" in t                                       # le modèle
    answer = ("Voici ce que je propose : une navette.\n```json\n" + json.dumps({
        "projet_id": sc.pid, "offres": [{"prestataire": "Navette", "prix": 45, "compte": "pour un nombre de personnes",
                                         "pour_combien": 8, "postes": ["Taxi"], "selectionnee": True,
                                         "echeances": [{"libelle": "Acompte à verser", "date": "2027-01-20",
                                                        "montant": 30}]}]}) + "\n```")
    before = sc.db.undo_token()
    r = sc.cl.post(f"{sc.url}/ia/apercu", data=dict(texte=answer))
    t = clean(r.text)
    assert "Ce qui va changer" in t
    assert "Offre « Navette » ajoutée : 45,00 € pour 8 personnes (il en faut 2 pour 10 personnes), pour Taxi (sélectionnée)" in t
    assert "Échéance « Acompte à verser » (20/01/2027) pour « Navette »" in t
    assert "Budget : 8 930,00 € → 8 840,00 € · prix par personne : 893,00 € → 884,00 €" in t
    assert sc.db.undo_token() == before                                   # l'aperçu n'enregistre rien
    r = sc.post(f"{sc.url}/ia/importer", dict(texte=answer))
    assert message(r) == "2 changements pour « Mer Rouge 2027 »."
    assert "Budget total 8 840,00 €" in sc.text()
    assert sc.db.last_action() == "changements proposés par l'IA pour « Mer Rouge 2027 »"
    sc.post("/annuler", {}, headers={"referer": f"http://testserver{sc.url}"})
    assert "Budget total 8 930,00 €" in sc.text()                        # une seule copie : tout est défait
    # erreurs : affichées, avec le bouton pour les recopier à l'IA
    r = sc.cl.post(f"{sc.url}/ia/apercu", data=dict(texte='{"projet_id": 1, "offres": [{"prestataire": "Y", "prix": 5}]}'))
    t = clean(r.text)
    assert "À corriger avant d'enregistrer" in t and "Offre 1 (Y) : Choisis au moins un poste pour cette offre." in t
    r = sc.cl.post(f"{sc.url}/ia/apercu", data=dict(texte="pas de json ici"))
    assert "Je ne trouve pas de bloc JSON" in clean(r.text)


def test_ai_draft_is_saved(screen):
    sc = screen.reference()
    r = sc.cl.put(f"{sc.url}/ia/brouillon", content="Devis du club A : 580 € par personne".encode())
    assert r.status_code == 200
    assert "Devis du club A : 580 € par personne" in sc.cl.get(f"{sc.url}/ia").text
    blank = sc.cl.post(f"{sc.url}/ia/brouillon/vider").json()["modele"]
    assert blank.startswith("=== PRÉPARER LE VOYAGE ===")
    assert sc.cl.post(f"{sc.url}/ia/brouillon/recuperer").json()["modele"] == "Devis du club A : 580 € par personne"
    assert sc.cl.get("/ia/guide_preparation.md").text.startswith("# Trip Compta — préparer un voyage")


def test_api_and_mcp(screen):
    sc = screen.reference()
    cl = sc.cl
    assert [p["nom"] for p in cl.get("/api/projets").json()] == ["Mer Rouge 2027"]
    r = cl.post("/api/projets", json=dict(nom="Week-end", participants=6))
    assert r.json()["projet_id"] == 2
    r = cl.post("/api/projets/2/import", json=dict(donnees={"offres": [
        {"prestataire": "Gîte", "prix": 600, "compte": "pour le groupe", "postes": ["Gîte"], "selectionnee": True}]}))
    assert r.json()["apercu"]["par_personne"] == 100.0
    assert cl.get("/api/projets/2").json()["budget"] == 600.0
    r = cl.post("/api/projets/2/import", json=dict(donnees={"participants": 0}))
    assert r.status_code == 400 and "au moins une personne" in r.json()["erreur"]
    assert cl.get("/api/projets/Week-end").json()["projet_id"] == 2
    from compta.mcp_server import build_server
    import asyncio
    srv = build_server(str(sc.db.dir))
    names = {t.name for t in asyncio.run(srv.list_tools())}
    assert {"lister_projets", "voir_projet", "creer_projet", "modifier_projet", "lire_guide_preparation"} <= names


def test_diagnostic_counts_without_names(screen):
    from compta import diagnostic as D
    sc = screen.reference()
    deadline(sc, "Taxi", "Acompte", "2027-02-01")
    text = D.build(version="1.0.0", folder=sc.db.home, db=sc.db)
    assert "voyages en préparation : 1" in text and "postes (en préparation) : 5" in text
    assert "offres (en préparation) : 7" in text and "échéances (en préparation) : 1" in text
    for name in ("Mer Rouge", "Transavia", "Hurghada", "Acompte"):
        assert name not in text


# ---------------------------------------------------------------- vocabulaire

def test_no_forbidden_words_on_new_screens(screen):
    sc = screen.reference()
    deadline(sc, "Taxi", "Acompte", "2027-01-08")
    for name in ("plan_launch.html", "plan_ai.html", "plan.html", "index.html", "dashboard.html"):
        from test_preparation_ecran import TEMPLATES
        src = (TEMPLATES / name).read_text(encoding="utf-8")
        assert not FORBIDDEN.search(src), (name, FORBIDDEN.search(src).group(0))
    for url in (sc.url, f"{sc.url}/lancer", f"{sc.url}/ia", "/"):
        t = sc.text(url)
        assert not FORBIDDEN.search(t), (url, FORBIDDEN.search(t).group(0))
    tid, _ = launch(sc)
    for url in (f"/v/{tid}", f"/v/{tid}/depenses"):
        t = sc.text(url)
        assert not FORBIDDEN.search(t), (url, FORBIDDEN.search(t).group(0))


# ---------------------------------------------------------------- « pour tout le monde » après un lancement

TEN = [f"Personne{i} Test" for i in range(10)]


def everyone_owes(sc, tid, n=10, each=89300):
    with sc.db.Session() as s:
        trip = s.get(M.Trip, tid)
        res = S.compute_trip(trip, E.PLANNED)
        assert len(trip.participants) == n
        assert all(len(e.shares) == n for e in trip.expenses), [len(e.shares) for e in trip.expenses]
        assert [x.share for x in res.people.values()] == [each] * n         # chacun (une famille règle ensemble)


@pytest.mark.parametrize("way", ["un par un", "plusieurs d'un coup", "personnes connues", "famille", "IA", "import IA"])
def test_every_way_of_adding_people_joins_the_launched_expenses(screen, way):
    sc = screen.reference()
    tid, _ = launch(sc)
    if way == "un par un":
        for n in TEN:
            sc.post(f"/v/{tid}/participants", dict(name=n))
    elif way == "plusieurs d'un coup":
        sc.post(f"/v/{tid}/participants", dict(names="\n".join(TEN)))
    elif way == "personnes connues":
        with sc.db.Session() as s:
            old = W.create_trip(s, "Voyage de l'an passé")
            for n in TEN:
                W.add_participant(s, old, n)
            s.commit()
        with sc.db.Session() as s:
            keys = [x["key"] for x in S.known_people(s.get(M.Trip, tid))]
        assert len(keys) == 10
        sc.post(f"/v/{tid}/participants/reprendre", dict(known=keys))
    elif way == "famille":
        for n in TEN:
            sc.post(f"/v/{tid}/participants", dict(name=n, family_pick="__new__" if n == TEN[0] else "Test",
                                                    family_new="Test"))
        with sc.db.Session() as s:
            assert all(p.family_id for p in s.get(M.Trip, tid).participants)
    elif way == "IA":
        Ops(sc.db).add_participants(tid, TEN)
    else:
        Ops(sc.db).import_trip({"voyage_id": tid, "participants": TEN}, str(tid))
    everyone_owes(sc, tid)


def test_personalised_shares_are_never_joined_blindly(screen):
    """Une part fixée par un autre chemin que le formulaire (ici par l'IA) : le nouveau venu n'y entre pas tout seul,
    la dépense perd « pour tout le monde » et la règle habituelle la marque « à vérifier »."""
    from compta.ops import do_fix_share
    sc = screen.reference()
    tid, _ = launch(sc)
    sc.post(f"/v/{tid}/participants", dict(names="Anne Martin\nBruno Petit"))
    with sc.db.Session() as s:
        trip = s.get(M.Trip, tid)
        do_fix_share(s, trip, "Anne Martin", "Transavia", "100")
        s.commit()
    r = sc.post(f"/v/{tid}/participants", dict(name="Chloé Roux"))
    with sc.db.Session() as s:
        trip = s.get(M.Trip, tid)
        chloe = next(p.id for p in trip.participants if p.name == "Chloé Roux")
        transavia = next(e for e in trip.expenses if e.label == "Transavia")
        assert chloe not in {x.participant_id for x in transavia.shares} and not transavia.for_all
        assert [x.reason for x in transavia.checks]                           # « à vérifier »
        assert all(chloe in {x.participant_id for x in e.shares} and e.for_all for e in trip.expenses
                   if e.label != "Transavia")
    assert "rejoindre" in r.headers["location"] or "Chloé Roux ajouté" in message(r)


def test_launched_trip_exported_imported_and_duplicated_keeps_everyone(screen):
    from compta import transfer as TR
    sc = screen.reference()
    tid, _ = launch(sc)
    with sc.db.Session() as s:
        blob = TR.export_zip(s, [s.get(M.Trip, tid)], sc.db.receipts_dir)
    r = sc.cl.post("/importer", files={"fichier": ("voyage.zip", blob, "application/zip")}, follow_redirects=False)
    with sc.db.Session() as s:
        if s.query(M.Trip).count() == 1:                  # l'import passe par un aperçu : on le confirme
            data, z = TR.read_zip(blob)
            new = TR.import_trip(s, data["voyages"][0], z, sc.db.receipts_dir)
            s.commit()
        new = max(s.query(M.Trip).all(), key=lambda x: x.id)
        assert new.id != tid and all(e.for_all and not e.shares for e in new.expenses)
        dup = TR.duplicate_trip(s, s.get(M.Trip, tid), "Mer Rouge 2028", with_people=True, with_expenses=True)
        s.commit()
        ids = (new.id, dup.id)
        assert all(e.for_all for e in dup.expenses)
    for x in ids:
        sc.post(f"/v/{x}/participants", dict(names="\n".join(TEN)))
        everyone_owes(sc, x)


def test_a_normal_trip_without_people_still_refuses_an_expense_for_nobody(screen):
    """Comme avant : sans participant, une dépense « pour personne » est refusée, par le formulaire comme par l'IA ;
    seul le lancement crée des dépenses « pour tout le monde »."""
    sc = screen
    sc.post("/voyages", dict(name="Voyage ordinaire", caisse="asso"))
    with sc.db.Session() as s:
        t = s.query(M.Trip).one()
        tid = t.id
        with pytest.raises(W.WriteError, match="Pour qui est cette dépense"):
            W.save_expense(s, t, label="Gîte", planned=10000, weights={})
        assert not t.expenses
    r = sc.post(f"/v/{tid}/depenses", dict(label="Gîte", planned="100", category="Logement"))
    assert refused(r) and "Pour qui" in message(r)
    with pytest.raises(OpsError, match="Pour qui"):
        Ops(sc.db).add_expense(tid, "Gîte", montant_prevu=100)
    with sc.db.Session() as s:
        assert not s.get(M.Trip, tid).expenses
