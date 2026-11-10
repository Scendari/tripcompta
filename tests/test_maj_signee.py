"""Mises à jour signées : les empreintes d'une version ne sont acceptées que signées par Trip Compta, pour cette
version précise ; outil de publication ; signe de vie attendu par l'installeur Windows après une mise à jour.

La vraie clé privée n'est jamais utilisée ici (elle n'est ni dans le dépôt ni dans l'application) : les essais
fabriquent leur propre paire de clés.
"""
import base64
import hashlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

import pytest

from compta import signature as SIG
from compta import updater as U
from compta.db import Database

ROOT = Path(__file__).resolve().parent.parent
REAL_KEYS = SIG.PUBLIC_KEYS                      # lues avant que les essais ne mettent leur clé
PRIVATE, PUBLIC = SIG.new_key()
OTHER_PRIVATE, OTHER_PUBLIC = SIG.new_key()
NAME = "TripCompta-essai.tar.gz"


def _archive() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        data = b"1.1.0"
        ti = tarfile.TarInfo("TripCompta/VERSION.txt")
        ti.size = len(data)
        t.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


CONTENT = _archive()
RELEASE = dict(tag_name="v1.1.0", body="", html_url=U.RELEASES_PAGE, published_at="2026-11-02T10:00:00Z",
               assets=[{"name": n, "browser_download_url": f"https://exemple.invalid/{n}"}
                       for n in (NAME, U.SUMS, U.SUMS_SIG)])


@pytest.fixture(autouse=True)
def own_world(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIPCOMPTA_CONFIG", str(tmp_path / "conf" / "reglages.json"))
    monkeypatch.delenv("TRIPCOMPTA_NO_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("TRIPCOMPTA_CLE_SIGNATURE", raising=False)
    monkeypatch.setattr(SIG, "PUBLIC_KEYS", (PUBLIC,))
    monkeypatch.setattr(U, "asset_name", lambda *a, **k: NAME)


def sums(version="1.1.0", digest=None, name=NAME) -> str:
    return f"{SIG.VERSION_LINE}{version}\n{digest or hashlib.sha256(CONTENT).hexdigest()}  {name}\n"


def attempt(tmp_path, text, signature, release=RELEASE):
    """Une installation avec ce que « la publication » sert ; renvoie (étape, erreur, lancé ?, fermé ?)."""
    def download(url, dest, progress=None):
        Path(dest).write_bytes(CONTENT)
        return hashlib.sha256(CONTENT).hexdigest()
    u = U.Updater(info=dict(version="1.0.0", commit="abc", dev=False), fetch=lambda: U.release_info(release),
                  download=download)
    u.check(force=True)
    u._read_text = lambda url: signature if url.endswith(".sig") else text
    target = tmp_path / "apps" / "TripCompta"
    target.mkdir(parents=True, exist_ok=True)
    db = Database(tmp_path / "donnees", app_version="1.0.0")
    launched, quitted = [], []
    u.run_install(db, port=8765, app_args=[], quit_app=lambda: quitted.append(1), location=(target, "prog"),
                  launch=lambda **kw: launched.append(kw), workdir=tmp_path / "maj")
    return u.job["step"], u.job["error"], launched, quitted


# ---------------------------------------------------------------- la clé embarquée
def test_embedded_public_key_is_a_real_key():
    assert len(REAL_KEYS) >= 1
    for k in REAL_KEYS:
        assert len(base64.b64decode(k, validate=True)) == 32, k
    # une signature faite avec une clé quelconque n'est jamais acceptée par l'application telle qu'elle est livrée
    data = sums().encode()
    assert not SIG.verify(data, SIG.sign(data, PRIVATE), keys=REAL_KEYS)


def test_private_key_is_not_in_the_project():
    """Aucun fichier suivi du dépôt ne ressemble à une clé privée, et le secret ne s'appelle qu'une variable."""
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split("\n")
    assert not [f for f in tracked if re.search(r"cle[-_]signature|\.key$|\.pem$", f)]
    src = (ROOT / "compta" / "signature.py").read_text(encoding="utf-8")
    assert "Ed25519PrivateKey.from_private_bytes" in src and "TRIPCOMPTA_CLE_SIGNATURE" in src
    assert len(re.findall(r'"[A-Za-z0-9+/]{43}="', src)) == len(REAL_KEYS)      # seulement les clés publiques


# ---------------------------------------------------------------- vérifier une signature
def test_verify_accepts_only_the_right_key_and_the_exact_bytes():
    data = sums().encode()
    sig = SIG.sign(data, PRIVATE)
    assert SIG.verify(data, sig) and SIG.verify(data, sig + "\n") and SIG.verify(data, " " + sig)
    assert SIG.public_of(PRIVATE) == PUBLIC
    assert not SIG.verify(data + b" ", sig)                                # un octet de plus
    assert not SIG.verify(data.replace(b"1.1.0", b"1.1.1"), sig)           # la version changée
    assert not SIG.verify(data, SIG.sign(data, OTHER_PRIVATE))             # signé par quelqu'un d'autre
    flipped = base64.b64encode(bytes([base64.b64decode(sig)[0] ^ 1]) + base64.b64decode(sig)[1:]).decode()
    assert not SIG.verify(data, flipped)
    for junk in ("", "pas du base64 !", "AAAA", sig[:-4], None):
        assert not SIG.verify(data, junk or "")
    assert SIG.verify(data, SIG.sign(data, OTHER_PRIVATE), keys=(PUBLIC, OTHER_PUBLIC))     # deux clés acceptées
    assert not SIG.verify(data, sig, keys=()) and not SIG.verify(data, sig, keys=("abîmée",))


def test_self_test_says_whether_checking_works(monkeypatch):
    assert SIG.self_test() is True
    monkeypatch.setattr(SIG, "new_key", lambda: (_ for _ in ()).throw(ImportError("bibliothèque absente")))
    assert SIG.self_test() is False


def test_signed_version_line():
    assert SIG.signed_version(sums("1.6.0")) == "1.6.0"
    assert SIG.signed_version(sums("2.0.0-beta1")) == "2.0.0-beta1"
    assert SIG.signed_version(f"{'a' * 64}  fichier\n") is None
    # les anciennes versions, qui ne connaissent pas cette ligne, lisent quand même les empreintes
    assert U.parse_sums(sums()) == {NAME: hashlib.sha256(CONTENT).hexdigest()}


# ---------------------------------------------------------------- l'application refuse ce qui n'est pas signé
def test_properly_signed_update_is_installed(tmp_path):
    text = sums()
    step, error, launched, quitted = attempt(tmp_path, text, SIG.sign(text.encode(), PRIVATE))
    assert step == "redemarrage" and error is None and len(launched) == 1 and quitted == [1]


def test_update_without_signature_is_refused(tmp_path):
    text = sums()
    no_sig = dict(RELEASE, assets=[a for a in RELEASE["assets"] if a["name"] != U.SUMS_SIG])
    for release, signature in ((no_sig, ""), (RELEASE, ""), (RELEASE, "n'importe quoi")):
        step, error, launched, quitted = attempt(tmp_path, text, signature, release)
        assert step == "erreur" and error == U.UNSIGNED and launched == [] and quitted == []
    low = U.UNSIGNED.lower()
    assert "rien n'a été installé" in low and "signature de trip compta" in low
    for jargon in ("sha", "ed25519", "hash", "clé publique", "manifeste", "checksum"):
        assert jargon not in low


def test_update_signed_by_someone_else_is_refused(tmp_path):
    text = sums()
    step, error, launched, quitted = attempt(tmp_path, text, SIG.sign(text.encode(), OTHER_PRIVATE))
    assert step == "erreur" and error == U.UNSIGNED and launched == [] and quitted == []
    assert not (tmp_path / "maj" / NAME).exists()                           # même pas téléchargé


def test_tampered_fingerprints_are_refused(tmp_path):
    """Quelqu'un remplace le fichier ET son empreinte dans la publication : la signature ne correspond plus."""
    signature = SIG.sign(sums().encode(), PRIVATE)
    forged = sums(digest=hashlib.sha256(b"programme malveillant").hexdigest())
    step, error, launched, quitted = attempt(tmp_path, forged, signature)
    assert step == "erreur" and error == U.UNSIGNED and launched == []


def test_old_signed_release_cannot_be_served_as_a_new_one(tmp_path):
    """Une ancienne publication, bien signée, resservie sous un numéro plus récent : refusée (pas de retour en
    arrière forcé)."""
    old = sums("1.0.5")
    step, error, launched, quitted = attempt(tmp_path, old, SIG.sign(old.encode(), PRIVATE))
    assert step == "erreur" and error == U.UNSIGNED and launched == []
    unversioned = f"{hashlib.sha256(CONTENT).hexdigest()}  {NAME}\n"
    step, error, launched, quitted = attempt(tmp_path, unversioned, SIG.sign(unversioned.encode(), PRIVATE))
    assert step == "erreur" and error == U.UNSIGNED


def test_signed_but_damaged_download_is_still_caught(tmp_path):
    text = sums(digest="0" * 64)
    step, error, launched, quitted = attempt(tmp_path, text, SIG.sign(text.encode(), PRIVATE))
    assert step == "erreur" and "abîmé" in error and launched == []


# ---------------------------------------------------------------- l'outil de publication
def tool(*args, key=None, cwd=None):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    env.pop("TRIPCOMPTA_CLE_SIGNATURE", None)
    if key:
        env["TRIPCOMPTA_CLE_SIGNATURE"] = key
    return subprocess.run([sys.executable, str(ROOT / "build_tools" / "signer_empreintes.py"), *map(str, args)],
                          capture_output=True, text=True, env=env, cwd=cwd or ROOT)


def load_tool(monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("signer_empreintes", ROOT / "build_tools" / "signer_empreintes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def release_folder(tmp_path) -> Path:
    d = tmp_path / "assets"
    d.mkdir()
    for name, data in (("TripCompta-mac-arm64.zip", b"mac arm"), ("TripCompta-mac-intel.zip", b"mac intel"),
                       ("TripCompta-windows-setup.exe", b"windows")):
        (d / name).write_bytes(data)
    (d / "autre.txt").write_text("pas un fichier de la version")
    return d


def test_tool_signs_and_checks_a_release_folder(tmp_path, monkeypatch, capsys):
    mod = load_tool(monkeypatch)
    d = release_folder(tmp_path)
    monkeypatch.setenv("TRIPCOMPTA_CLE_SIGNATURE", PRIVATE)
    mod.main(["signer", str(d), "1.6.0"])
    text = (d / "SHA256SUMS.txt").read_text(encoding="utf-8")
    assert text.splitlines() == ["# Trip Compta 1.6.0",
                                 f"{hashlib.sha256(b'mac arm').hexdigest()}  TripCompta-mac-arm64.zip",
                                 f"{hashlib.sha256(b'mac intel').hexdigest()}  TripCompta-mac-intel.zip",
                                 f"{hashlib.sha256(b'windows').hexdigest()}  TripCompta-windows-setup.exe"]
    sig = (d / "SHA256SUMS.txt.sig").read_text()
    assert SIG.verify(text.encode("utf-8"), sig)
    # exactement ce que l'application vérifie avant d'installer
    assert U.signed_sums(text, sig, "1.6.0")["TripCompta-windows-setup.exe"] == hashlib.sha256(b"windows").hexdigest()
    assert "version 1.6.0, 3 fichiers, signature valide" in capsys.readouterr().out
    mod.main(["verifier", str(d)])
    mod.main(["verifier", str(d), "1.6.0"])
    mod.main(["cle"])
    with pytest.raises(SystemExit) as e:
        mod.main(["verifier", str(d), "1.7.0"])
    assert "signé pour la version 1.6.0, pas pour 1.7.0" in str(e.value)
    (d / "TripCompta-windows-setup.exe").write_bytes(b"remplace apres coup")
    with pytest.raises(SystemExit) as e:
        mod.main(["verifier", str(d)])
    assert "l'empreinte de TripCompta-windows-setup.exe ne correspond pas" in str(e.value)
    (d / "TripCompta-windows-setup.exe").write_bytes(b"windows")
    (d / "SHA256SUMS.txt").write_text(text.replace("1.6.0", "1.6.1"), encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        mod.main(["verifier", str(d)])
    assert "la signature ne correspond pas" in str(e.value)


def test_tool_refuses_a_missing_or_foreign_key(tmp_path, monkeypatch):
    mod = load_tool(monkeypatch)
    d = release_folder(tmp_path)
    with pytest.raises(SystemExit) as e:
        mod.main(["signer", str(d), "1.6.0"])
    assert "la clé de signature manque (secret TRIPCOMPTA_CLE_SIGNATURE)" in str(e.value)
    monkeypatch.setenv("TRIPCOMPTA_CLE_SIGNATURE", OTHER_PRIVATE)       # une clé que l'application n'accepte pas
    with pytest.raises(SystemExit) as e:
        mod.main(["signer", str(d), "1.6.0"])
    assert "n'est pas celle que l'application accepte" in str(e.value)
    monkeypatch.setenv("TRIPCOMPTA_CLE_SIGNATURE", "pas une clé")
    with pytest.raises(SystemExit) as e:
        mod.main(["cle"])
    assert "illisible" in str(e.value)
    assert not (d / "SHA256SUMS.txt").exists() and not (d / "SHA256SUMS.txt.sig").exists()


def test_tool_real_run_rejects_a_key_the_shipped_app_does_not_know(tmp_path):
    """Lancé pour de vrai (comme dans la publication), avec l'application telle qu'elle est livrée : une clé
    d'essai est refusée, rien n'est signé."""
    d = release_folder(tmp_path)
    r = tool("signer", d, "1.6.0", key=PRIVATE)
    assert r.returncode != 0 and "n'est pas celle que l'application accepte" in r.stderr
    assert not (d / "SHA256SUMS.txt.sig").exists()
    r = tool("cle")
    assert r.returncode != 0 and "la clé de signature manque" in r.stderr
    assert tool().returncode != 0 and "signer DOSSIER" in tool().stderr


def test_tool_new_key_never_shows_the_private_key(tmp_path):
    dest = tmp_path / "secret" / "cle.txt"
    r = tool("nouvelle-cle", dest)
    assert r.returncode == 0, r.stderr
    private = dest.read_text().strip()
    assert private not in r.stdout and private not in r.stderr
    public = r.stdout.strip().splitlines()[-1]
    assert SIG.public_of(private) == public
    if os.name == "posix":
        assert oct(dest.stat().st_mode & 0o777) == "0o600"
    again = tool("nouvelle-cle", dest)                                    # jamais d'écrasement
    assert again.returncode != 0 and "existe déjà" in again.stderr and dest.read_text().strip() == private
    inside = tool("nouvelle-cle", ROOT / "cle-essai-a-ne-pas-creer.txt")
    assert inside.returncode != 0 and "pas être écrite dans le dépôt" in inside.stderr
    assert not (ROOT / "cle-essai-a-ne-pas-creer.txt").exists()


# ---------------------------------------------------------------- Windows : « j'ai démarré »
def test_confirm_started_writes_the_marker_once_this_version_answers(tmp_path):
    answers = iter([None, None, "1.0.0", "1.1.0"])                         # rien, rien, l'ancienne, puis la nouvelle
    assert U.confirm_started(1, "1.1.0", work=tmp_path / "maj", pause=0, ping=lambda: next(answers))
    assert (tmp_path / "maj" / U.STARTED).read_text(encoding="utf-8") == "1.1.0"


def test_confirm_started_gives_up_quietly(tmp_path):
    assert not U.confirm_started(1, "1.1.0", work=tmp_path / "maj", tries=5, pause=0, ping=lambda: None)
    assert not U.confirm_started(1, "1.1.0", work=tmp_path / "maj", tries=5, pause=0, ping=lambda: "1.0.0")
    assert not (tmp_path / "maj" / U.STARTED).exists()


def test_app_relaunched_after_update_says_it_started(tmp_path):
    """Pour de vrai : l'application relancée avec « --apres-maj » (comme par l'installeur Windows) écrit
    « demarre.txt » dans le dossier de travail des mises à jour dès qu'elle répond."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    conf = tmp_path / "conf" / "reglages.json"
    work = conf.parent / "maj"
    work.mkdir(parents=True)
    U.write_relaunch(work, port, ["--navigateur", "--data-dir", str(tmp_path / "voyages")])
    env = dict(os.environ, TRIPCOMPTA_CONFIG=str(conf), TRIPCOMPTA_NO_UPDATE_CHECK="1", TRIPCOMPTA_FENETRE_DISCRETE="1")
    for k in ("TRIPCOMPTA_DATA_DIR", "TRIPCOMPTA_RESTARTED", "TRIPCOMPTA_PORT_WAIT"):
        env.pop(k, None)
    p = subprocess.Popen([sys.executable, "-u", str(ROOT / "run.py"), "--apres-maj"], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        marker = work / U.STARTED
        for _ in range(240):
            if marker.exists():
                break
            time.sleep(0.25)
        assert marker.exists(), "pas de signe de vie"
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=5) as r:
            version = json.load(r)["version"]
        assert marker.read_text(encoding="utf-8") == version
        assert not (work / U.RELAUNCH).exists()                             # options de relance lues
    finally:
        try:
            urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/quitter", data=b"",
                                                          method="POST"), timeout=5)
        except Exception:
            pass
        try:
            p.wait(15)
        except subprocess.TimeoutExpired:
            p.kill()


def test_a_normal_launch_does_not_write_the_marker():
    src = (ROOT / "run.py").read_text(encoding="utf-8")
    assert re.search(r"if after_update:\s*\n(\s*#.*\n)*\s*from compta import updater as U", src)
    assert src.count("confirm_started") == 1


# ---------------------------------------------------------------- l'installeur et la publication disent la même chose
def test_windows_installer_waits_for_the_marker_and_can_go_back():
    iss = (ROOT / "build_tools" / "windows" / "TripCompta.iss").read_text(encoding="utf-8-sig")
    assert f"'\\{U.STARTED}'" in iss                                        # le même nom de fichier des deux côtés
    cmd = U.installer_command(Path("setup.exe"), Path("travail"), Path("ancien.exe"), 42)
    for option in (a.split("=")[0].lstrip("/") for a in cmd if "=" in a and not a.startswith("/LOG")):
        assert "{param:" + option + "|" in iss, option                      # chaque option donnée est lue
    assert "{param:ATTENTE|120}" in iss
    assert 'Parameters: "--apres-maj"' not in iss                           # la relance est faite par le code…
    assert iss.count("'--apres-maj'") >= 3                                  # … nouvelle version, ou ancienne remise
    for step in ("RenameFile(AppDir, AppDir + '.ancien')", "function RestoreOld", "procedure StartAfterUpdate",
                 "if IsUpdate then\n      StartAfterUpdate;", "Stop-Process -Force", "RestoreOld;"):
        assert step in iss, step
    # les messages d'échec, en français simple (ils sont affichés tels quels par l'ancienne version relancée)
    assert "La nouvelle version n''a pas démarré correctement. L''ancienne version a été remise en place." in iss
    assert iss.count("begin") == iss.count("end;") + iss.count("end\n") + iss.count("end.")


def test_release_workflow_signs_before_publishing():
    wf = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    cle = wf.index("signer_empreintes.py cle")
    build = wf.index("\n  build:")
    sign = wf.index("signer_empreintes.py signer assets")
    create = wf.index("gh release create")
    publish = wf.index('gh release edit "$TAG" --draft=false')
    assert cle < build < sign < create < publish                           # clé vérifiée avant de fabriquer
    assert "--draft" in wf[create:publish]
    assert wf.count("secrets.TRIPCOMPTA_CLE_SIGNATURE") == 2
    doc = (ROOT / "docs" / "PUBLIER.md").read_text(encoding="utf-8")
    for must in ("TRIPCOMPTA_CLE_SIGNATURE", "publications immuables", "signer_empreintes.py", "gh secret set"):
        assert must.lower() in doc.lower(), must
