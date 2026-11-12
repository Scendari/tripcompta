"""Page « Aide », et « Défaire » toujours accessible : l'écran « Dernières modifications ».

Voyage de test : Anne, Bruno, Chloé ; Gîte 300 € pour tous (100 € chacun). Puis trois versements de Bruno, notés
l'un après l'autre : 10 €, 20 €, 30 € (reste de Bruno : 90, 70, 40 €).
"""
import html
import re

from fastapi.testclient import TestClient

from compta import db as D
from compta import models as M
from compta import services as S
from compta.ops import Ops
from compta.web import create_app


def clean(t):
    return re.sub(r"\s+", " ", html.unescape(t).replace("\xa0", " "))


def visible(t):
    return clean(re.sub(r"<(script|style)\b.*?</\1>|<[^>]+>", " ", t, flags=re.S))


def setup(tmp_path):
    app = create_app(tmp_path / "voyages")
    cl = TestClient(app)
    ops = Ops(app.state.db)
    tid = ops.create_trip("Sortie test")["voyage_id"]
    ops.add_participants(tid, ["Anne", "Bruno", "Chloé"])
    ops.add_expense(tid, "Gîte", categorie="Logement", montant_reel="300")
    with app.state.db.Session() as s:
        bruno = s.query(M.Participant).filter_by(name="Bruno").one().id
    for amount in ("10", "20", "30"):
        r = cl.post(f"/v/{tid}/argent", data={"participant": str(bruno), "kind": "versement", "amount": amount},
                    follow_redirects=False)
        assert r.status_code == 303
    return app, cl, tid, bruno


def left(app, tid, pid):
    with app.state.db.Session() as s:
        return S.compute_trip(s.get(M.Trip, tid)).people[pid].balance


def tokens(page):
    return re.findall(r'name="jusqua" value="([^"]+)"', page)


# ------------------------------------------------------------------ dernières modifications
def test_history_is_reachable_from_every_page(tmp_path):
    app, cl, tid, bruno = setup(tmp_path)
    for url in ("/", f"/v/{tid}", f"/v/{tid}/depenses", "/reglages", "/aide", "/historique"):
        head = cl.get(url).text.split("</header>")[0]
        assert 'href="/historique"' in head and 'href="/aide"' in head, url
        assert 'action="/annuler"' not in head                       # jamais de bouton aveugle dans la barre


def test_history_lists_actions_in_plain_words(tmp_path):
    app, cl, tid, bruno = setup(tmp_path)
    page = cl.get("/historique").text
    seen = visible(page)
    labels = re.findall(r'<span class="what grow">([^<]+)</span>', page)
    assert [clean(x) for x in labels[:3]] == ["versement de 30,00 € (Bruno)", "versement de 20,00 € (Bruno)",
                                                      "versement de 10,00 € (Bruno)"]
    assert "↶ Défaire" in seen and "Revenir avant (défait 2 modifications)" in seen
    assert "Revenir avant (défait 3 modifications)" in seen
    assert f"garde les {D.UNDO_KEEP} dernières modifications" in seen and 'href="/reglages/sauvegardes"' in page
    # la première se défait sans question ; les autres demandent confirmation (elles en défont plusieurs)
    forms = re.findall(r'<form method="post" action="/historique/revenir"([^>]*)>', page)
    assert "data-confirm" not in forms[0] and all("data-confirm" in f for f in forms[1:])
    for jargon in ("snapshot", "instantané", "token", "base de données"):
        assert jargon not in seen.lower()


def test_undo_last_then_redo(tmp_path):
    app, cl, tid, bruno = setup(tmp_path)
    assert left(app, tid, bruno) == 4000
    first = tokens(cl.get("/historique").text)[0]
    r = cl.post("/historique/revenir", data={"jusqua": first}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/historique?")
    page = clean(cl.get(r.headers["location"]).text)
    assert "Défait : versement de 30,00 € (Bruno)." in page and "↷ Rétablir" in page
    assert left(app, tid, bruno) == 7000
    # sans le message (page rouverte plus tard) : « Rétablir » reste proposé sur cet écran
    page = cl.get("/historique").text
    assert "Tu viens de défaire" in clean(page) and 'action="/retablir"' in page
    rtok = re.search(r'name="r" value="([^"]+)"', page).group(1)
    cl.post("/retablir", data={"r": rtok}, headers={"referer": "http://testserver/historique"})
    assert left(app, tid, bruno) == 4000


def test_go_back_several_steps_and_redo_one_by_one(tmp_path):
    app, cl, tid, bruno = setup(tmp_path)
    toks = tokens(cl.get("/historique").text)
    r = cl.post("/historique/revenir", data={"jusqua": toks[2]}, follow_redirects=False)     # avant le 1er versement
    page = clean(cl.get(r.headers["location"]).text)
    assert "3 modifications défaites, jusqu'à « versement de 10,00 € (Bruno) »" in page
    assert left(app, tid, bruno) == 10000                                 # plus aucun versement
    with app.state.db.Session() as s:
        assert s.query(M.Movement).count() == 0
        assert s.query(M.Expense).count() == 1                            # la dépense, saisie avant, est toujours là
    for expected in (9000, 7000, 4000):                                   # « Rétablir » refait une à une
        cl.post("/retablir", data={})
        assert left(app, tid, bruno) == expected
    assert app.state.db.redo_token() is None


def test_unknown_or_stale_step_changes_nothing(tmp_path):
    app, cl, tid, bruno = setup(tmp_path)
    token = app.state.db.undo_token()
    for bad in ("", "20200101-000000-000000", "../x"):
        r = cl.post("/historique/revenir", data={"jusqua": bad}, follow_redirects=False)
        assert "e=1" in r.headers["location"]
        assert "rien n'a été défait" in clean(cl.get(r.headers["location"]).text)
    assert app.state.db.undo_token() == token and left(app, tid, bruno) == 4000


def test_empty_history_says_so(tmp_path):
    app = create_app(tmp_path / "voyages")
    page = clean(TestClient(app).get("/historique").text)
    assert "Rien à défaire pour l'instant" in page and "/historique/revenir" not in page


def test_undo_to_in_the_database_layer(tmp_path):
    app, cl, tid, bruno = setup(tmp_path)
    db = app.state.db
    hist = db.history()
    assert [clean(h["label"]) for h in hist[:3]] == ["versement de 30,00 € (Bruno)", "versement de 20,00 € (Bruno)",
                                              "versement de 10,00 € (Bruno)"]
    assert hist[0]["when"] >= hist[1]["when"] >= hist[2]["when"]
    assert db.undo_to("inconnu") == []
    assert [clean(x) for x in db.undo_to(hist[1]["token"])] == ["versement de 30,00 € (Bruno)",
                                                                "versement de 20,00 € (Bruno)"]
    assert left(app, tid, bruno) == 9000
    assert clean(db.history()[0]["label"]) == "versement de 10,00 € (Bruno)"


# ------------------------------------------------------------------ aide
def test_help_page_covers_the_basics_in_plain_words(tmp_path):
    app, cl, tid, bruno = setup(tmp_path)
    r = cl.get("/aide")
    assert r.status_code == 200
    page, seen = r.text, visible(r.text)
    for anchor in ("premiers-pas", "preparer", "mots", "astuces", "donnees", "probleme"):
        assert f'id="{anchor}"' in page and f'href="#{anchor}"' in page
    for word in ("La caisse", "La part", "Une avance", "Un versement", "Un remboursement", "Le reste à régler",
                 "Une remise, un supplément", "Un désistement", "Une famille", "Une étiquette", "Un justificatif",
                 "Entre nous", "Les centimes d'arrondi"):
        assert f"<dt>{word}</dt>" in page, word
    assert str(app.state.db.home) in seen                                # où sont les données
    assert "Rien n'est envoyé sur Internet" in seen
    for link in ("/historique", "/reglages/sauvegardes", "/reglages/donnees", "/reglages/protection",
                 "/reglages/diagnostic.txt", "https://tripcompta.plongee.bio/guide"):
        assert f'href="{link}"' in page, link
    low = seen.lower()
    for jargon in ("débit", "crédit", "écriture", "solde créditeur", "base de données", "snapshot", "sqlite",
                   "plongée", "plongeur", "json", "serveur"):
        assert jargon not in low, jargon


def test_help_page_stays_short_and_easy_to_scan(tmp_path):
    """Critique Impeccable de l'aide : pas plus de 1 200 mots (1 592 avant), le glossaire en deux groupes, un sommaire
    nommé, les raccourcis en liste, pas de petit texte au cas par cas ni de style écrit dans la page, pas de « solde »."""
    import re
    app, cl, tid, bruno = setup(tmp_path)
    page = cl.get("/aide").text
    body = page.split('<div class="help-page">', 1)[1].split("<script>", 1)[0]
    words = visible(body).split()
    assert len(words) <= 1200, len(words)
    assert re.findall(r'<h3 class="help-group">([^<]+)</h3>', body) == ["Pendant le voyage", "Pour préparer", "Au clavier"]
    assert '<nav class="help-nav" aria-label="Sommaire de l\'aide">' in body
    assert body.count("<kbd>") >= 3 and '<ul class="help-keys">' in body
    assert 'class="small"' not in body and "style=" not in body
    assert "sold" not in visible(body).lower()
    assert '<div class="data-path">' in body                             # le chemin sur sa propre ligne


def test_help_is_linked_and_survives_a_locked_folder(tmp_path):
    app, cl, tid, bruno = setup(tmp_path)
    assert 'href="/aide"' in cl.get("/").text
    assert cl.get("/aide").text.count("<h2 ") == 6


def test_problem_screens_do_not_offer_missing_pages(tmp_path):
    """Voyages illisibles : la barre du haut ne propose ni « Défaire » ni « Aide » (ces pages n'existent pas ici)."""
    import os
    f = tmp_path / "voyages"
    app = create_app(f)
    Ops(app.state.db).create_trip("Voyage")
    app.state.db.engine.dispose()
    for d in (D.backups_dir_for(f), f / ".annuler"):
        for p in d.glob("*.db"):
            p.unlink()
    with open(f / "compta.db", "r+b") as fh:
        fh.seek(100)
        fh.write(os.urandom(4000))
    page = TestClient(create_app(f)).get("/").text
    assert 'href="/historique"' not in page and 'href="/aide"' not in page


def test_browser_shortcut_and_buttons(server, page):
    base, tid, _ = server
    page.goto(f"{base}/v/{tid}")
    page.click(".topbar .help-btn")
    page.wait_for_url("**/aide")
    assert page.locator("h1").inner_text().strip().endswith("Aide")
    page.goto(f"{base}/v/{tid}/participants")
    page.keyboard.press("Control+z")                                     # hors d'un champ : dernières modifications
    page.wait_for_url("**/historique")
    assert "Dernières modifications" in page.locator("h1").inner_text()
    # dans un champ de saisie, le raccourci reste celui de la frappe
    page.goto(f"{base}/v/{tid}/depenses/nouvelle")
    page.fill("#label", "Essai")
    page.keyboard.press("Control+z")
    page.wait_for_timeout(200)
    assert "/depenses/nouvelle" in page.url
    assert not page.errors, page.errors
