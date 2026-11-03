"""Mises à jour dans le navigateur : bandeau, « À propos », « Vérifier maintenant », désactivation, page d'attente.
API GitHub simulée. Captures en clair, sombre et à la largeur d'un téléphone (dossier TRIPCOMPTA_SHOTS si défini)."""
import os
import threading
import time
from pathlib import Path

import pytest

from compta import updater as U
from compta.web import create_app

RELEASE = dict(tag_name="v1.1.0", published_at="2026-11-02T10:00:00Z", html_url=U.RELEASES_PAGE,
               body="- Nouveau : un mail tout prêt pour le trésorier\n- Les désistements apparaissent dans le bilan\n"
                    "- Correction : arrondis des remises",
               assets=[{"name": n, "browser_download_url": f"https://exemple.invalid/{n}"} for n in
                       ("TripCompta-mac-arm64.zip", "TripCompta-mac-intel.zip", "TripCompta-windows.zip",
                        "TripCompta-linux.tar.gz", "SHA256SUMS.txt")])


class GitHub:
    def __init__(self):
        self.online, self.calls = True, 0

    def __call__(self):
        self.calls += 1
        if not self.online:
            raise OSError("hors ligne")
        return U.release_info(RELEASE)


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    import uvicorn
    from tests.conftest import _free_port
    root = tmp_path_factory.mktemp("maj-ui")
    old = os.environ.get("TRIPCOMPTA_CONFIG")
    os.environ["TRIPCOMPTA_CONFIG"] = str(root / "conf" / "reglages.json")
    gh = GitHub()
    upd = U.Updater(info=dict(version="1.0.0", commit="abc1234", dev=False), fetch=gh)
    app = create_app(root / "donnees", updater=upd)
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", upd, gh
    srv.should_exit = True
    th.join(5)
    if old is None:
        os.environ.pop("TRIPCOMPTA_CONFIG", None)
    else:
        os.environ["TRIPCOMPTA_CONFIG"] = old


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def new_page(browser, dark=False, mobile=False):
    ctx = browser.new_context(viewport={"width": 390, "height": 844} if mobile else {"width": 1280, "height": 860},
                              color_scheme="dark" if dark else "light", locale="fr-FR")
    pg = ctx.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" else None)
    pg.on("dialog", lambda d: d.accept())
    return pg


def shot(pg, name):
    d = os.environ.get("TRIPCOMPTA_SHOTS")
    if d:
        Path(d).mkdir(parents=True, exist_ok=True)
        pg.screenshot(path=str(Path(d) / f"{name}.png"), full_page=True)


def no_horizontal_scroll(pg):
    return pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")


def test_flow(env, browser):
    url, upd, gh = env
    pg = new_page(browser)
    pg.goto(url + "/")
    assert pg.locator(".update-banner").count() == 0
    pg.get_by_role("link", name="ℹ️ À propos · version 1.0.0").click()
    assert pg.locator(".about-version").inner_text() == "Trip Compta 1.0.0"
    assert pg.get_by_text("Pas encore vérifié").is_visible()

    # hors ligne : message clair, rien d'autre
    gh.online = False
    pg.get_by_role("button", name="🔄 Vérifier maintenant").click()
    assert "Impossible de joindre le site des mises à jour" in pg.locator(".flash").inner_text()
    assert pg.locator(".update-card").count() == 0

    gh.online = True
    pg.get_by_role("button", name="🔄 Vérifier maintenant").click()
    assert "Une nouvelle version est disponible : 1.1.0" in pg.locator(".flash").inner_text()
    card = pg.locator(".update-card")
    assert "Nouvelle version : 1.1.0" in card.inner_text() and "un mail tout prêt" in card.inner_text()
    assert "Télécharger la nouvelle version" in card.inner_text()            # code source : pas installable ici
    assert pg.locator(".update-banner").count() == 0                       # pas en double avec la carte
    shot(pg, "a-propos-clair")

    # le bandeau sur une page ordinaire, puis « Plus tard »
    pg.goto(url + "/")
    banner = pg.locator(".update-banner")
    assert "Trip Compta 1.1.0 est disponible." in banner.inner_text()
    shot(pg, "bandeau-clair")
    banner.get_by_role("link", name="Voir les nouveautés").click()
    assert pg.url.endswith("/reglages/a-propos#nouveautes")
    pg.goto(url + "/")
    pg.locator(".update-banner").get_by_role("button", name="Plus tard").click()
    assert "tu retrouveras la mise à jour" in pg.locator(".flash").inner_text()
    assert pg.locator(".update-banner").count() == 0
    pg.goto(url + "/reglages/a-propos")
    assert pg.locator(".update-card").count() == 1                         # toujours dans « À propos »

    # désactiver / réactiver la vérification automatique
    box = pg.get_by_label("Vérifier automatiquement (une fois par jour)")
    assert box.is_checked()
    box.click()
    pg.wait_for_load_state()
    assert "Vérification automatique désactivée" in pg.locator(".flash").inner_text()
    assert not pg.get_by_label("Vérifier automatiquement (une fois par jour)").is_checked() and not upd.auto
    pg.get_by_label("Vérifier automatiquement (une fois par jour)").click()
    pg.wait_for_load_state()
    assert pg.get_by_label("Vérifier automatiquement (une fois par jour)").is_checked() and upd.auto
    assert pg.errors == []
    pg.context.close()


def test_install_page_shows_progress_and_error(env, browser, monkeypatch):
    url, upd, gh = env
    upd.later("")                                                          # bandeau de nouveau visible
    monkeypatch.setattr(upd, "installable", lambda: (True, ""))

    def fake_install(**kw):
        for step, pct in (("sauvegarde", 0), ("telechargement", 40), ("telechargement", 80)):
            upd.job.update(step=step, percent=pct)
            time.sleep(0.4)
        upd.job.update(step="erreur", error="Le fichier téléchargé est abîmé (son empreinte ne correspond pas). "
                                            "Rien n'a été changé : réessaie plus tard.")
    monkeypatch.setattr(upd, "run_install", fake_install)
    pg = new_page(browser)
    pg.goto(url + "/reglages/a-propos")
    pg.get_by_role("button", name="Mettre à jour maintenant").click()
    assert pg.get_by_role("heading", name="🔄 Mise à jour vers la version 1.1.0").is_visible()
    assert pg.url.endswith("/maj/en-cours")                 # adresse rechargeable (pas celle du formulaire)
    pg.wait_for_selector(".update-steps li.now[data-step='telechargement']")
    pg.wait_for_selector("#maj-err:not(.hidden)", timeout=10000)
    assert "empreinte ne correspond pas" in pg.locator("#maj-err").inner_text()
    assert pg.locator(".update-steps li.done[data-step='sauvegarde']").count() == 1
    assert pg.locator(".update-steps li.failed").count() == 1
    assert pg.locator(".update-banner").count() == 0                       # pas de bandeau pendant l'installation
    shot(pg, "installation-erreur")
    pg.get_by_role("link", name="← Retour").click()
    assert pg.get_by_role("button", name="Mettre à jour maintenant").is_visible()
    assert pg.errors == []
    pg.context.close()


@pytest.mark.parametrize("dark,mobile", [(True, False), (False, True), (True, True)])
def test_looks(env, browser, dark, mobile):
    url, upd, gh = env
    upd.later("")
    pg = new_page(browser, dark=dark, mobile=mobile)
    tag = ("sombre" if dark else "clair") + ("-mobile" if mobile else "")
    pg.goto(url + "/")
    assert pg.locator(".update-banner").is_visible() and no_horizontal_scroll(pg)
    shot(pg, f"bandeau-{tag}")
    bg = pg.evaluate("getComputedStyle(document.querySelector('.update-banner')).backgroundColor")
    assert bg == ("rgb(19, 48, 77)" if dark else "rgb(225, 237, 248)")    # --blue-bg du thème
    pg.goto(url + "/reglages/a-propos")
    assert no_horizontal_scroll(pg)
    shot(pg, f"a-propos-{tag}")
    assert pg.errors == []
    pg.context.close()


def test_reloaded_install_page_never_errors(env, browser):
    """Au redémarrage après la mise à jour, l'onglet se recharge : ni l'ancienne adresse du formulaire
    (/maj/installer, ce que faisait la 1.0.0-beta2) ni la page d'attente ne doivent afficher d'erreur."""
    url, upd, gh = env
    upd.job = None
    pg = new_page(browser)
    for path in ("/maj/installer", "/maj/en-cours"):
        r = pg.goto(url + path)
        assert r.status == 200 and pg.url.endswith("/reglages/a-propos"), path
        assert "Method Not Allowed" not in pg.content()
    assert pg.errors == []
    pg.context.close()
