"""Protection des voyages par un mot de passe (crypto.py, coffre.py) : chiffrement de la base et des justificatifs,
déverrouillage, clé de secours, changement de mot de passe, retrait, fichiers abîmés, plantage, deux ordinateurs,
trousseau.

Voyage de test (montants vérifiés à la main) : Anne, Bruno, Chloé.
  Gîte 300 € payé par l'asso, pour tous            → 100 € chacun
  Essence 90 € avancée par Anne, pour Anne et Bruno → 45 € chacun
  Parts : Anne 145, Bruno 145, Chloé 100 ; soldes : Anne 55, Bruno 145, Chloé 100.
"""
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from compta import coffre as CF
from compta import dossier as C
from compta import crypto as K
from compta import models as M
from compta import services as S
from compta.ops import Ops, OpsError
from compta.web import create_app

EXPECTED = {"Anne": (14500, 5500), "Bruno": (14500, 14500), "Chloé": (10000, 10000)}
PASSWORD = "Bulles de Plongée 2026"
# ce qui ne doit JAMAIS apparaître en clair (marqueurs d'au moins 5 octets : une suite plus courte apparaît par
# hasard dans des données chiffrées, qui sont aléatoires)
SECRETS = [b"Bruno", "Chloé".encode(), "Gîte".encode(), b"Essence", b"SQLite format 3", b"JFIF\x00", b"%PDF-",
           b"ticket"]


@pytest.fixture(autouse=True)
def fast_kdf(monkeypatch):
    """scrypt allégé pour les tests (le vrai réglage reste dans crypto.SCRYPT_N)."""
    monkeypatch.setattr(K, "SCRYPT_N", 2 ** 10)


@pytest.fixture
def keyring_mem(monkeypatch):
    """Trousseau simulé (en mémoire)."""
    store = {}
    monkeypatch.setattr(K, "keyring_available", lambda: True)
    monkeypatch.setattr(K, "remember", lambda v: store.__setitem__(v.id, v.key) or True)
    monkeypatch.setattr(K, "recall", lambda vid: K.Vault(store[vid], vid) if vid in store else None)
    monkeypatch.setattr(K, "forget", lambda vid: store.pop(vid, None))
    return store


@pytest.fixture
def env(tmp_path, monkeypatch, keyring_mem):
    monkeypatch.delenv("TRIPCOMPTA_DATA_DIR", raising=False)
    monkeypatch.setenv("TRIPCOMPTA_CONFIG", str(tmp_path / "Réglages ordi" / "reglages.json"))
    docs = tmp_path / "Mes Documents"
    monkeypatch.setenv("TRIPCOMPTA_DOCUMENTS", str(docs))
    return tmp_path, docs / "TripCompta"


def seed(app) -> int:
    ops = Ops(app.state.db)
    tid = ops.create_trip("Sortie test")["voyage_id"]
    ops.add_participants(tid, ["Anne", "Bruno", "Chloé"])
    ops.add_expense(tid, "Gîte", categorie="Logement", montant_reel="300")
    ops.add_expense(tid, "Essence", categorie="Transport", montant_reel="90", paye_par="Anne", pour=["Anne", "Bruno"])
    return tid


def add_receipts(app, tid):
    """Deux justificatifs (photo + PDF), avec des octets reconnaissables."""
    cl = TestClient(app)
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (30, 20), (200, 60, 40)).save(buf, "JPEG")
    jpg = buf.getvalue()
    pdf = b"%PDF-1.4\n% ticket Essence Anne\n%%EOF\n"
    cl.post(f"/v/{tid}/justificatifs", files=[("files", ("ticket péage.jpg", jpg, "image/jpeg")),
                                              ("files", ("facture.pdf", pdf, "application/pdf"))])
    return {"ticket péage.jpg": jpg, "facture.pdf": pdf}


def amounts(app, tid) -> dict:
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        return {p.name: (p.share, p.balance) for p in S.compute_trip(t).people.values()}


def receipts_bytes(app, tid) -> dict:
    with app.state.db.Session() as s:
        return {r.original_name: (app.state.db.receipts_dir / r.stored_name).read_bytes()
                for r in s.get(M.Trip, tid).receipts}


def no_clear_text(folder: Path) -> list:
    """Fichiers du dossier qui contiennent quelque chose de lisible (hors verrou, LISEZMOI, clé, à trier)."""
    bad = []
    for f in folder.rglob("*"):
        if not f.is_file() or f.name in (K.README, K.KEY_FILE) or C.INBOX in f.parts:
            continue
        data = f.read_bytes()
        hits = [s_ for s_ in SECRETS if s_ in data]
        if hits:
            bad.append((f.relative_to(folder).as_posix(), hits))
    return bad


def activate(cl, password=PASSWORD, remember=False):
    r = cl.post("/protection/activer", data={"mot_de_passe": password, "confirmation": password,
                                              **({"souvenir": "1"} if remember else {})})
    assert "Étape 2 sur 2" in r.text, r.text[:500]
    key = re.search(r'id="recovery">([A-Z2-9-]+)<', r.text).group(1)
    n = int(re.search(r"groupe n° (\d+)", r.text).group(1))
    return key, n, r


def confirm(cl, key, n, wrong=False):
    group = key.split("-")[n - 1]
    return cl.post("/protection/confirmer", data={"groupe": "XXXX" if wrong else group.lower()})


def stop(app):
    if app.state.coffre is not None:
        app.state.coffre.stop()
    app.state.db.engine.dispose()


def protect(env, remember=False):
    """Voyage de test dans le dossier par défaut, puis protection activée (et « redémarrage »)."""
    tmp, folder = env
    app = create_app()
    tid = seed(app)
    files = add_receipts(app, tid)
    cl = TestClient(app)
    key, n, _ = activate(cl, remember=remember)
    r = confirm(cl, key, n)
    assert "maintenant protégés" in r.text
    stop(app)
    return tid, files, key


# ---------------------------------------------------------------- activer, verrouillé, déverrouiller, retirer
def test_round_trip_activate_unlock_remove(env):
    tmp, folder = env
    tid, files, key = protect(env)
    # le dossier : seulement des fichiers chiffrés
    names = {p.name for p in folder.iterdir()} - {C.INBOX}
    assert names == {K.KEY_FILE, K.DB_FILE, K.README, C.RECEIPTS}      # plus d'historique ni de sauvegardes ici
    assert all(p.name.endswith(K.SUFFIX) for p in (folder / C.RECEIPTS).iterdir())
    assert len(list((folder / C.RECEIPTS).iterdir())) == 2
    assert no_clear_text(folder) == []
    assert "mot de passe" in (folder / K.README).read_text(encoding="utf-8")

    app = create_app()                                           # redémarrage : verrouillé
    assert app.state.coffre.locked
    cl = TestClient(app)
    r = cl.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/deverrouiller"
    assert "Mot de passe de tes voyages" in cl.get("/deverrouiller").text
    assert cl.get("/api/voyages").status_code == 423
    r = cl.post("/deverrouiller", data={"mot_de_passe": "mauvais mot de passe"})
    assert "Mot de passe incorrect" in r.text and app.state.coffre.locked
    r = cl.post("/deverrouiller", data={"mot_de_passe": PASSWORD}, follow_redirects=True)
    assert "Voyages déverrouillés" in r.text and not app.state.coffre.locked
    assert amounts(app, tid) == EXPECTED
    assert receipts_bytes(app, tid) == files                     # octet pour octet
    assert "🔒 Verrouiller" in cl.get("/").text and "protégé par mot de passe" in cl.get("/").text
    # une saisie : publiée chiffrée
    cl.post("/voyages", data={"name": "Voyage secret"})
    assert no_clear_text(folder) == [] and b"Voyage secret" not in (folder / K.DB_FILE).read_bytes()
    # arrêt : plus rien en clair sur l'ordinateur
    local = app.state.coffre.local
    stop(app)
    leaks = [p for p in local.rglob("*") if p.is_file() and any(s_ in p.read_bytes() for s_ in SECRETS)]
    assert leaks == []

    # retirer la protection : tout redevient lisible, mêmes montants et justificatifs
    app = create_app()
    cl = TestClient(app)
    cl.post("/deverrouiller", data={"mot_de_passe": PASSWORD})
    r = cl.post("/protection/retirer", data={"actuel": "faux"})
    assert "Mot de passe actuel incorrect" in r.text and K.is_protected(folder)
    r = cl.post("/protection/retirer", data={"actuel": PASSWORD})
    assert "protection est retirée" in r.text
    assert app.state.coffre is None                              # directement dans le dossier, jusqu'au redémarrage
    assert "Voyage secret" in cl.get("/").text
    assert not [p_ for p_ in folder.iterdir() if p_.name.startswith("compta (")]   # pas de fausse copie en conflit
    stop(app)
    assert not K.is_protected(folder) and (folder / C.DB).exists() and not (folder / K.DB_FILE).exists()
    assert not list((folder / C.RECEIPTS).glob(f"*{K.SUFFIX}"))
    app = create_app()
    assert app.state.coffre is None
    assert amounts(app, tid) == EXPECTED and receipts_bytes(app, tid) == files
    assert "Voyage secret" in TestClient(app).get("/").text
    stop(app)


def test_activation_requires_noted_recovery_key(env):
    tmp, folder = env
    app = create_app()
    seed(app)
    cl = TestClient(app)
    r = cl.post("/protection/activer", data={"mot_de_passe": "court", "confirmation": "court"})
    assert "au moins 8 caractères" in r.text
    r = cl.post("/protection/activer", data={"mot_de_passe": PASSWORD, "confirmation": PASSWORD + "x"})
    assert "pas identiques" in r.text
    key, n, page = activate(cl)
    assert len(key) == 39 and "Imprimer" in page.text
    r = confirm(cl, key, n, wrong=True)
    assert "Ce n'est pas le groupe" in r.text.replace("&#39;", "'") and not K.is_protected(folder)
    assert (folder / C.DB).exists()                              # rien n'a bougé tant que ce n'est pas confirmé
    assert "maintenant protégés" in confirm(cl, key, n).text
    stop(app)


def test_recovery_key_and_password_change(env):
    tmp, folder = env
    tid, files, key = protect(env)
    app = create_app()
    cl = TestClient(app)
    r = cl.post("/deverrouiller/secours", data={"secours": "AAAA-BBBB-CCCC-DDDD-EEEE-FFFF-GGGG-HHHH",
                                                 "nouveau": "nouveau mot de passe", "confirmation": "nouveau mot de passe"})
    assert "Clé de secours incorrecte" in r.text
    r = cl.post("/deverrouiller/secours", data={"secours": key.lower().replace("-", " "),   # saisie tolérante
                                                 "nouveau": "nouveau mot de passe", "confirmation": "nouveau mot de passe"},
                follow_redirects=True)
    assert "Nouveau mot de passe enregistré" in r.text and amounts(app, tid) == EXPECTED
    # changer de mot de passe (depuis les réglages)
    r = cl.post("/protection/mot-de-passe", data={"actuel": "nouveau mot de passe", "nouveau": "troisième essai !",
                                                  "confirmation": "troisième essai !"}, follow_redirects=True)
    assert "Mot de passe changé" in r.text
    # nouvelle clé de secours : l'ancienne ne marche plus
    r = cl.post("/protection/secours", data={"actuel": "troisième essai !"})
    key2 = re.search(r'id="recovery">([A-Z2-9-]+)<', r.text).group(1)
    n2 = int(re.search(r"groupe n° (\d+)", r.text).group(1))
    r = cl.post("/protection/secours/confirmer", data={"groupe": key2.split("-")[n2 - 1]}, follow_redirects=True)
    assert "Nouvelle clé de secours enregistrée" in r.text
    stop(app)
    with pytest.raises(K.WrongPassword):
        K.unlock(folder, recovery=key)
    with pytest.raises(K.WrongPassword):
        K.unlock(folder, password=PASSWORD)
    assert K.unlock(folder, password="troisième essai !").id == K.unlock(folder, recovery=key2).id


def test_damaged_or_truncated_files_are_refused(env):
    tmp, folder = env
    tid, files, key = protect(env)
    good = (folder / K.DB_FILE).read_bytes()
    v = K.unlock(folder, password=PASSWORD)
    bad = bytearray(good)
    bad[100] ^= 1
    for blob in (bytes(bad), good[:len(good) // 2], b"TCC1", b"n'importe quoi"):
        with pytest.raises(K.Damaged):
            v.decrypt(blob, "base")
    with pytest.raises(K.Damaged):                               # un justificatif ne passe pas pour la base
        v.decrypt(next((folder / C.RECEIPTS).iterdir()).read_bytes(), "base")
    (folder / K.DB_FILE).write_bytes(bytes(bad))
    app = create_app()
    cl = TestClient(app)
    r = cl.post("/deverrouiller", data={"mot_de_passe": PASSWORD})
    assert "abîmés ou incomplets" in r.text and app.state.coffre.locked
    (folder / K.DB_FILE).write_bytes(good)                       # le cloud a fini de synchroniser
    r = cl.post("/deverrouiller", data={"mot_de_passe": PASSWORD}, follow_redirects=True)
    assert "Voyages déverrouillés" in r.text and amounts(app, tid) == EXPECTED
    stop(app)


def test_crash_during_encryption_loses_nothing(env, monkeypatch):
    """Plantage pendant le chiffrement (avant l'écriture de la clé) : la protection n'est pas active, les fichiers
    en clair sont intacts ; on peut recommencer."""
    tmp, folder = env
    app = create_app()
    tid = seed(app)
    files = add_receipts(app, tid)
    cl = TestClient(app)
    key, n, _ = activate(cl)
    real = K.Vault.encrypt_file
    calls = []

    def boom(self, src, dst, role):
        calls.append(role)
        if len(calls) == 2:
            raise OSError("disque débranché")
        return real(self, src, dst, role)
    monkeypatch.setattr(K.Vault, "encrypt_file", boom)
    r = confirm(cl, key, n)
    assert "n'a pas pu être activée" in r.text.replace("&#39;", "'") and not K.is_protected(folder)
    assert amounts(app, tid) == EXPECTED and receipts_bytes(app, tid) == files
    monkeypatch.setattr(K.Vault, "encrypt_file", real)
    key, n, _ = activate(cl)
    assert "maintenant protégés" in confirm(cl, key, n).text
    stop(app)
    app = create_app()
    TestClient(app).post("/deverrouiller", data={"mot_de_passe": PASSWORD})
    assert amounts(app, tid) == EXPECTED and receipts_bytes(app, tid) == files
    stop(app)


def test_crash_while_unlocked_wipes_working_copy_next_start(env):
    tmp, folder = env
    tid, files, key = protect(env)
    app = create_app()
    TestClient(app).post("/deverrouiller", data={"mot_de_passe": PASSWORD})
    local = app.state.coffre.local
    assert (local / C.DB).exists()
    app.state.db.engine.dispose()                                # « plantage » : pas d'arrêt propre
    app2 = create_app()                                          # redémarrage : copie en clair effacée
    assert app2.state.coffre.locked and not (local / C.DB).exists() and not (local / C.RECEIPTS).exists()
    stop(app2)


# ---------------------------------------------------------------- se souvenir, verrouiller, IA
def test_remember_on_this_computer(env, keyring_mem):
    tmp, folder = env
    tid, files, key = protect(env, remember=True)
    app = create_app()                                           # le trousseau donne la clé : pas d'écran
    assert not app.state.coffre.locked and amounts(app, tid) == EXPECTED
    cl = TestClient(app)
    assert "Cet ordinateur se souvient du mot de passe" in cl.get("/reglages/protection").text
    assert "cet ordinateur se souvient du mot de passe" in cl.get("/reglages").text
    r = cl.post("/verrouiller", follow_redirects=True)           # verrouiller sans quitter
    assert "Mot de passe de tes voyages" in r.text and app.state.coffre.locked
    assert not (app.state.coffre.local / C.DB).exists()
    with pytest.raises(OpsError, match="verrouillé"):
        Ops(app.state.db).create_trip("Par l'IA")
    cl.post("/deverrouiller", data={"mot_de_passe": PASSWORD})   # sans cocher : oublié
    assert keyring_mem == {}
    stop(app)
    app = create_app()
    assert app.state.coffre.locked
    stop(app)


def test_ai_server_locked(env):
    tmp, folder = env
    tid, files, key = protect(env)
    app = create_app()
    local = app.state.coffre.local
    assert (local / CF.LOCKED_FLAG).exists()
    from compta.mcp_server import build_server
    build_server(str(local))                                     # ne crée aucune base
    assert not (local / C.DB).exists()
    TestClient(app).post("/deverrouiller", data={"mot_de_passe": PASSWORD})
    assert not (local / CF.LOCKED_FLAG).exists()
    page = TestClient(app).get(f"/v/{tid}/ia").text
    assert '"--home"' not in page.replace("&#34;", '"').replace("&quot;", '"')   # justificatifs en local
    stop(app)


# ---------------------------------------------------------------- deux ordinateurs, justificatifs à trier
def test_two_computers_on_a_protected_folder(tmp_path, monkeypatch, keyring_mem):
    """Un dossier protégé dans le cloud, deux ordinateurs (chacun sa copie de travail), sans verrou entre eux :
    chacun à son tour, puis les deux en même temps — jamais une saisie écrasée en silence, rien en clair."""
    monkeypatch.setenv("TRIPCOMPTA_CONFIG", str(tmp_path / "conf" / "r.json"))
    shared = tmp_path / "Mon Drive é" / "TripCompta"
    shared.mkdir(parents=True)

    def make(who):
        monkeypatch.setenv("TRIPCOMPTA_ORDINATEUR", who)
        return create_app(shared, coffre=dict(local=tmp_path / f"copie {who}", host=who))

    def names(app):
        with app.state.db.Session() as s_:
            return sorted(t.name for t in s_.query(M.Trip).all())

    a = make("PC-A")
    tid = seed(a)
    files = add_receipts(a, tid)
    ca = TestClient(a)
    key, n, _ = activate(ca)
    assert "maintenant protégés" in confirm(ca, key, n).text
    stop(a)
    assert no_clear_text(shared) == []

    # chacun son tour
    a = make("PC-A")
    ca = TestClient(a)
    ca.post("/deverrouiller", data={"mot_de_passe": PASSWORD})
    ca.post("/voyages", data={"name": "Saisi sur A"})
    stop(a)
    b = make("Mac-B")
    cb = TestClient(b)
    assert b.state.coffre.locked
    cb.post("/deverrouiller", data={"mot_de_passe": PASSWORD})
    assert names(b) == ["Saisi sur A", "Sortie test"]
    assert amounts(b, tid) == EXPECTED and receipts_bytes(b, tid) == files
    cb.post("/voyages", data={"name": "Saisi sur B"})
    stop(b)
    a = make("PC-A")
    ca = TestClient(a)
    ca.post("/deverrouiller", data={"mot_de_passe": PASSWORD})
    assert names(a) == ["Saisi sur A", "Saisi sur B", "Sortie test"]
    assert "sur « Mac-B »" in ca.get("/").text                 # modifiés il y a peu sur l'autre ordinateur

    # en même temps : B enregistre après A → la version de B n'écrase rien, elle est déposée à côté
    b = make("Mac-B")
    cb = TestClient(b)
    cb.post("/deverrouiller", data={"mot_de_passe": PASSWORD})
    ca.post("/voyages", data={"name": "A en même temps"})
    cb.post("/voyages", data={"name": "B en même temps"})
    assert "A en même temps" in names(b) and "B en même temps" not in names(b)   # B suit la version du dossier
    page = cb.get("/").text
    assert "modifiés sur un autre ordinateur pendant que tu travaillais" in page
    conflicts = b.state.coffre.conflicts()
    assert len(conflicts) == 1 and conflicts[0]["name"].startswith("voyages (version de Mac-B ") and conflicts[0]["valid"]
    assert "Une autre version de tes voyages est apparue" in cb.get("/").text
    # récupérer la saisie de B : exporter l'autre version, l'importer à côté
    r = cb.get("/reglages/conflit/export", params={"nom": conflicts[0]["name"]})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    r = cb.post("/importer", files={"fichier": ("autre.zip", r.content, "application/zip")}, follow_redirects=True)
    token = re.search(r'action="/importer/([^"]+)"', r.text).group(1)
    cb.post(f"/importer/{token}", data={})
    assert "B en même temps" in names(b)
    cb.post("/reglages/conflit", data={"nom": conflicts[0]["name"]})
    assert b.state.coffre.conflicts() == []
    assert no_clear_text(shared) == []

    # justificatifs à trier : chiffrés au rangement, original supprimé
    from PIL import Image
    n_before = len(list((shared / C.RECEIPTS).glob(f"*{K.SUFFIX}")))   # 2 + les 2 du voyage importé
    assert n_before == 4
    Image.new("RGB", (20, 20), (1, 2, 3)).save(shared / C.INBOX / "IMG_0001.jpg")
    r = cb.post(f"/v/{tid}/justificatifs/a-trier", data={"nom": ["IMG_0001.jpg"]}, follow_redirects=True)
    assert "ajouté" in r.text and not (shared / C.INBOX / "IMG_0001.jpg").exists()
    assert not (shared / C.INBOX / C.INBOX_DONE).exists()
    assert len(list((shared / C.RECEIPTS).glob(f"*{K.SUFFIX}"))) == n_before + 1 and no_clear_text(shared) == []
    stop(a)
    stop(b)


def test_stale_working_copy_never_overwrites_the_folder(tmp_path, monkeypatch, keyring_mem):
    """B a des changements pas encore enregistrés (écrits par l'IA, puis plantage), A enregistre entre-temps. Au
    redémarrage de B (clé mémorisée) : la version de A est reprise, celle de B déposée à côté — rien d'écrasé."""
    monkeypatch.setenv("TRIPCOMPTA_CONFIG", str(tmp_path / "conf" / "r.json"))
    shared = tmp_path / "Dropbox" / "TripCompta"
    shared.mkdir(parents=True)

    def make(who):
        monkeypatch.setenv("TRIPCOMPTA_ORDINATEUR", who)
        return create_app(shared, coffre=dict(local=tmp_path / f"copie {who}", host=who))

    a = make("PC-A")
    tid = seed(a)
    ca = TestClient(a)
    key, n, _ = activate(ca, remember=True)
    confirm(ca, key, n)
    stop(a)
    a, b = make("PC-A"), make("Mac-B")                         # clé mémorisée : déverrouillés tout de suite
    assert not a.state.coffre.locked and not b.state.coffre.locked
    Ops(b.state.db).create_trip("Saisi par l'IA sur B")         # pas enregistré dans le dossier…
    b.state.db.engine.dispose()                                  # …et B plante
    TestClient(a).post("/voyages", data={"name": "Saisi sur A"})
    stop(a)
    b = make("Mac-B")
    with b.state.db.Session() as s_:
        got = sorted(t.name for t in s_.query(M.Trip).all())
    assert got == ["Saisi sur A", "Sortie test"]                 # la version du dossier (A)
    conflicts = b.state.coffre.conflicts()
    assert len(conflicts) == 1 and conflicts[0]["valid"]
    assert "pas encore enregistrés" in TestClient(b).get("/").text
    v = b.state.coffre.vault
    tmp = tmp_path / "verif.db"
    v.decrypt_file(shared / conflicts[0]["name"], tmp, "base")
    con = sqlite3.connect(tmp)
    assert sorted(r_[0] for r_ in con.execute("SELECT name FROM trips")) == ["Saisi par l'IA sur B", "Sortie test"]
    con.close()
    v.decrypt_file(shared / K.DB_FILE, tmp, "base")              # et le dossier a bien gardé la saisie de A
    con = sqlite3.connect(tmp)
    assert "Saisi sur A" in [r_[0] for r_ in con.execute("SELECT name FROM trips")]
    con.close()
    assert no_clear_text(shared) == []
    stop(b)


def test_protection_screens_light_dark_phone(env):
    import uvicorn
    from playwright.sync_api import sync_playwright
    from tests.conftest import _free_port
    tmp, folder = env
    app = create_app()
    seed(app)
    servers = []

    def serve(a):
        port = _free_port()
        srv = uvicorn.Server(uvicorn.Config(a, host="127.0.0.1", port=port, log_level="error"))
        th = threading.Thread(target=srv.run, daemon=True)
        th.start()
        while not srv.started:
            time.sleep(0.05)
        servers.append((srv, th))
        return f"http://127.0.0.1:{port}"
    url = serve(app)
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
                    pg.goto(url + "/protection/activer")
                    pg.locator("#p-pw").fill("abc")
                    assert pg.locator(".pw-word").inner_text() == "trop court"
                    pg.locator("#p-pw").fill(PASSWORD)
                    assert pg.locator(".pw-word").inner_text() in ("bon", "très bon")
                    shoot = [("protection-1", None)]
                    if shots:
                        Path(shots).mkdir(parents=True, exist_ok=True)
                        pg.screenshot(path=f"{shots}/protection-1-{tag}.png", full_page=True)
                    pg.locator("#p-again").fill(PASSWORD)
                    pg.get_by_role("button", name="Continuer").click()
                    assert pg.locator(".recovery-key").is_visible()
                    assert pg.evaluate("document.documentElement.scrollWidth") <= w + 1, ("secours", tag)
                    if shots:
                        pg.screenshot(path=f"{shots}/protection-2-{tag}.png", full_page=True)
                    pg.goto(url + "/reglages/protection")
                    assert pg.evaluate("document.documentElement.scrollWidth") <= w + 1, ("reglages", tag)
                    assert errs == []
                    pg.close()
            # page de déverrouillage (application protégée, verrouillée)
            pg = br.new_page()
            pg.goto(url + "/protection/activer")
            pg.locator("#p-pw").fill(PASSWORD)
            pg.locator("#p-again").fill(PASSWORD)
            pg.get_by_role("button", name="Continuer").click()
            key = pg.locator(".recovery-key").inner_text()
            n = int(re.search(r"groupe n° (\d+)", pg.content()).group(1))
            pg.locator("#p-group").fill(key.split("-")[n - 1])
            pg.get_by_role("button", name="J'ai noté ma clé : protéger mes voyages").click()
            assert "maintenant protégés" in pg.content()
            pg.close()
            stop(app)
            app2 = create_app()
            url2 = serve(app2)
            for dark in (False, True):
                for w in (1280, 390):
                    tag = ("sombre" if dark else "clair") + ("-mobile" if w < 500 else "")
                    pg = br.new_page(viewport={"width": w, "height": 900}, color_scheme="dark" if dark else "light")
                    pg.goto(url2 + "/")
                    assert pg.url.endswith("/deverrouiller")
                    assert pg.evaluate("document.documentElement.scrollWidth") <= w + 1, ("deverrouiller", tag)
                    if shots:
                        pg.screenshot(path=f"{shots}/deverrouiller-{tag}.png", full_page=True)
                    pg.close()
            pg = br.new_page()
            pg.goto(url2 + "/")
            pg.locator("#u-pw").fill(PASSWORD)
            pg.get_by_role("button", name="Déverrouiller").click()
            assert "Voyages déverrouillés" in pg.locator(".flash").inner_text()
            pg.goto(url2 + "/reglages/protection")
            if shots:
                pg.screenshot(path=f"{shots}/protection-reglages.png", full_page=True)
            br.close()
            stop(app2)
    finally:
        for srv, th in servers:
            srv.should_exit = True
            th.join(5)


def test_no_clear_copy_elsewhere_while_protected(env):
    """Dossier protégé : « Ranger ailleurs » et « Revenir sur cet ordinateur » sont refusés (sinon une copie lisible
    serait créée) ; la page l'explique."""
    tmp, folder = env
    tid, files, key = protect(env)
    app = create_app()
    cl = TestClient(app)
    cl.post("/deverrouiller", data={"mot_de_passe": PASSWORD})
    page = cl.get("/reglages/donnees").text.replace("&#39;", "'")
    assert "Sur <b>cet ordinateur</b>" in page and "protégés par un mot de passe" in page
    assert 'value="ranger"' not in page and "Ranger mes voyages sur cet ordinateur" not in page
    other = tmp / "Dropbox" / "TripCompta"
    other.parent.mkdir()
    for data in ({"action": "ranger", "dossier": "__autre__", "autre": str(other), "mode": "copier"},
                 {"action": "local"}):
        r = cl.post("/reglages/donnees", data=data)
        assert "retire d'abord la protection" in r.text.replace("&#39;", "'")
    assert not other.exists() and K.is_protected(folder) and no_clear_text(folder) == []
    stop(app)


def test_many_pdf_thumbnails_at_once(env):
    """Page Justificatifs avec un cache de vignettes vide (après chaque déverrouillage) : beaucoup de vignettes de
    PDF demandées en même temps. La bibliothèque PDF ne le supporte pas : sans verrou, tout le processus plantait."""
    import uvicorn
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor
    from fpdf import FPDF
    from tests.conftest import _free_port
    tmp, folder = env
    app = create_app()
    tid = seed(app)
    files = []
    for i in range(12):
        d = FPDF()
        d.add_page()
        d.set_font("helvetica", size=40)
        d.cell(0, 30, f"Facture {i}")
        files.append(("files", (f"facture-{i}.pdf", bytes(d.output()), "application/pdf")))
    TestClient(app).post(f"/v/{tid}/justificatifs", files=files)
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    while not srv.started:
        time.sleep(0.05)
    try:
        with app.state.db.Session() as s:
            ids = [r.id for r in s.get(M.Trip, tid).receipts]

        def thumb(rid):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/v/{tid}/justificatifs/{rid}/vignette", timeout=30) as r:
                return r.headers["content-type"], len(r.read())
        with ThreadPoolExecutor(12) as ex:
            got = list(ex.map(thumb, ids * 2))
        assert all(ct == "image/jpeg" and n > 500 for ct, n in got)
    finally:
        srv.should_exit = True
        th.join(5)
        app.state.db.engine.dispose()


def test_ai_drafts_follow_the_unlocked_folder(env):
    """App démarrée verrouillée : après déverrouillage, les brouillons IA vont dans la vraie copie de travail."""
    tmp, folder = env
    tid, files, key = protect(env)
    app = create_app()
    cl = TestClient(app)
    cl.post("/deverrouiller", data={"mot_de_passe": PASSWORD})
    cl.post(f"/v/{tid}/ia/brouillon/mise-a-jour", json={"texte": "Nom du voyage : Sortie test\\nUne note"})
    local = app.state.coffre.local
    assert list((local / "brouillons_ia").glob(f"voyage-{tid}-*")) and not (local / ".verrouille" / "brouillons_ia").exists()
    stop(app)


def test_no_undo_button_after_unlocking(env):
    """Déverrouiller (mot de passe ou clé de secours) ne modifie aucune donnée : pas de bouton « Défaire » (il aurait
    défait la dernière vraie modification, faite avant le verrouillage). Une vraie modification le propose toujours."""
    tmp, folder = env
    tid, files, key = protect(env)
    app = create_app()
    cl = TestClient(app)
    r = cl.post("/deverrouiller", data={"mot_de_passe": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"] and "&d=" not in r.headers["location"]
    page = cl.get(r.headers["location"]).text
    assert "Voyages déverrouillés" in page and "/annuler" not in page
    r = cl.post("/voyages", data={"name": "Vraie modification"}, follow_redirects=False)
    assert "&d=" in r.headers["location"]                       # une vraie action : « Défaire » proposé
    cl.post("/verrouiller")
    r = cl.post("/deverrouiller/secours", data={"secours": key, "nouveau": "un autre mot de passe",
                                                 "confirmation": "un autre mot de passe"}, follow_redirects=False)
    assert "msg=" in r.headers["location"] and "&d=" not in r.headers["location"]
    stop(app)
