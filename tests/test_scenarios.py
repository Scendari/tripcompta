"""Scénarios utilisateur complets, joués dans l'application web (comme le ferait un organisateur)."""
import re

import pytest
from fastapi.testclient import TestClient

from compta import engine as E
from compta import models as M
from compta.services import compute_trip
from compta.web import create_app


@pytest.fixture()
def app(tmp_path):
    a = create_app(tmp_path / "data")
    return TestClient(a), a.state.db


class Orga:
    """Petit robot « organisateur » qui clique dans l'application."""

    def __init__(self, client, db):
        self.c, self.db = client, db
        self.tid = None
        self.P = {}

    def get(self, url):
        r = self.c.get(f"/v/{self.tid}{url}")
        assert r.status_code == 200, (url, r.status_code)
        return r.text

    def post(self, url, data=None, files=None, referer=None):
        headers = {"referer": f"http://testserver/v/{self.tid}{referer}"} if referer else {}
        r = self.c.post(f"/v/{self.tid}{url}", data=data or {}, files=files, headers=headers)
        assert r.status_code == 200, (url, r.status_code, r.text[:300])
        return r

    def flash(self, r):
        m = re.search(r'<div class="flash"[^>]*>(.*?)</div>', r.text, re.S)
        return m.group(1).strip() if m else None

    def create_trip(self, name, people):
        r = self.c.post("/voyages", data={"name": name, "start": "2027-06-01", "end": "2027-06-03"})
        self.tid = int(re.search(r"/v/(\d+)", str(r.url)).group(1))
        self.post("/participants", {"names": "\n".join(people)})
        self.P = {p.name: p.id for p in self.trip().participants}

    def trip(self):
        s = self.db.Session()
        self._s = s
        return s.get(M.Trip, self.tid)

    def result(self, mode=E.ACTUAL):
        t = self.trip()
        r = compute_trip(t, mode)
        self._s.close()
        return r

    def expense(self, label, planned=None, actual=None, payer="asso", who=None, weights=None, category="Divers",
                eid=None, files=None, fixed=None):
        who = who or list(self.P)
        data = {"label": label, "category": category, "planned": planned or "", "actual": actual or "",
                "payer": "asso" if payer == "asso" else str(self.P[payer])}
        for n in who:
            data[f"inc_{self.P[n]}"] = "1"
            data[f"w_{self.P[n]}"] = str((weights or {}).get(n, 1))
        for n, v in (fixed or {}).items():          # montant fixé 🔒 dans la dépense
            data[f"m_{self.P[n]}"] = v
            data[f"fx_{self.P[n]}"] = "1"
        return self.post(f"/depenses/{eid}" if eid else "/depenses", data, files=files)

    def eid(self, label):
        t = self.trip()
        e = next(e.id for e in t.expenses if e.label == label)
        self._s.close()
        return e

    def pay(self, name, amount, kind="versement"):
        return self.post("/argent", {"participant": self.P[name], "amount": amount, "kind": kind})

    def balance(self, name, mode=E.ACTUAL):
        return self.result(mode).people[self.P[name]].balance


# --------------------------------------------------------------------------- scénario 1

def test_weekend_from_budget_to_everyone_settled(app):
    """Un week-end : budget prévisionnel, acomptes, montants réels, régularisation, tout le monde soldé."""
    o = Orga(*app)
    o.create_trip("Week-end Groix", ["Anne", "Bob", "Chloé", "David", "Emma"])
    # Budget prévisionnel
    o.expense("Gîte", planned="1000", category="Logement")
    o.expense("Bateau", planned="250", category="Transport / Covoit")
    o.expense("Plongées", planned="600", who=["Anne", "Bob", "Chloé"], category="Plongée")
    r = o.result()
    assert r.total_cost == 185000
    assert r.people[o.P["Anne"]].share == 20000 + 5000 + 20000
    assert r.people[o.P["David"]].share == 25000
    # Acomptes de 100 € chacun
    for n in o.P:
        o.pay(n, "100")
    assert o.result().total_paid == 50000
    # Après le voyage : montants réels (le gîte coûte moins cher, les plongées plus)
    o.expense("Gîte", planned="1000", actual="950", category="Logement", eid=o.eid("Gîte"))
    o.expense("Bateau", planned="250", actual="250", category="Transport / Covoit", eid=o.eid("Bateau"))
    o.expense("Plongées", planned="600", actual="630", who=["Anne", "Bob", "Chloé"], category="Plongée",
              eid=o.eid("Plongées"))
    # Bob a avancé le pot de fin (40 €) pour tout le monde
    o.expense("Pot de fin", actual="40", payer="Bob", category="Repas")
    r = o.result()
    assert r.total_cost == 95000 + 25000 + 63000 + 4000
    assert r.people[o.P["Anne"]].share == 19000 + 5000 + 21000 + 800
    assert r.people[o.P["Bob"]].balance == 19000 + 5000 + 21000 + 800 - 10000 - 4000
    # Régularisation visible sur la fiche : écart prévu / réel
    page = o.get(f"/p/{o.P['David']}")
    assert "de moins que prévu" in page or "de plus que prévu" in page
    # « Qui doit quoi à qui » : on clique sur chaque « C'est fait »
    for a, b, c in o.result().settlements():
        kind = "remboursement" if a == E.ASSO else "versement"
        pid = (b if a == E.ASSO else a).referent
        o.post("/argent", {"kind": kind, "participant": pid, "amount": f"{c // 100},{c % 100:02d}"})
    r = o.result()
    assert all(p.balance == 0 for p in r.people.values())
    assert r.to_collect == 0 and r.to_refund == 0
    assert "Tout est réglé" in o.c.get("/").text
    assert "Tout est réglé" in o.get("/argent")
    # Caisse : entrées − sorties = 0 (tout l'argent reçu a servi à payer et rembourser) hors avances
    t = o.trip()
    cash_in = sum(m.amount for m in t.movements if m.kind == "versement")
    cash_out = sum(m.amount for m in t.movements if m.kind == "remboursement") + \
        sum(e.actual for e in t.expenses if e.payer_id is None)
    o._s.close()
    assert cash_in - cash_out == r.rounding_gain


# --------------------------------------------------------------------------- scénario 2

def test_thierry_pays_less_than_luc(app):
    """Le cas de la spec : « Thierry paiera moins que Luc ». Montants fixés 🔒 dans la dépense,
    remise sur tout le voyage sur la fiche du participant. Montants vérifiés à la main."""
    o = Orga(*app)
    o.create_trip("Sortie", ["Thierry", "Luc", "Marie", "Paul"])
    # Gîte 400 € : Thierry fixé à 70 €, Luc à 110 € ; Marie et Paul : (400 − 180) / 2 = 110 €
    o.expense("Gîte", actual="400", category="Logement", fixed={"Thierry": "70", "Luc": "110"})
    gite = o.eid("Gîte")
    r = o.result()
    assert [r.share(o.P[n], gite) for n in ("Thierry", "Luc", "Marie", "Paul")] == [7000, 11000, 11000, 11000]
    # le gîte réel passe à 440 € : les montants fixés ne bougent pas, Marie et Paul absorbent → 130 € chacun
    o.expense("Gîte", actual="440", category="Logement", eid=gite, fixed={"Thierry": "70", "Luc": "110"})
    r = o.result()
    assert [r.share(o.P[n], gite) for n in ("Thierry", "Luc", "Marie", "Paul")] == [7000, 11000, 13000, 13000]
    assert r.is_balanced() and r.asso_total == 0
    # remise de 20 € pour Paul (encadrant), offerte par l'asso, sur sa fiche
    o.post("/remises", {"participant": o.P["Paul"], "kind": "reduce", "value": "20", "reason": "encadrant",
                        "by": "asso"})
    r = o.result()
    assert r.people[o.P["Paul"]].share == 11000 and r.asso_total == 2000 and r.is_balanced()
    assert sum(r.people[p].share for p in o.P.values()) + r.asso_total - r.rounding_gain == 44000
    assert "−20,00 €" in o.get("/participants/soldes").replace("\u202f", " ").replace("\xa0", " ")
    page = o.get("/participants/remises").replace("\u202f", " ").replace("\xa0", " ")
    assert "Remise <b>20,00 €</b>" in page and "encadrant" in page
    # « Qui paie quoi » : cadenas sur les montants fixés, colonne « Remise »
    rep = o.get("/repartition")
    assert "🔒" in rep and ">Remise</th>" in rep
    # supplément de 15 € pour Marie, compensé par les autres ; puis on l'enlève (montant vide)
    o.post("/remises", {"participant": o.P["Marie"], "kind": "increase", "value": "15", "by": "others"})
    assert o.result().people[o.P["Marie"]].share == 14500
    aid = next(a.id for a in o.trip().adjustments if a.participant_id == o.P["Marie"])
    o._s.close()
    o.post(f"/remises/{aid}/supprimer")
    assert o.result().people[o.P["Marie"]].share == 13000
    assert len([a for a in o.trip().adjustments]) == 1        # seule la remise de Paul reste
    o._s.close()


# --------------------------------------------------------------------------- scénario 3

def test_withdrawal_then_comeback(app):
    o = Orga(*app)
    o.create_trip("Sortie", ["Anne", "Bob", "Chloé"])
    o.expense("Acompte gîte", actual="300", category="Logement")
    o.expense("Bateau", actual="90")
    o.pay("Chloé", "150")
    acompte = o.eid("Acompte gîte")
    # Aperçu : Chloé garde l'acompte, le groupe reprend le bateau -> on lui doit 50 €
    pv = o.post("/desistement/apercu", {"participant": o.P["Chloé"], "kept": acompte, "absorber": "group"}).text
    assert "50,00" in pv
    r = o.post("/desistement", {"participant": o.P["Chloé"], "kept": acompte, "absorber": "group",
                                "date": "2027-05-01", "refund": "50"})
    assert "Désistement" in o.flash(r)
    res = o.result()
    assert res.people[o.P["Chloé"]].balance == 0
    assert res.share(o.P["Anne"], o.eid("Bateau")) == 4500
    assert "désisté" in o.get("/")
    row = o.get("/comptes").split(">Chloé")[1].split("</td>")[0]   # Tableau des comptes : la ligne grisée dit pourquoi
    assert 'withdrawn-pill' in row and '>Désisté<' in row
    rep = o.get("/repartition")                                    # Qui paie quoi : sans importance, ni grisé ni étiquette
    assert "withdrawn" not in rep.split(">Chloé")[0].rsplit("<tr", 1)[1] and "withdrawn-pill" not in rep
    # Un second envoi MODIFIE son désistement (pas de doublon) : elle garde l'acompte, comme avant
    r = o.post("/desistement", {"participant": o.P["Chloé"], "kept": acompte, "absorber": "group", "date": "2027-05-01"})
    assert "modifié" in o.flash(r)
    assert len(o.trip().withdrawals) == 1 and o.result().people[o.P["Chloé"]].balance == 0
    # Elle revient finalement : on annule le désistement
    o.post(f"/desistement/{o.P['Chloé']}/annuler")
    res = o.result()
    assert res.share(o.P["Chloé"], o.eid("Bateau")) == 3000
    assert res.people[o.P["Chloé"]].balance == 10000 + 3000 - 15000 + 5000


# --------------------------------------------------------------------------- scénario 4

def test_mistakes_are_caught_with_clear_messages(app):
    o = Orga(*app)
    o.create_trip("Sortie", ["Anne", "Bob"])
    ref = "/depenses/nouvelle"
    # montant illisible
    r = o.post("/depenses", {"label": "Gîte", "actual": "douze", f"inc_{o.P['Anne']}": "1"}, referer=ref)
    assert "Montant invalide" in o.flash(r) and r.url.path.endswith("/depenses/nouvelle")
    # montant négatif
    r = o.post("/depenses", {"label": "Gîte", "actual": "-12", f"inc_{o.P['Anne']}": "1"}, referer=ref)
    assert "positif" in o.flash(r)
    # personne cochée
    r = o.post("/depenses", {"label": "Gîte", "actual": "12"}, referer=ref)
    assert "Coche au moins une personne" in o.flash(r)
    # aucun montant
    r = o.post("/depenses", {"label": "Gîte", f"inc_{o.P['Anne']}": "1"}, referer=ref)
    assert "au moins un montant" in o.flash(r)
    # versement sans montant / négatif
    r = o.post("/argent", {"participant": o.P["Anne"], "amount": "", "kind": "versement"})
    assert "montant positif" in o.flash(r)
    r = o.post("/argent", {"participant": o.P["Anne"], "amount": "abc"}, referer="/argent")
    assert "Montant invalide" in o.flash(r)
    # montant fixé plus grand que la dépense
    r = o.expense("Gîte", actual="100", fixed={"Anne": "150"})
    assert "plus que la dépense" in o.flash(r)
    o.expense("Gîte", actual="100")
    # supprimer quelqu'un qui a déjà payé -> on propose le désistement
    o.pay("Anne", "10")
    r = o.post(f"/participants/{o.P['Anne']}/supprimer")
    assert "désiste" in o.flash(r)
    # doublon de participant ignoré
    r = o.post("/participants", {"names": "anne\nBob"})
    assert "déjà dans la liste" in o.flash(r)
    # rien n'a été cassé
    assert o.result().is_balanced()
    assert len(o.trip().expenses) == 1


# --------------------------------------------------------------------------- scénario 5

def test_undo_several_steps(app):
    o = Orga(*app)
    o.create_trip("Sortie", ["Anne", "Bob"])
    o.expense("Gîte", actual="100")
    o.pay("Anne", "50")
    o.pay("Bob", "50")
    assert o.result().total_paid == 10000
    for expected in (5000, 0):
        r = o.c.post("/annuler", headers={"referer": f"http://testserver/v/{o.tid}/argent"})
        assert "Défait" in r.text
        assert o.result().total_paid == expected
    r = o.c.post("/annuler", headers={"referer": f"http://testserver/v/{o.tid}/argent"})
    assert len(o.trip().expenses) == 0                     # la dépense est annulée à son tour


# --------------------------------------------------------------------------- scénario 6

def test_receipts_and_expense_deletion(app):
    o = Orga(*app)
    o.create_trip("Sortie", ["Anne", "Bob"])
    o.expense("Plongée 1", actual="60")
    o.expense("Plongée 2", actual="60")
    e1, e2 = o.eid("Plongée 1"), o.eid("Plongée 2")
    # une facture pour deux dépenses
    o.post("/justificatifs", {"expenses": [e1, e2]}, files={"files": ("club.pdf", b"%PDF-1.4 x", "application/pdf")})
    t = o.trip()
    rec = t.receipts[0]
    assert rec.code == "J01" and {l.expense_id for l in rec.links} == {e1, e2}
    o._s.close()
    assert "Tout est justifié" in o.get("/justificatifs")
    assert o.c.get(f"/v/{o.tid}/justificatifs/{rec.id}/fichier").content.startswith(b"%PDF")
    # ancien ajustement sur la dépense 1 puis suppression de la dépense : l'ajustement disparaît aussi
    with o.db.Session() as s_:
        s_.add(M.Adjustment(trip_id=o.tid, participant_id=o.P["Anne"], expense_id=e1, value=-1000,
                            compensation="asso"))
        s_.commit()
    o.post(f"/depenses/{e1}/supprimer")
    t = o.trip()
    assert not t.adjustments and len(t.receipts[0].links) == 1
    o._s.close()
    assert o.result().is_balanced()


# --------------------------------------------------------------------------- scénario 7

def test_entre_nous_is_isolated(app):
    o = Orga(*app)
    o.create_trip("Sortie", ["Anne", "Bob", "Chloé"])
    o.expense("Gîte", actual="300")
    before = {n: o.balance(n) for n in o.P}
    r = o.post("/entre-nous", {"name": "Resto", "members": [o.P["Anne"], o.P["Bob"]]})
    sid = int(re.search(r"/entre-nous/(\d+)", str(r.url)).group(1))
    o.post(f"/entre-nous/{sid}/depense", {"label": "Pizza", "amount": "33,33", "payer": o.P["Anne"],
                                          "for": [o.P["Anne"], o.P["Bob"]]})
    page = o.get(f"/entre-nous/{sid}")
    assert "Bob</b> doit <b>16,66" in page or "Bob</b> doit <b>16,67" in page
    # la tuile de la liste a sa bulle : qui participe, les dépenses, qui doit combien à qui
    tiles = o.get("/entre-nous").replace("\u202f", " ").replace("\xa0", " ")
    pop = tiles.split('class="sub-pop"')[1].split('</div>\n  </div>')[0]
    assert "Anne, Bob" in pop and "Chloé" not in pop and "Pizza : 33,33 €" in pop and "payé par Anne" in pop
    assert re.search(r"Bob doit <b>16,6[67] €</b> à Anne", pop)
    assert "1 remboursement à faire" in tiles and "1 dépense ·" in tiles     # pas de « (s) »
    assert {n: o.balance(n) for n in o.P} == before          # aucun effet sur la caisse de l'asso
    assert "Resto" not in o.c.get(f"/v/{o.tid}/export.pdf").text
    # un compte d'une seule personne est refusé
    r = o.post("/entre-nous", {"name": "Solo", "members": [o.P["Anne"]]})
    assert "au moins 2" in o.flash(r)


# --------------------------------------------------------------------------- scénario 8

def test_late_participant_and_rename(app):
    o = Orga(*app)
    o.create_trip("Sortie", ["Anne", "Bob"])
    o.expense("Gîte", actual="100")
    o.expense("Plongée", actual="50", who=["Anne"])
    o.post("/participants", {"names": "Chloé", "join_all": "1"})
    o.P = {p.name: p.id for p in o.trip().participants}
    o._s.close()
    r = o.result()
    gite = [r.share(o.P[n], o.eid("Gîte")) for n in ("Anne", "Bob", "Chloé")]
    assert sorted(gite) == [3333, 3333, 3334]                      # ajoutée au gîte (tout le monde) : 100 / 3, tombe juste
    assert r.people[o.P["Chloé"]].share == r.share(o.P["Chloé"], o.eid("Gîte"))
    assert r.share(o.P["Chloé"], o.eid("Plongée")) == 0            # pas à la plongée d'Anne
    o.post(f"/participants/{o.P['Chloé']}", {"name": "Chloé M.", "phone": "0600000000"})
    assert "Chloé M." in o.get("/") and "Chloé M." in o.get("/repartition")


# --------------------------------------------------------------------------- scénario 9

def test_unequal_shares_nights_and_half_share(app):
    o = Orga(*app)
    o.create_trip("Sortie", ["Anne", "Bob", "Léo"])
    # Anne 3 nuits, Bob 2 nuits, Léo (enfant) demi-part sur 3 nuits = 1,5
    o.expense("Gîte", actual="650", weights={"Anne": 3, "Bob": 2, "Léo": "1,5"})
    r = o.result()
    g = o.eid("Gîte")
    assert r.share(o.P["Anne"], g) == 30000
    assert r.share(o.P["Bob"], g) == 20000
    assert r.share(o.P["Léo"], g) == 15000
    pv = o.post("/depenses/apercu", {"actual": "650", f"inc_{o.P['Anne']}": "1", f"w_{o.P['Anne']}": "3",
                                     f"inc_{o.P['Bob']}": "1", f"w_{o.P['Bob']}": "2",
                                     f"inc_{o.P['Léo']}": "1", f"w_{o.P['Léo']}": "1,5"}).text
    assert "300,00" in pv and "150,00" in pv
    # l'explication du calcul est donnée sur la fiche
    page = o.get(f"/p/{o.P['Léo']}")
    assert "part inégale, 1,5 sur 6,5" in page and "part(s)" not in page
