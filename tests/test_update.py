"""Mises à jour : versions, choix du fichier, vérification (API GitHub SIMULÉE, jamais appelée pour de vrai),
empreinte, sauvegarde avant mise à jour, protection d'une base ouverte par une version plus récente, pages."""
import hashlib
import html
import io
import json
import sqlite3
import tarfile
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from compta import signature as SIG
from compta import updater as U
from compta import version as V
from compta.db import SCHEMA, Database, NewerDatabaseError, stored_schema, stored_version
from compta.web import create_app

RELEASE = dict(tag_name="v1.1.0", body="- Nouveau : le mail au trésorier\n- Correction : arrondis",
               html_url="https://github.com/Scendari/tripcompta/releases/tag/v1.1.0",
               published_at="2026-11-02T10:00:00Z",
               assets=[{"name": n, "browser_download_url": f"https://exemple.invalid/{n}"} for n in
                       ("TripCompta-mac-arm64.zip", "TripCompta-mac-intel.zip",
                        "TripCompta-windows-setup.exe", "TripCompta-essai.tar.gz", "SHA256SUMS.txt",
                        "SHA256SUMS.txt.sig")])
TEST_PRIVATE, TEST_PUBLIC = SIG.new_key()        # clé d'essai : la vraie clé privée n'est jamais sur cet ordinateur


@pytest.fixture(autouse=True)
def own_config(tmp_path, monkeypatch):
    """Chaque test a ses propres réglages d'ordinateur (et donc son dossier de mise à jour), et une clé d'essai."""
    monkeypatch.setenv("TRIPCOMPTA_CONFIG", str(tmp_path / "conf" / "reglages.json"))
    monkeypatch.delenv("TRIPCOMPTA_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(SIG, "PUBLIC_KEYS", (TEST_PUBLIC,))


def serve(u, sums_text, version="1.1.0", key=None):
    """La publication simulée sert ce fichier d'empreintes (précédé de sa version) et sa signature."""
    text = f"{SIG.VERSION_LINE}{version}\n{sums_text}"
    sig = SIG.sign(text.encode("utf-8"), key or TEST_PRIVATE)
    u._read_text = lambda url: sig if url.endswith(".sig") else text
    return text, sig


class FakeGitHub:
    def __init__(self, release=RELEASE, fail=False):
        self.release, self.fail, self.calls = release, fail, 0

    def __call__(self):
        self.calls += 1
        if self.fail:
            raise OSError("pas d'internet")
        return U.release_info(self.release)


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make(version="1.0.0", dev=False, fetch=None, now=None, download=None):
    return U.Updater(info=dict(version=version, commit="abc1234", dev=dev), fetch=fetch or FakeGitHub(),
                     now=now or Clock(), download=download)


# ---------------------------------------------------------------- versions

def test_version_order():
    chain = ["1.0.0-dev", "1.0.0-alpha1", "1.0.0-beta1", "1.0.0-beta2", "1.0.0-beta10", "1.0.0-rc1", "1.0.0",
             "1.0.1", "1.1.0", "1.10.0", "2.0.0"]
    parsed = [V.parse(v) for v in chain]
    assert parsed == sorted(parsed) and len(set(parsed)) == len(chain)
    assert V.is_newer("1.0.0", "1.0.0-beta1") and not V.is_newer("1.0.0-beta1", "1.0.0")
    assert V.is_newer("v1.0.1", "1.0.0") and not V.is_newer("1.0.0", "1.0.0")
    assert V.parse("v1.2.3") == (1, 2, 3, 9, 0) and V.parse("1.2.3-beta.2") == (1, 2, 3, 2, 2)
    for bad in ("", None, "1.2", "abc", "1.2.3-truc", "1.2.3.4"):
        assert V.parse(bad) is None
        assert not V.is_newer(bad, "1.0.0") and not V.is_newer("1.0.0", bad)
    assert V.base("1.2.0-beta3") == V.base("1.2.0") == (1, 2, 0)


def test_build_info_from_source_is_dev():
    info = V.build_info()
    from compta import __version__
    assert info["dev"] is True and info["version"] == f"{__version__}-dev"
    assert V.label(dict(version="1.0.0-dev", commit="a3f9c21", dev=True)) == "1.0.0-dev (a3f9c21)"
    assert V.label(dict(version="1.0.0", commit="a3f9c21", dev=False)) == "1.0.0"


def test_asset_for_each_computer():
    assert U.asset_name("darwin", "arm64") == "TripCompta-mac-arm64.zip"
    assert U.asset_name("darwin", "x86_64") == "TripCompta-mac-intel.zip"
    assert U.asset_name("win32", "AMD64") == "TripCompta-windows-setup.exe"      # l'installeur
    assert U.asset_name("linux", "x86_64") is None             # plus de version Linux
    assert U.asset_name("linux", "aarch64") is None
    assert U.asset_name("win32", "ARM64") is None
    assert U.asset_name("freebsd14", "amd64") is None


def test_release_info_and_sums():
    info = U.release_info(RELEASE)
    assert info["version"] == "1.1.0" and info["date"] == "2026-11-02" and "trésorier" in info["notes"]
    assert info["assets"]["SHA256SUMS.txt"].endswith("SHA256SUMS.txt") and len(info["assets"]) == 6
    a, b = "a" * 64, "B" * 64
    sums = U.parse_sums(f"{a}  TripCompta-windows-setup.exe\n{b} *TripCompta-mac-arm64.zip\n\nn'importe quoi\n")
    assert sums == {"TripCompta-windows-setup.exe": a, "TripCompta-mac-arm64.zip": "b" * 64}


def test_install_location():
    assert U.install_location(frozen=False) is None                       # code source : rien à remplacer
    mac = U.install_location("/Applications/Trip Compta.app/Contents/MacOS/Trip Compta", "darwin", True)
    assert mac == (Path("/Applications/Trip Compta.app").resolve(), str(Path("Contents/MacOS/Trip Compta")))
    assert U.install_location("/tmp/x/Trip Compta", "darwin", True) is None   # pas dans un .app
    lin = U.install_location("/opt/apps/Trip Compta/Trip Compta", "linux", True)
    assert lin == (Path("/opt/apps/Trip Compta").resolve(), "Trip Compta")


def test_clean_args():
    assert U.clean_args(["--port", "8770", "--no-browser", "--data-dir", "/x y", "--port=9", "-psn_0_123"]) == \
        ["--data-dir", "/x y"]


# ---------------------------------------------------------------- vérification

def test_check_once_a_day_then_cached():
    gh, clock = FakeGitHub(), Clock()
    u = make(fetch=gh, now=clock)
    assert u.due() and u.check()["latest"]["version"] == "1.1.0" and gh.calls == 1
    clock.t += 3600 * 23                                       # moins d'un jour : pas de nouvelle demande
    assert not u.due() and u.check()["skipped"] and gh.calls == 1
    assert u.available()["version"] == "1.1.0"                 # gardé en mémoire (sans internet)
    clock.t += 3600 * 2
    u.check()
    assert gh.calls == 2
    u.check(force=True)                                        # « Vérifier maintenant » : toujours
    assert gh.calls == 3


def test_offline_is_silent():
    gh = FakeGitHub(fail=True)
    u = make(fetch=gh)
    res = u.check()                                            # ne lève jamais
    assert res["ok"] is False and "pas d'internet" in res["error"] and u.available() is None
    assert u.checked_at() is None and u.due()                  # on réessaiera
    # une réponse illisible non plus
    u2 = make(fetch=lambda: dict(version="n'importe quoi", assets={}))
    assert u2.check()["ok"] is False and u2.latest() is None


def test_auto_off_and_dev_never_check():
    gh = FakeGitHub()
    u = make(fetch=gh)
    u.set_auto(False)
    assert not u.auto and not u.due() and u.check()["skipped"] and gh.calls == 0
    u.check(force=True)
    assert gh.calls == 1                                       # le bouton marche quand même
    d = make(version="1.0.0-dev", dev=True, fetch=gh)
    assert not d.due() and d.start_background() is None
    d.check(force=True)
    assert d.available() is None and d.banner() is None        # jamais proposé en développement
    assert d.installable() == (False, "C'est une version de développement : elle se met à jour avec le code source.")


def test_env_disables_background(monkeypatch):
    monkeypatch.setenv("TRIPCOMPTA_NO_UPDATE_CHECK", "1")
    u = make()
    assert not u.due() and u.start_background() is None


def test_available_only_if_newer_and_later():
    u = make(version="1.1.0")
    u.check()
    assert u.available() is None                               # même version
    u = make(version="1.0.0-beta3")
    assert u.available()["version"] == "1.1.0" and u.banner()
    u.later("1.1.0")
    assert u.banner() is None and u.available()                # bandeau masqué, toujours dans « À propos »
    PRE = dict(RELEASE, tag_name="v1.2.0")
    u2 = make(fetch=FakeGitHub(PRE))
    u2.check(force=True)
    assert u2.banner()["version"] == "1.2.0"                   # une version encore plus récente : de nouveau


# ---------------------------------------------------------------- base de données

def test_db_remembers_version_and_refuses_newer_structure(tmp_path):
    """Ce qui compte, c'est le numéro de structure écrit dans la base : une application qui en connaît un plus
    petit refuse d'y toucher ; deux versions au même numéro s'entendent, quel que soit leur numéro de version."""
    d = tmp_path / "donnees"
    Database(d, app_version="1.1.0", schema=SCHEMA + 1)        # une version future, structure suivante
    assert stored_version(d / "compta.db") == "1.1.0" and stored_schema(d / "compta.db") == SCHEMA + 1
    before = hashlib.sha256((d / "compta.db").read_bytes()).hexdigest()
    with pytest.raises(NewerDatabaseError) as e:
        Database(d, app_version="1.0.0")
    assert (e.value.stored, e.value.current) == ("1.1.0", "1.0.0")
    assert "plus récente" in str(e.value)
    assert hashlib.sha256((d / "compta.db").read_bytes()).hexdigest() == before   # rien modifié
    db = Database(d, app_version="1.2.0", schema=SCHEMA + 1)   # même structure : pas de souci
    assert stored_version(db.path) == "1.2.0"
    assert stored_version(tmp_path / "absente.db") is None and stored_schema(tmp_path / "absente.db") is None


def test_same_structure_opens_whatever_the_version(tmp_path):
    """Un ordinateur resté à la version d'avant n'est pas bloqué tant que la structure n'a pas changé."""
    d = tmp_path / "donnees"
    Database(d, app_version="1.6.0")
    assert stored_schema(d / "compta.db") == SCHEMA
    db = Database(d, app_version="1.5.0")                      # plus ancienne, même structure : elle ouvre
    assert stored_version(db.path) == "1.5.0" and stored_schema(db.path) == SCHEMA


def test_sqlite_settings_are_explicit(tmp_path):
    """Un seul fichier au repos (pas de -wal / -shm à côté), écriture complète sur le disque, clés étrangères, attente."""
    db = Database(tmp_path / "d")
    with db.engine.connect() as c:
        get = lambda name: c.exec_driver_sql(f"PRAGMA {name}").scalar()
        assert get("journal_mode") == "delete"
        assert get("synchronous") == 3                         # EXTRA
        assert get("foreign_keys") == 1
        assert get("busy_timeout") == 5000
    with db.Session() as s:
        from compta import models as M
        s.add(M.Trip(name="x"))
        s.commit()
    assert sorted(p.name for p in (tmp_path / "d").glob("compta.db*")) == ["compta.db"]


def test_missing_columns_are_added_from_the_models(tmp_path):
    """Une base dont la structure est plus ancienne est alignée sur les modèles : les colonnes absentes sont
    ajoutées avec leur valeur par défaut, les données restent, et le numéro de structure est écrit."""
    from compta import models as M
    d = tmp_path / "ancienne"
    first = Database(d)
    with first.Session() as s:
        s.add(M.Trip(name="Avant", pay_info="x"))
        s.commit()
    first.engine.dispose()
    con = sqlite3.connect(d / "compta.db")
    con.execute("ALTER TABLE trips DROP COLUMN pay_info")
    con.execute("PRAGMA user_version = 1")
    con.commit()
    con.close()
    db = Database(d)
    assert stored_schema(db.path) == SCHEMA
    with db.Session() as s:
        t = s.query(M.Trip).one()
        assert (t.name, t.pay_info, t.rounding) == ("Avant", "", "participants")


def test_old_database_without_version_opens(tmp_path):
    d = tmp_path / "ancienne"
    d.mkdir()
    con = sqlite3.connect(d / "compta.db")
    con.execute("CREATE TABLE settings (key VARCHAR(100) PRIMARY KEY, value TEXT)")
    con.commit()
    con.close()
    Database(d, app_version="1.0.0")
    assert stored_version(d / "compta.db") == "1.0.0"


def test_newer_database_page(tmp_path):
    d = tmp_path / "donnees"
    Database(d, app_version="2.0.0", schema=SCHEMA + 1)
    app = create_app(d, updater=make(version="1.0.0"))
    assert app.state.db is None
    c = TestClient(app)
    r = c.get("/")
    assert r.status_code == 409
    assert "trop ancienne pour tes données" in r.text and "Trip Compta 2.0.0" in r.text and "Rien n'a été modifié" in r.text
    assert "Télécharger la dernière version" in r.text and U.RELEASES_PAGE in r.text
    assert "ferme cette fenêtre" in r.text                     # sinon l'ancienne reste ouverte, sans qu'on le voie
    assert c.get("/v/1/depenses").status_code == 409
    assert c.get("/ping").json()["version"] == "1.0.0"
    assert stored_version(d / "compta.db") == "2.0.0"


def test_backup_before_update(tmp_path):
    db = Database(tmp_path / "d", app_version="1.0.0")
    f = U.backup_database(db, "1.1.0-beta1")
    assert f == db.backups_dir / "avant-maj-1.1.0-beta1.db" and f.exists()
    con = sqlite3.connect(f)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()


# ---------------------------------------------------------------- installation (téléchargement simulé)

def fake_download(content: bytes):
    def dl(url, dest, progress=None):
        for p in (10, 50, 99):
            progress and progress(p)
        Path(dest).write_bytes(content)
        return hashlib.sha256(content).hexdigest()
    return dl


def archive_bytes(name: str, folder: str) -> bytes:
    buf = io.BytesIO()
    if name.endswith(".zip"):
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(f"{folder}/VERSION.txt", "1.1.0")
    else:
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            data = b"1.1.0"
            ti = tarfile.TarInfo(f"{folder}/VERSION.txt")
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def install_env(tmp_path, monkeypatch):
    name = U.asset_name() or "TripCompta-essai.tar.gz"          # hors Mac : une archive d'essai, même mécanique
    if name.endswith(".exe"):                                   # Windows : l'installeur, voir test_install_windows
        name = "TripCompta-essai.tar.gz"
    monkeypatch.setattr(U, "asset_name", lambda *a, **k: name)
    folder = "TripCompta.app" if name.startswith("TripCompta-mac") else "TripCompta"
    content = archive_bytes(name if not name.startswith("TripCompta-mac") else "x.zip", folder)
    target = tmp_path / "apps" / folder
    target.mkdir(parents=True)
    db = Database(tmp_path / "donnees", app_version="1.0.0")
    return name, content, target, db, tmp_path


def run(u, db, target, sums_text, launched, quitted):
    serve(u, sums_text)
    u.run_install(db, port=8765, app_args=["--data-dir", "x"], quit_app=lambda: quitted.append(1),
                  location=(target, "prog"), launch=lambda **kw: launched.append(kw))


def test_install_success(install_env):
    name, content, target, db, tmp = install_env
    u = make(download=fake_download(content))
    u.check()
    launched, quitted = [], []
    run(u, db, target, f"{hashlib.sha256(content).hexdigest()}  {name}\n", launched, quitted)
    assert u.job["step"] == "redemarrage" and u.job["error"] is None, u.job
    assert quitted == [1] and len(launched) == 1
    kw = launched[0]
    assert kw["target"] == target and kw["version"] == "1.1.0" and kw["port"] == 8765
    assert kw["app_args"] == ["--data-dir", "x"] and kw["exe_rel"] == "prog"
    assert (kw["new"] / "VERSION.txt").read_text() == "1.1.0" and kw["new"].name == target.name
    assert (db.backups_dir / "avant-maj-1.1.0.db").exists()
    assert not (kw["work"] / name).exists()                     # l'archive est effacée une fois dépliée
    assert kw["work"] == U.work_dir()                           # à côté des réglages, jamais dans les données
    assert (kw["work"] / "en-cours.txt").read_text() == "1.1.0"   # la nouvelle version s'y reconnaîtra
    assert db.dir not in kw["work"].parents


def test_install_bad_checksum_changes_nothing(install_env):
    name, content, target, db, tmp = install_env
    u = make(download=fake_download(content))
    u.check()
    launched, quitted = [], []
    run(u, db, target, f"{'0' * 64}  {name}\n", launched, quitted)
    assert u.job["step"] == "erreur" and "abîmé" in u.job["error"] and "Rien n'a été changé" in u.job["error"]
    assert launched == [] and quitted == [] and target.exists()


def test_install_missing_file_for_this_computer(install_env, monkeypatch):
    name, content, target, db, tmp = install_env
    rel = dict(RELEASE, assets=[a for a in RELEASE["assets"] if a["name"] != name])
    u = make(fetch=FakeGitHub(rel), download=fake_download(content))
    u.check()
    launched, quitted = [], []
    run(u, db, target, "", launched, quitted)
    assert u.job["step"] == "erreur" and "pas de fichier pour cet ordinateur" in u.job["error"]
    assert launched == [] and quitted == []


def test_install_download_failure(install_env):
    name, content, target, db, tmp = install_env

    def broken(url, dest, progress=None):
        raise OSError("connexion coupée")
    u = make(download=broken)
    u.check()
    launched, quitted = [], []
    run(u, db, target, f"{'0' * 64}  {name}\n", launched, quitted)
    assert u.job["step"] == "erreur" and "connexion coupée" in u.job["error"] and launched == []


def test_extract_refuses_escaping_zip(tmp_path, monkeypatch):
    monkeypatch.setattr(U.sys, "platform", "win32")             # chemin zipfile (pas ditto)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../dehors.txt", "x")
    (tmp_path / "a.zip").write_bytes(buf.getvalue())
    with pytest.raises(U.UpdateError):
        U.extract(tmp_path / "a.zip", tmp_path / "out")
    assert not (tmp_path / "dehors.txt").exists()


def test_notices(tmp_path):
    w = tmp_path / "maj"
    w.mkdir()
    u = make(version="1.1.0")
    assert u.startup_notice(w) is None and u.failure_notice(w) is None
    (w / "en-cours.txt").write_text("1.1.0")
    assert make(version="1.0.0").startup_notice(w) is None                 # l'ancienne relancée : rien
    assert (w / "en-cours.txt").exists()
    assert u.startup_notice(w) == "✓ Trip Compta est à jour : version 1.1.0."
    assert u.startup_notice(w) is None and not (w / "en-cours.txt").exists()   # une seule fois
    (w / "en-cours.txt").write_text("1.1.0")
    (w / "echec.txt").write_text("\ufeffLa version 1.1.0 n'a pas démarré correctement.\n", encoding="utf-8")
    assert make().failure_notice(w) == "⚠️ La version 1.1.0 n'a pas démarré correctement."
    assert make().failure_notice(w) is None and not (w / "en-cours.txt").exists()


# ---------------------------------------------------------------- pages

@pytest.fixture
def client(tmp_path):
    def build(upd):
        return TestClient(create_app(tmp_path / "web", updater=upd))
    return build


def test_ping_and_about_page_release(client):
    u = make(version="1.0.0")
    c = client(u)
    assert c.get("/ping").json()["version"] == "1.0.0"
    r = c.get("/reglages/a-propos")
    assert "Trip Compta 1.0.0" in r.text and "Pas encore vérifié" in r.text and "Version de développement" not in r.text
    assert 'href="/reglages"' in c.get("/").text                        # ⚙️ Réglages, en haut de chaque page
    r = c.get("/reglages")
    assert "Version et mises à jour" in r.text and "Trip Compta 1.0.0" in r.text


def test_about_page_dev(client):
    u = make(version="1.0.0-dev", dev=True)
    c = client(u)
    r = c.get("/reglages/a-propos")
    assert "Trip Compta 1.0.0-dev (abc1234)" in r.text and "Version de développement" in r.text
    r = c.post("/reglages/verifier-maj")
    assert "Dernière version publiée : 1.1.0" in r.text
    assert "est disponible" not in c.get("/").text                 # jamais de bandeau en développement
    r = c.post("/maj/installer")
    assert "Pas de nouvelle version à installer" in r.text


def test_banner_check_and_later(client, monkeypatch):
    monkeypatch.setattr(U, "asset_name", lambda *a, **k: "TripCompta-mac-arm64.zip")   # un ordinateur qui a sa version (pas Linux)
    gh = FakeGitHub()
    u = make(fetch=gh)
    c = client(u)
    assert "est disponible" not in c.get("/").text
    r = c.post("/reglages/verifier-maj")
    assert "Une nouvelle version est disponible : 1.1.0" in r.text
    home = c.get("/").text
    assert "Trip Compta 1.1.0 est disponible" in home and "Voir les nouveautés" in home and "Mettre à jour" in home
    about = c.get("/reglages/a-propos").text
    assert "Nouvelle version : 1.1.0" in about and "Nouveau : le mail au trésorier" in about and "Publiée le 02/11/2026" in about
    # pas une version installée (code source) : on propose le téléchargement, avec la raison
    assert "ne tourne pas depuis une version installée" in about and "Télécharger la nouvelle version" in about
    r = c.post("/maj/plus-tard", data={"version": "1.1.0"}, headers={"referer": "http://testserver/"})
    assert "tu retrouveras la mise à jour" in r.text and "est disponible." not in c.get("/").text
    assert "Nouvelle version : 1.1.0" in c.get("/reglages/a-propos").text
    # sur un ordinateur sans version toute prête (Linux…), la raison le dit, et le téléchargement reste proposé
    monkeypatch.setattr(U, "asset_name", lambda *a, **k: None)
    about = c.get("/reglages/a-propos").text
    assert "Pas de version toute prête pour cet ordinateur" in about and "Télécharger la nouvelle version" in about


def test_check_offline_message_and_toggle(client):
    u = make(fetch=FakeGitHub(fail=True))
    c = client(u)
    r = c.post("/reglages/verifier-maj")
    assert "Impossible de joindre le site des mises à jour" in r.text
    r = c.post("/reglages/maj-auto", data={"auto": "0"})
    assert "Vérification automatique désactivée" in r.text and not u.auto
    assert json.loads(Path(U.PATHS.config_file()).read_text())["maj_auto"] is False
    r = c.post("/reglages/maj-auto", data={"auto": "1"})
    assert "activée" in r.text and u.auto


def test_up_to_date_message(client):
    u = make(version="1.1.0")
    c = client(u)
    r = c.post("/reglages/verifier-maj")
    assert "Tu as déjà la dernière version" in r.text


def test_notice_after_update(client, tmp_path):
    w = U.work_dir()
    w.mkdir(parents=True)
    (w / "en-cours.txt").write_text("1.1.0")
    c = client(make(version="1.1.0"))
    assert "Trip Compta est à jour : version 1.1.0" in c.get("/").text
    assert "est à jour" not in c.get("/").text                    # une seule fois
    # échec écrit par le script APRÈS la relance de l'ancienne version : vu à la page suivante
    (w / "echec.txt").write_text("La version 1.2.0 n'a pas démarré correctement. L'ancienne version a été remise "
                                 "en place.", encoding="utf-8")
    assert "⚠️ La version 1.2.0 n'a pas démarré correctement" in html.unescape(c.get("/").text)
    assert "n'a pas démarré" not in html.unescape(c.get("/").text)


def test_install_route_runs_job(client, monkeypatch):
    """La route lance l'installation en arrière-plan et la page d'attente suit /maj/etat."""
    u = make()
    u.check()
    monkeypatch.setattr(u, "installable", lambda: (True, ""))
    started = []
    monkeypatch.setattr(u, "run_install", lambda **kw: (started.append(kw), u.job.update(step="erreur", error="Test")))
    c = client(u)
    r = c.post("/maj/installer")
    assert "Mise à jour vers la version 1.1.0" in r.text and "Copie de sécurité de tes données" in r.text
    import time
    for _ in range(50):
        if started:
            break
        time.sleep(0.02)
    assert started and started[0]["port"] and callable(started[0]["quit_app"])
    assert c.get("/maj/etat").json() == dict(step="erreur", error="Test", percent=0, version="1.1.0")
    assert c.get("/maj/etat").json() == {"step": "aucune"}          # erreur vue une fois : on peut réessayer


def test_newer_database_page_offers_update(tmp_path, monkeypatch):
    """Données ouvertes par une version plus récente : si la version publiée sait les ouvrir, un bouton met à
    jour directement depuis cet écran (sans toucher aux données) ; sinon, le lien de téléchargement."""
    d = tmp_path / "donnees"
    Database(d, app_version="1.1.0", schema=SCHEMA + 1)     # publiée : 1.1.0 (FakeGitHub)
    before = (d / "compta.db").read_bytes()
    u = make(version="1.0.0")
    monkeypatch.setattr(u, "installable", lambda: (True, ""))
    started = []
    monkeypatch.setattr(u, "run_install", lambda **kw: started.append(kw))
    app = create_app(d, updater=u)
    c = TestClient(app)
    r = c.get("/")
    assert r.status_code == 409 and "Mettre à jour vers la 1.1.0" in r.text and 'action="/maj/installer"' in r.text
    r = c.post("/maj/installer", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/maj/en-cours"
    import time
    for _ in range(50):
        if started:
            break
        time.sleep(0.02)
    assert started and started[0]["db"] is None
    assert "Mise à jour vers la version 1.1.0" in c.get("/maj/en-cours").text
    assert c.get("/maj/etat").json()["step"] == "sauvegarde"
    assert (d / "compta.db").read_bytes() == before         # rien touché
    assert stored_version(d / "compta.db") == "1.1.0"


def test_newer_database_page_published_too_old(tmp_path):
    """Données ouvertes par une 2.0.0, publiée : 1.1.0 → la mise à jour n'aiderait pas : pas de bouton."""
    d = tmp_path / "donnees"
    Database(d, app_version="2.0.0", schema=SCHEMA + 1)
    c = TestClient(create_app(d, updater=make(version="1.0.0")))
    r = c.get("/")
    assert "Mettre à jour vers" not in r.text and "Télécharger la dernière version" in r.text
    assert c.post("/maj/installer", follow_redirects=False).headers["location"] == "/"


def test_newer_database_page_not_installable(tmp_path, monkeypatch):
    """Mise à jour impossible (ex. Mac : app ouverte depuis Téléchargements) : on dit pourquoi, et le lien."""
    d = tmp_path / "donnees"
    Database(d, app_version="1.1.0", schema=SCHEMA + 1)
    u = make(version="1.0.0")
    monkeypatch.setattr(U, "install_location", lambda *a, **k: (
        Path("/private/var/folders/x/T/AppTranslocation/ABC/d/Trip Compta.app"), "Contents/MacOS/Trip Compta"))
    monkeypatch.setattr(U, "asset_name", lambda *a, **k: "TripCompta-mac-arm64.zip")
    r = TestClient(create_app(d, updater=u)).get("/")
    assert "1.1.0 est disponible" in r.text and "Glisse-la dans le dossier Applications" in r.text
    assert "Mettre à jour vers" not in r.text and "Télécharger la dernière version" in r.text


def test_backup_skipped_without_database():
    assert U.backup_database(None, "1.2.0") is None


# ---------------------------------------------------------------- Windows : l'installeur
def test_install_windows_runs_the_installer(tmp_path, monkeypatch):
    """Windows : l'installeur téléchargé et vérifié est lancé en silencieux (il attend la fermeture de Trip Compta,
    installe et relance) ; rien n'est déplié ni remplacé par Trip Compta lui-même."""
    name = "TripCompta-windows-setup.exe"
    monkeypatch.setattr(U, "asset_name", lambda *a, **k: name)
    content = b"MZ faux installeur"
    target = tmp_path / "Programs" / "TripCompta"
    target.mkdir(parents=True)
    db = Database(tmp_path / "donnees", app_version="1.0.0")
    u = make(download=fake_download(content))
    u.check()
    launched, quitted = [], []
    serve(u, f"{hashlib.sha256(content).hexdigest()}  {name}\n")
    u.run_install(db, port=8790, app_args=["--navigateur"], quit_app=lambda: quitted.append(1),
                  location=(target, "TripCompta.exe"), launch=lambda **kw: launched.append(kw))
    assert u.job["step"] == "redemarrage" and u.job["error"] is None, u.job
    assert quitted == [1] and len(launched) == 1
    kw = launched[0]
    assert kw["installer"].read_bytes() == content and kw["current"] == target / "TripCompta.exe"
    assert (kw["work"] / "en-cours.txt").read_text() == "1.1.0"
    assert (db.backups_dir / "avant-maj-1.1.0.db").exists()
    # relancé par l'installeur avec « --apres-maj » : mêmes options, même adresse, pas de nouvel onglet
    assert U.relaunch_args(kw["work"]) == ["--navigateur", "--port", "8790", "--no-browser"]
    assert U.relaunch_args(kw["work"]) == ["--no-browser"]                 # lu une seule fois
    cmd = U.installer_command(kw["installer"], kw["work"], kw["current"], 4242)
    assert cmd[0] == str(kw["installer"]) and "/VERYSILENT" in cmd and "/ATTENDRE=4242" in cmd
    assert f"/ANCIEN={target / 'TripCompta.exe'}" in cmd and f"/ECHEC={kw['work'] / 'echec.txt'}" in cmd


def test_windows_install_needs_no_admin(monkeypatch, tmp_path):
    monkeypatch.setattr(U.sys, "platform", "win32")
    monkeypatch.setattr(U, "asset_name", lambda *a, **k: "TripCompta-windows-setup.exe")
    monkeypatch.setattr(U, "install_location", lambda *a, **k: (tmp_path / "x", "TripCompta.exe"))
    assert make().installable() == (True, "")


def test_relaunched_after_update_reads_options(monkeypatch, tmp_path):
    import importlib
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    run_mod = importlib.import_module("run")
    U.work_dir().mkdir(parents=True, exist_ok=True)
    U.write_relaunch(U.work_dir(), 8791, ["--navigateur"])
    seen = {}

    def fake_parse(self, *a, **k):
        seen["argv"] = list(_sys.argv[1:])
        raise SystemExit(0)
    import argparse
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", fake_parse)
    monkeypatch.setattr(_sys, "argv", ["TripCompta.exe", "--apres-maj"])
    import os
    monkeypatch.setenv("TRIPCOMPTA_RESTARTED", "")              # rétabli tel quel à la fin du test
    monkeypatch.delenv("TRIPCOMPTA_RESTARTED")
    with pytest.raises(SystemExit):
        run_mod.main()
    assert seen["argv"] == ["--navigateur", "--port", "8791", "--no-browser"]
    assert os.environ.get("TRIPCOMPTA_RESTARTED") == "1"


@pytest.mark.parametrize("exe,where", [
    ("/Applications/TripCompta.app/Contents/MacOS/TripCompta", None),
    ("/Users/zoe/Applications/TripCompta.app/Contents/MacOS/TripCompta", None),
    ("/Applications/Outils/TripCompta.app/Contents/MacOS/TripCompta", None),
    ("/Users/zoe/Downloads/TripCompta.app/Contents/MacOS/TripCompta", "Téléchargements"),
    ("/Users/zoe/Desktop/TripCompta.app/Contents/MacOS/TripCompta", "le Bureau"),
    ("/private/var/folders/x/T/AppTranslocation/ABC/d/TripCompta.app/Contents/MacOS/TripCompta",
     "le dossier où tu l'as téléchargée"),
    ("/Volumes/TripCompta/TripCompta.app/Contents/MacOS/TripCompta", "une image disque ou un disque externe"),
    ("/opt/x/TripCompta.app/Contents/MacOS/TripCompta", "« /opt/x »"),
])
def test_mac_app_outside_applications_is_noticed(exe, where):
    assert U.misplaced_app(exe, "darwin", True, home="/Users/zoe") == where


def test_mac_paths_read_as_is_on_another_computer(monkeypatch):
    """Les essais Mac tournent aussi sous Windows : un chemin Mac n'y devient pas « D:/Applications/… »."""
    from pathlib import PureWindowsPath
    monkeypatch.setattr(U.sys, "platform", "win32")
    monkeypatch.setattr(U, "Path", PureWindowsPath)              # comme sous Windows : chemins à la Windows
    assert U.misplaced_app("/Applications/TripCompta.app/Contents/MacOS/TripCompta", "darwin", True, home="/Users/zoe") is None
    assert U.misplaced_app("/Users/zoe/Downloads/TripCompta.app/Contents/MacOS/TripCompta", "darwin", True,
                           home="/Users/zoe") == "Téléchargements"
    assert U.misplaced_app("/opt/x/TripCompta.app/Contents/MacOS/TripCompta", "darwin", True, home="/Users/zoe") == "« /opt/x »"


def test_misplaced_never_on_windows_or_from_source():
    assert U.misplaced_app("C:/Users/zoe/Downloads/TripCompta/TripCompta.exe", "win32", True) is None
    assert U.misplaced_app("/Users/zoe/Downloads/TripCompta.app/Contents/MacOS/TripCompta", "darwin", False) is None


def test_windows_version_resource():
    """Le programme Windows porte son nom et son numéro de version (Propriétés → Détails) : le fichier écrit pour
    PyInstaller est lisible par son propre lecteur, avec les bons nombres."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_tc", Path(__file__).resolve().parents[1] / "build_tools" / "build.py")
    try:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except ImportError:
        pytest.skip("PyInstaller absent")
    assert mod.windows_version_numbers("1.5.0") == (1, 5, 0, 0)
    assert mod.windows_version_numbers("2.10.3-beta2") == (2, 10, 3, 0)
    assert mod.windows_version_numbers("1.5.0-dev") == (1, 5, 0, 0)
    text = mod.windows_version_file("1.5.0-beta2")

    class Node:                                                 # mêmes noms que ceux lus par PyInstaller
        def __init__(self, *args, **kw):
            self.args, self.kw = args, kw
    names = ("VSVersionInfo", "FixedFileInfo", "StringFileInfo", "StringTable", "StringStruct", "VarFileInfo",
             "VarStruct")
    info = eval(text, {n: type(n, (Node,), {}) for n in names})
    assert info.kw["ffi"].kw["filevers"] == (1, 5, 0, 0) == info.kw["ffi"].kw["prodvers"]
    table = info.kw["kids"][0].args[0][0]
    assert table.args[0] == "040C04B0"                          # français, Unicode
    strings = {s.args[0]: s.args[1] for s in table.args[1]}
    assert strings["ProductName"] == "Trip Compta" and strings["FileVersion"] == "1.5.0-beta2"
    assert strings["OriginalFilename"] == "TripCompta.exe"


def test_home_page_says_to_move_the_app_into_applications(tmp_path):
    app = create_app(tmp_path / "d", updater=make(version="1.0.0"))
    c = TestClient(app)
    assert 'id="hors-applications"' not in c.get("/").text
    app.state.misplaced = "Téléchargements"
    page = c.get("/").text
    assert 'id="hors-applications"' in page and "ouverte depuis Téléchargements" in page
    assert "Glisse Trip Compta dans le dossier Applications" in page
    from compta.ops import Ops
    tid = Ops(app.state.db).create_trip("x")["voyage_id"]
    assert 'id="hors-applications"' not in c.get(f"/v/{tid}").text      # seulement sur l'accueil
