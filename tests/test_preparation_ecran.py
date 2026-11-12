"""Préparer un voyage, tranche 2 : les écrans.

Le projet de référence de la tranche 1 (voir test_preparation.py) est construit par les formulaires, puis les
montants AFFICHÉS sont vérifiés (calculés à la main dans le brief) :
- 893,00 € par personne à 10, 926,67 € à 9 ;
- Forfait Club A : « −250,00 € au total, −25,00 € par personne » ; Air France : « +1 100,00 € au total, +110,00 € par
  personne » ;
- marge 5 % : 937,65 € par personne et 446,50 € de marge ; prix annoncé 950,00 € : 570,00 € de côté ; 880,00 € : il
  manque 130,00 € ;
- retenir le forfait puis Hôtel X : Plongées « pas encore choisi », budget 5 130,00 €.
"""
import html
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from compta import models as M
from compta.web import create_app

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "compta" / "templates"
FORBIDDEN = re.compile(r"\b(lots?|forfaitaires?|quote-parts?|ventilations?|scénarios?|comptabilité)\b", re.I)


def clean(t):
    """Le texte visible d'une page : sans balises ni scripts, espaces insécables remplacées."""
    t = re.sub(r"<(script|style)\b.*?</\1>", " ", t, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html.unescape(t).replace("\xa0", " ").replace(" ", " "))


def message(r):
    msg = parse_qs(urlparse(r.headers["location"]).query).get("msg", [""])[0]
    return msg.replace("\xa0", " ").replace("\u202f", " ")


def refused(r):
    return r.status_code == 303 and "e=1" in r.headers["location"]


class Screen:
    """Un projet construit à l'écran, comme le ferait l'organisateur."""

    def __init__(self, tmp_path):
        self.app = create_app(tmp_path / "donnees")
        self.db = self.app.state.db
        self.cl = TestClient(self.app)

    def post(self, url, data=None, **kw):
        return self.cl.post(url, data=data or {}, follow_redirects=False, **kw)

    def text(self, url=None):
        r = self.cl.get(url or self.url)
        assert r.status_code == 200, (url, r.status_code)
        return clean(r.text)

    def create(self, name="Mer Rouge 2027", people="10", place="Hurghada", **extra):
        r = self.post("/preparer", dict(name=name, people=people, place=place, **extra))
        assert r.status_code == 303 and not refused(r), message(r)
        self.url = r.headers["location"].split("?")[0]
        self.pid = int(self.url.rsplit("/", 1)[1])
        return r

    def plan(self, s):
        return s.get(M.Plan, self.pid)

    def item(self, name):
        with self.db.Session() as s:
            return next(i.id for i in self.plan(s).items if i.name == name)

    def offer(self, label):
        with self.db.Session() as s:
            return next(o.id for o in self.plan(s).offers if o.label == label)

    def add_item(self, name):
        r = self.post(f"{self.url}/postes", dict(name=name))
        assert not refused(r), message(r)
        return r

    def add_offer(self, label, price, counting, items, quantity="1", quantity_label="", capacity="", **extra):
        data = dict(label=label, price=price, counting=counting, quantity=quantity, quantity_label=quantity_label,
                    capacity=capacity, items=[str(self.item(i)) for i in items], **extra)
        r = self.post(f"{self.url}/offres", data)
        assert not refused(r), message(r)
        return r

    def choose(self, label):
        r = self.post(f"{self.url}/offres/{self.offer(label)}/retenir")
        assert not refused(r), message(r)
        return r

    def reference(self):
        """Le projet de référence, saisi à l'écran. Retenues : Transavia, Taxi, Hôtel X, Club B, Minibus."""
        self.create()
        for n in ("Avion", "Taxi", "Hôtel", "Plongées", "Minibus"):
            self.add_item(n)
        self.add_offer("Transavia", "210", "personne", ["Avion"])
        self.add_offer("Air France", "320,00", "personne", ["Avion"])
        self.add_offer("Taxi", "60", "nombre", ["Taxi"], capacity="4")
        self.add_offer("Hôtel X", "90", "nombre", ["Hôtel"], capacity="2", quantity="5", quantity_label="nuits")
        self.add_offer("Club B", "38", "personne", ["Plongées"], quantity="10", quantity_label="plongées")
        self.add_offer("Minibus", "600", "groupe", ["Minibus"])
        self.add_offer("Forfait Club A", "580", "personne", ["Hôtel", "Plongées"])
        for o in ("Transavia", "Taxi", "Hôtel X", "Club B", "Minibus"):
            self.choose(o)
        return self


@pytest.fixture
def screen(tmp_path):
    return Screen(tmp_path)


def offer_block(page_html, oid):
    """Le texte d'une offre (sa première apparition, sous son premier poste)."""
    m = re.search(rf'<li class="plan-offer[^"]*" id="offre-{oid}">(.*?)</li>', page_html, flags=re.S)
    assert m, oid
    return clean(m.group(1))


def item_block(page_html, iid):
    m = re.search(rf'<section class="card plan-item" id="poste-{iid}"(.*?)</section>', page_html, flags=re.S)
    assert m, iid
    return clean(m.group(1))


# ---------------------------------------------------------------- le scénario complet, montants affichés

def test_reference_project_amounts_on_screen(screen):
    sc = screen.reference()
    page = sc.cl.get(sc.url).text
    t = clean(page)
    assert "Budget total 8 930,00 €" in t
    assert "Ça revient à 893,00 € par personne." in t
    assert "Budget incomplet" not in t
    # le prix de chaque offre, en clair, et son total
    assert "210,00 € par personne" in offer_block(page, sc.offer("Transavia"))
    assert "2 100,00 € pour le groupe · 210,00 € par personne" in offer_block(page, sc.offer("Transavia"))
    assert "60,00 € pour 4 personnes (il en faut 3 pour 10 personnes)" in offer_block(page, sc.offer("Taxi"))
    assert "180,00 € pour le groupe · 18,00 € par personne" in offer_block(page, sc.offer("Taxi"))
    assert "90,00 € pour 2 personnes × 5 nuits (il en faut 5 pour 10 personnes)" in offer_block(page, sc.offer("Hôtel X"))
    assert "2 250,00 € pour le groupe" in offer_block(page, sc.offer("Hôtel X"))
    assert "38,00 € par personne × 10 plongées" in offer_block(page, sc.offer("Club B"))
    assert "600,00 € pour tout le groupe" in offer_block(page, sc.offer("Minibus"))
    assert "600,00 € pour le groupe · 60,00 € par personne" in offer_block(page, sc.offer("Minibus"))
    # ce qui est retenu, poste par poste
    assert "Transavia · 2 100,00 €" in item_block(page, sc.item("Avion"))
    assert "Taxi · 180,00 €" in item_block(page, sc.item("Taxi"))
    # « si tu la retiens »
    af = offer_block(page, sc.offer("Air France"))
    assert "+1 100,00 € au total, +110,00 € par personne" in af and "remplace Transavia" in af
    assert "Retenir" in af and "✓ Retenue" not in af
    fa = offer_block(page, sc.offer("Forfait Club A"))
    assert "−250,00 € au total, −25,00 € par personne" in fa
    assert "remplace Hôtel X et Club B" in fa and "sans offre" not in fa
    assert "couvre aussi Plongées" in fa
    tr = offer_block(page, sc.offer("Transavia"))
    assert "✓ Retenue" in tr and "Ne plus retenir" in tr and "Si tu la retiens" not in tr
    # le forfait apparaît aussi sous Plongées, avec « couvre aussi Hôtel »
    assert "Forfait Club A" in item_block(page, sc.item("Plongées"))
    assert "couvre aussi Hôtel" in item_block(page, sc.item("Plongées"))


def test_people_margin_and_announced_price_on_screen(screen):
    sc = screen.reference()
    t = sc.text()
    assert "Pas de marge pour l'instant." in t and "0,00 € de marge" not in t
    assert "Pas de prix annoncé. Repère : 893,00 € par personne." in t
    r = sc.post(f"{sc.url}/participants", dict(people="9"))
    assert message(r) == "Calculé pour 9 personnes"
    t = sc.text()
    assert "Budget total 8 340,00 €" in t and "Ça revient à 926,67 € par personne." in t
    sc.post(f"{sc.url}/participants", dict(people="10"))
    r = sc.post(f"{sc.url}/marge", dict(margin_kind="pourcent", margin_value="5"))
    assert message(r) == "Marge : 5 %"
    t = sc.text()
    assert "Avec la marge : 937,65 € par personne, soit 446,50 € de marge." in t
    assert "Pas de prix annoncé. Repère : avec ta marge, 937,65 € par personne." in t
    sc.post(f"{sc.url}/marge", dict(margin_kind="euros", margin_value="20"))
    assert "Avec la marge : 913,00 € par personne, soit 200,00 € de marge." in sc.text()
    sc.post(f"{sc.url}/marge", dict(margin_kind="pourcent", margin_value="5,5"))
    assert "Avec la marge : 942,12 € par personne" in sc.text()     # 8 930 × 5,5 % = 491,15 ; 9 421,15 / 10
    r = sc.post(f"{sc.url}/prix-annonce", dict(announced="950"))
    assert message(r) == "Prix annoncé : 950,00 €"
    assert "À ce prix, il te reste 570,00 € de côté (57,00 € par personne)." in sc.text()
    sc.post(f"{sc.url}/prix-annonce", dict(announced="880,00"))
    page = sc.cl.get(sc.url).text
    assert re.search(r'<p class="plan-alert" id="reserve" role="alert">', page)
    assert "À ce prix, il manque 130,00 € (13,00 € par personne)." in clean(page)
    sc.post(f"{sc.url}/participants", dict(people="9"))
    sc.post(f"{sc.url}/prix-annonce", dict(announced="950"))
    assert "il te reste 210,00 € de côté" in sc.text()
    r = sc.post(f"{sc.url}/prix-annonce", dict(announced=""))
    assert message(r) == "Prix annoncé retiré" and "Pas de prix annoncé" in sc.text()
    # changer les participants n'a touché ni la marge ni le lieu
    with sc.db.Session() as s:
        p = sc.plan(s)
        assert (p.people, p.margin_kind, p.margin_value, p.place) == (9, "pourcent", 550, "Hurghada")


def test_choose_package_then_hotel_leaves_dives_unchosen(screen):
    sc = screen.reference()
    r = sc.choose("Forfait Club A")
    assert message(r) == "Offre « Forfait Club A » retenue (à la place de « Hôtel X », « Club B »)"
    page = sc.cl.get(sc.url).text
    t = clean(page)
    assert "Budget total 8 680,00 €" in t and "868,00 € par personne" in t
    assert "compris dans Forfait Club A" in item_block(page, sc.item("Hôtel"))
    assert "compris dans Forfait Club A" in item_block(page, sc.item("Plongées"))
    h = offer_block(page, sc.offer("Hôtel X"))
    assert "Si tu la retiens : remplace Forfait Club A, et Plongées n'aurait plus d'offre (le budget serait incomplet)." in h
    assert "3 550,00" not in h and "355,00" not in h and "down-text" not in page.split(f'id="offre-{sc.offer("Hôtel X")}"')[1].split("</li>")[0]
    assert not h.rstrip().endswith("·")
    sc.choose("Hôtel X")
    page = sc.cl.get(sc.url).text
    t = clean(page)
    assert "Budget total 5 130,00 €" in t
    assert "Budget incomplet : il manque Plongées" in t
    assert "Pour l'instant, ça revient à 513,00 € par personne (sans Plongées)." in t
    assert "pas encore choisi" in item_block(page, sc.item("Plongées"))
    assert "Hôtel X · 2 250,00 €" in item_block(page, sc.item("Hôtel"))


def test_package_split_shows_each_part(screen):
    sc = screen.reference()
    oid, h, p = sc.offer("Forfait Club A"), sc.item("Hôtel"), sc.item("Plongées")
    r = sc.post(f"{sc.url}/offres/{oid}", dict(label="Forfait Club A", price="580", counting="personne", quantity="1",
                                                items=[str(h), str(p)], split="montant",
                                                **{f"share_{h}": "300", f"share_{p}": "280"}))
    assert not refused(r), message(r)
    sc.choose("Forfait Club A")
    page = sc.cl.get(sc.url).text
    assert "compris dans Forfait Club A · 3 000,00 €" in item_block(page, h)
    assert "compris dans Forfait Club A · 2 800,00 €" in item_block(page, p)
    sc.post(f"{sc.url}/offres/{oid}", dict(label="Forfait Club A", price="580", counting="personne", quantity="1",
                                            items=[str(h), str(p)], split="pourcent",
                                            **{f"share_{h}": "55", f"share_{p}": "45"}))
    page = sc.cl.get(sc.url).text
    assert "· 3 190,00 €" in item_block(page, h) and "· 2 610,00 €" in item_block(page, p)
    # le formulaire de modification revient avec la répartition saisie
    form = sc.cl.get(f"{sc.url}/offres/{oid}/modifier").text
    assert f'name="share_{h}" value="55"' in form and 'value="pourcent" checked' in form


# ---------------------------------------------------------------- refus : le formulaire revient

def test_refusals_return_to_the_form_with_the_field(screen):
    sc = screen.reference()
    h, p = sc.item("Hôtel"), sc.item("Plongées")
    cases = [
        (dict(label="", price="10", counting="personne", items=[str(h)]), "label", "nom à l'offre"),
        (dict(label="X", price="-5", counting="personne", items=[str(h)]), "price", "négatif"),
        (dict(label="X", price="abc", counting="personne", items=[str(h)]), "price", "Montant invalide"),
        (dict(label="X", price="", counting="personne", items=[str(h)]), "price", "Indique le prix"),
        (dict(label="X", price="10", counting="nombre", capacity="0", items=[str(h)]), "capacity", "Au moins 1"),
        (dict(label="X", price="10", counting="personne", quantity="0", items=[str(h)]), "quantity", "quantité"),
        (dict(label="X", price="10", counting="personne", quantity="deux", items=[str(h)]), "quantity", "nombre entier"),
        (dict(label="X", price="10", counting="personne"), "items", "au moins un poste"),
        (dict(label="X", price="580", counting="personne", items=[str(h), str(p)], split="montant",
              **{f"share_{h}": "300", f"share_{p}": "270"}), "shares", "580,00"),
        (dict(label="X", price="580", counting="personne", items=[str(h), str(p)], split="pourcent",
              **{f"share_{h}": "60", f"share_{p}": "30"}), "shares", "100 %"),
    ]
    before = sc.db.undo_token()
    for data, field, words in cases:
        r = sc.post(f"{sc.url}/offres", data)
        assert refused(r), data
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q["f"] == [field] and words in q["msg"][0], (data, q)
        assert urlparse(r.headers["location"]).path == f"{sc.url}/offres/nouvelle", r.headers["location"]
    assert sc.db.undo_token() == before                     # aucune copie « Défaire » laissée par un refus
    with sc.db.Session() as s:
        assert len(sc.plan(s).offers) == 7
    # modification refusée : retour au formulaire de cette offre
    oid = sc.offer("Taxi")
    r = sc.post(f"{sc.url}/offres/{oid}", dict(label="Taxi", price="60", counting="nombre", capacity="", items=[str(sc.item("Taxi"))]))
    assert refused(r) and urlparse(r.headers["location"]).path == f"{sc.url}/offres/{oid}/modifier"
    # projet
    for url, data, field in [("/preparer", dict(name="", people="10"), "name"),
                             ("/preparer", dict(name="X", people="0"), "people"),
                             ("/preparer", dict(name="X", people=""), "people"),
                             ("/preparer", dict(name="X", people="10", start="2027-05-10", end="2027-05-01"), "end"),
                             (f"{sc.url}/participants", dict(people="0"), "people"),
                             (f"{sc.url}/marge", dict(margin_kind="pourcent", margin_value="-3"), "margin_value"),
                             (f"{sc.url}/marge", dict(margin_kind="pourcent", margin_value="cinq"), "margin_value"),
                             (f"{sc.url}/prix-annonce", dict(announced="-1"), "announced"),
                             (f"{sc.url}/modifier", dict(name="  "), "name"),
                             (f"{sc.url}/postes", dict(name="hotel"), "name"),
                             (f"{sc.url}/postes", dict(name=""), "name")]:
        r = sc.post(url, data)
        assert refused(r), (url, data)
        assert parse_qs(urlparse(r.headers["location"]).query)["f"] == [field], (url, data, r.headers["location"])
    assert sc.db.undo_token() == before
    r = sc.post("/preparer", dict(name="X", people="0"))
    assert r.headers["location"].startswith("/?preparer=1&")       # l'accueil rouvre le panneau du projet
    with sc.db.Session() as s:
        assert s.query(M.Plan).count() == 1 and sc.plan(s).people == 10


def test_a_refused_form_page_can_be_opened_again(screen):
    """L'adresse du refus mène bien au formulaire (le navigateur y remet la saisie, voir test_formulaire_refuse)."""
    sc = screen.reference()
    r = sc.post(f"{sc.url}/offres", dict(label="", price="10", counting="personne", items=[str(sc.item("Taxi"))]))
    page = sc.cl.get(r.headers["location"]).text
    assert f'action="{sc.url}/offres"' in page and "Nouvelle offre" in clean(page)
    assert f'value="{sc.item("Taxi")}" data-name="Taxi" checked' in page          # le poste reste coché


# ---------------------------------------------------------------- défaire / rétablir depuis l'écran

def test_undo_and_redo_from_the_screen(screen):
    sc = screen.reference()
    r = sc.choose("Forfait Club A")
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q.get("d"), "le message propose « Défaire »"
    page = clean(sc.cl.get(r.headers["location"]).text)
    assert "↶ Défaire" in page
    assert sc.db.last_action() == "offre « Forfait Club A » retenue"
    r = sc.post("/annuler", dict(d=q["d"][0]), headers={"referer": f"http://testserver{sc.url}"})
    assert message(r) == "Défait : offre « Forfait Club A » retenue."
    assert urlparse(r.headers["location"]).path == sc.url
    assert "Budget total 8 930,00 €" in sc.text()
    rq = parse_qs(urlparse(r.headers["location"]).query)
    r = sc.post("/retablir", dict(r=rq["r"][0]), headers={"referer": f"http://testserver{sc.url}"})
    assert message(r) == "Rétabli : offre « Forfait Club A » retenue."
    assert "Budget total 8 680,00 €" in sc.text()
    # les libellés de « Défaire » se lisent sans le code sous les yeux
    labels = [h["label"] for h in sc.db.history()][:12]
    assert "offre « Forfait Club A » retenue" in labels
    assert "offre « Minibus » ajoutée à « Mer Rouge 2027 »" in labels
    sc.post(f"{sc.url}/postes/{sc.item('Taxi')}/supprimer")
    assert sc.db.last_action() == "poste « Taxi » supprimé"
    sc.post(f"{sc.url}/participants", dict(people="9"))
    assert sc.db.last_action() == "« Mer Rouge 2027 » : 9 personnes"


def test_undoing_the_creation_goes_back_home(screen):
    sc = screen
    sc.create()
    r = sc.post("/annuler", {}, headers={"referer": f"http://testserver{sc.url}"})
    assert message(r) == "Défait : création du voyage en préparation « Mer Rouge 2027 »."
    assert urlparse(r.headers["location"]).path == "/"
    # depuis le formulaire d'une offre, « Défaire » ramène à la page du projet
    sc.create(name="Autre")
    sc.add_item("Avion")
    r = sc.post("/annuler", {}, headers={"referer": f"http://testserver{sc.url}/offres/nouvelle?poste=1"})
    assert urlparse(r.headers["location"]).path == sc.url


# ---------------------------------------------------------------- postes : ordre, renommer, supprimer

def test_ordering_renaming_and_deleting_items(screen):
    sc = screen.reference()
    order = lambda: [i for i in re.findall(r'<h3 id="poste-\d+-h">([^<]+)</h3>', sc.cl.get(sc.url).text)]   # noqa: E731
    assert order() == ["Avion", "Taxi", "Hôtel", "Plongées", "Minibus"]
    r = sc.post(f"{sc.url}/postes/{sc.item('Minibus')}/monter")
    assert message(r) == "« Minibus » monté"
    assert order() == ["Avion", "Taxi", "Hôtel", "Minibus", "Plongées"]
    sc.post(f"{sc.url}/postes/{sc.item('Avion')}/descendre")
    assert order() == ["Taxi", "Avion", "Hôtel", "Minibus", "Plongées"]
    r = sc.post(f"{sc.url}/postes/{sc.item('Taxi')}/monter")              # déjà en tête : rien ne change
    assert order()[0] == "Taxi" and "d=" not in r.headers["location"]
    page = sc.cl.get(sc.url).text
    assert re.search(r'aria-label="Monter « Taxi »" disabled', page)
    assert re.search(r'aria-label="Descendre « Plongées »" disabled', page)
    r = sc.post(f"{sc.url}/postes/{sc.item('Minibus')}/renommer", dict(name="Minibus sur place"))
    assert message(r) == "Poste renommé : « Minibus sur place »"
    assert "Minibus sur place" in order()
    # supprimer l'Hôtel : la confirmation nomme ce qui part et ce qui reste
    page = sc.cl.get(sc.url).text
    confirm = re.search(rf'postes/{sc.item("Hôtel")}/supprimer" class="inline" data-confirm="([^"]+)"', page).group(1)
    confirm = html.unescape(confirm)
    assert confirm == ("Supprimer le poste « Hôtel » ? L'offre « Hôtel X » part avec lui. « Forfait Club A » reste pour "
                       "ses autres postes (sans sa répartition).")
    r = sc.post(f"{sc.url}/postes/{sc.item('Hôtel')}/supprimer")
    assert message(r) == "Poste « Hôtel » supprimé avec l'offre « Hôtel X »"
    page = sc.cl.get(sc.url).text
    assert "Hôtel X" not in clean(page)
    fa = offer_block(page, sc.offer("Forfait Club A"))
    assert "couvre aussi" not in fa                                          # le forfait ne couvre plus que Plongées
    assert "Budget total 6 680,00 €" in clean(page)                          # 8 930 − 2 250


def test_suggested_items_and_empty_project(screen):
    sc = screen
    sc.create(name="Week-end", people="4", place="")
    t = sc.text()
    assert "Pour commencer, trois étapes" in t and "Ajoute tes postes" in t and "Retiens une offre par poste" in t
    page = sc.cl.get(sc.url).text
    assert '<ol class="plan-steps">' in page                       # numéros visibles (liste numérotée ordinaire)
    assert "Ajoute tes postes : ce qu'il faut payer" in t
    result = clean(re.search(r'<section class="card plan-result".*?</section>', page, re.S).group(0))
    assert "Le prix par personne apparaîtra dès que tu auras retenu une offre." in result
    assert "0,00" not in result
    for s in ("Transport aller", "Hébergement", "Repas", "Activités", "Transport sur place", "Transport retour",
              "Assurance"):
        assert f">{s}</button>" in sc.cl.get(sc.url).text
    sc.add_item("Hébergement")
    page = sc.cl.get(sc.url).text
    assert ">Hébergement</button>" not in page                     # une idée déjà prise n'est plus proposée
    assert "Pour commencer, trois étapes" not in clean(page)
    t = clean(page)
    assert "pas encore choisi" in t and "Le prix par personne apparaîtra dès que tu auras retenu une offre." in t
    assert "Aucune offre retenue pour l'instant." in t
    result = clean(re.search(r'<section class="card plan-result".*?</section>', page, re.S).group(0))
    assert "€" not in result.replace("en € par personne", "")         # aucun montant tant que rien n'est retenu
    assert "Pas encore d'offre pour ce poste." in clean(page)
    assert f'/offres/nouvelle?poste={sc.item("Hébergement")}' in page


# ---------------------------------------------------------------- accueil

def test_home_without_projects_and_first_use(screen):
    sc = screen
    t = sc.text("/")
    assert "Bienvenue" in t
    assert "Prépare-le d'abord" in t and "compare des prix et trouve un prix par personne" in t
    page = sc.cl.get("/").text
    assert 'action="/preparer"' in page
    sc.post("/voyages", dict(name="Hendaye", caisse="asso"))
    page = sc.cl.get("/").text
    t = clean(page)
    assert "+ Préparer un voyage" in t and "+ Nouveau voyage" in t and "Importer un voyage" in t
    assert "Préparer 0" in t and "Faire la compta 1" in t                 # les deux onglets, avec leur nombre
    assert 'id="onglet-preparer" aria-labelledby="tab-preparer" hidden' in page   # pas de projet : la compta d'abord
    assert "Pas encore de voyage en préparation" in t and "comparer des prix" in t
    assert 'id="preparer" class="home-panel"' in page


def test_home_lists_projects(screen):
    sc = screen.reference()
    sc.choose("Forfait Club A")
    sc.choose("Hôtel X")                                     # Plongées n'a plus d'offre retenue
    sc.create(name="Week-end Annecy", people="6", place="", start="2027-06-12", end="2027-06-14")
    sc.add_item("Gîte")
    sc.add_item("Repas")
    page = sc.cl.get("/").text
    t = clean(page)
    assert "Préparer 2" in t and 'id="onglet-compta" aria-labelledby="tab-compta" hidden' in page   # onglet ouvert
    rows = [clean(x) for x in re.findall(r'<tr class="home-row plan-row">(.*?)</tr>', page, re.S)]
    assert [r.split()[0] for r in rows] == ["Week-end", "Mer"]                # le plus récent d'abord
    assert " ".join(rows[1].split()) == "Mer Rouge 2027 Hurghada à fixer 10 4 sur 5 — 513,00 €"
    assert 'title="Pas encore choisi : Plongées"' in page
    assert " ".join(rows[0].split()) == "Week-end Annecy 12/06/2027 → 14/06/2027 6 0 sur 2 — —"   # rien de retenu :
    # pas de prix (il serait de 0,00 €)
    assert 'href="/preparer/1"' in page and 'href="/preparer/2"' in page
    # sans voyage, avec des projets : l'accueil ordinaire, pas la page de bienvenue
    assert "Bienvenue" not in t and "Pas encore de voyage en cours" in t


def test_deleting_a_project(screen):
    sc = screen.reference()
    page = sc.cl.get(sc.url).text
    confirm = html.unescape(re.search(r'action="/preparer/1/supprimer" class="inline"\s+data-confirm="([^"]+)"', page).group(1))
    assert confirm == ("Supprimer le projet « Mer Rouge 2027 » ? Ses 5 postes et ses 7 offres partent avec lui. "
                       "Tu pourras encore annuler juste après.")
    r = sc.post(f"{sc.url}/supprimer")
    assert message(r) == "Projet « Mer Rouge 2027 » supprimé (tu peux encore annuler)"
    assert sc.cl.get(sc.url).status_code == 404
    assert "Mer Rouge 2027" not in sc.text("/")
    sc.post("/annuler", {}, headers={"referer": "http://testserver/"})
    assert "Budget total 8 930,00 €" in sc.text()


def test_unknown_project_item_or_offer_is_a_plain_404(screen):
    sc = screen.reference()
    for url in ("/preparer/99", f"{sc.url}/offres/99/modifier"):
        r = sc.cl.get(url)
        assert r.status_code == 404 and "n'existe pas" in clean(r.text)
    for url in (f"{sc.url}/offres/99/retenir", f"{sc.url}/postes/99/supprimer", "/preparer/99/supprimer"):
        assert sc.post(url).status_code == 404
    other = Screen.__new__(Screen)
    vars(other).update(vars(sc))
    other.create(name="Autre")
    # une offre d'un autre projet, par l'adresse de celui-ci : introuvable
    assert sc.post(f"{other.url}/offres/{sc.offer('Transavia')}/retenir").status_code == 404


def test_live_preview(screen):
    sc = screen.reference()
    r = sc.cl.post(f"{sc.url}/offres/apercu", data=dict(price="60", counting="nombre", capacity="4", quantity="1"))
    assert clean(r.text).strip() == "Pour 10 personnes : 180,00 € au total, soit 18,00 € par personne."
    r = sc.cl.post(f"{sc.url}/offres/apercu", data=dict(price="", counting="personne"))
    assert "Indique un prix" in r.text
    r = sc.cl.post(f"{sc.url}/offres/apercu", data=dict(price="abc", counting="personne"))
    assert "Montant invalide" in r.text
    before = sc.db.undo_token()
    sc.cl.post(f"{sc.url}/offres/apercu", data=dict(price="10", counting="personne"))
    assert sc.db.undo_token() == before                         # l'aperçu n'écrit rien


# ---------------------------------------------------------------- vocabulaire et aide

def test_no_forbidden_words_in_templates_or_screens(screen):
    for name in ("plan.html", "plan_offer.html", "index.html", "help.html"):
        src = (TEMPLATES / name).read_text(encoding="utf-8")
        assert not FORBIDDEN.search(src), (name, FORBIDDEN.search(src).group(0))
    src = (ROOT / "compta" / "pages" / "preparation.py").read_text(encoding="utf-8")
    shown = " ".join(re.findall(r'f?"([^"]*)"', src))
    assert not FORBIDDEN.search(re.sub(r"P\.LOT|\"lot\"", "", shown)), FORBIDDEN.search(shown)
    sc = screen.reference()
    sc.choose("Forfait Club A")
    for url in ("/", sc.url, f"{sc.url}/offres/nouvelle?poste={sc.item('Taxi')}",
                f"{sc.url}/offres/{sc.offer('Taxi')}/modifier", f"{sc.url}/offres/{sc.offer('Forfait Club A')}/modifier",
                "/aide"):
        t = sc.text(url)
        assert not FORBIDDEN.search(t), (url, FORBIDDEN.search(t).group(0))


def test_help_explains_preparing_a_trip(screen):
    t = screen.text("/aide")
    assert "Préparer un voyage" in t and "+ Préparer un voyage" in t
    for word in ("Un voyage en préparation", "Un poste", "Une offre", "L'offre retenue", "Le prix par personne",
                 "La marge", "Le prix annoncé"):
        assert word in t, word


def test_no_forbidden_words_in_messages():
    """Les messages montrés à l'écran (refus, avis après une action, aperçu) : relevés dans le code, tous."""
    import ast

    def said(path, start=None):
        src = path.read_text(encoding="utf-8")
        if start:
            src = src[src.index(start):]
        out = []
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Call):
                fn = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                if fn in ("WriteError", "FormError", "refuse", "back", "HTMLResponse", "ValueError", "write"):
                    for arg in node.args:
                        out += [n.value for n in ast.walk(arg) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        return out

    msgs = (said(ROOT / "compta" / "writes.py", "# ---------------------------------------------------------------- préparer")
            + said(ROOT / "compta" / "pages" / "preparation.py") + said(ROOT / "compta" / "preparation.py"))
    assert len(msgs) > 60
    bad = [m for m in msgs if FORBIDDEN.search(m)]
    assert not bad, bad


def test_incomplete_budget_says_so(screen):
    """Des postes sans offre retenue : chaque phrase chiffrée dit « pour l'instant »."""
    sc = screen.reference()
    sc.post(f"{sc.url}/offres/{sc.offer('Taxi')}/ne-plus-retenir")
    sc.post(f"{sc.url}/offres/{sc.offer('Hôtel X')}/ne-plus-retenir")
    t = sc.text()                                          # 8 930 − 180 − 2 250 = 6 500
    assert "Budget total 6 500,00 €" in t and "Budget incomplet : il manque Taxi, Hôtel" in t
    assert "Pour l'instant, ça revient à 650,00 € par personne (sans Taxi ni Hôtel)." in t
    assert "Pas de prix annoncé. Repère pour l'instant : 650,00 € par personne." in t
    sc.post(f"{sc.url}/marge", dict(margin_kind="pourcent", margin_value="5"))
    t = sc.text()                                          # 6 500 × 5 % = 325 ; 6 825 / 10
    assert "Pour l'instant, avec la marge : 682,50 € par personne, soit 325,00 € de marge." in t
    assert "Repère pour l'instant : avec ta marge, 682,50 € par personne." in t
    sc.post(f"{sc.url}/prix-annonce", dict(announced="700"))
    assert "Pour l'instant, à ce prix, il te reste 500,00 € de côté (50,00 € par personne)." in sc.text()
    assert '<p class="plan-out" id="reserve">' in sc.cl.get(sc.url).text      # pas en vert : rien n'est sûr encore
    sc.post(f"{sc.url}/prix-annonce", dict(announced="600"))
    assert "À ce prix, il manque déjà 500,00 € (50,00 € par personne)." in sc.text()
    sc.post(f"{sc.url}/offres/{sc.offer('Transavia')}/ne-plus-retenir")
    assert "(sans Avion, Taxi ni Hôtel)" in sc.text()
