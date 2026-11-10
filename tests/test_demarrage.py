"""Créer un voyage : réglages neutres par défaut (ni rôle ni catégorie de plongée), « jeu Plongée » en option,
« Qui tient la caisse ? » posé tout de suite, carte « Pour démarrer » sur la page du voyage."""
import html
import re
from urllib.parse import unquote

from fastapi.testclient import TestClient

from compta import models as M
from compta import services as S
from compta.ops import Ops
from compta.web import create_app

NEUTRAL = ["Logement", "Transport / Covoit", "Repas", "Activités", "Divers"]
DIVING = ["Logement", "Transport / Covoit", "Plongée", "Location matériel", "Repas", "Activités accompagnants", "Divers"]


def clean(t):
    return re.sub(r"\s+", " ", html.unescape(t).replace(" ", " ").replace("\xa0", " "))


def create(c, **data):
    r = c.post("/voyages", data={"name": "Rando", **data}, follow_redirects=False)
    assert r.status_code == 303
    loc = unquote(r.headers["location"])
    m = re.search(r"/v/(\d+)/", loc)
    return (int(m.group(1)) if m else None), loc


def trip(app, tid):
    s = app.state.db.Session()
    return s, s.get(M.Trip, tid)


def test_ordinary_trip_has_nothing_about_diving(tmp_path):
    app = create_app(tmp_path / "d")
    c = TestClient(app)
    tid, loc = create(c)
    assert "/participants?ajout=1" in loc and "Voyage créé" in loc
    s, t = trip(app, tid)
    assert [x.name for x in sorted(t.categories, key=lambda x: x.position)] == NEUTRAL
    assert list(S.trip_roles(t).values()) == ["Organisateur", "Co-organisateur"]
    assert not S.is_diving(t)
    s.close()
    # aucun écran du voyage ne parle de plongée
    Ops(app.state.db).add_participants(tid, ["Anne", "Bob"])
    for path in ("", "/participants", "/participants?ajout=1", "/depenses/nouvelle", "/categories", "/repartition",
                 "/argent", "/envoyer", "/asso", "/participants/soldes"):
        page = clean(c.get(f"/v/{tid}{path}").text).lower()
        for word in ("plong", "nitrox", "gonflage", "palanqu"):
            assert word not in page, (path, word)
    orga = clean(c.get(f"/v/{tid}/participants/organisation").text)
    assert "Ajouter les réglages de plongée" in orga                   # la seule mention : le bouton pour l'ajouter
    # une personne ajoutée à l'écran n'a aucun rôle d'office
    c.post(f"/v/{tid}/participants", data={"name": "Chloé"})
    s, t = trip(app, tid)
    assert all(p.role_list == [] for p in t.participants)
    s.close()


def test_diving_trip_gets_the_diving_set(tmp_path):
    app = create_app(tmp_path / "d")
    c = TestClient(app)
    tid, _ = create(c, plongee="1")
    s, t = trip(app, tid)
    assert [x.name for x in sorted(t.categories, key=lambda x: x.position)] == DIVING
    assert list(S.trip_roles(t).values())[:2] == ["Plongeur", "Accompagnant"] and S.is_diving(t)
    s.close()
    assert "Ajouter les réglages de plongée" not in c.get(f"/v/{tid}/participants/organisation").text
    cats = clean(c.get(f"/v/{tid}/categories").text)
    assert "+ Gonflage / Nitrox" in cats


def test_home_form_remembers_the_last_kind_of_trip(tmp_path):
    app = create_app(tmp_path / "d")
    c = TestClient(app)
    box = lambda: re.search(r'<input type="checkbox" name="plongee" value="1"\s*(checked)?', c.get("/?nouveau=1").text)
    assert box() and box().group(1) is None                            # premier voyage : pas coché
    create(c, plongee="1")
    assert box().group(1) == "checked"                                 # dernier voyage : plongée → proposé coché
    create(c, name="Ski")
    assert box().group(1) is None


def test_diving_set_added_later_in_one_click(tmp_path):
    """Voyage ordinaire, puis « Ajouter les réglages de plongée » : rôles, catégories ; les inscrits deviennent
    Plongeur. Aucun montant ne change. « Défaire » remet tout comme avant."""
    app = create_app(tmp_path / "d")
    c = TestClient(app)
    ops = Ops(app.state.db)
    tid, _ = create(c)
    ops.add_participants(tid, ["Anne", "Bob"])
    ops.add_expense(tid, "Gîte", montant_reel="200")
    before = {p["nom"]: p["sa_part"] for p in ops.trip(tid)["participants"]}
    assert before == {"Anne": 100.0, "Bob": 100.0}
    r = c.post(f"/v/{tid}/jeu-plongee", follow_redirects=True)
    assert "Réglages de plongée ajoutés" in clean(r.text)
    s, t = trip(app, tid)
    assert [x.name for x in t.categories][-3:] == ["Plongée", "Location matériel", "Activités accompagnants"]
    assert all(p.role_list == ["plongeur"] for p in t.participants) and S.is_diving(t)
    s.close()
    assert {p["nom"]: p["sa_part"] for p in ops.trip(tid)["participants"]} == before
    r = c.post(f"/v/{tid}/jeu-plongee", follow_redirects=True)
    assert "a déjà les réglages de plongée" in clean(r.text)
    app.state.db.undo()
    s, t = trip(app, tid)
    assert not S.is_diving(t) and [x.name for x in sorted(t.categories, key=lambda x: x.position)] == NEUTRAL
    s.close()


def test_cash_held_by_an_association_with_its_name(tmp_path):
    app = create_app(tmp_path / "d")
    c = TestClient(app)
    tid, _ = create(c, caisse="asso", asso_name="Club de rando des Aravis")
    s, t = trip(app, tid)
    assert t.cashier_id is None and S.trip_asso_name(t) == "Club de rando des Aravis" and not t.participants
    s.close()
    assert "Club de rando des Aravis" in clean(c.get(f"/v/{tid}/asso").text)
    # le nom est proposé pour le voyage suivant
    assert 'value="Club de rando des Aravis"' in c.get("/?nouveau=1").text


def test_cash_held_by_a_person_who_becomes_the_first_participant(tmp_path):
    """Pas d'association : Camille tient la caisse. Elle est ajoutée aux participants, et les comptes se règlent
    avec elle. Vérifié à la main : repas 90 € payé par la caisse, à 3 → 30 € chacun ; Camille ne « doit » rien."""
    app = create_app(tmp_path / "d")
    c = TestClient(app)
    ops = Ops(app.state.db)
    tid, loc = create(c, caisse="personne", cashier_name="  Camille   Durand ")
    assert "Camille Durand tient la caisse" in loc
    s, t = trip(app, tid)
    assert [p.name for p in t.participants] == ["Camille Durand"] and S.cashier(t).name == "Camille Durand"
    s.close()
    ops.add_participants(tid, ["Anne", "Bob"])
    ops.add_expense(tid, "Repas", montant_reel="90")
    msgs = clean(c.get(f"/v/{tid}/envoyer").text)
    assert "Tu tiens la caisse" in msgs and "Il te reste 30,00 € à verser à la caisse (Camille Durand)" in msgs
    assert "tient la caisse" in clean(c.get(f"/v/{tid}/asso").text)


def test_cash_person_needs_a_name_and_nothing_is_created(tmp_path):
    app = create_app(tmp_path / "d")
    c = TestClient(app)
    tid, loc = create(c, caisse="personne", cashier_name="   ")
    assert tid is None and "Écris le nom de la personne qui tient la caisse" in loc and loc.endswith("&e=1&f=cashier_name")
    with app.state.db.Session() as s:
        assert s.query(M.Trip).count() == 0


def test_getting_started_card_ticks_itself(tmp_path):
    app = create_app(tmp_path / "d")
    c = TestClient(app)
    ops = Ops(app.state.db)
    tid, _ = create(c)

    def card():
        page = c.get(f"/v/{tid}").text
        if 'id="pour-demarrer"' not in page:
            return None
        block = page.split('id="pour-demarrer"')[1].split("</section>")[0]
        return [("done" if 'class="done"' in li else "todo", clean(re.sub(r"<[^>]+>", " ", li)))
                for li in block.split("<li")[1:]]
    steps = card()
    assert [s for s, _ in steps] == ["todo"] * 4
    assert "Ajoute les participants" in steps[0][1] and "Ajouter des participants" in steps[0][1]
    ops.add_participants(tid, ["Anne", "Bob"])
    assert [s for s, _ in card()] == ["done", "todo", "todo", "todo"]
    ops.add_expense(tid, "Gîte", montant_reel="200")
    assert [s for s, _ in card()] == ["done", "done", "todo", "todo"]
    # la prochaine étape a le bouton plein, les suivantes un bouton discret
    page = c.get(f"/v/{tid}").text.split('id="pour-demarrer"')[1].split("</section>")[0]
    assert f'<a class="btn small" href="/v/{tid}/argent">Noter un versement</a>' in page
    assert f'<a class="btn small ghost" href="/v/{tid}/envoyer">Envoyer les comptes</a>' in page
    ops.add_movement(tid, "Anne", "50")
    assert card() is None                                              # le voyage est lancé : la carte s'efface


def test_no_getting_started_card_on_the_example_trip(tmp_path):
    app = create_app(tmp_path / "d")
    c = TestClient(app)
    c.post("/exemple/ouvrir")
    with app.state.db.Session() as s:
        tid = s.query(M.Trip).filter_by(is_demo=True).one().id
    assert 'id="pour-demarrer"' not in c.get(f"/v/{tid}").text


def test_ai_companion_in_an_ordinary_trip_brings_the_role_back(tmp_path):
    """Voyage ordinaire, l'IA signale un accompagnant : le rôle « Accompagnant » revient dans ce voyage (lui seul)."""
    app = create_app(tmp_path / "d")
    c = TestClient(app)
    ops = Ops(app.state.db)
    tid, _ = create(c)
    ops.add_participants(tid, ["Anne", {"nom": "Léa", "accompagnant": True}])
    s, t = trip(app, tid)
    assert list(S.trip_roles(t).values()) == ["Accompagnant", "Organisateur", "Co-organisateur"]
    roles = {p.name: p.role_list for p in t.participants}
    assert roles == {"Anne": [], "Léa": ["accompagnant"]}
    s.close()


def test_ai_guide_and_template_say_nothing_about_diving_by_default():
    from compta import importer
    tpl = importer.template().lower() + importer.update_template().lower()
    assert "plong" not in tpl and "palanqu" not in tpl and "nitrox" not in tpl
    guide = importer.guide()
    # la plongée n'apparaît plus que comme un cas particulier (voyage de plongée), jamais dans l'exemple
    example = re.findall(r"```json\s*(\{.*?\})\s*```", guide, re.S)[0].lower()
    assert "plong" not in example
    assert guide.lower().count("plong") <= 8
