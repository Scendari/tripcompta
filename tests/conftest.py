"""Fixtures partagées : serveur réel avec données de démo + navigateur Playwright."""
import socket
import threading
import time

import pytest


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    import uvicorn
    from compta.demo import seed_demo
    from compta.web import create_app

    data = tmp_path_factory.mktemp("e2e")
    app = create_app(data)
    tid = seed_demo(app.state.db)
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", tid, data
    srv.should_exit = True
    th.join(5)


@pytest.fixture(scope="module")
def page(server):
    with __import__("playwright.sync_api", fromlist=["x"]).sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1366, "height": 900}, accept_downloads=True, locale="fr-FR")
        pg = ctx.new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.on("dialog", lambda d: d.accept())
        pg.errors = errors
        yield pg
        browser.close()


