"""Quand quelque chose casse : page en français pour une erreur imprévue (jamais « Internal Server Error »), journal
de l'application, alerte si la copie de secours du jour échoue, champs numériques d'un formulaire vérifiés."""
import html
import re

import pytest
from fastapi.testclient import TestClient

from compta import journal
from compta.ops import Ops
from compta.web import create_app


def clean(t):
    return re.sub(r"\s+", " ", html.unescape(t).replace("\xa0", " "))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIPCOMPTA_CONFIG", str(tmp_path / "reglages" / "reglages.json"))
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.import_trip({"voyage": {"nom": "x"}, "participants": ["Anne", "Bob", "Chloé"],
                           "depenses": [{"libelle": "Gîte", "reel": "300", "paye_par": "asso", "pour": "tous"}]})["voyage_id"]
    return app, ops, tid, TestClient(app, raise_server_exceptions=False), tmp_path


def boom(app, path="/boum"):
    @app.get(path)
    def _boom():
        raise RuntimeError("panne de test : Anne doit 100,00 €")
    return path


# ---------------------------------------------------------------- erreur imprévue
def test_unexpected_error_shows_a_french_page(env):
    app, ops, tid, c, tmp = env
    r = c.get(boom(app))
    page = clean(r.text)
    assert r.status_code == 500
    assert "Internal Server Error" not in page and "Traceback" not in page and "RuntimeError" not in page
    assert "Trip Compta a rencontré un problème" in page
    assert "Rien de ce qui était enregistré n'est perdu" in page
    assert "Retour à mes voyages" in page and 'href="/"' in r.text
    assert "panne de test" not in page                         # le détail technique reste dans le journal


def test_unexpected_error_is_written_to_the_journal_with_its_time(env):
    app, ops, tid, c, tmp = env
    r = c.get(boom(app))
    when = re.search(r"Problème noté à (\d\d:\d\d:\d\d)", clean(r.text)).group(1)
    log = journal.file().read_text(encoding="utf-8")
    assert journal.file().parent == tmp / "reglages"
    assert "erreur imprévue : GET /boum" in log and "RuntimeError" in log and "Traceback" in log
    assert when in log                                         # l'heure affichée permet de retrouver la ligne
    assert str(journal.file()) in html.unescape(r.text)


def test_unexpected_error_on_the_api_is_json(env):
    app, ops, tid, c, tmp = env
    r = c.get(boom(app, "/api/boum"))
    assert r.status_code == 500 and "problème imprévu" in r.json()["detail"]


def test_data_is_untouched_after_an_unexpected_error(env):
    app, ops, tid, c, tmp = env
    before = ops.trip(tid)
    c.get(boom(app))
    assert ops.trip(tid) == before
    assert c.get(f"/v/{tid}").status_code == 200                # l'application continue de marcher


def test_malformed_address_is_a_french_page(env):
    app, ops, tid, c, tmp = env
    r = c.get("/v/abc/participants")
    assert r.status_code == 404 and "Cette page n'existe pas" in clean(r.text)
    assert "detail" not in r.text and "int_parsing" not in r.text
    api = c.get("/api/voyages/abc")                             # l'API garde ses réponses JSON
    assert api.status_code in (400, 404, 422) and api.headers["content-type"].startswith("application/json")


def test_missing_trip_keeps_its_own_message(env):
    app, ops, tid, c, tmp = env
    r = c.get("/v/999")
    assert r.status_code == 404 and "Voyage introuvable" in clean(r.text)


# ---------------------------------------------------------------- journal
def test_journal_records_startup_without_personal_data(env):
    app, ops, tid, c, tmp = env
    c.post(f"/v/{tid}/participants", data={"name": "Denis Secret", "roles_form": "1"})
    c.post(f"/v/{tid}/argent", data={"kind": "versement", "participant": "1", "amount": "123,45"})
    log = journal.file().read_text(encoding="utf-8")
    assert "démarrage : Trip Compta" in log
    for private in ("Anne", "Denis Secret", "123,45", "12345", "Gîte"):
        assert private not in log


def test_journal_rotates(env):
    app, ops, tid, c, tmp = env
    for i in range(4000):
        journal.log.info("ligne de remplissage %05d %s", i, "x" * 200)
    files = sorted(p.name for p in journal.file().parent.glob("journal.log*"))
    assert files == ["journal.log", "journal.log.1", "journal.log.2"]
    assert all(p.stat().st_size <= 320_000 for p in journal.file().parent.glob("journal.log*"))


def test_app_starts_without_a_writable_journal(tmp_path, monkeypatch):
    blocked = tmp_path / "fichier"
    blocked.write_text("pas un dossier")
    monkeypatch.setenv("TRIPCOMPTA_CONFIG", str(blocked / "reglages.json"))
    app = create_app(tmp_path / "d")
    assert TestClient(app).get("/").status_code == 200


# ---------------------------------------------------------------- copie de secours du jour
def test_backup_failure_is_shown_and_logged_then_clears(env, monkeypatch):
    app, ops, tid, c, tmp = env
    db = app.state.db
    real = db.daily_backup

    def fail():
        raise OSError("disque plein")
    monkeypatch.setattr(db, "daily_backup", fail)
    r = c.post(f"/v/{tid}/participants", data={"name": "Denis", "roles_form": "1"})
    assert r.status_code == 200                                 # l'ajout a marché quand même
    assert "Denis" in [p["nom"] for p in ops.trip(tid)["participants"]]
    page = clean(c.get(f"/v/{tid}").text)
    assert "La copie de secours d'aujourd'hui n'a pas pu être faite" in page
    log = journal.file().read_text(encoding="utf-8")
    assert log.count("copie de secours du jour impossible") == 1 and "disque plein" in log
    c.post(f"/v/{tid}/participants", data={"name": "Émile", "roles_form": "1"})
    assert journal.file().read_text(encoding="utf-8").count("copie de secours du jour impossible") == 1   # dit une fois
    monkeypatch.setattr(db, "daily_backup", real)
    c.post(f"/v/{tid}/participants", data={"name": "Fanny", "roles_form": "1"})
    assert "copie de secours" not in clean(c.get(f"/v/{tid}").text)


def test_no_backup_banner_in_normal_use(env):
    app, ops, tid, c, tmp = env
    c.post(f"/v/{tid}/participants", data={"name": "Denis", "roles_form": "1"})
    assert "copie de secours" not in clean(c.get(f"/v/{tid}").text)


# ---------------------------------------------------------------- champs numériques
@pytest.mark.parametrize("url,data", [
    ("/v/{tid}/argent", {"kind": "versement", "participant": "abc", "amount": "10"}),
    ("/v/{tid}/asso/caisse", {"cashier": "p", "participant": "x"}),
    ("/v/{tid}/desistement", {"participant": "1.5", "kept": ["a", "2"]}),
])
def test_text_in_a_number_field_is_refused_not_a_crash(env, url, data):
    app, ops, tid, c, tmp = env
    before = ops.trip(tid)
    r = c.post(url.format(tid=tid), data=data, headers={"referer": f"http://testserver/v/{tid}"})
    assert r.status_code in (200, 404, 405), r.status_code      # jamais 500
    assert "Trip Compta a rencontré un problème" not in r.text
    assert ops.trip(tid) == before


def test_entre_nous_bad_numbers_are_refused(env):
    app, ops, tid, c, tmp = env
    ids = [p["id"] for p in ops.trip(tid)["participants"]]
    c.post(f"/v/{tid}/entre-nous", data={"name": "Courses", "members": [str(ids[0]), str(ids[1]), "zz"]})
    sid = int(re.search(r"/entre-nous/(\d+)", c.get(f"/v/{tid}/entre-nous").text).group(1))
    base = f"/v/{tid}/entre-nous/{sid}"
    r = c.post(base + "/depense", data={"label": "Pain", "amount": "5", "payer": "?", "for": ["x", str(ids[0])]})
    assert r.status_code == 200 and 'class="flash err"' in r.text
    r = c.post(base + "/regler", data={"sender": "", "receiver": str(ids[1]), "amount": "5"})
    assert r.status_code == 200 and "Choisis qui rembourse et qui reçoit" in clean(r.text)
    r = c.post(base + "/regler", data={"sender": str(ids[2]), "receiver": str(ids[1]), "amount": "5"})   # hors du compte
    assert "Choisis qui rembourse et qui reçoit" in clean(r.text)
    r = c.post(base + "/supprimer-ligne", data={"expense": "abc"})
    assert r.status_code == 200
    r = c.post(base + "/regler", data={"sender": str(ids[0]), "receiver": str(ids[1]), "amount": "5"})
    assert "a remboursé 5,00 €" in clean(r.text)


def test_app_opens_even_if_the_startup_backup_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIPCOMPTA_CONFIG", str(tmp_path / "reglages" / "reglages.json"))
    create_app(tmp_path / "d")                                  # crée la base
    from compta.db import Database

    def fail(self):
        raise OSError("dossier en lecture seule")
    monkeypatch.setattr(Database, "daily_backup", fail)
    app = create_app(tmp_path / "d")
    page = clean(TestClient(app).get("/").text)
    assert "La copie de secours d'aujourd'hui n'a pas pu être faite" in page
    assert "dossier en lecture seule" in journal.file().read_text(encoding="utf-8")
