"""Garde-fous du dossier des voyages (sans synchronisation) :
- base abîmée à l'ouverture → reprise de la dernière copie intacte, le fichier abîmé est gardé à côté ;
- fichier simplement indisponible (encore dans le cloud, disque débranché) → on n'y touche pas, on explique ;
- « modifiés en dernier sur un autre ordinateur » : noté dans la base, signalé à l'ouverture ;
- la sauvegarde du jour se fait aussi quand Trip Compta reste ouvert plusieurs jours."""
import datetime as dt
import html
import json
import os
import re
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from compta import db as D
from compta.ops import Ops
from compta.web import create_app, writer_notice


def clean(t):
    return re.sub(r"\s+", " ", html.unescape(t).replace("\xa0", " "))


def make(folder, who="PC-Anne"):
    os.environ["TRIPCOMPTA_ORDINATEUR"] = who
    try:
        return create_app(folder)
    finally:
        os.environ.pop("TRIPCOMPTA_ORDINATEUR", None)


@pytest.fixture(autouse=True)
def _computer(monkeypatch):
    monkeypatch.setenv("TRIPCOMPTA_ORDINATEUR", "PC-Anne")


def trip_names(folder):
    con = sqlite3.connect(folder / "compta.db")
    try:
        return sorted(r[0] for r in con.execute("SELECT name FROM trips"))
    finally:
        con.close()


def damage(path):
    """Écrase le milieu du fichier (comme une copie interrompue) : l'en-tête reste celui d'une base SQLite."""
    data = bytearray(path.read_bytes())
    for i in range(1024, len(data) - 100):
        data[i] = 0xAB
    path.write_bytes(bytes(data))


def seed(folder, names):
    app = create_app(folder)
    ops = Ops(app.state.db)
    for n in names:
        ops.import_trip({"voyage": {"nom": n}, "participants": ["Anne", "Bob"]})
    app.state.db.engine.dispose()
    return app


# ------------------------------------------------------------------ base abîmée
def test_damaged_db_is_replaced_by_latest_intact_copy(tmp_path):
    f = tmp_path / "voyages"
    seed(f, ["Hendaye", "Marseille"])            # chaque ajout laisse un instantané « Annuler » (avant l'ajout)
    assert D.db_state(f / "compta.db") == "ok"
    damage(f / "compta.db")
    assert D.db_state(f / "compta.db") == "abimee"
    before = (f / "compta.db").read_bytes()

    app = create_app(f)
    # l'instantané le plus récent : pris juste avant l'ajout de « Marseille »
    assert trip_names(f) == ["Hendaye"]
    kept = list(f.glob("compta (abîmée *).db"))
    assert len(kept) == 1 and kept[0].read_bytes() == before         # jamais effacé
    page = clean(TestClient(app).get("/").text)
    assert "Le fichier de tes voyages était abîmé" in page and "Ce qui a été saisi après est perdu" in page
    assert kept[0].name in page
    assert "était abîmé" not in clean(TestClient(app).get("/").text)   # une seule fois


def test_recovery_skips_damaged_backups(tmp_path):
    f = tmp_path / "voyages"
    seed(f, ["Hendaye"])
    db = f / "compta.db"
    for p in (f / ".annuler").glob("*.db"):
        p.unlink()
    good = D.backups_dir_for(f) / "compta-2026-01-01.db"
    D.Database._copy(db, good)
    bad = D.backups_dir_for(f) / "compta-2026-01-02.db"
    D.Database._copy(db, bad)
    damage(bad)
    os.utime(good, (time.time() - 3600, time.time() - 3600))
    os.utime(bad, (time.time() - 60, time.time() - 60))                 # la plus récente… mais abîmée
    for p in D.backups_dir_for(f).glob("compta-2*.db"):
        if p not in (good, bad):
            p.unlink()
    damage(db)
    d = D.Database(f)
    assert d.recovered and trip_names(f) == ["Hendaye"]
    assert d.recovered["when"].timestamp() == pytest.approx(good.stat().st_mtime, abs=1)
    assert bad.exists()                                                 # les sauvegardes ne sont pas touchées


def test_damaged_without_backup_explains_and_touches_nothing(tmp_path):
    f = tmp_path / "voyages"
    seed(f, ["Hendaye"])
    for d in (D.backups_dir_for(f), f / ".annuler"):
        for p in d.glob("*.db"):
            p.unlink()
    damage(f / "compta.db")
    before = (f / "compta.db").read_bytes()
    app = create_app(f)
    assert app.state.db is None
    r = TestClient(app).get("/")
    assert r.status_code == 503
    t = clean(r.text)
    assert "Le fichier de tes voyages est abîmé" in t and "Rien n'a été effacé ni modifié" in t
    assert (f / "compta.db").read_bytes() == before
    assert not list(f.glob("compta (abîmée*"))
    assert TestClient(app).get("/ping").json()["app"] == "tripcompta"


def test_not_a_database_is_damaged(tmp_path):
    p = tmp_path / "compta.db"
    p.write_bytes(b"ceci n'est pas une base" * 100)
    assert D.db_state(p) == "abimee"


# ------------------------------------------------------------------ fichier indisponible (pas abîmé)
def test_unavailable_file_is_never_renamed(tmp_path, monkeypatch):
    f = tmp_path / "voyages"
    seed(f, ["Hendaye"])
    before = (f / "compta.db").read_bytes()
    from compta import dossier as cloud
    monkeypatch.setattr(cloud, "cloud_only", lambda p: True)          # « disponible en ligne seulement »
    assert D.db_state(f / "compta.db") == "indisponible"
    restarted = []
    app = create_app(f, restart=lambda: restarted.append(1))
    c = TestClient(app)
    r = c.get("/voyages")
    assert r.status_code == 503
    t = clean(r.text)
    assert "pas disponibles pour l'instant" in t and "Rien n'a été modifié" in t and "Réessayer" in t
    assert (f / "compta.db").read_bytes() == before and not list(f.glob("compta (abîmée*"))
    r = c.post("/reessayer")
    assert "essaie de nouveau" in clean(r.text)
    time.sleep(0.6)
    assert restarted == [1]


def test_unreadable_file_is_unavailable(tmp_path):
    if os.name == "nt" or os.geteuid() == 0:
        pytest.skip("droits POSIX")
    p = tmp_path / "compta.db"
    sqlite3.connect(p).execute("CREATE TABLE x(a)").connection.close()
    p.chmod(0)
    try:
        assert D.db_state(p) == "indisponible"
    finally:
        p.chmod(0o644)


# ------------------------------------------------------------------ dernier ordinateur qui a écrit
def test_last_writer_is_recorded_and_announced(tmp_path, monkeypatch):
    f = tmp_path / "voyages"
    app = make(f, "PC-Anne")
    monkeypatch.setenv("TRIPCOMPTA_ORDINATEUR", "PC-Anne")
    Ops(app.state.db).import_trip({"voyage": {"nom": "Hendaye"}, "participants": ["Anne"]})
    w = app.state.db.last_writer()
    assert w["ordinateur"] == "PC-Anne" and (dt.datetime.now() - w["quand"]).total_seconds() < 60
    app.state.db.engine.dispose()

    # même ordinateur : rien à dire
    assert writer_notice(D.Database(f), me="PC-Anne") is None
    # autre ordinateur, il y a moins d'une heure : avertissement (Trip Compta y est peut-être encore ouvert)
    app_b = make(f, "PC-Bob")
    t = clean(TestClient(app_b).get("/").text)
    assert "Tes voyages ont été modifiés il y a moins d'une minute sur « PC-Anne »" in t
    assert "ferme-le avant de continuer" in t
    # récent : l'avertissement revient tant que c'est récent (le danger est réel)
    assert "PC-Anne" in (writer_notice(D.Database(f), me="PC-Bob") or "")


def test_old_change_elsewhere_is_said_once_per_computer(tmp_path):
    f = tmp_path / "voyages"
    d = D.Database(f)
    os.environ["TRIPCOMPTA_ORDINATEUR"] = "PC-Anne"
    d.mark_writer()
    now = dt.datetime.now() + dt.timedelta(days=1, minutes=1)              # « hier », quelle que soit l heure
    n = writer_notice(d, me="PC-Bob", now=now)
    assert n.startswith("Dernière modification de tes voyages : sur « PC-Anne », hier à ")
    assert writer_notice(d, me="PC-Bob", now=now) is None             # déjà dit : pas à chaque lancement
    os.environ["TRIPCOMPTA_ORDINATEUR"] = "PC-Chloé"
    d.mark_writer()
    assert "PC-Chloé" in writer_notice(d, me="PC-Bob", now=now)       # un nouvel ordinateur : on le dit


def test_writer_is_not_an_undo_step(tmp_path):
    """Noter l'ordinateur ne crée pas d'étape « Défaire » : Défaire annule l'action, et rien d'autre."""
    f = tmp_path / "voyages"
    app = create_app(f)
    ops = Ops(app.state.db)
    tid = ops.import_trip({"voyage": {"nom": "Hendaye"}, "participants": ["Anne"]})["voyage_id"]
    n_snaps = len(list((f / ".annuler").glob("*.db")))
    ops.add_participants(tid, ["Bob"])
    assert len(list((f / ".annuler").glob("*.db"))) == n_snaps + 1
    names = lambda: sorted(p["nom"] for p in ops.trip(tid)["participants"])
    assert names() == ["Anne", "Bob"]
    os.environ["TRIPCOMPTA_ORDINATEUR"] = "PC-Bob"
    try:
        app.state.db.undo()
    finally:
        os.environ["TRIPCOMPTA_ORDINATEUR"] = "PC-Anne"
    assert names() == ["Anne"]                                         # l'action est défaite…
    assert trip_names(f) == ["Hendaye"]                                # …et seulement elle
    assert app.state.db.undo_token() is not None                      # l'import reste à défaire, rien de plus
    assert app.state.db.last_writer()["ordinateur"] == "PC-Bob"        # Défaire est une modification


# ------------------------------------------------------------------ sauvegarde du jour
def test_daily_backup_while_app_stays_open(tmp_path):
    f = tmp_path / "voyages"
    app = create_app(f)
    c = TestClient(app)
    for p in D.backups_dir_for(f).glob("*.db"):
        p.unlink()                                                     # comme le lendemain matin
    c.post("/voyages", data={"name": "Hendaye"})
    today = D.backups_dir_for(f) / f"compta-{dt.date.today().isoformat()}.db"
    assert today.exists() and D.db_intact(today)


def test_human_when():
    now = dt.datetime(2026, 11, 5, 15, 0)
    assert D.human_when(now - dt.timedelta(seconds=20), now) == "il y a moins d'une minute"
    assert D.human_when(now - dt.timedelta(minutes=3), now) == "il y a 3 min"
    assert D.human_when(dt.datetime(2026, 11, 5, 9, 5), now) == "aujourd'hui à 9 h 05"
    assert D.human_when(dt.datetime(2026, 11, 4, 18, 30), now) == "hier à 18 h 30"
    assert D.human_when(dt.datetime(2026, 10, 3, 9, 5), now) == "le 03/10 à 9 h 05"
    assert D.human_when(dt.datetime(2025, 10, 3, 9, 5), now) == "le 03/10/2025 à 9 h 05"
