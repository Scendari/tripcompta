"""Le serveur de Trip Compta n'écoute que sur cet ordinateur (127.0.0.1), mais un site ouvert dans le navigateur
pourrait lui envoyer des requêtes (formulaire caché, script : « CSRF ») ou se faire passer pour lui en changeant
l'adresse de son propre nom (« DNS rebinding »). Refusé ; ce qui vient de Trip Compta (fenêtre, onglet, IA via
l'API, relance) passe."""
import pytest
from fastapi.testclient import TestClient

from compta.web import create_app, request_refusal

HOST = "127.0.0.1:8765"


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path / "d")


def trips(app):
    from compta import models as M
    with app.state.db.Session() as s:
        return sorted(t.name for t in s.query(M.Trip).all())


def post(c, url, headers, **kw):
    return c.post(url, headers=headers, follow_redirects=False, **kw)


def test_pages_of_trip_compta_pass(app):
    c = TestClient(app, base_url=f"http://{HOST}")
    # le navigateur (onglet ou fenêtre) : Origin = Trip Compta lui-même
    r = post(c, "/voyages", {"origin": f"http://{HOST}", "sec-fetch-site": "same-origin"}, data={"name": "Fenêtre"})
    assert r.status_code == 303
    # localhost et [::1] aussi
    c2 = TestClient(app, base_url="http://localhost:8765")
    assert post(c2, "/voyages", {"origin": "http://localhost:8765"}, data={"name": "Localhost"}).status_code == 303
    # un programme (IA via l'API, curl) : ni Origin ni Sec-Fetch-Site
    r = c.post("/api/voyages", json={"nom": "Par l'IA"})
    assert r.status_code < 400, r.text
    # adresse tapée dans la barre : Sec-Fetch-Site « none »
    assert post(c, "/voyages", {"sec-fetch-site": "none"}, data={"name": "Barre"}).status_code == 303
    assert trips(app) == ["Barre", "Fenêtre", "Localhost", "Par l'IA"]
    assert c.get("/").status_code == 200


def test_other_sites_cannot_change_anything(app):
    c = TestClient(app, base_url=f"http://{HOST}")
    before = trips(app)
    for headers in ({"origin": "https://site-malveillant.example"},
                    {"origin": "http://127.0.0.1:9999"},                      # autre programme local, autre port
                    {"origin": "null"},                                       # cadre « sandbox » d'un autre site
                    {"origin": f"https://{HOST}"},
                    {"sec-fetch-site": "cross-site"},
                    {"sec-fetch-site": "same-site"}):
        r = post(c, "/voyages", headers, data={"name": "Piège"})
        assert r.status_code == 403, headers
        assert "ne vient pas d'une page de Trip Compta" in r.text
        r = c.post("/api/voyages", json={"nom": "Piège"}, headers=headers)
        assert r.status_code == 403 and "Trip Compta" in r.json()["detail"]
        assert post(c, "/quitter", headers).status_code == 403              # ni l'arrêter
    assert trips(app) == before


def test_dns_rebinding_is_refused(app):
    """Un site qui se fait passer pour 127.0.0.1 garde son propre nom dans l'adresse (Host) : refusé, même pour lire."""
    c = TestClient(app, base_url="http://site-malveillant.example:8765")
    r = c.get("/")
    assert r.status_code == 400 and "ne répond qu'à cet ordinateur" in r.text
    assert c.get("/api/voyages").status_code == 400
    assert c.get("/tous-les-voyages.zip").status_code == 400
    assert c.post("/voyages", data={"name": "x"}).status_code == 400


def test_problem_screens_are_guarded_too(tmp_path):
    from compta.db import Database
    db = Database(tmp_path / "v")
    db.engine.dispose()
    data = bytearray((tmp_path / "v" / "compta.db").read_bytes())
    for i in range(1024, len(data) - 100):
        data[i] = 0xAB
    (tmp_path / "v" / "compta.db").write_bytes(bytes(data))
    for p in (tmp_path / "v").rglob("*.db"):
        if p.name != "compta.db":
            p.unlink()
    app = create_app(tmp_path / "v")
    assert app.state.db is None                                  # écran « abîmé »
    c = TestClient(app, base_url=f"http://{HOST}")
    assert post(c, "/quitter", {"origin": "https://site-malveillant.example"}).status_code == 403
    assert TestClient(app, base_url="http://evil.example").get("/").status_code == 400


def test_rule():
    ok = {"host": HOST, "origin": f"http://{HOST}"}
    assert request_refusal("POST", ok) is None
    assert request_refusal("GET", {"host": HOST, "origin": "https://x.example"}) is None   # lire : la page ne voit rien
    assert request_refusal("POST", {"host": "[::1]:8765", "origin": "http://[::1]:8765"}) is None
    assert request_refusal("GET", {"host": "x.example"})[0] == 400
    assert request_refusal("POST", {"host": HOST, "origin": "http://localhost:8765"})[0] == 403   # pas la même page
    assert request_refusal("DELETE", {"host": HOST, "sec-fetch-site": "cross-site"})[0] == 403


def test_window_and_browser_mode_really_work(tmp_path):
    """Un vrai navigateur (Chromium, comme le mode navigateur et WebView2) sur Trip Compta : les formulaires passent ;
    une page d'un autre site (autre adresse) ne peut rien modifier."""
    import threading
    import time
    import uvicorn
    from playwright.sync_api import sync_playwright
    from tests.conftest import _free_port
    app = create_app(tmp_path / "d")
    port, evil_port = _free_port(), _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    # « site malveillant » : une autre adresse (localhost:autre port) qui envoie un formulaire caché à Trip Compta
    from http.server import BaseHTTPRequestHandler, HTTPServer
    page = (f'<form id=f method=post action="http://127.0.0.1:{port}/voyages"><input name=name value=Piege></form>'
            f'<script>document.getElementById("f").submit()</script>').encode()

    class Evil(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *a):
            pass
    evil = HTTPServer(("127.0.0.1", evil_port), Evil)
    threading.Thread(target=evil.serve_forever, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    try:
        with sync_playwright() as p:
            br = p.chromium.launch()
            pg = br.new_page()
            pg.goto(f"http://127.0.0.1:{port}/")
            pg.evaluate("""() => { const f = document.createElement('form'); f.method = 'post'; f.action = '/voyages';
                const i = document.createElement('input'); i.name = 'name'; i.value = 'Depuis la page'; f.append(i);
                document.body.append(f); f.submit(); }""")
            pg.wait_for_function("document.body && document.body.innerText.includes('Depuis la page')")
            pg.goto(f"http://localhost:{evil_port}/")
            pg.wait_for_function("document.body && document.body.innerText.includes('ne vient pas d')")
            br.close()
        assert trips(app) == ["Depuis la page"]
    finally:
        evil.shutdown()
        srv.should_exit = True
        th.join(5)


def test_no_get_route_changes_the_data(tmp_path):
    """Le contrôle d'origine ne porte que sur POST/PUT/PATCH/DELETE : aucune page GET ne doit donc modifier les
    voyages. On ouvre TOUTES les routes GET (exemple de démonstration) et on compare le contenu de la base."""
    import re
    import sqlite3
    from compta.demo import seed_demo
    app = create_app(tmp_path / "d")
    tid = seed_demo(app.state.db, replace=True)
    from compta import models as M
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        ids = {"tid": tid, "eid": t.expenses[0].id, "pid": t.participants[0].id,
               "rid": t.receipts[0].id if t.receipts else 1, "cid": t.categories[0].id if t.categories else 1,
               "fid": t.families[0].id if getattr(t, "families", None) else 1,
               "sid": t.subaccounts[0].id if getattr(t, "subaccounts", None) else 1,
               "mid": t.movements[0].id if getattr(t, "movements", None) else 1}

    def dump():
        con = sqlite3.connect(app.state.db.path)
        try:
            return [l_ for l_ in con.iterdump() if "'app_version'" not in l_]
        finally:
            con.close()
    before = dump()
    c = TestClient(app, base_url=f"http://{HOST}")
    seen = []
    for route in app.routes:
        if "GET" not in (getattr(route, "methods", None) or set()) or route.path.startswith(("/static", "/api/docs")):
            continue
        url = route.path
        for name in re.findall(r"{(\w+)(?::\w+)?}", url):
            url = re.sub(r"{%s(?::\w+)?}" % name, str(ids.get(name, "x")), url)
        c.get(url, follow_redirects=False)
        seen.append(url)
    assert len(seen) > 40
    assert dump() == before, "une route GET a modifié la base"
