"""Page IA : l'utilisateur est prévenu que le message part chez un tiers ; les points « à vérifier » laissés par
l'IA restent affichés après l'enregistrement, jusqu'à « C'est vérifié »."""
import html
import json
import re

from fastapi.testclient import TestClient

from compta import models as M
from compta import services as S
from compta.ops import Ops
from compta.web import create_app


def clean(t):
    return re.sub(r"\s+", " ", html.unescape(t).replace(" ", " ").replace("\xa0", " "))


def setup(tmp_path):
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.import_trip({"voyage": {"nom": "Week-end"}, "participants": ["Luc", "Anne"]})["voyage_id"]
    return app, ops, tid, TestClient(app)


def answer(tid, notes, label="Pot de fin", amount="45"):
    return json.dumps({"voyage_id": tid, "depenses": [{"libelle": label, "reel": amount, "paye_par": "asso",
                                                        "pour": "tous"}], "a_verifier": notes}, ensure_ascii=False)


NOTE = "Le ticket du pot de fin n'est pas lisible : 45 € ou 48 € ?"


def test_warning_before_copying(tmp_path):
    app, ops, tid, c = setup(tmp_path)
    for mode in ("initialisation", "mise-a-jour"):
        page = clean(c.get(f"/v/{tid}/ia?modele={mode}").text)
        assert "Bon à savoir avant de copier" in page
        assert "tu envoies ces informations à l'entreprise qui la fait fonctionner, en dehors de ton ordinateur" in page
        assert "Trip Compta, lui, n'envoie rien" in page
    raw = c.get(f"/v/{tid}/ia?modele=mise-a-jour").text
    # l'avertissement est placé avant le bouton qui copie
    assert raw.index('id="ia-confidentialite"') < raw.index('id="copy-msg"')
    assert "ce que chacun a payé et ce qu'il doit encore" in clean(raw)


def test_preview_keeps_nothing(tmp_path):
    app, ops, tid, c = setup(tmp_path)
    r = c.post(f"/v/{tid}/ia/apercu", data={"texte": answer(tid, [NOTE]), "modele": "mise-a-jour"})
    assert NOTE in clean(r.text) and "resteront affichés dans « Où j'en suis »" in clean(r.text)
    with app.state.db.Session() as s:
        assert S.ai_notes(s.get(M.Trip, tid)) == []                      # l'aperçu n'enregistre rien
    assert 'id="points-ia"' not in c.get(f"/v/{tid}").text


def test_notes_stay_after_saving_until_checked(tmp_path):
    app, ops, tid, c = setup(tmp_path)
    r = c.post(f"/v/{tid}/ia/importer", data={"texte": answer(tid, [NOTE, "Anne : 2 ou 3 nuits ?"]),
                                              "modele": "mise-a-jour"}, follow_redirects=True)
    page = clean(r.text)
    assert "2 points à vérifier sont gardés plus bas" in page
    assert NOTE in page and "Anne : 2 ou 3 nuits ?" in page and "C'est vérifié" in page
    assert "À regarder" in page
    # aussi en haut de la page IA
    ia = clean(c.get(f"/v/{tid}/ia").text)
    assert "Points à vérifier, laissés par l'IA" in ia and NOTE in ia
    # « C'est vérifié » : le point s'en va, l'autre reste
    r = c.post(f"/v/{tid}/ia/point-verifie", data={"texte": NOTE}, follow_redirects=True)
    page = clean(r.text)
    assert "Point vérifié" in page and NOTE not in page and "Anne : 2 ou 3 nuits ?" in page
    # deux fois de suite : rien de cassé
    r = c.post(f"/v/{tid}/ia/point-verifie", data={"texte": NOTE}, follow_redirects=True)
    assert "déjà retiré" in clean(r.text)
    # depuis la page IA, on y revient
    r = c.post(f"/v/{tid}/ia/point-verifie", data={"texte": "Anne : 2 ou 3 nuits ?", "retour": "ia"},
               follow_redirects=False)
    assert r.headers["location"].startswith(f"/v/{tid}/ia?")
    assert 'id="points-ia"' not in c.get(f"/v/{tid}").text and 'id="points-ia"' not in c.get(f"/v/{tid}/ia").text


def test_same_note_twice_is_kept_once_and_undo_restores(tmp_path):
    app, ops, tid, c = setup(tmp_path)
    c.post(f"/v/{tid}/ia/importer", data={"texte": answer(tid, [NOTE]), "modele": "mise-a-jour"})
    c.post(f"/v/{tid}/ia/importer", data={"texte": answer(tid, [NOTE.upper(), "Autre point"], "Courses", "20"),
                                          "modele": "mise-a-jour"})
    with app.state.db.Session() as s:
        assert [n["texte"] for n in S.ai_notes(s.get(M.Trip, tid))] == [NOTE, "Autre point"]
    c.post(f"/v/{tid}/ia/point-verifie", data={"texte": NOTE})
    app.state.db.undo()                                                  # « Défaire » : le point revient
    with app.state.db.Session() as s:
        assert [n["texte"] for n in S.ai_notes(s.get(M.Trip, tid))] == [NOTE, "Autre point"]


def test_note_text_is_escaped_and_single_string_accepted(tmp_path):
    app, ops, tid, c = setup(tmp_path)
    data = json.loads(answer(tid, []))
    data["a_verifier"] = "<b>Qui</b> a payé le \"péage\" ?"
    c.post(f"/v/{tid}/ia/importer", data={"texte": json.dumps(data), "modele": "mise-a-jour"})
    raw = c.get(f"/v/{tid}").text
    assert "<b>Qui</b>" not in raw and "&lt;b&gt;Qui&lt;/b&gt; a payé" in raw
    r = c.post(f"/v/{tid}/ia/point-verifie", data={"texte": "<b>Qui</b> a payé le \"péage\" ?"}, follow_redirects=True)
    assert "Point vérifié" in clean(r.text)


def test_api_path_keeps_and_shows_notes(tmp_path):
    """Par l'API ou le serveur MCP (sans passer par l'écran), les points sont gardés de la même façon."""
    app, ops, tid, c = setup(tmp_path)
    ops.import_trip(json.loads(answer(tid, [NOTE])), str(tid))
    assert ops.trip(tid)["points_a_verifier"] == [NOTE]
    assert NOTE in clean(c.get(f"/v/{tid}").text)
    # une simulation ne garde rien
    ops.import_trip(json.loads(answer(tid, ["Simulé"], "Autre", "5")), str(tid), simulation=True)
    assert ops.trip(tid)["points_a_verifier"] == [NOTE]
