"""Sauvegardes complètes (base + justificatifs), rangées à part, et écran « Revenir à une sauvegarde ».

Voyage de test (montants vérifiés à la main) : Anne, Bruno, Chloé.
  Gîte 300 € payé par la caisse, pour tous           → 100 € chacun
  Essence 90 € avancée par Anne, pour Anne et Bruno  → 45 € chacun
  Parts : Anne 145, Bruno 145, Chloé 100 ; restes à payer : Anne 55 (145 − 90 avancés), Bruno 145, Chloé 100.
"""
import datetime as dt
import html
import os
import re
import shutil
import sqlite3

import pytest
from fastapi.testclient import TestClient

from compta import crypto as K
from compta import db as D
from compta import models as M
from compta import services as S
from compta.ops import Ops
from compta.web import create_app

EXPECTED = {"Anne": (14500, 5500), "Bruno": (14500, 14500), "Chloé": (10000, 10000)}   # (part, solde) en centimes
PDF = b"%PDF-1.4\n% ticket Essence Anne\n%%EOF\n"
PDF2 = b"%PDF-1.4\n% facture du gite\n%%EOF\n"


def clean(t):
    return re.sub(r"\s+", " ", html.unescape(t).replace("\xa0", " "))


def visible(t):
    """Le texte que l'on lit à l'écran (sans les balises)."""
    return clean(re.sub(r"<(script|style)\b.*?</\1>|<[^>]+>", " ", t, flags=re.S))


def seed(app) -> int:
    ops = Ops(app.state.db)
    tid = ops.create_trip("Sortie test")["voyage_id"]
    ops.add_participants(tid, ["Anne", "Bruno", "Chloé"])
    ops.add_expense(tid, "Gîte", categorie="Logement", montant_reel="300")
    ops.add_expense(tid, "Essence", categorie="Transport", montant_reel="90", paye_par="Anne", pour=["Anne", "Bruno"])
    return tid


def amounts(app, tid) -> dict:
    with app.state.db.Session() as s:
        return {p.name: (p.share, p.balance) for p in S.compute_trip(s.get(M.Trip, tid)).people.values()}


def trip_names(app) -> list:
    with app.state.db.Session() as s:
        return sorted(t.name for t in s.query(M.Trip).all())


def upload(cl, tid, name="ticket.pdf", data=PDF):
    r = cl.post(f"/v/{tid}/justificatifs", files=[("files", (name, data, "application/pdf"))], follow_redirects=False)
    assert r.status_code == 303


def stored_names(app) -> list:
    with app.state.db.Session() as s:
        return [r.stored_name for r in s.query(M.Receipt).order_by(M.Receipt.id)]


def age(app, days: int) -> str:
    """Fait de la sauvegarde d'aujourd'hui celle d'il y a `days` jours (nom et date du fichier)."""
    db = app.state.db
    today = db.backups_dir / f"compta-{dt.date.today().isoformat()}.db"
    assert today.exists()
    when = dt.datetime.now() - dt.timedelta(days=days)
    old = db.backups_dir / f"compta-{when.date().isoformat()}.db"
    os.replace(today, old)
    os.utime(old, (when.timestamp(), when.timestamp()))
    return old.name


@pytest.fixture
def voyage(tmp_path):
    """Un dossier de voyages avec le voyage de test, un justificatif, et une sauvegarde « d'il y a 2 jours »."""
    f = tmp_path / "voyages"
    app = create_app(f)
    cl = TestClient(app)
    tid = seed(app)
    upload(cl, tid)
    for p in app.state.db.backups_dir.glob("compta-*.db"):
        p.unlink()                                   # la sauvegarde du jour : refaite maintenant, voyage rempli
    app.state.db.daily_backup()
    name = age(app, 2)
    return f, app, cl, tid, name


# ------------------------------------------------------------------ rangées à part, complètes
def test_long_date_in_french():
    assert D.long_date(dt.date(2026, 11, 3)) == "mardi 3 novembre 2026"
    assert D.long_date(dt.date(2027, 8, 1)) == "dimanche 1er août 2027"


def test_backups_live_apart_from_the_trips_folder(tmp_path):
    f = tmp_path / "voyages"
    app = create_app(f)
    db = app.state.db
    assert db.complete_backups
    assert f not in db.backups_dir.parents and db.backups_dir != f / "sauvegardes"
    assert not (f / "sauvegardes").exists()                             # plus rien dans le dossier des voyages
    assert db.backups_dir == D.backups_dir_for(f)
    assert str(f) in (db.backups_dir / "A LIRE.txt").read_text(encoding="utf-8")
    assert (db.backups_dir / f"compta-{dt.date.today().isoformat()}.db").exists()
    # un autre dossier de voyages a ses propres sauvegardes
    assert D.backups_dir_for(tmp_path / "autres") != db.backups_dir


def test_receipts_are_copied_into_the_backups(tmp_path):
    f = tmp_path / "voyages"
    app = create_app(f)
    cl = TestClient(app)
    tid = seed(app)
    pool = app.state.db.backups_dir / D.RECEIPTS_POOL
    upload(cl, tid)                                                    # la sauvegarde du jour existe déjà
    (name,) = stored_names(app)
    assert (pool / name).read_bytes() == PDF                           # copié tout de suite, sans attendre demain
    upload(cl, tid, "facture.pdf", PDF2)
    names = stored_names(app)
    assert sorted(p.name for p in pool.iterdir() if not p.name.startswith(".")) == sorted(names)
    assert (pool / names[1]).read_bytes() == PDF2
    # un justificatif supprimé de l'écran reste dans les sauvegardes
    with app.state.db.Session() as s:
        rid = s.query(M.Receipt).first().id
    cl.post(f"/v/{tid}/justificatifs/{rid}/supprimer")
    assert (pool / names[0]).exists()


def test_mirror_does_nothing_when_nothing_moved(tmp_path):
    f = tmp_path / "voyages"
    app = create_app(f)
    cl = TestClient(app)
    tid = seed(app)
    upload(cl, tid)
    db = app.state.db
    assert db.mirror_receipts() == 0                                   # déjà fait, rien n'a bougé
    assert db.mirror_receipts(force=True) == 0                         # même en regardant tout : déjà là
    (name,) = stored_names(app)
    (db.backups_dir / D.RECEIPTS_POOL / name).write_bytes(b"tronque")   # copie incomplète (taille différente)
    assert db.mirror_receipts(force=True) == 1
    assert (db.backups_dir / D.RECEIPTS_POOL / name).read_bytes() == PDF


# ------------------------------------------------------------------ l'écran
def test_page_lists_backups_in_plain_words(voyage):
    f, app, cl, tid, name = voyage
    cl.post("/voyages", data={"name": "Deuxième voyage", "caisse": "asso"})     # refait la sauvegarde du jour
    page = clean(cl.get("/reglages/sauvegardes").text)
    two_days = dt.date.today() - dt.timedelta(days=2)
    assert f"Sauvegarde du {D.long_date(two_days)}" in page
    assert f"Sauvegarde du {D.long_date(dt.date.today())}" in page
    assert "1 voyage (Sortie test) · 2 dépenses · 1 justificatif" in page
    assert "Aujourd'hui, tes voyages contiennent :</b> 2 voyages · 2 dépenses · 1 justificatif" in page
    assert page.count("Revenir à cette sauvegarde</button>") == 2
    assert 'data-confirm="Revenir à cette sauvegarde' in page and "tu pourras défaire ce retour" in page
    assert "à part du dossier de tes voyages" in page and str(app.state.db.backups_dir) in page
    seen = visible(cl.get("/reglages/sauvegardes").text).lower().replace(str(app.state.db.backups_dir).lower(), "")
    for jargon in ("snapshot", "backup", "restaur", "base de données", ".db", "instantané"):
        assert jargon not in seen, jargon
    settings = clean(cl.get("/reglages").text)
    assert "Sauvegardes" in settings and "Dernière : " in settings and "2 gardées" in settings


def test_return_to_a_backup_brings_back_data_and_receipts(voyage):
    f, app, cl, tid, name = voyage
    db = app.state.db
    assert amounts(app, tid) == EXPECTED
    (receipt,) = stored_names(app)
    # depuis la sauvegarde : une dépense supprimée, un voyage ajouté, et le fichier du justificatif perdu
    with db.Session() as s:
        eid = next(e.id for e in s.get(M.Trip, tid).expenses if e.label == "Essence")
    cl.post(f"/v/{tid}/depenses/{eid}/supprimer")
    cl.post("/voyages", data={"name": "Créé après", "caisse": "asso"})
    (db.receipts_dir / receipt).unlink()
    assert amounts(app, tid) == {"Anne": (10000, 10000), "Bruno": (10000, 10000), "Chloé": (10000, 10000)}
    assert trip_names(app) == ["Créé après", "Sortie test"]

    r = cl.post("/reglages/sauvegardes/revenir", data={"nom": name}, follow_redirects=False)
    assert r.status_code == 303 and "e=1" not in r.headers["location"]
    page = clean(cl.get(r.headers["location"]).text)
    assert "Tes voyages sont revenus à l'état du" in page and "1 justificatif remis à sa place" in page
    assert "Tu peux encore défaire ce retour" in page
    assert trip_names(app) == ["Sortie test"]
    assert amounts(app, tid) == EXPECTED                               # au centime près, comme avant
    assert (db.receipts_dir / receipt).read_bytes() == PDF             # le fichier est revenu
    assert cl.get(f"/v/{tid}/depenses").status_code == 200

    # l'état d'avant le retour est gardé : dans la liste, et par « Défaire »
    kinds = [b["kind"] for b in db.backups()]
    assert kinds.count("retour") == 1
    assert "Juste avant ton dernier retour à une sauvegarde" in page
    cl.post("/annuler", data={})
    assert trip_names(app) == ["Créé après", "Sortie test"]
    assert amounts(app, tid)["Anne"] == (10000, 10000)


def test_the_before_return_copy_can_be_used_too(voyage):
    f, app, cl, tid, name = voyage
    cl.post("/voyages", data={"name": "Créé après", "caisse": "asso"})
    cl.post("/reglages/sauvegardes/revenir", data={"nom": name})
    assert trip_names(app) == ["Sortie test"]
    before = next(b for b in app.state.db.backups() if b["kind"] == "retour")
    assert before["trips"] == ["Sortie test", "Créé après"]
    cl.post("/reglages/sauvegardes/revenir", data={"nom": before["name"]})
    assert trip_names(app) == ["Créé après", "Sortie test"]


def test_only_three_before_return_copies_are_kept(voyage, monkeypatch):
    f, app, cl, tid, name = voyage
    db = app.state.db
    base = dt.datetime(2026, 11, 10, 9, 0, 0)

    class Clock(dt.datetime):
        tick = 0

        @classmethod
        def now(cls, tz=None):
            cls.tick += 1
            return base + dt.timedelta(seconds=cls.tick)

    monkeypatch.setattr(D.dt, "datetime", Clock)
    for _ in range(5):
        db.restore_backup(name)
    monkeypatch.undo()
    assert len(list(db.backups_dir.glob("avant-retour-*.db"))) == D.BEFORE_RETURN_KEEP == 3
    assert (db.backups_dir / name).exists()                            # la sauvegarde reprise n'est pas consommée


def test_refused_backups_change_nothing(voyage):
    f, app, cl, tid, name = voyage
    db = app.state.db
    cl.post("/voyages", data={"name": "Créé après", "caisse": "asso"})
    token = db.undo_token()

    def refused(nom, words):
        r = cl.post("/reglages/sauvegardes/revenir", data={"nom": nom}, follow_redirects=False)
        assert r.status_code == 303 and "e=1" in r.headers["location"]
        assert words in clean(cl.get(r.headers["location"]).text)
        assert trip_names(app) == ["Créé après", "Sortie test"] and db.undo_token() == token

    refused("inconnue.db", "Cette sauvegarde est introuvable")
    refused("../compta.db", "Cette sauvegarde est introuvable")        # jamais un fichier hors des sauvegardes
    refused("", "Cette sauvegarde est introuvable")
    # abîmée
    bad = db.backups_dir / "compta-2026-01-02.db"
    shutil.copyfile(db.backups_dir / name, bad)
    with open(bad, "r+b") as fh:
        fh.seek(100)
        fh.write(os.urandom(3000))
    refused(bad.name, "abîmée")
    assert "Cette sauvegarde est illisible" in clean(cl.get("/reglages/sauvegardes").text)   # dite, sans bouton
    # faite par une version plus récente
    newer = db.backups_dir / "compta-2026-01-03.db"
    shutil.copyfile(db.backups_dir / name, newer)
    con = sqlite3.connect(newer)
    con.execute(f"PRAGMA user_version = {D.SCHEMA + 1}")
    con.commit()
    con.close()
    refused(newer.name, "version plus récente de Trip Compta")
    assert not list(db.backups_dir.glob("avant-retour-*.db"))          # aucun retour n'a commencé


def test_missing_receipt_is_said(voyage):
    f, app, cl, tid, name = voyage
    db = app.state.db
    (receipt,) = stored_names(app)
    (db.receipts_dir / receipt).unlink()
    (db.backups_dir / D.RECEIPTS_POOL / receipt).unlink()              # perdu des deux côtés
    page = clean(cl.get("/reglages/sauvegardes").text)
    assert "1 justificatif de cette sauvegarde est introuvable" in page
    r = cl.post("/reglages/sauvegardes/revenir", data={"nom": name}, follow_redirects=False)
    assert "1 justificatif de cette sauvegarde est introuvable" in clean(cl.get(r.headers["location"]).text)
    assert amounts(app, tid) == EXPECTED                               # les comptes, eux, sont revenus


def test_trips_folder_lost_backups_still_there(voyage):
    """Le dossier des voyages est effacé (mauvaise manipulation, synchronisation) : les sauvegardes, à part,
    permettent de tout retrouver, justificatifs compris."""
    f, app, cl, tid, name = voyage
    (receipt,) = stored_names(app)
    app.state.db.engine.dispose()
    shutil.rmtree(f)
    app2 = create_app(f)
    cl2 = TestClient(app2)
    assert trip_names(app2) == []
    page = clean(cl2.get("/reglages/sauvegardes").text)
    assert "1 voyage (Sortie test) · 2 dépenses · 1 justificatif" in page
    cl2.post("/reglages/sauvegardes/revenir", data={"nom": name})
    assert trip_names(app2) == ["Sortie test"]
    assert amounts(app2, tid) == EXPECTED
    assert (app2.state.db.receipts_dir / receipt).read_bytes() == PDF
    r = cl2.get(f"/v/{tid}/justificatifs")
    assert r.status_code == 200 and "ticket.pdf" in r.text


def test_backup_from_before_a_new_column_still_opens(voyage):
    """Une sauvegarde faite avant l'ajout d'une colonne : reprise, puis alignée sur la structure actuelle."""
    f, app, cl, tid, name = voyage
    db = app.state.db
    con = sqlite3.connect(db.backups_dir / name)
    con.execute("ALTER TABLE trips DROP COLUMN pay_info")
    con.commit()
    con.close()
    db.restore_backup(name)
    assert amounts(app, tid) == EXPECTED
    assert cl.get(f"/v/{tid}/envoyer").status_code == 200


def test_failed_backup_banner_points_to_the_screen(tmp_path, monkeypatch):
    f = tmp_path / "voyages"
    app = create_app(f)
    cl = TestClient(app)
    for p in app.state.db.backups_dir.glob("compta-*.db"):
        p.unlink()
    monkeypatch.setattr(D.Database, "daily_backup", lambda self: (_ for _ in ()).throw(OSError("disque plein")))
    cl.post("/voyages", data={"name": "Hendaye", "caisse": "asso"})
    page = clean(cl.get("/").text)
    assert "La copie de secours d'aujourd'hui n'a pas pu être faite" in page
    assert 'href="/reglages/sauvegardes"' in cl.get("/").text


# ------------------------------------------------------------------ changer de dossier, mot de passe
def test_backups_follow_when_the_folder_changes(tmp_path, monkeypatch):
    monkeypatch.delenv("TRIPCOMPTA_DATA_DIR", raising=False)
    monkeypatch.setenv("TRIPCOMPTA_CONFIG", str(tmp_path / "conf" / "reglages.json"))
    monkeypatch.setenv("TRIPCOMPTA_DOCUMENTS", str(tmp_path / "Mes Documents"))
    app = create_app()
    cl = TestClient(app)
    tid = seed(app)
    upload(cl, tid)
    name = age(app, 3)
    (receipt,) = stored_names(app)
    cloud = tmp_path / "Dropbox" / "TripCompta"
    cloud.parent.mkdir()
    assert "ont été copiés" in clean(cl.post("/reglages/donnees", data={"dossier": str(cloud)}).text)
    app.state.arreter()
    app2 = create_app()
    db2 = app2.state.db
    assert db2.dir == cloud and db2.backups_dir == D.backups_dir_for(cloud) != app.state.db.backups_dir
    assert name in [b["name"] for b in db2.backups()]                  # l'ancienne sauvegarde est toujours proposée
    assert (db2.backups_dir / D.RECEIPTS_POOL / receipt).read_bytes() == PDF
    assert str(cloud) in (db2.backups_dir / "A LIRE.txt").read_text(encoding="utf-8")
    app2.state.arreter()


def test_password_protection_leaves_no_readable_backup(tmp_path, monkeypatch):
    """Protéger par un mot de passe : les sauvegardes en clair (base et justificatifs) rangées à part sont effacées ;
    celles de la base suivent, chiffrées. Le retour à une sauvegarde marche toujours, justificatifs compris."""
    monkeypatch.setattr(K, "SCRYPT_N", 2 ** 10)
    store = {}
    monkeypatch.setattr(K, "keyring_available", lambda: True)
    monkeypatch.setattr(K, "remember", lambda v: store.__setitem__(v.id, v.key) or True)
    monkeypatch.setattr(K, "recall", lambda vid: K.Vault(store[vid], vid) if vid in store else None)
    monkeypatch.setattr(K, "forget", lambda vid: store.pop(vid, None))
    monkeypatch.delenv("TRIPCOMPTA_DATA_DIR", raising=False)
    conf = tmp_path / "Réglages ordi"
    monkeypatch.setenv("TRIPCOMPTA_CONFIG", str(conf / "reglages.json"))
    monkeypatch.setenv("TRIPCOMPTA_DOCUMENTS", str(tmp_path / "Mes Documents"))
    app = create_app()
    cl = TestClient(app)
    tid = seed(app)
    upload(cl, tid)
    for p in app.state.db.backups_dir.glob("compta-*.db"):
        p.unlink()
    app.state.db.daily_backup()                                        # la sauvegarde contient le voyage rempli
    name = age(app, 2)
    apart = app.state.db.backups_dir
    assert (apart / name).exists() and list((apart / D.RECEIPTS_POOL).iterdir())
    password = "Bulles de Plongée 2026"
    r = cl.post("/protection/activer", data={"mot_de_passe": password, "confirmation": password, "souvenir": "1"})
    key = re.search(r'id="recovery">([A-Z2-9-]+)<', r.text).group(1)
    n = int(re.search(r"groupe n° (\d+)", r.text).group(1))
    r = cl.post("/protection/confirmer", data={"groupe": key.split("-")[n - 1].lower()})
    assert "maintenant protégés" in clean(r.text)
    assert not apart.exists()                                          # plus rien de lisible à part
    for p in conf.rglob("*"):
        if p.is_file() and p.suffix != ".json":
            assert b"Essence" not in p.read_bytes() and b"ticket Essence" not in p.read_bytes(), p
    app.state.arreter()

    app2 = create_app()                                                # le trousseau se souvient : déverrouillé
    cl2 = TestClient(app2)
    db2 = app2.state.db
    assert not db2.complete_backups and name in [b["name"] for b in db2.backups()]
    page = clean(cl2.get("/reglages/sauvegardes").text)
    assert "protégés par un mot de passe" in page and "à part du dossier de tes voyages" not in page
    Ops(db2).create_trip("Créé après")
    (receipt,) = stored_names(app2)
    (db2.receipts_dir / receipt).unlink()
    cl2.post("/reglages/sauvegardes/revenir", data={"nom": name})
    assert trip_names(app2) == ["Sortie test"] and amounts(app2, tid) == EXPECTED
    assert (db2.receipts_dir / receipt).read_bytes() == PDF            # redéchiffré depuis le dossier
    app2.state.arreter()
