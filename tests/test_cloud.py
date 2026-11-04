"""Voyages rangés dans un dossier synchronisé (cloud.py) : verrou, lecture seule, prise de main, publication
d'un coup, reprise de la version du dossier, copies en conflit, dossier absent ou protégé, fichiers « dans le
cloud seulement », justificatifs à trier, migration aller-retour.

Deux « ordinateurs » sont simulés dans le même test : deux applications sur le même dossier partagé, chacune avec
sa copie de travail et son identité (nom d'ordinateur, numéro de processus).

Voyage de test (montants vérifiés à la main) : Anne, Bruno, Chloé.
  Gîte 300 € payé par l'asso, pour tous            → 100 € chacun
  Essence 90 € avancée par Anne, pour Anne et Bruno → 45 € chacun
  Parts : Anne 145, Bruno 145, Chloé 100 (total 390) ; soldes : Anne 145 − 90 = 55, Bruno 145, Chloé 100.
"""
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from compta import cloud as C
from compta import models as M
from compta import services as S
from compta.ops import Ops, OpsError
from compta.web import create_app

EXPECTED = {"Anne": (14500, 5500), "Bruno": (14500, 14500), "Chloé": (10000, 10000)}   # (part, solde) en centimes


@pytest.fixture
def cloud_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIPCOMPTA_CONFIG", str(tmp_path / "reglages" / "reglages.json"))
    monkeypatch.setenv("TRIPCOMPTA_DOCUMENTS", str(tmp_path / "Mes Documents"))
    shared = tmp_path / "Mon Drive é" / "TripCompta"          # espaces et accents
    shared.mkdir(parents=True)
    return tmp_path, shared


def make(tmp, shared, who, pid):
    return create_app(cloud=dict(shared=shared, local=tmp / f"copie {who}", identity=dict(host=who, pid=pid),
                                 background=False))


def stop(app):
    """« ⏻ Arrêter » : verrou libéré et base refermée (Windows ne remplace pas un fichier encore ouvert)."""
    app.state.sync.stop()
    app.state.db.engine.dispose()


def seed(app) -> int:
    ops = Ops(app.state.db)
    tid = ops.create_trip("Sortie test")["voyage_id"]
    ops.add_participants(tid, ["Anne", "Bruno", "Chloé"])
    ops.add_expense(tid, "Gîte", categorie="Logement", montant_reel="300")
    ops.add_expense(tid, "Essence", categorie="Transport", montant_reel="90", paye_par="Anne", pour=["Anne", "Bruno"])
    return tid


def amounts(app, tid) -> dict:
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        r = S.compute_trip(t)
        return {p.name: (p.share, p.balance) for p in r.people.values()}


def trip_names(path: Path) -> list:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return sorted(r[0] for r in con.execute("SELECT name FROM trips"))
    finally:
        con.close()


# ---------------------------------------------------------------- verrou, lecture seule, prise de main
def test_two_computers_lock_readonly_takeover(cloud_env):
    tmp, shared = cloud_env
    a = make(tmp, shared, "PC-A", 101)
    b = make(tmp, shared, "Mac-B", 202)
    sa, sb = a.state.sync, b.state.sync
    assert not sa.readonly and sb.readonly and sb.owner["ordinateur"] == "PC-A"
    lock = sa.read_lock()
    assert lock["ordinateur"] == "PC-A" and lock["depuis"] and lock["battement"]

    ca, cb = TestClient(a), TestClient(b)
    ca.post("/voyages", data={"name": "Voyage A"})
    assert trip_names(shared / C.DB) == ["Voyage A"]            # publié tout de suite
    # B : lecture seule, explication claire, rien d'écrit (ni dans sa copie ni dans le dossier)
    page = cb.get("/").text
    assert "Voyage A" in page and "Lecture seule" in page and "ouverts sur « PC-A »" in page
    r = cb.post("/voyages", data={"name": "Voyage B"}, headers={"referer": "http://t/"}, follow_redirects=True)
    assert "Lecture seule" in r.text and "Ferme Trip Compta là-bas" in r.text
    assert cb.post("/api/voyages", json={"nom": "x"}).status_code in (404, 405, 423)
    assert trip_names(shared / C.DB) == ["Voyage A"]
    with pytest.raises(OpsError, match="lecture seule"):          # l'IA (autre processus) non plus
        Ops(b.state.db).create_trip("Par l'IA")
    assert "Prendre la main" in page and "préviens-le d" in page.replace("&#39;", "'")   # A vivant : avec mise en garde

    # A ne bat plus depuis plus de 3 minutes : B peut prendre la main
    sb.clock = lambda: time.time() + C.STALE + 5
    page = cb.get("/").text
    assert "ne donne plus signe de vie" in page and "Prendre la main" in page
    r = cb.post("/reglages/prendre-la-main", headers={"referer": "http://t/"}, follow_redirects=True)
    assert "Tu as la main" in r.text and not sb.readonly
    cb.post("/voyages", data={"name": "Voyage B"})
    assert trip_names(shared / C.DB) == ["Voyage A", "Voyage B"]
    # A se réveille : son battement voit que B a la main → lecture seule, sans rien écraser
    sa.beat()
    assert sa.readonly and sa.owner["ordinateur"] == "Mac-B"
    r = ca.post("/voyages", data={"name": "Voyage A2"}, headers={"referer": "http://t/"}, follow_redirects=True)
    assert "Lecture seule" in r.text
    assert "Voyage B" in ca.get("/").text                        # en revenant sur l'app : version du dossier
    assert trip_names(shared / C.DB) == ["Voyage A", "Voyage B"]
    sa.stop()
    sb.stop()


def test_release_on_quit_and_automatic_handover(cloud_env):
    tmp, shared = cloud_env
    a = make(tmp, shared, "PC-A", 101)
    b = make(tmp, shared, "Mac-B", 202)
    TestClient(a).post("/voyages", data={"name": "Voyage A"})
    stop(a)                                                      # « ⏻ Arrêter » : verrou libéré
    assert not (shared / C.LOCK).exists()
    b.state.sync.beat()                                          # battement suivant : B reprend la main seul
    assert not b.state.sync.readonly
    cb = TestClient(b)
    assert "« PC-A » a fermé Trip Compta : tu as de nouveau la main" in cb.get("/").text
    cb.post("/voyages", data={"name": "Voyage B"})
    a2 = make(tmp, shared, "PC-A", 303)                          # A relancé plus tard : lecture seule, voit tout
    assert a2.state.sync.readonly and "Voyage B" in TestClient(a2).get("/").text
    b.state.sync.stop()
    a2.state.sync.stop()


def test_same_computer_crash_lock_is_reclaimed(cloud_env):
    """Planté sur le même ordinateur (processus disparu) : au redémarrage, l'app reprend la main toute seule."""
    tmp, shared = cloud_env
    a = make(tmp, shared, "PC-A", 999_999_9)                    # numéro de processus qui n'existe pas
    assert not a.state.sync.readonly
    a2 = make(tmp, shared, "PC-A", os.getpid())
    assert not a2.state.sync.readonly
    a2.state.sync.stop()


def test_no_lost_input_two_computers_in_turn(cloud_env):
    """Chacun son tour, 10 allers-retours : toutes les saisies sont là à la fin, sur les deux ordinateurs."""
    tmp, shared = cloud_env
    names = []
    for i in range(10):
        who = ("PC-A", 1000 + i) if i % 2 == 0 else ("Mac-B", 2000 + i)
        app = make(tmp, shared, *who)
        assert not app.state.sync.readonly
        TestClient(app).post("/voyages", data={"name": f"Voyage {i}"})
        names.append(f"Voyage {i}")
        stop(app)
    assert trip_names(shared / C.DB) == sorted(names)
    for who in ("PC-A", "Mac-B"):
        app = make(tmp, shared, who, 5)
        page = TestClient(app).get("/").text
        assert all(n in page for n in names)
        stop(app)


# ---------------------------------------------------------------- publication d'un coup
def test_published_file_always_valid_under_random_sync(cloud_env):
    """Un « synchroniseur » copie le fichier du dossier à des moments quelconques, y compris pendant les
    enregistrements : chaque copie est une base complète et valide, et il n'y a jamais de -journal, -wal ou -shm."""
    tmp, shared = cloud_env
    a = make(tmp, shared, "PC-A", 101)
    tid = seed(a)
    ops = Ops(a.state.db)
    remote = tmp / "cloud distant"
    remote.mkdir()
    stop, copies, bad, extra = threading.Event(), [], [], set()

    def synchroniser():
        import random
        n = 0
        while not stop.is_set():
            for f in os.listdir(shared):
                if f.endswith(("-journal", "-wal", "-shm")):
                    extra.add(f)
            try:
                dst = remote / f"copie-{n % 8}.db"             # quelques noms réutilisés : le disque ne se remplit pas
                shutil.copyfile(shared / C.DB, dst)
                n += 1
                copies.append(n)
                if not C.is_valid_db(dst):
                    bad.append(f"copie {n}")
            except FileNotFoundError:
                bad.append("fichier absent pendant un remplacement")
            except PermissionError:                    # Windows : fichier momentanément bloqué, on réessaie
                pass
            time.sleep(random.uniform(0, 0.004))       # un vrai client lit par intermittence
    th = threading.Thread(target=synchroniser, daemon=True)
    th.start()
    for i in range(40):
        ops.add_expense(tid, f"Café {i}", categorie="Repas", montant_reel=f"{i + 1}")
        for _ in range(5):                             # Windows : remplacement bloqué par une lecture → réessai
            res = a.state.sync.publish()               # (dans l'app : au battement suivant)
            if res == "ok":
                break
        assert res == "ok"
    stop.set()
    th.join(5)
    assert len(copies) > 20 and bad == [] and extra == set()
    con = sqlite3.connect(f"file:{shared / C.DB}?mode=ro", uri=True)
    assert con.execute("SELECT COUNT(*) FROM expenses").fetchone()[0] == 42
    con.close()
    assert not [f for f in os.listdir(shared) if f.endswith(".envoi")]   # aucun fichier temporaire oublié
    a.state.sync.stop()


def test_ai_writes_are_published_by_background_tick(cloud_env):
    """L'IA écrit dans la copie de travail (autre processus) : le battement suivant la publie."""
    tmp, shared = cloud_env
    a = make(tmp, shared, "PC-A", 101)
    sa = a.state.sync
    Ops(a.state.db).create_trip("Écrit par l'IA")               # sans passer par une page
    assert sa.local_dirty()
    assert sa.publish() == "ok" and not sa.local_dirty()
    assert trip_names(shared / C.DB) == ["Écrit par l'IA"]
    sa.stop()


def test_shared_changed_behind_our_back_is_kept_as_conflict(cloud_env):
    """Le dossier change alors qu'on a la main (synchronisation tardive d'un autre ordinateur) : on n'écrase
    rien ; notre version est déposée à côté, comme une copie en conflit à trancher."""
    tmp, shared = cloud_env
    a = make(tmp, shared, "PC-A", 101)
    ca = TestClient(a)
    ca.post("/voyages", data={"name": "Mien"})
    other = tmp / "autre.db"
    shutil.copyfile(shared / C.DB, other)
    con = sqlite3.connect(other)
    con.execute("UPDATE trips SET name=?", ("Arrivé d'ailleurs",))
    con.commit()
    con.close()
    C.atomic_copy(other, shared / C.DB)
    Ops(a.state.db).create_trip("Saisi pendant ce temps")
    assert a.state.sync.publish() == "conflit"
    side = [f for f in os.listdir(shared) if f.startswith("compta (version de PC-A")]
    assert len(side) == 1 and trip_names(shared / side[0]) == ["Mien", "Saisi pendant ce temps"]
    assert trip_names(shared / C.DB) == ["Arrivé d'ailleurs"]
    page = ca.get("/").text                                       # la version du dossier est reprise…
    assert "Arrivé d" in page
    assert "autre version" in page and "Choisir" in page          # …et le conflit est signalé
    a.state.sync.stop()


def test_pull_keeps_unpublished_local_changes(cloud_env):
    tmp, shared = cloud_env
    a = make(tmp, shared, "PC-A", 101)
    TestClient(a).post("/voyages", data={"name": "Publié"})
    stop(a)
    b = make(tmp, shared, "Mac-B", 202)
    TestClient(b).post("/voyages", data={"name": "De B"})
    stop(b)
    # A avait une saisie non publiée (dossier indisponible à ce moment-là) puis redémarre
    local = tmp / "copie PC-A" / C.DB
    con = sqlite3.connect(local)
    con.execute("UPDATE trips SET name='Pas envoyé'")
    con.commit()
    con.close()
    a2 = make(tmp, shared, "PC-A", 303)
    kept = list((tmp / "copie PC-A" / "sauvegardes").glob("pas-encore-envoyee-*.db"))
    assert len(kept) == 1 and trip_names(kept[0]) == ["Pas envoyé"]
    page = TestClient(a2).get("/").text
    assert "De B" in page and "pas-encore-envoyee" in page and "Rien n" in page
    a2.state.sync.stop()


# ---------------------------------------------------------------- détections
def test_conflicted_copies_detected_and_resolved(cloud_env):
    tmp, shared = cloud_env
    a = make(tmp, shared, "PC-A", 101)
    ca = TestClient(a)
    ca.post("/voyages", data={"name": "Actuel"})
    other = tmp / "x.db"
    shutil.copyfile(shared / C.DB, other)
    con = sqlite3.connect(other)
    con.execute("UPDATE trips SET name='Version Dropbox'")
    con.commit()
    con.close()
    for n in ("compta (conflicted copy 2026-11-03).db", "compta (copie en conflit de Paul).db",
              "compta-PC-DE-PAUL.db", "compta (1).db", "compta 2.db"):
        shutil.copyfile(other, shared / n)
    (shared / "comptable.txt").write_text("sans rapport")
    assert len(a.state.sync.conflicts()) == 5
    page = ca.get("/").text
    assert "5 autres versions des voyages sont apparues" in page
    page = ca.get("/reglages/donnees").text
    assert "Plusieurs versions de tes voyages" in page and "compta-PC-DE-PAUL.db" in page
    # garder la version actuelle : rangée de côté, jamais effacée
    ca.post("/reglages/conflit", data={"nom": "compta (1).db", "choix": "ranger"})
    assert (shared / C.OLD_VERSIONS / "compta (1).db").exists() and len(a.state.sync.conflicts()) == 4
    # utiliser une autre version : elle devient la version publiée ; l'actuelle est gardée
    r = ca.post("/reglages/conflit", data={"nom": "compta 2.db", "choix": "utiliser"}, follow_redirects=True)
    assert "Cette version est maintenant celle de tes voyages" in r.text
    assert trip_names(shared / C.DB) == ["Version Dropbox"]
    olds = os.listdir(shared / C.OLD_VERSIONS)
    assert "compta 2.db" in olds and any(o.startswith("compta (remplacée le") for o in olds)
    assert "Version Dropbox" in ca.get("/").text
    # nom piégé : refusé
    r = ca.post("/reglages/conflit", data={"nom": "../reglages.json", "choix": "utiliser"}, follow_redirects=True)
    assert "Version introuvable" in r.text
    a.state.sync.stop()


def test_missing_folder(cloud_env):
    tmp, shared = cloud_env
    gone = tmp / "Dropbox débranchée" / "TripCompta"
    a = make(tmp, gone, "PC-A", 101)
    assert a.state.sync.problem and a.state.sync.readonly
    assert not gone.exists() and not gone.parent.exists()        # rien n'est créé à sa place
    cl = TestClient(a)
    page = cl.get("/").text
    assert "est introuvable" in page and "logiciel de ton cloud est lancé" in page
    r = cl.post("/voyages", data={"name": "x"}, headers={"referer": "http://t/"}, follow_redirects=True)
    assert "Lecture seule" in r.text
    gone.mkdir(parents=True)                                      # le dossier revient : tout repart
    a.state.sync.problem = a.state.sync.problem                  # (pas de redémarrage nécessaire)
    import compta.web as W
    page = cl.get("/").text
    assert "est introuvable" not in page and not a.state.sync.readonly
    cl.post("/voyages", data={"name": "Après retour"})
    assert trip_names(gone / C.DB) == ["Après retour"]
    a.state.sync.stop()


@pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="droits POSIX")
def test_folder_not_writable(cloud_env):
    tmp, shared = cloud_env
    a = make(tmp, shared, "PC-A", 101)
    TestClient(a).post("/voyages", data={"name": "Avant"})
    stop(a)
    os.chmod(shared, 0o555)
    try:
        b = make(tmp, shared, "Mac-B", 202)
        assert b.state.sync.readonly and "ne peut pas écrire" in b.state.sync.problem
        page = TestClient(b).get("/").text
        assert "Avant" in page and "ne peut pas écrire" in page
    finally:
        os.chmod(shared, 0o755)


def test_cloud_only_file(cloud_env):
    """iCloud : « .compta.db.icloud » à la place du fichier (pas encore téléchargé)."""
    tmp, shared = cloud_env
    (shared / ".compta.db.icloud").write_bytes(b"bplist")
    a = make(tmp, shared, "PC-A", 101)
    assert a.state.sync.readonly
    page = TestClient(a).get("/").text
    assert "pas encore téléchargé" in page and "Toujours garder sur cet appareil" in page
    assert not (shared / C.DB).exists()                         # surtout pas une base vide à sa place


def test_service_names_and_cloud_roots(tmp_path):
    assert C.service_name("/Users/x/Library/CloudStorage/Dropbox/TripCompta") == "Dropbox"
    assert C.service_name("/Users/x/Library/CloudStorage/GoogleDrive-a@b.fr/Mon Drive/TripCompta") == "Google Drive"
    assert C.service_name(r"C:\Users\x\OneDrive\Documents\TripCompta") == "OneDrive"
    assert C.service_name("/Users/x/Library/Mobile Documents/com~apple~CloudDocs/TripCompta") == "iCloud Drive"
    assert C.service_name("/home/x/kDrive/TripCompta") == "kDrive"
    assert C.service_name("/home/x/Documents/boxes/TripCompta") is None
    home = tmp_path / "maison"
    (home / "Library/CloudStorage/GoogleDrive-paul@exemple.fr/Mon Drive").mkdir(parents=True)
    (home / "Library/CloudStorage/Dropbox").mkdir(parents=True)
    (home / "Library/Mobile Documents/com~apple~CloudDocs/TripCompta").mkdir(parents=True)
    (home / "Library/Mobile Documents/com~apple~CloudDocs/TripCompta/compta.db").write_bytes(b"")
    (home / "OneDrive - Asso").mkdir()
    roots = C.cloud_roots(home=home, env={"OneDrive": str(home / "OneDrive - Asso")}, platform="darwin")
    got = {r["name"]: (Path(r["target"]).relative_to(home).as_posix(), r["has_data"]) for r in roots}
    assert got == {
        "Dropbox": ("Library/CloudStorage/Dropbox/TripCompta", False),
        "Google Drive (paul@exemple.fr)": ("Library/CloudStorage/GoogleDrive-paul@exemple.fr/Mon Drive/TripCompta", False),
        "iCloud Drive": ("Library/Mobile Documents/com~apple~CloudDocs/TripCompta", True),
        "OneDrive": ("OneDrive - Asso/TripCompta", False)}


# ---------------------------------------------------------------- justificatifs à trier
def test_inbox_receipts(cloud_env):
    tmp, shared = cloud_env
    a = make(tmp, shared, "PC-A", 101)
    tid = seed(a)
    inbox = shared / C.INBOX
    from PIL import Image
    Image.new("RGB", (40, 30), (200, 80, 40)).save(inbox / "ticket péage.jpg")
    (inbox / "facture.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    (inbox / ".DS_Store").write_bytes(b"x")
    (inbox / "notes.txt").write_text("pas un justificatif")
    cl = TestClient(a)
    assert "📥 2 justificatifs à trier" in cl.get(f"/v/{tid}").text
    page = cl.get(f"/v/{tid}/justificatifs").text
    assert "ticket péage.jpg" in page and "facture.pdf" in page and "notes.txt" not in page
    assert cl.get("/a-trier/vignette", params={"nom": "ticket péage.jpg"}).headers["content-type"] == "image/jpeg"
    assert cl.get("/a-trier/fichier", params={"nom": "../compta.db"}).status_code == 404
    with a.state.db.Session() as s:
        gite = next(e.id for e in s.get(M.Trip, tid).expenses if e.label == "Gîte")
    r = cl.post(f"/v/{tid}/justificatifs/a-trier", data={"nom": ["ticket péage.jpg"], "expenses": [str(gite)]},
                follow_redirects=True)
    assert "1 justificatif(s) ajouté(s) : J01" in r.text
    assert (inbox / C.INBOX_DONE / "ticket péage.jpg").exists() and not (inbox / "ticket péage.jpg").exists()
    with a.state.db.Session() as s:
        rec = s.get(M.Trip, tid).receipts[0]
        assert rec.original_name == "ticket péage.jpg" and [l.expense_id for l in rec.links] == [gite]
        assert (shared / C.RECEIPTS / rec.stored_name).exists()      # dans le dossier partagé
    assert not (shared / C.RECEIPTS / ".vignettes").exists()      # vignettes : sur l'ordinateur
    assert "📥 1 justificatif à trier" in cl.get(f"/v/{tid}").text
    a.state.sync.stop()


# ---------------------------------------------------------------- choisir le dossier, migration aller-retour
def test_migration_round_trip_same_amounts(tmp_path, monkeypatch):
    monkeypatch.delenv("TRIPCOMPTA_DATA_DIR", raising=False)
    monkeypatch.delenv("COMPTA_DATA_DIR", raising=False)
    monkeypatch.setenv("TRIPCOMPTA_CONFIG", str(tmp_path / "conf" / "reglages.json"))
    docs = tmp_path / "Mes Documents"
    monkeypatch.setenv("TRIPCOMPTA_DOCUMENTS", str(docs))
    app = create_app()                                           # dossier par défaut : Documents/TripCompta
    assert app.state.db.home == docs / "TripCompta" and app.state.sync is None
    tid = seed(app)
    assert amounts(app, tid) == EXPECTED
    (app.state.db.receipts_dir / "1_ticket.jpg").write_bytes(b"jpg")
    cl = TestClient(app)
    page = cl.get("/reglages/donnees").text
    assert "Où sont mes voyages" in page and "Sur cet ordinateur" in page
    cloud = tmp_path / "Dropbox (Asso)" / "TripCompta"
    cloud.parent.mkdir()                                         # le dossier du service existe, pas encore TripCompta
    r = cl.post("/reglages/donnees", data={"action": "ranger", "dossier": "__autre__", "autre": str(cloud),
                                           "mode": "copier"})
    assert "Tes voyages ont été copiés" in r.text
    assert (cloud / C.DB).exists() and (cloud / C.RECEIPTS / "1_ticket.jpg").exists() and (cloud / C.INBOX).is_dir()
    assert not (cloud / "sauvegardes").exists() and not (cloud / ".annuler").exists()   # restent sur l'ordinateur
    assert (docs / "TripCompta" / C.DB).exists()                # l'ancien dossier n'est pas touché

    app2 = create_app()                                          # « redémarrage »
    assert app2.state.sync is not None and app2.state.db.home == cloud and not app2.state.sync.readonly
    assert amounts(app2, tid) == EXPECTED
    cl2 = TestClient(app2)
    page = cl2.get("/reglages/donnees").text
    assert "Dans <b>Dropbox</b>" in page
    assert "☁️ Rangé dans Dropbox" in cl2.get("/").text
    # déjà des voyages dans ce dossier : on ne copie pas par-dessus
    r = cl2.post("/reglages/donnees", data={"action": "ranger", "dossier": "__autre__", "autre": str(cloud),
                                            "mode": "copier"})
    assert "déjà rangés dans ce dossier" in r.text
    # retour sur cet ordinateur : copie ; ce qui y était est gardé dans ses sauvegardes
    Ops(app2.state.db).add_participants(tid, ["David"], inclure_dans_depenses_communes=False)   # saisie « cloud »
    app.state.db.engine.dispose()                                # (la 1re app, arrêtée, n'a plus la base ouverte)
    r = cl2.post("/reglages/donnees", data={"action": "local"})
    assert "de nouveau rangés sur cet ordinateur" in r.text
    assert list((docs / "TripCompta" / "sauvegardes").glob("avant-retour-*.db"))
    assert not (cloud / C.LOCK).exists()                         # verrou libéré
    app3 = create_app()
    assert app3.state.sync is None and app3.state.db.home == docs / "TripCompta"
    got = amounts(app3, tid)
    assert got["David"] == (0, 0) and {k: v for k, v in got.items() if k != "David"} == EXPECTED
    assert (docs / "TripCompta" / "justificatifs" / "1_ticket.jpg").exists()
    # 2e ordinateur : « Utiliser les voyages déjà dans ce dossier »
    r = TestClient(app3).post("/reglages/donnees", data={"action": "ranger", "dossier": str(cloud),
                                                        "mode": "utiliser"})
    assert "va utiliser les voyages rangés" in r.text
    app2.state.db.engine.dispose()                               # (arrêtée : sa copie de travail est refermée)
    app4 = create_app()
    got = amounts(app4, tid)
    assert app4.state.db.home == cloud and got.pop("David") == (0, 0) and got == EXPECTED
    app4.state.sync.stop()


def test_legacy_synced_folder_can_be_protected(tmp_path, monkeypatch):
    """Ancien réglage : un dossier synchronisé choisi sans protection → bouton « Protéger ce dossier »."""
    monkeypatch.delenv("TRIPCOMPTA_DATA_DIR", raising=False)
    monkeypatch.setenv("TRIPCOMPTA_CONFIG", str(tmp_path / "conf" / "reglages.json"))
    from compta import paths as P
    old = tmp_path / "Dropbox" / "TripCompta"
    P.write_config(data_dir=str(old))
    app = create_app()
    tid = seed(app)
    page = TestClient(app).get("/reglages/donnees").text
    assert "n'est pas protégé" in page and "Protéger ce dossier" in page
    TestClient(app).post("/reglages/donnees", data={"action": "proteger"})
    app2 = create_app()
    assert app2.state.sync is not None and app2.state.db.home == old and amounts(app2, tid) == EXPECTED
    app2.state.sync.stop()


# ---------------------------------------------------------------- écrans : clair, sombre, téléphone
def test_screens_light_dark_phone(cloud_env):
    import uvicorn
    from playwright.sync_api import sync_playwright
    from tests.conftest import _free_port
    tmp, shared = cloud_env
    a = make(tmp, shared, "PC-A", 101)
    tid = seed(a)
    assert a.state.sync.publish() == "ok"                       # (écrit par l'IA : publié au battement suivant)
    b = make(tmp, shared, "MacBook Pro de Paul", 202)
    (shared / "compta (conflicted copy 2026-11-03).db").write_bytes((shared / C.DB).read_bytes())
    from PIL import Image
    Image.new("RGB", (40, 30), (40, 120, 200)).save(shared / C.INBOX / "IMG_2041.jpg")
    servers = []
    for app in (a, b):
        port = _free_port()
        srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        th = threading.Thread(target=srv.run, daemon=True)
        th.start()
        while not srv.started:
            time.sleep(0.05)
        servers.append((srv, th, f"http://127.0.0.1:{port}"))
    shots = os.environ.get("TRIPCOMPTA_SHOTS")
    try:
        with sync_playwright() as p:
            br = p.chromium.launch()
            for dark in (False, True):
                for w in (1280, 390):
                    tag = ("sombre" if dark else "clair") + ("-mobile" if w < 500 else "")
                    pg = br.new_page(viewport={"width": w, "height": 900}, color_scheme="dark" if dark else "light")
                    errs = []
                    pg.on("pageerror", lambda e: errs.append(str(e)))
                    for name, url in (("reglages", servers[0][2] + "/reglages/donnees"),
                                      ("lecture-seule", servers[1][2] + f"/v/{tid}"),
                                      ("a-trier", servers[0][2] + f"/v/{tid}/justificatifs")):
                        pg.goto(url)
                        wide = pg.evaluate(f"""() => [...document.querySelectorAll('body *')]
                            .filter(e => e.getBoundingClientRect().right > {w + 1})
                            .map(e => e.tagName + '.' + e.className + ' « ' + (e.textContent || '').trim().slice(0, 50) + ' »')""")
                        assert pg.evaluate("document.documentElement.scrollWidth") <= w + 1, (name, tag, wide[:6])
                        if shots:
                            Path(shots).mkdir(parents=True, exist_ok=True)
                            pg.screenshot(path=f"{shots}/cloud-{name}-{tag}.png", full_page=True)
                    assert errs == []
                    pg.close()
            pg = br.new_page()
            pg.goto(servers[1][2] + f"/v/{tid}")
            assert "ouverts sur « PC-A »" in pg.locator(".cloud-banner.ro").inner_text()
            assert "Lecture seule" in pg.locator(".cloud-banner.ro").inner_text()
            pg.goto(servers[0][2] + "/reglages/donnees")
            pg.locator("input[name=autre]").fill("/tmp/ailleurs")
            assert pg.locator("input[value=__autre__]").is_checked()
            br.close()
    finally:
        for srv, th, _ in servers:
            srv.should_exit = True
            th.join(5)
        a.state.sync.stop()
        b.state.sync.stop()


def test_ai_server_gets_shared_folder(cloud_env):
    """L'IA (serveur MCP, autre processus) travaille sur la copie de travail ; ses justificatifs vont dans le
    dossier partagé."""
    tmp, shared = cloud_env
    a = make(tmp, shared, "PC-A", 101)
    tid = seed(a)
    import html
    page = html.unescape(TestClient(a).get(f"/v/{tid}/ia").text).replace("\\\\", "\\")   # JSON : \\ → \
    assert '"--data-dir"' in page and str(tmp / "copie PC-A") in page
    assert '"--home"' in page and str(shared) in page
    from compta.mcp_server import build_server
    build_server(str(tmp / "copie PC-A"), str(shared))
    from compta.db import Database
    d = Database(tmp / "copie PC-A", home=shared)
    assert d.receipts_dir == shared / C.RECEIPTS and d.backups_dir == tmp / "copie PC-A" / "sauvegardes"
    a.state.sync.stop()


def test_cloud_only_file_is_never_read(cloud_env, monkeypatch):
    """Fichier « en ligne seulement » (vrai cas iCloud vérifié sur Mac : drapeau SF_DATALESS) : l'app ne le lit pas
    (le lire forcerait un téléchargement qui bloquerait la page) ; elle reprend dès qu'il est téléchargé."""
    tmp, shared = cloud_env
    a = make(tmp, shared, "PC-A", 101)
    TestClient(a).post("/voyages", data={"name": "Publié"})
    stop(a)
    b = make(tmp, shared, "Mac-B", 202)
    read = []
    real_hash = C.file_hash
    monkeypatch.setattr(C, "file_hash", lambda p: read.append(Path(p).name) or real_hash(p))
    monkeypatch.setattr(C, "cloud_only", lambda p: Path(p) == shared / C.DB)
    os.utime(shared / C.DB, None)                                # « changé » dans le dossier
    b.state.sync._seen_sig = None
    assert b.state.sync.shared_changed() is False and C.DB not in read
    stop(b)


def test_older_version_refuses_shared_folder(cloud_env):
    """Une version d'avant le dossier synchronisé (1.0.x) ne doit pas écrire dans un dossier enregistré par une
    version plus récente : elle affiche « installe la nouvelle version » et ne touche à rien."""
    from compta import updater as U
    from compta import __version__
    tmp, shared = cloud_env
    a = make(tmp, shared, "PC-A", 101)
    TestClient(a).post("/voyages", data={"name": "Récent"})
    stop(a)
    before = (shared / C.DB).read_bytes()
    old = U.Updater(info=dict(version="1.0.0-beta3", commit="x", dev=False), fetch=lambda: None)
    assert __version__ > "1.0.0"
    app = create_app(shared, updater=old)                        # l'ancienne app : directement sur le dossier
    r = TestClient(app).get("/")
    import html
    assert r.status_code == 409 and __version__ in html.unescape(r.text)
    assert (shared / C.DB).read_bytes() == before                # rien d'écrit
    assert not (shared / ".annuler").exists() and not (shared / "sauvegardes").exists()


def test_same_computer_second_window_message(cloud_env):
    """Deux exemplaires sur le MÊME ordinateur : le message le dit (pas « ouverts sur <mon propre Mac> »)."""
    tmp, shared = cloud_env
    a = make(tmp, shared, "MacBook de test", os.getppid())       # processus vivant, autre que celui-ci
    b = make(tmp, shared, "MacBook de test", os.getpid())
    assert not a.state.sync.readonly and b.state.sync.readonly
    page = TestClient(b).get("/").text.replace("&#39;", "'")
    assert "déjà ouvert sur cet ordinateur" in page and "ouverts sur « MacBook de test »" not in page
    assert "Prendre la main" not in page
    r = TestClient(b).post("/voyages", data={"name": "x"}, headers={"referer": "http://t/"}, follow_redirects=True)
    assert "déjà ouvert sur cet ordinateur" in r.text.replace("&#39;", "'")
    stop(a)
    stop(b)


def test_launching_twice_shows_the_running_one(monkeypatch):
    """Cliquer sur l'icône alors que Trip Compta tourne déjà : l'onglet de celle qui tourne s'ouvre, aucune 2e
    n'est lancée (sinon elle serait en lecture seule et on ne saurait plus laquelle utiliser)."""
    import importlib
    import uvicorn
    from tests.conftest import _free_port
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    run = importlib.import_module("run")
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    while not srv.started:
        time.sleep(0.05)
    try:
        assert run.running_instance(port, 1) == f"http://127.0.0.1:{port}/"
        assert run.running_instance(_free_port(), 1) is None
        opened, started = [], []
        monkeypatch.setattr(run, "running_instance", lambda *a: f"http://127.0.0.1:{port}/")
        monkeypatch.setattr(run.webbrowser, "open", opened.append)
        monkeypatch.setattr("compta.web.create_app", lambda *a, **k: started.append(1))
        monkeypatch.delenv("TRIPCOMPTA_DATA_DIR", raising=False)
        monkeypatch.delenv("TRIPCOMPTA_RESTARTED", raising=False)
        monkeypatch.setattr(sys, "argv", ["run.py"])
        run.main()
        assert opened == [f"http://127.0.0.1:{port}/"] and started == []
    finally:
        srv.should_exit = True
        th.join(5)


def test_take_over_while_other_is_alive(cloud_env):
    """« Prendre la main » même si l'autre ordinateur est actif : on a la main, la dernière version publiée est
    reprise, et l'autre passe en lecture seule à son battement suivant (sans rien écraser)."""
    tmp, shared = cloud_env
    a = make(tmp, shared, "PC-A", 101)
    b = make(tmp, shared, "Mac-B", 202)
    ca, cb = TestClient(a), TestClient(b)
    ca.post("/voyages", data={"name": "Saisi sur A"})
    r = cb.post("/reglages/prendre-la-main", headers={"referer": "http://t/"}, follow_redirects=True)
    assert "Tu as la main" in r.text and "PC-A" in r.text and not b.state.sync.readonly
    assert "Saisi sur A" in cb.get("/").text                     # la version de A a été reprise
    cb.post("/voyages", data={"name": "Saisi sur B"})
    a.state.sync.beat()                                          # A apprend qu'on a pris la main
    assert a.state.sync.readonly
    page = ca.get("/").text
    assert "« Mac-B » a pris la main" in page and "Saisi sur B" in page
    r = ca.post("/voyages", data={"name": "Trop tard"}, headers={"referer": "http://t/"}, follow_redirects=True)
    assert "Lecture seule" in r.text
    assert trip_names(shared / C.DB) == ["Saisi sur A", "Saisi sur B"]
    r = cb.post("/reglages/prendre-la-main", headers={"referer": "http://t/"}, follow_redirects=True)
    assert "Tu as déjà la main" in r.text
    stop(a)
    stop(b)


def test_this_computer_is_an_option_of_the_list(tmp_path, monkeypatch):
    """« 💻 Sur cet ordinateur » est une option de « Ranger mes voyages ailleurs », avec les deux modes :
    y copier ses voyages (l'ancien contenu est gardé dans les sauvegardes), ou reprendre ceux qui y sont déjà."""
    monkeypatch.delenv("TRIPCOMPTA_DATA_DIR", raising=False)
    monkeypatch.setenv("TRIPCOMPTA_CONFIG", str(tmp_path / "conf" / "reglages.json"))
    docs = tmp_path / "Mes Documents"
    monkeypatch.setenv("TRIPCOMPTA_DOCUMENTS", str(docs))
    app = create_app()                                           # sur cet ordinateur : option « actuel »
    tid = seed(app)
    page = TestClient(app).get("/reglages/donnees").text
    assert "💻 Sur cet ordinateur" in page and "Revenir sur cet ordinateur" not in page
    assert re.search(r'value="__local__"[^>]*disabled', page)
    cloud = tmp_path / "Dropbox" / "TripCompta"
    cloud.parent.mkdir()
    TestClient(app).post("/reglages/donnees", data={"dossier": "__autre__", "autre": str(cloud), "mode": "copier"})
    app.state.db.engine.dispose()
    app2 = create_app()                                          # dans le cloud : l'option redevient choisissable
    cl = TestClient(app2)
    page = cl.get("/reglages/donnees").text
    assert not re.search(r'value="__local__"[^>]*disabled', page) and "contient une ancienne copie" in page
    assert re.search(r'value="__local__" data-has="0"', page)    # « Y copier » reste choisi : rien ne se perd
    Ops(app2.state.db).create_trip("Saisi dans le cloud")
    app2.state.sync.publish()
    # reprendre les voyages déjà sur cet ordinateur : sans la saisie faite dans le cloud
    r = cl.post("/reglages/donnees", data={"dossier": "__local__", "mode": "utiliser"})
    assert "va utiliser les voyages rangés sur cet ordinateur" in r.text
    app3 = create_app()
    assert app3.state.sync is None and amounts(app3, tid) == EXPECTED
    assert "Saisi dans le cloud" not in TestClient(app3).get("/").text
    app3.state.db.engine.dispose()
    # ou y copier ses voyages : la saisie revient, l'ancien contenu est gardé dans les sauvegardes
    from compta import paths as P
    P.write_config(data_dir=str(cloud), partage=True)
    app4 = create_app()
    r = TestClient(app4).post("/reglages/donnees", data={"dossier": "__local__", "mode": "copier"})
    assert "de nouveau rangés sur cet ordinateur" in r.text
    assert list((docs / "TripCompta" / "sauvegardes").glob("avant-retour-*.db"))
    app4.state.db.engine.dispose()
    app5 = create_app()
    assert app5.state.sync is None and "Saisi dans le cloud" in TestClient(app5).get("/").text
    app5.state.db.engine.dispose()


def test_settings_page(cloud_env):
    """⚙️ Réglages : un bouton sur chaque page, et une page qui donne l'état de chaque réglage d'un coup d'œil."""
    import uvicorn
    from playwright.sync_api import sync_playwright
    from tests.conftest import _free_port
    tmp, shared = cloud_env
    a = make(tmp, shared, "PC-A", 101)
    tid = seed(a)
    cl = TestClient(a)
    assert 'href="/reglages"' in cl.get(f"/v/{tid}").text
    page = cl.get("/reglages").text
    assert "Où sont mes voyages" in page and "Dans Google Drive" in page and "Non protégés" in page
    assert "Version et mises à jour" in page and "Protéger ›" in page
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(a, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    while not srv.started:
        time.sleep(0.05)
    shots = os.environ.get("TRIPCOMPTA_SHOTS")
    try:
        with sync_playwright() as p:
            br = p.chromium.launch()
            for dark in (False, True):
                for w in (1280, 390):
                    pg = br.new_page(viewport={"width": w, "height": 900}, color_scheme="dark" if dark else "light")
                    pg.goto(f"http://127.0.0.1:{port}/v/{tid}")
                    pg.get_by_role("link", name="Réglages").click()
                    assert pg.url.endswith("/reglages")
                    assert pg.evaluate("document.documentElement.scrollWidth") <= w + 1
                    if shots:
                        Path(shots).mkdir(parents=True, exist_ok=True)
                        tag = ("sombre" if dark else "clair") + ("-mobile" if w < 500 else "")
                        pg.screenshot(path=f"{shots}/reglages-{tag}.png", full_page=True)
                    pg.get_by_role("link", name=re.compile("Mot de passe")).click()
                    assert pg.url.endswith("/reglages/protection") and pg.get_by_text("Protéger mes voyages…").is_visible()
                    pg.close()
            br.close()
    finally:
        srv.should_exit = True
        th.join(5)
        stop(a)


def test_no_false_alarm_when_nothing_really_changed(cloud_env):
    """La copie de travail a « bougé » sans vraie saisie (réécrite à l'identique, version de l'application) : la
    version du dossier est reprise sans message ni copie de secours (fausse alerte vue en vrai le 4/11)."""
    tmp, shared = cloud_env
    a = make(tmp, shared, "PC-A", 101)
    TestClient(a).post("/voyages", data={"name": "Publié"})
    stop(a)
    b = make(tmp, shared, "Mac-B", 202)
    TestClient(b).post("/voyages", data={"name": "De B"})
    stop(b)
    local = tmp / "copie PC-A" / C.DB                            # rien de saisi ici : seulement « touchée »
    con = sqlite3.connect(local)
    con.execute("UPDATE settings SET value='9.9.9-autre' WHERE key='app_version'")
    con.commit()
    con.execute("VACUUM")
    con.close()
    a2 = make(tmp, shared, "PC-A", 303)
    assert not list((tmp / "copie PC-A" / "sauvegardes").glob("pas-encore-envoyee-*"))
    page = TestClient(a2).get("/").text
    assert "De B" in page and "pas-encore-envoyee" not in page and "Attention" not in page
    stop(a2)


def test_failed_publish_is_retried(cloud_env, monkeypatch):
    """Envoi raté (dossier bloqué un instant) : la saisie reste « à envoyer » et part à l'essai suivant."""
    tmp, shared = cloud_env
    a = make(tmp, shared, "PC-A", 101)
    Ops(a.state.db).create_trip("Saisie importante")
    real = C.atomic_copy
    monkeypatch.setattr(C, "atomic_copy", lambda *x, **k: (_ for _ in ()).throw(PermissionError("bloqué")))
    assert a.state.sync.publish() == "erreur" and a.state.sync.local_dirty()
    monkeypatch.setattr(C, "atomic_copy", real)
    assert a.state.sync.publish() == "ok" and trip_names(shared / C.DB) == ["Saisie importante"]
    stop(a)


def test_closing_the_tab_hands_over(cloud_env):
    """Personne ne clique sur « Arrêter » : quand plus aucun onglet n'est ouvert depuis 2 minutes, l'app rend la
    main ; l'autre ordinateur la prend tout seul. Un onglet rouvert la reprend si elle est libre, sinon s'ouvre en
    lecture seule. Rien n'est perdu au passage."""
    tmp, shared = cloud_env
    a = make(tmp, shared, "PC du club", 101)
    b = make(tmp, shared, "Mac de Stef", 202)
    ca, cb = TestClient(a), TestClient(b)
    Ops(a.state.db).create_trip("Saisi par l'IA, pas encore envoyé")   # sera envoyé en rendant la main
    assert not a.state.idle_check()                              # un onglet vient d'être vu : rien
    assert b.state.sync.readonly
    # les « /ping » d'autres programmes (lancement, outils) ne comptent pas comme un onglet ouvert
    ca.get("/ping")
    assert a.state.idle_check(now=time.time() + C.IDLE_AFTER + 1)
    assert a.state.sync.idle and not (shared / C.LOCK).exists()
    assert trip_names(shared / C.DB) == ["Saisi par l'IA, pas encore envoyé"]
    b.state.sync.beat()                                          # le Mac prend la main tout seul
    assert not b.state.sync.readonly
    page = cb.get("/").text
    assert "Saisi par l" in page and "Lecture seule" not in page
    cb.post("/voyages", data={"name": "Saisi sur le Mac"})
    a.state.sync.beat()                                          # le PC, sans onglet, ne reprend rien tout seul
    assert a.state.sync.idle
    page = ca.get("/").text                                      # onglet rouvert sur le PC : le Mac a la main
    assert "Lecture seule" in page and "« Mac de Stef »" in page and "Saisi sur le Mac" in page
    assert not a.state.sync.idle and a.state.sync.readonly
    # le Mac est fermé (onglet) à son tour : le PC reprend la main dès son prochain onglet
    assert b.state.idle_check(now=time.time() + C.IDLE_AFTER + 1)
    a.state.sync.beat()
    assert not a.state.sync.readonly
    ca.post("/voyages", data={"name": "Saisi sur le PC"})
    assert trip_names(shared / C.DB) == sorted(["Saisi par l'IA, pas encore envoyé", "Saisi sur le Mac",
                                                "Saisi sur le PC"])
    # un onglet qui interroge l'app (/ping?vu=1) la garde éveillée
    a.state.last_seen = 0
    ca.get("/ping", params={"vu": "1"})
    assert not a.state.idle_check()
    stop(a)
    stop(b)


def test_open_tab_keeps_the_hand(cloud_env):
    """Dans un vrai navigateur : un onglet ouvert, sans aucun clic, signale sa présence (toutes les 4 s)."""
    import uvicorn
    from playwright.sync_api import sync_playwright
    from tests.conftest import _free_port
    tmp, shared = cloud_env
    a = make(tmp, shared, "PC du club", 101)
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(a, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    while not srv.started:
        time.sleep(0.05)
    try:
        with sync_playwright() as p:
            br = p.chromium.launch()
            pg = br.new_page()
            pg.goto(f"http://127.0.0.1:{port}/")
            a.state.last_seen = 0                                # comme si plus personne n'était là…
            pg.wait_for_timeout(6000)                            # …mais l'onglet est ouvert
            assert time.time() - a.state.last_seen < 6 and not a.state.idle_check()
            br.close()
    finally:
        srv.should_exit = True
        th.join(5)
        stop(a)


def test_last_save_time_shown_right_after_launch(cloud_env):
    """Juste après le lancement (rien modifié) : l'heure du dernier enregistrement dans le dossier synchronisé
    s'affiche quand même (date du fichier), avec la date si ce n'est pas aujourd'hui."""
    import os
    import time as _t
    tmp, shared = cloud_env
    a = make(tmp, shared, "PC-A", 101)
    TestClient(a).post("/voyages", data={"name": "Voyage"})
    stop(a)
    b = make(tmp, shared, "PC-A", 102)                       # nouveau lancement : rien publié depuis
    assert b.state.sync.published_at is None
    hm = _t.strftime("%H h %M", _t.localtime((shared / C.DB).stat().st_mtime)).lstrip("0")
    page = TestClient(b).get("/").text
    assert "enregistré à " in page and hm.replace(" h ", " h ") in page
    old = _t.time() - 3 * 86400                              # enregistré il y a 3 jours (autre ordinateur, par ex.)
    os.utime(shared / C.DB, (old, old))
    page = TestClient(b).get("/").text
    assert "enregistré le " in page and _t.strftime("%d/%m", _t.localtime(old)) in page
    stop(b)
