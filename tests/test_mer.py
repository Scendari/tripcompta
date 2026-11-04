"""« Mes voyages » : la mer (bateaux, surprises) et la liste qui défile au-dessus d'elle, dans un vrai navigateur."""
import datetime as dt
import socket
import threading
import time

import pytest
import uvicorn

from compta import models as M
from compta.demo import seed_demo
from compta.web import create_app

pw = pytest.importorskip("playwright.sync_api")

EGGS = ["whale", "dolphins", "gull", "bottle", "lighthouse", "flyingfish", "turtle", "school", "regatta", "coin",
        "diver", "sunset", "perch", "party"]
CLEAR = ("document.querySelectorAll('#sea-life > *, #sea-under > *, .harbour > .sea-mirror, .harbour > .school, "
         ".harbour > .sunset').forEach(function (n) { n.remove(); })")


def serve(app):
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    while not srv.started:
        time.sleep(0.05)
    return srv, th, f"http://127.0.0.1:{port}/"


@pytest.fixture
def browser():
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def test_list_scrolls_above_the_sea(tmp_path, browser):
    """20 voyages : seule la liste défile, et elle s'arrête au-dessus de la mer (qui reste visible, rien n'est caché)."""
    app = create_app(tmp_path / "d")
    with app.state.db.Session() as s:
        for i in range(20):
            s.add(M.Trip(name=f"Voyage {i + 1}", created=dt.datetime(2026, 1, 1) + dt.timedelta(days=i)))
        s.commit()
    srv, th, url = serve(app)
    try:
        pg = browser.new_page(viewport={"width": 1280, "height": 800})
        pg.goto(url)
        sea_top = pg.evaluate("document.querySelector('.harbour').getBoundingClientRect().top")
        list_bottom = pg.evaluate("document.querySelector('.page-scroll').getBoundingClientRect().bottom")
        assert abs(sea_top - list_bottom) < 1 and sea_top > 600
        pg.mouse.move(640, 300)
        pg.mouse.wheel(0, 4000)
        pg.wait_for_timeout(300)
        assert pg.evaluate("document.querySelector('.page-scroll').scrollTop") > 300     # la liste défile…
        assert pg.evaluate("window.scrollY") == 0                                          # … pas la page
        assert pg.evaluate("document.querySelector('.harbour').getBoundingClientRect().top") == sea_top
        last = pg.evaluate("(function(){var c=document.querySelectorAll('.trip-card');return c[c.length-1].getBoundingClientRect().bottom})()")
        assert last <= sea_top                                                             # dernière carte lisible
    finally:
        srv.should_exit = True
        th.join(5)


def test_every_surprise_plays_both_ways(tmp_path, browser):
    """Chaque surprise se lance, dans un sens puis dans l'autre, sans erreur ; elle dessine bien quelque chose."""
    app = create_app(tmp_path / "d")
    seed_demo(app.state.db)
    srv, th, url = serve(app)
    try:
        pg = browser.new_page(viewport={"width": 1100, "height": 700})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(url)
        assert pg.evaluate("window.tcMer.names().length") == len(EGGS)
        for egg in EGGS:
            for mirror in (False, True):
                pg.evaluate(CLEAR)
                assert pg.evaluate(f"window.tcMer.play('{egg}', {str(mirror).lower()})") is True, egg
                pg.wait_for_timeout(120)
                drawn = pg.evaluate("document.querySelectorAll('#sea-life *, #sea-under *, .harbour > .school, "
                                    ".harbour > .sunset, .harbour > .sea-mirror *').length")
                assert drawn > 0 or egg == "party", egg                  # les confettis arrivent après 2,6 s
        assert errors == []
    finally:
        srv.should_exit = True
        th.join(5)


def test_no_surprise_when_reduced_motion(tmp_path, browser):
    app = create_app(tmp_path / "d")
    srv, th, url = serve(app)
    try:
        pg = browser.new_page(reduced_motion="reduce")
        pg.goto(url)
        assert pg.evaluate("getComputedStyle(document.getElementById('sea-life')).display") == "none"
        assert pg.evaluate("getComputedStyle(document.querySelector('.harbour .b1')).animationName") == "none"
    finally:
        srv.should_exit = True
        th.join(5)


def test_party_only_when_all_trips_settled(tmp_path):
    """« Tout est réglé » (confettis) : seulement si tous les voyages sont réglés."""
    from fastapi.testclient import TestClient
    app = create_app(tmp_path / "d")
    c = TestClient(app)
    assert 'data-regle=""' in c.get("/").text                          # aucun voyage
    seed_demo(app.state.db)                                              # voyage avec des restes à recevoir
    assert 'data-regle=""' in c.get("/").text


def test_party_flag_when_everything_settled(tmp_path):
    from fastapi.testclient import TestClient
    app = create_app(tmp_path / "d")
    with app.state.db.Session() as s:
        t = M.Trip(name="Rien à régler")
        t.participants = [M.Participant(name="Alice")]
        s.add(t)
        s.commit()
    assert 'data-regle="1"' in TestClient(app).get("/").text           # personne ne doit rien, rien à rendre
