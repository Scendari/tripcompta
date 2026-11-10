"""Jeton de session : tiré au hasard à chaque lancement, exigé partout sauf /ping et les fichiers d'apparence.

Sans lui, un autre programme (ou une autre session ouverte sur le même ordinateur) ne peut ni lire ni modifier les
voyages à l'adresse locale de Trip Compta. La fenêtre le reçoit dans la première adresse qu'elle ouvre ; un
programme le donne dans un en-tête.
"""
import html
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from compta import fenetre
from compta import models as M
from compta.ops import Ops
from compta.web import NO_TOKEN, create_app, token_cookie

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "jeton-d-essai_0123456789"


def clean(t):
    return re.sub(r"\s+", " ", html.unescape(t).replace("\xa0", " "))


def setup(tmp_path, token=TOKEN):
    app = create_app(tmp_path / "voyages", token=token)
    ops = Ops(app.state.db)
    tid = ops.create_trip("Voyage très secret")["voyage_id"]
    ops.add_participants(tid, ["Anne", "Bruno"])
    ops.add_expense(tid, "Gîte", montant_reel="300")
    return app, tid


def count(app, model):
    with app.state.db.Session() as s:
        return s.query(model).count()


# ---------------------------------------------------------------- sans jeton : rien
def test_without_the_token_nothing_is_readable(tmp_path):
    app, tid = setup(tmp_path)
    cl = TestClient(app)
    for url in ("/", f"/v/{tid}", f"/v/{tid}/depenses", "/reglages", "/aide", f"/v/{tid}/export.xlsx",
                "/reglages/diagnostic.txt", "/tous-les-voyages.zip"):
        r = cl.get(url)
        assert r.status_code == 403, url
        assert "Voyage très secret" not in r.text and "Anne" not in r.text and "Gîte" not in r.text
        assert NO_TOKEN in clean(r.text)
    r = cl.get("/api/voyages")
    assert r.status_code == 403 and "Authorization: Bearer" in r.json()["detail"] and "secret" not in r.text
    assert cl.get(f"/api/voyages/{tid}").status_code == 403


def test_without_the_token_nothing_can_be_changed(tmp_path):
    app, tid = setup(tmp_path)
    cl = TestClient(app)
    before = (count(app, M.Trip), count(app, M.Participant), count(app, M.Expense), count(app, M.Movement))
    assert cl.post("/voyages", data={"name": "Intrus"}).status_code == 403
    assert cl.post(f"/v/{tid}/participants", data={"names": "Intrus"}).status_code == 403
    assert cl.post(f"/v/{tid}/supprimer").status_code == 403
    assert cl.post("/api/voyages", json={"nom": "Intrus"}).status_code == 403
    assert cl.post(f"/api/voyages/{tid}/mouvements", json={"participant": "Anne", "montant": "5"}).status_code == 403
    assert cl.post("/quitter").status_code == 403 and cl.post("/annuler").status_code == 403
    # le jeton dans l'adresse ne vaut que pour OUVRIR une page (GET), jamais pour modifier
    assert cl.post(f"/voyages?jeton={TOKEN}", data={"name": "Intrus"}).status_code == 403
    assert before == (count(app, M.Trip), count(app, M.Participant), count(app, M.Expense), count(app, M.Movement))


def test_ping_and_look_files_stay_open(tmp_path):
    app, tid = setup(tmp_path)
    cl = TestClient(app)
    info = cl.get("/ping").json()
    assert info["app"] == "tripcompta" and info["jeton"] is True and set(info) <= {"app", "boot", "version", "jeton"}
    assert cl.get("/static/app.css").status_code == 200
    assert TOKEN not in json.dumps(info)


def test_wrong_or_old_token_is_refused(tmp_path):
    app, tid = setup(tmp_path)
    cl = TestClient(app)
    for bad in ("", "x", TOKEN[:-1], TOKEN + "x", TOKEN.upper()):
        r = cl.get(f"/?jeton={bad}", follow_redirects=False)
        assert r.status_code == 403 and "set-cookie" not in r.headers, bad
        assert cl.get("/api/voyages", headers={"X-TripCompta-Jeton": bad}).status_code == 403
        assert cl.get("/api/voyages", headers={"Authorization": f"Bearer {bad}"}).status_code == 403
    assert cl.get("/api/voyages", headers={"Authorization": TOKEN}).status_code == 403        # sans « Bearer »
    cl.cookies.set(token_cookie(None), "faux")
    assert cl.get("/").status_code == 403


# ---------------------------------------------------------------- avec le jeton
def test_first_address_turns_the_token_into_a_cookie(tmp_path):
    app, tid = setup(tmp_path)
    cl = TestClient(app)
    r = cl.get(f"/?jeton={TOKEN}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"          # le jeton quitte aussitôt l'adresse
    cookie = r.headers["set-cookie"].lower()
    assert cookie.startswith(f"{token_cookie(None)}={TOKEN.lower()}") or TOKEN in r.headers["set-cookie"]
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    assert "max-age" not in cookie and "expires" not in cookie              # le temps de la session seulement
    page = cl.get("/")                                                      # le client a gardé le témoin
    assert page.status_code == 200 and "Voyage très secret" in page.text
    assert TOKEN not in page.text                                           # jamais écrit dans une page ordinaire
    # tout marche ensuite comme d'habitude, y compris modifier
    r = cl.post(f"/v/{tid}/argent", data={"participant": "1", "kind": "versement", "amount": "10"},
                follow_redirects=False)
    assert r.status_code == 303 and count(app, M.Movement) == 1


def test_other_query_parameters_survive_the_redirect(tmp_path):
    app, tid = setup(tmp_path)
    cl = TestClient(app)
    r = cl.get(f"/v/{tid}/depenses?tri=montant&jeton={TOKEN}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/v/{tid}/depenses?tri=montant"
    # déjà connu (témoin) : une adresse avec un jeton, même périmé, est simplement nettoyée
    r = cl.get(f"/v/{tid}/depenses?jeton=ancien&tri=date", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/v/{tid}/depenses?tri=date"
    assert "set-cookie" not in r.headers


def test_a_program_gives_the_token_in_a_header(tmp_path):
    app, tid = setup(tmp_path)
    cl = TestClient(app)
    for headers in ({"Authorization": f"Bearer {TOKEN}"}, {"authorization": f"bearer {TOKEN}"},
                    {"X-TripCompta-Jeton": TOKEN}):
        r = cl.get("/api/voyages", headers=headers)
        assert r.status_code == 200 and "Voyage très secret" in r.text, headers
    r = cl.post(f"/api/voyages/{tid}/mouvements", json={"participant": "Anne", "montant": "5"},
                headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and count(app, M.Movement) == 1
    # la page IA donne ce jeton aux programmes (elle-même n'est lisible qu'avec lui)
    page = clean(cl.get(f"/v/{tid}/ia", headers={"X-TripCompta-Jeton": TOKEN}).text)
    assert f"Authorization: Bearer {TOKEN}" in page and "change à chaque lancement" in page


def test_two_trip_comptas_do_not_share_their_cookie(tmp_path):
    """Deux Trip Compta ouverts (deux dossiers) : même ordinateur, deux adresses, deux jetons. Le témoin de l'un ne
    vaut pas chez l'autre, et ne l'écrase pas."""
    a, _ = setup(tmp_path / "a", token="jeton-A")
    b, _ = setup(tmp_path / "b", token="jeton-B")
    ca = TestClient(a, base_url="http://127.0.0.1:8765")
    cb = TestClient(b, base_url="http://127.0.0.1:8766")
    assert ca.get("/?jeton=jeton-A").status_code == 200
    assert token_cookie(8765) != token_cookie(8766)
    cb.cookies.set(token_cookie(8765), "jeton-A")                 # le navigateur envoie les deux témoins aux deux
    assert cb.get("/").status_code == 403
    assert cb.get("/?jeton=jeton-B").status_code == 200
    ca.cookies.set(token_cookie(8766), "jeton-B")
    assert ca.get("/").status_code == 200                         # le témoin de A est toujours bon chez A


def test_no_token_given_means_no_check(tmp_path):
    """Application créée directement (les essais, un outil) : pas de jeton, pas de contrôle, /ping le dit."""
    app = create_app(tmp_path / "voyages")
    cl = TestClient(app)
    assert cl.get("/").status_code == 200 and cl.get("/ping").json()["jeton"] is False
    assert "Authorization: Bearer" not in cl.get("/aide").text


def test_problem_screens_are_protected_too(tmp_path):
    """Voyages illisibles : l'écran d'explication (et le diagnostic) demandent aussi le jeton."""
    from compta import db as D
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
    broken = create_app(f, token=TOKEN)
    assert broken.state.db is None
    cl = TestClient(broken)
    assert cl.get("/").status_code == 403 and cl.get("/reglages/diagnostic.txt").status_code == 403
    assert cl.get("/ping").json()["jeton"] is True
    assert cl.get("/", headers={"X-TripCompta-Jeton": TOKEN}).status_code == 503
    assert cl.get("/reglages/diagnostic.txt", headers={"X-TripCompta-Jeton": TOKEN}).status_code == 200


# ---------------------------------------------------------------- les adresses ouvertes par l'application
def test_entry_address_and_headers():
    assert fenetre.with_token("http://127.0.0.1:8765/", "abc") == "http://127.0.0.1:8765/?jeton=abc"
    assert fenetre.with_token("http://127.0.0.1:8765/v/3/depenses?tri=date#e12", "a b/&") == \
        "http://127.0.0.1:8765/v/3/depenses?tri=date&jeton=a%20b%2F%26#e12"
    assert fenetre.with_token("http://127.0.0.1:8765/", None) == "http://127.0.0.1:8765/"
    assert fenetre.with_token("http://127.0.0.1:8765/", "") == "http://127.0.0.1:8765/"
    assert fenetre.token_headers("abc") == {"X-TripCompta-Jeton": "abc"} and fenetre.token_headers(None) == {}


def test_each_new_window_opens_with_the_token(monkeypatch):
    import types

    class Event:                                  # comme ceux de pywebview : on s'y abonne avec « += »
        def __iadd__(self, fn):
            return self

    made = []
    fake = types.SimpleNamespace(windows=[], create_window=lambda title, url, **kw: made.append(url) or
                                 types.SimpleNamespace(events=types.SimpleNamespace(loaded=Event(), shown=Event())))
    monkeypatch.setitem(sys.modules, "webview", fake)
    f = fenetre.Fenetres("http://127.0.0.1:8765/", None, token="abc")
    f.new()
    f.new("http://127.0.0.1:8765/v/2/depenses")
    assert made == ["http://127.0.0.1:8765/?jeton=abc", "http://127.0.0.1:8765/v/2/depenses?jeton=abc"]
    made.clear()
    fenetre.Fenetres("http://127.0.0.1:8765/", None).new()
    assert made == ["http://127.0.0.1:8765/"]


def test_running_instance_file_carries_the_token_privately(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIPCOMPTA_CONFIG", str(tmp_path / "conf" / "reglages.json"))
    import run
    lock = run.InstanceLock(tmp_path / "voyages")
    assert lock.running_token() is None
    lock.publish(8765, "abc")
    assert json.loads(lock.info.read_text(encoding="utf-8")) == {"pid": os.getpid(), "port": 8765, "jeton": "abc"}
    assert lock.running_token() == "abc"
    if os.name == "posix":
        assert oct(lock.info.stat().st_mode & 0o777) == "0o600"          # lisible par cette session seulement
    lock.publish(8766, None)
    assert lock.running_token() is None


def test_second_launch_talks_to_the_first_with_its_token(monkeypatch):
    import run
    seen = []

    class Reply:
        status = 200

        def __init__(self, body=b"{}"):
            self.body = body

        def read(self, *a):
            return self.body

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, timeout=0):
        if isinstance(req, str):
            seen.append(("GET", req, {}))
            return Reply(b'{"app": "tripcompta", "version": "1.0.0", "jeton": true}')
        seen.append((req.get_method(), req.full_url, dict(req.header_items())))
        return Reply()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    assert run.show_running_window("http://127.0.0.1:8765/", "abc")
    assert seen[-1] == ("POST", "http://127.0.0.1:8765/fenetre/montrer", {"X-tripcompta-jeton": "abc"})
    assert run.show_running_window("http://127.0.0.1:8765/")                 # version sans jeton : comme avant
    assert seen[-1][2] == {}
    assert run.needs_token("http://127.0.0.1:8765/") is True


# ---------------------------------------------------------------- pour de vrai : l'application lancée
def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url, token=None):
    req = urllib.request.Request(url, headers={"X-TripCompta-Jeton": token} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _launch(tmp_path, port, **extra):
    env = dict(os.environ, TRIPCOMPTA_CONFIG=str(tmp_path / "conf" / "reglages.json"), TRIPCOMPTA_NO_UPDATE_CHECK="1",
               PYTHONIOENCODING="utf-8")
    for k in ("TRIPCOMPTA_DATA_DIR", "TRIPCOMPTA_RESTARTED", "TRIPCOMPTA_PORT_WAIT", "TRIPCOMPTA_JETON"):
        env.pop(k, None)
    env.update(extra)
    return subprocess.Popen([sys.executable, "-u", str(ROOT / "run.py"), "--no-browser", "--data-dir",
                             str(tmp_path / "voyages"), "--port", str(port)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")


def _wait_ping(port, seconds=30):
    for _ in range(seconds * 4):
        code, body = None, ""
        try:
            code, body = _get(f"http://127.0.0.1:{port}/ping")
        except OSError:
            pass
        if code == 200:
            return json.loads(body)
        time.sleep(0.25)
    return None


def _stop(p, port, token):
    try:
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/quitter", data=b"", method="POST",
                                                      headers={"X-TripCompta-Jeton": token}), timeout=5).close()
    except Exception:
        pass
    try:
        p.wait(15)
    except subprocess.TimeoutExpired:
        p.kill()
        raise AssertionError("l'application ne s'est pas arrêtée à la demande (jeton refusé ?)")


def test_launched_app_draws_its_own_token(tmp_path):
    port = _free_port()
    p = _launch(tmp_path, port)
    token = None
    try:
        info = _wait_ping(port)
        assert info and info["jeton"] is True
        code, body = _get(f"http://127.0.0.1:{port}/")
        assert code == 403 and NO_TOKEN in clean(body)
        assert _get(f"http://127.0.0.1:{port}/api/voyages")[0] == 403
        # le jeton est dans le fichier « où joindre celle qui tourne », à côté des réglages de la session
        files = list((tmp_path / "conf").glob("instance-*.json"))
        assert len(files) == 1
        token = json.loads(files[0].read_text(encoding="utf-8"))["jeton"]
        assert len(token) >= 32
        assert _get(f"http://127.0.0.1:{port}/", token)[0] == 200
        assert _get(f"http://127.0.0.1:{port}/api/voyages", token) == (200, "[]")
        # demander l'arrêt sans jeton : refusé, elle tourne toujours
        try:
            urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/quitter", data=b"",
                                                          method="POST"), timeout=5)
            refused = False
        except urllib.error.HTTPError as e:
            refused = e.code == 403
        assert refused and _wait_ping(port, 3)
    finally:
        if token:
            _stop(p, port, token)
        else:
            p.kill()
    out = p.stdout.read()
    assert f"?jeton={token}" in out                               # l'adresse à ouvrir, avec le jeton


def test_restarted_app_keeps_the_same_token(tmp_path):
    """Relancée (changement de dossier, mise à jour…) avec TRIPCOMPTA_JETON : même jeton, la page ouverte reste bonne.
    Deux lancements ordinaires, eux, n'ont jamais le même."""
    port = _free_port()
    p = _launch(tmp_path, port, TRIPCOMPTA_JETON="jeton-garde-par-le-redemarrage")
    try:
        assert _wait_ping(port)
        assert _get(f"http://127.0.0.1:{port}/", "jeton-garde-par-le-redemarrage")[0] == 200
        assert _get(f"http://127.0.0.1:{port}/", "un-autre")[0] == 403
    finally:
        _stop(p, port, "jeton-garde-par-le-redemarrage")
    src = (ROOT / "compta" / "scripts" / "remplacer.sh").read_text(encoding="utf-8")
    assert "TRIPCOMPTA_JETON" in src                              # la mise à jour Mac le transmet aussi


def test_browser_enters_with_the_token_then_works_normally(tmp_path):
    import uvicorn
    from playwright.sync_api import sync_playwright
    app, tid = setup(tmp_path)
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(locale="fr-FR")
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(fenetre.with_token(base + "/", TOKEN))
            assert page.url == base + "/"                           # le jeton n'est plus dans l'adresse
            assert "Voyage très secret" in page.locator("body").inner_text()
            cookies = {c["name"]: c for c in ctx.cookies()}
            c = cookies[token_cookie(port)]
            assert c["httpOnly"] and c["sameSite"] == "Strict" and c["expires"] == -1
            assert page.evaluate("document.cookie") == ""           # invisible pour les scripts de la page
            # naviguer et modifier, comme d'habitude
            page.goto(f"{base}/v/{tid}/argent/mouvements?k=versement")
            page.select_option("#participant", index=1)
            page.fill("#amount", "25")
            page.click("#ajout button:has-text('Enregistrer')")
            page.wait_for_url("**/argent/mouvements?msg=*")
            assert count(app, M.Movement) == 1
            # un lien vers une dépense, ouvert dans une nouvelle fenêtre de l'application (adresse avec jeton)
            page.goto(fenetre.with_token(f"{base}/v/{tid}/depenses?tri=nom", TOKEN))
            assert page.url == f"{base}/v/{tid}/depenses?tri=nom"
            # un autre navigateur (ou une autre session), sans le jeton : refusé, en clair
            other = browser.new_context(locale="fr-FR").new_page()
            resp = other.goto(base + "/")
            assert resp.status == 403
            text = other.locator("body").inner_text()
            assert "ne s'ouvre que depuis l'application" in text and "secret" not in text
            assert not errors, errors
            browser.close()
    finally:
        srv.should_exit = True
        th.join(5)


def test_real_window_receives_the_token():
    """La vraie fenêtre (WebKit sur Mac), en mode discret : elle ouvre l'adresse avec le jeton, puis une action faite
    DANS la page (qui n'a que le témoin) est acceptée."""
    if not fenetre.available():
        pytest.skip("pas de fenêtre possible ici")
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="tripcompta-jeton-"))
    port = _free_port()
    env = dict(os.environ, TRIPCOMPTA_CONFIG=str(tmp / "conf" / "reglages.json"), TRIPCOMPTA_NO_UPDATE_CHECK="1",
               TRIPCOMPTA_FENETRE_DISCRETE="1", TRIPCOMPTA_ESSAI_FENETRE="1", TRIPCOMPTA_JETON=TOKEN,
               PYTHONIOENCODING="utf-8")
    for k in ("TRIPCOMPTA_DATA_DIR", "TRIPCOMPTA_RESTARTED", "TRIPCOMPTA_PORT_WAIT"):
        env.pop(k, None)
    p = subprocess.Popen([sys.executable, "-u", str(ROOT / "run.py"), "--fenetre", "--data-dir", str(tmp / "voyages"),
                          "--port", str(port)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        info = {}
        for _ in range(240):
            info = _wait_ping(port, 1) or {}
            if info.get("fenetre") or p.poll() is not None:
                break
            time.sleep(0.25)
        assert info.get("fenetre"), "la fenêtre n'a pas affiché Trip Compta"
        assert "Trip Compta" in info["fenetre"]["titre"]            # la page d'accueil, pas l'écran de refus
        made = False
        for _ in range(60):
            code, body = _get(f"http://127.0.0.1:{port}/api/voyages", TOKEN)
            if code == 200 and "Essai de la fenêtre" in body:
                made = True
                break
            time.sleep(0.5)
        assert made, "l'action faite dans la fenêtre n'est pas arrivée"
    finally:
        _stop(p, port, TOKEN)
