"""Dates de paiement : un acompte avant telle date, le reste avant le départ. Chaque compte est alors « à jour »,
« en retard » ou « soldé » ; la carte « À relancer aujourd'hui » prépare le message (rien ne part tout seul).

Le voyage des essais (montants vérifiés à la main) : Anne, Bruno, Chloé, David, et la famille Martin (Paul paie
pour Julie et lui).
- Gîte 1 200 € payé par l'asso, pour les six : 200,00 € chacun ;
- Essence 90 € payée par David, pour les six : 15,00 € chacun ;
  → part de chacun : 215,00 € ; famille Martin : 430,00 €.
Dates du voyage : acompte 80 € par personne il y a 10 jours, 2e paiement 50 € par personne dans 5 jours, le reste
dans 20 jours. Chaque date demande d'avoir payé en tout : 80, puis 130, puis 215 (famille : 160, 260, 430).
- Anne a versé 50 €        → il manque 30 € sur l'acompte : EN RETARD de 30,00 € depuis 10 jours ;
- Bruno a versé 215 €      → SOLDÉ ;
- Chloé a versé 80 €       → À JOUR ; prochain paiement : 50,00 € ;
- David a avancé 90 €      → l'acompte (80) est couvert, et 10 € du 2e paiement : À JOUR, prochain : 40,00 € ;
- famille Martin : 100 €   → acompte de 160 : EN RETARD de 60,00 €.
À relancer : Anne 30,00 € + famille Martin 60,00 € = 90,00 €.
"""
import datetime as dt
import html
import json
import re
import socket
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from fastapi.testclient import TestClient

from compta import engine as E
from compta import models as M
from compta import services as S
from compta import transfer as TR
from compta import writes as W
from compta.ops import Ops, OpsError
from compta.web import create_app

TODAY = dt.date.today()
PAST, SOON, LATER = TODAY - dt.timedelta(days=10), TODAY + dt.timedelta(days=5), TODAY + dt.timedelta(days=20)
FR = "%d/%m/%Y"


def clean(t):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t)).replace("\xa0", " "))


def message(r):
    return parse_qs(urlparse(r.headers["location"]).query).get("msg", [""])[0].replace("\xa0", " ")


def refused(r):
    return r.status_code == 303 and "e=1" in r.headers["location"]


@pytest.fixture
def trip(tmp_path):
    app = create_app(tmp_path / "voyages")
    ops = Ops(app.state.db)
    tid = ops.create_trip("Week-end")["voyage_id"]
    ops.add_participants(tid, ["Anne", "Bruno", "Chloé", "David",
                               {"nom": "Paul Martin", "famille": "Martin", "telephone": "06 12 34 56 78",
                                "email": "paul@example.org"},
                               {"nom": "Julie Martin", "famille": "Martin"}])
    ops.add_expense(tid, "Gîte", montant_reel="1200")
    ops.add_expense(tid, "Essence", montant_reel="90", paye_par="David")
    for who, amount in (("Anne", "50"), ("Bruno", "215"), ("Chloé", "80"), ("Paul Martin", "100")):
        ops.add_movement(tid, who, amount, date="2027-01-05")
    return app, TestClient(app), ops, tid


def set_dates(cl, tid, pour=None, rows=None):
    rows = rows or [("", PAST, "80"), ("", SOON, "50"), ("", LATER, "")]
    data = {}
    for i, (name, date, amount) in enumerate(rows):
        data.update({f"nom_{i}": name, f"date_{i}": date.isoformat() if date else "", f"montant_{i}": amount})
    if pour:
        data["pour"] = str(pour)
    return cl.post(f"/v/{tid}/argent/dates", data=data, follow_redirects=False)


def plans(app, tid, today=None):
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        r = S.compute_trip(t)
        got = S.pay_plans(t, r, today)
        return {a.name: got[a.key] for a in r.accounts if a.key in got}


def pid(app, tid, name):
    with app.state.db.Session() as s:
        return next(p.id for p in s.get(M.Trip, tid).participants if p.name == name)


# ---------------------------------------------------------------- les états, à la main
def test_states_and_amounts_by_hand(trip):
    app, cl, ops, tid = trip
    assert plans(app, tid) == {}                                   # pas de dates : rien n'est signalé
    r = set_dates(cl, tid)
    assert r.status_code == 303 and not refused(r)
    assert "Acompte : 80,00 € par personne" in message(r) and "2e paiement : 50,00 € par personne" in message(r)
    assert f"Solde : tout ce qui reste, au plus tard le {LATER:{FR}}" in message(r)
    assert "2 paiements en retard" in message(r) and r.headers["location"].endswith("#relances")
    p = plans(app, tid)
    assert {k: v["label"] for k, v in p.items()} == {
        "Anne": "en retard", "Bruno": "soldé", "Chloé": "à jour", "David": "à jour", "Famille Martin": "en retard"}
    # Anne : 215 de part, 50 versés. Acompte 80 : 50 payés, 30 manquent. Puis 50, puis 215 − 130 = 85.
    a = p["Anne"]
    assert (a["late"], a["since"], a["days"], a["rest"]) == (3000, PAST, 10, 16500)
    assert [(x["nom"], x["due"], x["paid"], x["missing"], x["late"]) for x in a["lines"]] == [
        ("Acompte", 8000, 5000, 3000, True), ("2e paiement", 5000, 0, 5000, False), ("Solde", 8500, 0, 8500, False)]
    assert a["next"]["missing"] == 5000 and a["next"]["date"] == SOON
    # Bruno : tout payé
    assert p["Bruno"]["late"] == 0 and p["Bruno"]["next"] is None and all(x["missing"] == 0 for x in p["Bruno"]["lines"])
    # Chloé : 80 versés = l'acompte, pile
    assert p["Chloé"]["late"] == 0 and (p["Chloé"]["next"]["missing"], p["Chloé"]["next"]["date"]) == (5000, SOON)
    # David : 90 avancés pour le groupe comptent comme payés : acompte couvert, 10 sur le 2e paiement
    d = p["David"]
    assert [(x["paid"], x["missing"]) for x in d["lines"]] == [(8000, 0), (1000, 4000), (0, 8500)]
    assert d["next"]["missing"] == 4000
    # Famille Martin : deux personnes, donc 160 d'acompte ; 100 versés : 60 manquent. Puis 100, puis 430 − 260 = 170.
    f = p["Famille Martin"]
    assert (f["late"], f["rest"], f["own"]) == (6000, 33000, False)
    assert [(x["due"], x["missing"]) for x in f["lines"]] == [(16000, 6000), (10000, 10000), (17000, 17000)]
    # tout se retrouve : ce que les dates demandent en tout = la part de chacun
    for name, plan in p.items():
        assert sum(x["due"] for x in plan["lines"]) == (43000 if name == "Famille Martin" else 21500)
        assert sum(x["missing"] for x in plan["lines"]) == plan["rest"]


def test_a_date_is_late_only_the_day_after(trip):
    """« Au plus tard le 1er mars » laisse toute la journée du 1er."""
    app, cl, ops, tid = trip
    set_dates(cl, tid, rows=[("Acompte", TODAY, "80"), ("Solde", LATER, "")])
    assert plans(app, tid)["Anne"]["label"] == "à jour"
    assert plans(app, tid, TODAY + dt.timedelta(days=1))["Anne"]["label"] == "en retard"
    assert plans(app, tid, TODAY + dt.timedelta(days=1))["Anne"]["days"] == 1
    late = plans(app, tid, LATER + dt.timedelta(days=3))["Anne"]           # les deux dates dépassées
    assert (late["late"], late["since"], late["days"]) == (16500, TODAY, 23)


def test_amounts_never_ask_more_than_the_share(trip):
    """Un acompte plus gros que la part : on ne demande jamais plus que ce que la personne doit en tout."""
    app, cl, ops, tid = trip
    set_dates(cl, tid, rows=[("Acompte", PAST, "300"), ("Solde", LATER, "")])
    a = plans(app, tid)["Anne"]
    assert [(x["due"], x["missing"]) for x in a["lines"]] == [(21500, 16500), (0, 0)] and a["late"] == 16500
    assert plans(app, tid)["Bruno"]["label"] == "soldé"


def test_only_fixed_amounts_leave_the_rest_without_a_date(trip):
    app, cl, ops, tid = trip
    set_dates(cl, tid, rows=[("Acompte", PAST, "50")])
    p = plans(app, tid)
    assert p["Anne"]["label"] == "à jour" and p["Anne"]["next"] is None and p["Anne"]["rest"] == 16500
    assert p["Famille Martin"]["label"] == "à jour"                          # 100 versés = 2 × 50


def test_withdrawn_family_member_no_longer_counts(trip):
    """Julie se désiste : la famille ne doit plus l'acompte que pour une personne."""
    app, cl, ops, tid = trip
    set_dates(cl, tid)
    ops.withdraw(tid, "Julie Martin", pris_en_charge_par="asso")
    f = plans(app, tid)["Famille Martin"]
    assert f["lines"][0]["due"] == 8000 and f["label"] == "à jour"            # 100 versés ≥ 80
    assert f["lines"][1]["missing"] == 3000                                   # 130 demandés, 100 payés


def test_cash_holder_has_no_dates(tmp_path):
    app = create_app(tmp_path / "voyages")
    cl = TestClient(app)
    r = cl.post("/voyages", data={"name": "Entre amis", "caisse": "personne", "cashier_name": "Léa"},
                follow_redirects=False)
    tid = int(re.search(r"/v/(\d+)/", r.headers["location"]).group(1))
    ops = Ops(app.state.db)
    ops.add_participants(tid, ["Max"])
    ops.add_expense(tid, "Gîte", montant_reel="200", paye_par="Léa")
    set_dates(cl, tid, rows=[("Acompte", PAST, "50")])
    assert set(plans(app, tid)) == {"Max"}                                     # Léa tient la caisse : rien à verser
    assert plans(app, tid)["Max"]["late"] == 5000
    page = clean(cl.get(f"/v/{tid}/argent/dates").text)
    assert "Max" in page.split("Pour qui ?")[1] and "Léa" not in page.split("Pour qui ?")[1]
    assert refused(cl.get(f"/v/{tid}/argent/dates?pour={pid(app, tid, 'Léa')}", follow_redirects=False))


# ---------------------------------------------------------------- l'écran
def test_money_page_before_any_date(trip):
    app, cl, ops, tid = trip
    page = cl.get(f"/v/{tid}/argent").text
    assert "Tu attends des paiements à des dates précises ?" in page and "Fixer des dates de paiement" in page
    assert 'id="relances"' not in page and "pill late" not in page and "pay-state" not in page
    form = cl.get(f"/v/{tid}/argent/dates").text
    assert 'placeholder="Acompte"' in form and 'placeholder="Solde"' in form and form.count('name="date_') == 3
    assert "Ne plus utiliser de dates" not in form                             # rien à retirer encore
    assert "data-no-today" in form                                             # pas de bouton « Aujourd'hui » ici


def test_money_page_shows_who_is_late_and_the_reminder(trip):
    app, cl, ops, tid = trip
    set_dates(cl, tid)
    raw = cl.get(f"/v/{tid}/argent").text
    page = clean(raw)
    card = clean(raw[raw.index('id="relances"'):raw.index('class="settle-card"')])
    assert "À relancer aujourd'hui 90,00 €" in card                            # 30 + 60
    assert card.index("Anne") < card.index("Famille Martin")
    assert f"30,00 € à régler depuis le {PAST:{FR}} (10 jours)" in card
    assert f"60,00 € à régler depuis le {PAST:{FR}} (10 jours)" in card
    assert "à Paul Martin · 06 12 34 56 78 · paul@example.org" in card and "pas de numéro · pas d'e-mail" in card
    assert "Rien ne part tout seul" in card
    for absent in ("Bruno", "Chloé", "David"):
        assert absent not in card
    # les lignes « À recevoir » : l'état de chacun
    rows = clean(raw[raw.index('class="settle-card"'):raw.index('id="dates"')])
    assert f"Anne en retard 30,00 € depuis le {PAST:{FR}} 165,00 €" in rows
    assert f"Chloé à jour prochain paiement : 50,00 € au plus tard le {SOON:{FR}} 135,00 €" in rows
    assert f"David à jour prochain paiement : 40,00 € au plus tard le {SOON:{FR}} 125,00 €" in rows
    assert f"Famille Martin (à Paul Martin) en retard 60,00 € depuis le {PAST:{FR}} 330,00 €" in rows
    assert "Bruno" not in rows                                                 # soldé : plus rien à recevoir de lui
    # le rappel des dates
    assert f"Acompte : 80,00 € par personne, au plus tard le {PAST:{FR}}" in page
    assert f"Solde : tout ce qui reste, au plus tard le {LATER:{FR}}" in page
    # le message, prêt pour WhatsApp et l'e-mail, sans PDF à joindre
    wa = re.search(r'href="(https://wa\.me/33612345678\?text=[^"]+)"', raw).group(1)
    text = unquote(html.unescape(wa).split("text=")[1]).replace("\xa0", " ")
    assert text == (
        "Bonjour Paul, un petit rappel pour le voyage « Week-end ».\n"
        f"➜ Il vous reste 60,00 € à verser à l'asso : c'était à régler pour le {PAST:{FR}} (Acompte).\n"
        f"Ensuite : 100,00 € au plus tard le {SOON:{FR}} (2e paiement).\n"
        f"Ensuite : 170,00 € au plus tard le {LATER:{FR}} (Solde).\n"
        "\nSi vous avez payé entre-temps, ne tenez pas compte de ce message. Merci !")
    mail = html.unescape(re.search(r'href="(mailto:paul@example\.org\?[^"]+)"', raw).group(1))
    assert "subject=Rappel%20de%20paiement" in mail
    assert "data-send-pdf" not in raw[raw.index('id="relances"'):raw.index('class="settle-card"')]


def test_reminder_gives_how_to_pay_and_all_late_dates(trip):
    app, cl, ops, tid = trip
    set_dates(cl, tid, rows=[("Acompte", PAST, "80"), ("", TODAY - dt.timedelta(days=2), "50"), ("Solde", LATER, "")])
    cl.post(f"/v/{tid}/envoyer/comment-payer", data={"pay_info": "Virement : IBAN FR76 1234, au nom du club."})
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        r = S.compute_trip(t)
        late = S.late_accounts(t, r)
        # le plus ancien retard d'abord (l'acompte), puis ceux qui n'ont manqué que le 2e paiement
        assert [a.name for a, _ in late] == ["Anne", "Famille Martin", "Chloé", "David"]
        assert [p["late"] for _, p in late] == [8000, 16000, 5000, 4000]          # 30 + 50 ; 60 + 100 ; 50 ; 40
        text = S.reminder_message(t, r, *late[0]).replace("\xa0", " ")
    assert text == (
        "Bonjour Anne, un petit rappel pour le voyage « Week-end ».\n"
        "➜ Il te reste 80,00 € à verser à l'asso, dates dépassées :\n"
        f"   • 30,00 € pour le {PAST:{FR}} (Acompte)\n"
        f"   • 50,00 € pour le {TODAY - dt.timedelta(days=2):{FR}} (2e paiement)\n"
        f"Ensuite : 85,00 € au plus tard le {LATER:{FR}} (Solde).\n"
        "\nComment payer :\nVirement : IBAN FR76 1234, au nom du club.\n"
        "\nSi tu as payé entre-temps, ne tiens pas compte de ce message. Merci !")


def test_account_message_and_send_page_mention_the_dates(trip):
    app, cl, ops, tid = trip
    set_dates(cl, tid)
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        r = S.compute_trip(t)
        anne = next(a for a in r.accounts if a.name == "Anne")
        bruno = next(a for a in r.accounts if a.name == "Bruno")
        text = S.account_message(t, r, anne).replace("\xa0", " ")
        assert "➜ Il te reste 165,00 € à verser à l'asso.\n" \
               f"   • 30,00 € au plus tard le {PAST:{FR}} (Acompte) — date dépassée\n" \
               f"   • 50,00 € au plus tard le {SOON:{FR}} (2e paiement)\n" \
               f"   • 85,00 € au plus tard le {LATER:{FR}} (Solde)\n" in text
        assert "au plus tard" not in S.account_message(t, r, bruno)
    page = clean(cl.get(f"/v/{tid}/envoyer").text)
    assert "Anne doit 165,00 € en retard" in page and "Chloé doit 135,00 € pas de numéro" in page
    assert "Bruno soldé" in page                                              # avec des dates : « soldé »
    # la fiche d'Anne : le détail date par date
    block = clean(cl.get(f"/v/{tid}/participants/comptes?p={pid(app, tid, 'Anne')}").text)
    assert f"Acompte : 80,00 € au plus tard le {PAST:{FR}} en retard : il manque 30,00 €" in block
    assert f"2e paiement : 50,00 € au plus tard le {SOON:{FR}} à venir" in block
    assert "Lui donner d'autres dates" in block
    # « Où j'en suis » : une seule alerte pour les retards, pas « n'a encore rien versé » en double
    home = clean(cl.get(f"/v/{tid}").text)
    assert "2 paiements en retard (90,00 €) : Anne, Famille Martin. Le message de relance est prêt." in home


def test_without_dates_the_words_do_not_change(trip):
    """Sans dates de paiement, rien ne bouge à l'écran : un compte réglé reste « à jour »."""
    app, cl, ops, tid = trip
    page = clean(cl.get(f"/v/{tid}/envoyer").text)
    assert "Bruno à jour" in page and "soldé" not in page and "en retard" not in page
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        r = S.compute_trip(t)
        assert "au plus tard" not in S.account_message(t, r, r.accounts[0])
        assert not any("en retard" in a["text"] for a in S.alerts(t, r))


def test_paying_clears_the_reminder(trip):
    app, cl, ops, tid = trip
    set_dates(cl, tid)
    ops.add_movement(tid, "Anne", "30")
    raw = cl.get(f"/v/{tid}/argent").text
    card = clean(raw[raw.index('id="relances"'):raw.index('class="settle-card"')])
    assert "À relancer aujourd'hui 60,00 €" in card and "Anne" not in card
    ops.add_movement(tid, "Paul Martin", "60")
    raw = cl.get(f"/v/{tid}/argent").text
    assert 'id="relances"' not in raw and "en retard" not in clean(raw)
    assert plans(app, tid)["Anne"]["label"] == "à jour"


# ---------------------------------------------------------------- des dates pour quelqu'un
def test_own_dates_for_one_person(trip):
    """Anne a obtenu un délai : tout dans 3 jours. Elle n'est plus signalée en retard."""
    app, cl, ops, tid = trip
    set_dates(cl, tid)
    anne = pid(app, tid, "Anne")
    form = clean(cl.get(f"/v/{tid}/argent/dates?pour={anne}").text)
    assert "Dates de paiement de Anne" in form and "Anne doit en tout 215,00 €" in form
    assert "il reste 165,00 € à verser" in form
    assert "Montant (€)" in form and "par personne" not in form.split("Quoi")[1]
    r = set_dates(cl, tid, pour=anne, rows=[("Délai accordé", TODAY + dt.timedelta(days=3), "")])
    assert "Dates de paiement de Anne enregistrées : Délai accordé : tout ce qui reste" in message(r)
    p = plans(app, tid)
    assert p["Anne"]["label"] == "à jour" and p["Anne"]["own"] is True
    assert (p["Anne"]["next"]["missing"], p["Anne"]["next"]["nom"]) == (16500, "Délai accordé")
    assert p["Famille Martin"]["label"] == "en retard" and p["Chloé"]["own"] is False     # les autres : inchangés
    raw = cl.get(f"/v/{tid}/argent").text
    assert "Dates différentes pour :" in clean(raw) and f"/argent/dates?pour={anne}" in raw
    card = clean(raw[raw.index('id="relances"'):raw.index('class="settle-card"')])
    assert "À relancer aujourd'hui 60,00 €" in card and "Anne" not in card
    # la page des dates du voyage liste les exceptions, et ne propose plus Anne
    page = cl.get(f"/v/{tid}/argent/dates").text
    assert "Délai accordé : tout ce qui reste" in clean(page)
    assert f'<option value="{anne}">' not in page
    # retirer : Anne suit de nouveau les dates du voyage
    r = cl.post(f"/v/{tid}/argent/dates/retirer", data={"pour": str(anne)}, follow_redirects=False)
    assert "Anne suit de nouveau les dates du voyage" in message(r)
    assert plans(app, tid)["Anne"]["label"] == "en retard" and plans(app, tid)["Anne"]["own"] is False


def test_own_dates_for_a_family_are_for_the_whole_family(trip):
    app, cl, ops, tid = trip
    set_dates(cl, tid)
    julie, paul = pid(app, tid, "Julie Martin"), pid(app, tid, "Paul Martin")
    # demandées en passant par Julie : rangées pour la famille (celui qui paie), montants pour tout le compte
    r = set_dates(cl, tid, pour=julie, rows=[("En deux fois", PAST, "100"), ("", LATER, "")])
    assert "Dates de paiement de Famille Martin enregistrées" in message(r)
    f = plans(app, tid)["Famille Martin"]
    assert f["own"] and f["label"] == "à jour"                                 # 100 versés = les 100 demandés
    assert [(x["due"], x["missing"]) for x in f["lines"]] == [(10000, 0), (33000, 33000)]
    with app.state.db.Session() as s:
        assert set(S.pay_dates(s.get(M.Trip, tid))["comptes"]) == {paul}


def test_removing_trip_dates_keeps_own_dates(trip):
    app, cl, ops, tid = trip
    set_dates(cl, tid)
    set_dates(cl, tid, pour=pid(app, tid, "Anne"), rows=[("Délai", PAST, "")])
    r = cl.post(f"/v/{tid}/argent/dates/retirer", follow_redirects=False)
    assert "Dates de paiement du voyage retirées" in message(r) and "sauf pour ceux qui ont leurs propres dates" in message(r)
    p = plans(app, tid)
    assert set(p) == {"Anne"} and p["Anne"]["late"] == 16500
    r = cl.post(f"/v/{tid}/argent/dates/retirer", follow_redirects=False)
    assert message(r) == "Rien à changer"


# ---------------------------------------------------------------- saisie refusée, défaire, voyage clôturé
def test_refused_entries(trip):
    app, cl, ops, tid = trip
    here = f"/v/{tid}/argent/dates"
    r = set_dates(cl, tid, rows=[("Acompte", None, "80")])
    assert refused(r) and "Indique la date de « Acompte »" in message(r) and "f=date_0" in r.headers["location"]
    r = set_dates(cl, tid, rows=[("", None, "")])
    assert refused(r) and "au moins une date" in message(r)
    r = set_dates(cl, tid, rows=[("Acompte", PAST, "abc")])
    assert refused(r) and "Montant invalide" in message(r)
    r = cl.post(here, data={"nom_0": "Tout", "date_0": PAST.isoformat(), "nom_1": "Suite", "date_1": LATER.isoformat(),
                            "montant_1": "50"}, follow_redirects=False, headers={"referer": f"http://testserver{here}"})
    assert refused(r) and "à la dernière date" in message(r)
    r = cl.post(here, data={"date_0": PAST.isoformat(), "date_1": LATER.isoformat()}, follow_redirects=False,
                headers={"referer": f"http://testserver{here}"})
    assert refused(r) and "qu'une fois" in message(r)
    assert refused(set_dates(cl, tid, pour=9999))
    assert plans(app, tid) == {}                                               # rien n'a été écrit
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        with pytest.raises(W.WriteError):
            W.set_pay_dates(t, [("x", PAST, 100)] * (S.PAY_STEPS_MAX + 1))
        with pytest.raises(W.WriteError):
            W.set_pay_dates(t, [("x", PAST, -5)])
        with pytest.raises(W.WriteError):
            W.set_pay_dates(t, [("x", PAST, 100)], pid=9999)


def test_undo_and_closed_trip(trip):
    app, cl, ops, tid = trip
    r = set_dates(cl, tid)
    assert "&d=" in r.headers["location"]                                      # « Défaire » est proposé
    assert plans(app, tid)["Anne"]["label"] == "en retard"
    cl.post("/annuler")
    assert plans(app, tid) == {}
    set_dates(cl, tid)
    cl.post(f"/v/{tid}/cloturer")
    r = set_dates(cl, tid, rows=[("Acompte", PAST, "10")])
    assert "clôturé" in message(r) and plans(app, tid)["Anne"]["late"] == 3000   # figé, mais toujours lisible
    assert cl.get(f"/v/{tid}/argent").status_code == 200 and cl.get(f"/v/{tid}/argent/dates").status_code == 200


def test_stored_text_survives_oddities(trip):
    """Le texte enregistré est relu avec prudence : une ligne abîmée ou une personne partie ne casse rien."""
    app, cl, ops, tid = trip
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        t.pay_dates = json.dumps({"etapes": [{"nom": "A", "date": "pas une date", "montant": 100},
                                             {"nom": "B", "date": PAST.isoformat(), "montant": "x"},
                                             {"nom": "C", "date": PAST.isoformat(), "montant": 5000}, "n'importe quoi"],
                                  "comptes": {"9999": [{"nom": "Z", "date": PAST.isoformat()}], "abc": []}})
        s.commit()
    with app.state.db.Session() as s:
        d = S.pay_dates(s.get(M.Trip, tid))
        assert [x["nom"] for x in d["etapes"]] == ["C"] and d["comptes"] == {}
    assert cl.get(f"/v/{tid}/argent").status_code == 200
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        t.pay_dates = "{pas du json"
        s.commit()
    assert plans(app, tid) == {} and cl.get(f"/v/{tid}/argent").status_code == 200


# ---------------------------------------------------------------- l'IA, l'API, le transfert
def test_ai_gives_and_reads_the_dates(trip):
    app, cl, ops, tid = trip
    out = ops.import_trip({"voyage_id": tid, "dates_paiement": [
        {"nom": "Acompte", "date": PAST.isoformat(), "montant": "80"},
        {"date": SOON.isoformat(), "montant": 50},
        {"nom": "Solde", "date": LATER.isoformat()},
        {"pour": "Anne", "nom": "Délai", "date": LATER.isoformat()}]}, voyage=tid)
    assert any("Dates de paiement du voyage : Acompte : 80,00" in a.replace("\xa0", " ") for a in out["actions"])
    assert any("Dates de paiement de Anne : Délai : tout ce qui reste" in a for a in out["actions"])
    d = ops.trip(tid)
    assert d["dates_paiement"] == [
        {"nom": "Acompte", "date": PAST.isoformat(), "montant": 80.0},
        {"nom": "2e paiement", "date": SOON.isoformat(), "montant": 50.0},
        {"nom": "Solde", "date": LATER.isoformat(), "montant": None},
        {"pour": "Anne", "nom": "Délai", "date": LATER.isoformat(), "montant": None}]
    states = {x["qui"]: x for x in d["paiements"]}
    assert states["Anne"] == {"qui": "Anne", "etat": "à jour", "reste": 165.0, "prochain_paiement": 165.0,
                              "au_plus_tard": LATER.isoformat()}
    assert states["Bruno"] == {"qui": "Bruno", "etat": "soldé", "reste": 0.0}
    assert states["Famille Martin"] == {"qui": "Famille Martin", "etat": "en retard", "reste": 330.0,
                                        "en_retard_de": 60.0, "depuis": PAST.isoformat(), "jours_de_retard": 10,
                                        "prochain_paiement": 100.0, "au_plus_tard": SOON.isoformat()}
    assert any("1 paiement en retard" in a for a in d["alertes"])
    # une simulation ne change rien
    ops.set_pay_dates(tid, [{"date": PAST.isoformat(), "montant": "10"}], simulation=True)
    assert ops.trip(tid)["dates_paiement"][0]["montant"] == 80.0
    # les erreurs sont dites en clair, et regroupées dans un import
    for bad, words in (([{"montant": "80"}], "il manque la date"), ([{"date": "32/13/2027"}], "invalide"),
                       ([{"date": PAST.isoformat(), "pour": "Zoé"}], "Zoé"), ("demain", "liste"),
                       ([{"date": PAST.isoformat()}, {"date": LATER.isoformat()}], "qu'une fois")):
        with pytest.raises(OpsError) as err:
            ops.set_pay_dates(tid, bad)
        assert words in str(err.value), str(err.value)
    with pytest.raises(OpsError) as err:
        ops.import_trip({"voyage_id": tid, "dates_paiement": [{"montant": "80"}]}, voyage=tid)
    assert "Dates de paiement :" in str(err.value.errors)
    # l'API
    r = cl.post(f"/api/voyages/{tid}/dates-paiement", json={"dates": [{"nom": "Tout", "date": PAST.isoformat()}]})
    assert r.status_code == 200 and "Dates de paiement du voyage : Tout : tout ce qui reste" in r.json()["message"]
    assert plans(app, tid)["Chloé"]["late"] == 13500
    guide = (Path(S.__file__).parent / "static" / "ia" / "guide_ia.md").read_text(encoding="utf-8")
    assert '"dates_paiement"' in guide and "`paiements`" in guide and "N'invente pas de dates" in guide


def test_dates_travel_with_an_exported_trip_but_not_with_a_copy(trip):
    app, cl, ops, tid = trip
    set_dates(cl, tid)
    set_dates(cl, tid, pour=pid(app, tid, "Anne"), rows=[("Délai", LATER, "")])
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        data = TR.export_zip(s, [t], app.state.db.receipts_dir)
        trips, _ = TR.import_zip(s, data, app.state.db.receipts_dir)
        s.commit()
        new = trips[0].id
        copy = TR.duplicate_trip(s, t, "Week-end 2028")
        s.commit()
        copy_id = copy.id
    assert new != tid
    p = plans(app, new)
    assert p["Anne"]["own"] and p["Anne"]["label"] == "à jour"                 # ses dates l'ont suivie (nouveau numéro)
    assert p["Famille Martin"]["late"] == 6000 and p["Chloé"]["own"] is False
    with app.state.db.Session() as s:
        assert set(S.pay_dates(s.get(M.Trip, new))["comptes"]) == {pid(app, new, "Anne")}
        assert s.get(M.Trip, copy_id).pay_dates == ""                           # un nouveau voyage a d'autres dates


# ---------------------------------------------------------------- dans le navigateur
def test_in_the_browser(trip):
    import uvicorn
    from playwright.sync_api import expect, sync_playwright
    app, cl, ops, tid = trip
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(locale="fr-FR", viewport={"width": 1100, "height": 900})
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"{base}/v/{tid}/argent")
            page.get_by_role("link", name="Fixer des dates de paiement").click()
            assert page.locator(".today-btn").count() == 0
            page.fill("#date_0", PAST.isoformat())
            page.fill("#montant_0", "80")
            page.fill("#date_1", LATER.isoformat())
            page.get_by_role("button", name="Enregistrer").click()
            page.wait_for_url(re.compile(r"/argent\?msg="))
            card = page.locator("#relances")
            expect(card).to_be_visible()
            expect(card.locator(".relance")).to_have_count(2)
            expect(card).to_contain_text("90,00")
            row = card.locator("#relance-f1")
            expect(row.locator(".sent-mark")).to_be_hidden()
            # le message retouché part bien retouché, par WhatsApp comme par e-mail
            row.locator("summary").click()
            area = row.locator("textarea")
            assert area.input_value().startswith("Bonjour Paul, un petit rappel")
            area.fill("Coucou Paul, pense à l'acompte !")
            row.locator(".share-btn").click()
            menu = row.locator(".share-menu")
            expect(menu).to_be_visible()
            wa = menu.locator('a[href^="https://wa.me/"]').get_attribute("href")
            assert wa == "https://wa.me/33612345678?text=" + "Coucou%20Paul%2C%20pense%20%C3%A0%20l'acompte%20!"
            mail = menu.locator('a[href^="mailto:"]').get_attribute("href")
            assert mail.startswith("mailto:paul@example.org?subject=Rappel%20de%20paiement") and "Coucou%20Paul" in mail
            assert menu.locator("text=Télécharger le PDF").count() == 0          # une relance : pas de PDF à joindre
            ctx.grant_permissions(["clipboard-read", "clipboard-write"])
            menu.get_by_role("menuitem", name="Copier le message").click()
            expect(row.locator(".sent-mark")).to_be_visible()                     # « ✓ préparé »
            row.locator("[data-msg-reset]").click()
            assert area.input_value().startswith("Bonjour Paul, un petit rappel")
            # noter le paiement de la famille depuis la ligne : elle quitte la carte
            page.locator('[data-toggle-row="#pay-versement-%d"]' % pid(app, tid, "Paul Martin")).first.click()
            pay = page.locator("#pay-versement-%d" % pid(app, tid, "Paul Martin"))
            pay.locator('input[name="amount"]').fill("60")
            pay.get_by_role("button", name="Enregistrer").click()
            page.wait_for_url(re.compile(r"/argent\?msg="))
            expect(page.locator("#relances .relance")).to_have_count(1)
            expect(page.locator("#relances")).to_contain_text("Anne")
            # écran étroit, thème sombre : rien ne déborde
            narrow = browser.new_context(locale="fr-FR", viewport={"width": 420, "height": 800}, color_scheme="dark")
            pg = narrow.new_page()
            for url in (f"{base}/v/{tid}/argent", f"{base}/v/{tid}/argent/dates"):
                pg.goto(url)
                assert pg.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"), url
            assert not errors, errors
            browser.close()
    finally:
        srv.should_exit = True
        th.join(5)
