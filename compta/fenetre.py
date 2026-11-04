"""Trip Compta dans sa propre fenêtre (Mac, Windows) au lieu d'un onglet du navigateur.

Le serveur tourne dans un fil à part ; la fenêtre affiche les mêmes pages avec le moteur web du système (WebKit sur
Mac, Edge WebView2 sous Windows). Fermer la dernière fenêtre, Cmd+Q, « Quitter » du Dock ou l'extinction de
l'ordinateur arrêtent Trip Compta proprement (enregistre et rend la main aux autres ordinateurs, comme « ⏻ Arrêter »).

Le navigateur reste le mode de secours : Linux, lancement depuis le code source, option --navigateur.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
import webbrowser

TITLE = "Trip Compta"
# essais automatiques : fenêtres invisibles, sans icône dans le Dock, sans prendre le focus de l'utilisateur
DISCRETE = bool(os.environ.get("TRIPCOMPTA_FENETRE_DISCRETE"))


def available() -> bool:
    """La fenêtre est possible ici (pywebview présent, Mac ou Windows)."""
    if sys.platform not in ("darwin", "win32"):
        return False
    try:
        import webview  # noqa: F401
    except Exception:
        return False
    return True


def wanted(frozen: bool, navigateur: bool, forcer: bool) -> bool:
    """Mode fenêtre par défaut pour l'application fabriquée (Mac, Windows) ; navigateur depuis le code source, sous
    Linux, ou avec --navigateur. --fenetre force la fenêtre (essais depuis le code source)."""
    if navigateur:
        return False
    return (forcer or frozen) and available()


def _ready(url: str, timeout: float = 30) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url + "ping", timeout=1) as r:
                if json.load(r).get("app") == "tripcompta":
                    return True
        except Exception:
            time.sleep(0.1)
    return False


class Fenetres:
    """Les fenêtres ouvertes de Trip Compta (une ou plusieurs, comme des onglets)."""

    def __init__(self, base: str):
        self.base = base                       # http://127.0.0.1:8765/

    def is_ours(self, url: str) -> bool:
        return str(url).startswith(self.base)

    def new(self, url: str | None = None):
        import webview
        n = len(webview.windows)
        return webview.create_window(TITLE, url or self.base, width=1280, height=860, min_size=(820, 560),
                                     x=None if not n else 80 + 30 * n, y=None if not n else 60 + 30 * n,
                                     text_select=True, zoomable=True, hidden=DISCRETE, focus=not DISCRETE)

    def new_later(self, url: str | None = None):
        """Nouvelle fenêtre sans bloquer l'appelant (route du serveur, menu)."""
        threading.Thread(target=self.new, args=(url,), daemon=True).start()

    def show(self):
        """Trip Compta relancé alors qu'il tourne : remettre sa fenêtre devant, ou en rouvrir une."""
        import webview
        if not webview.windows:
            self.new_later()
            return
        if DISCRETE:                           # essais : on note seulement la demande
            self.shown = getattr(self, "shown", 0) + 1
            print(f"fenêtre ramenée ({self.shown})", flush=True)
            return
        w = webview.windows[-1]
        w.restore()
        w.show()

    def current(self):
        import webview
        return webview.active_window() or (webview.windows[-1] if webview.windows else None)

    def open_in_browser(self):
        w = self.current()
        cur = w.get_current_url() if w else None
        webbrowser.open(cur if cur and self.is_ours(cur) else self.base)

    def find(self):
        w = self.current()
        if w:
            w.evaluate_js("window.tcFind && window.tcFind()")


def downloads_dir():
    from pathlib import Path
    d = os.environ.get("TRIPCOMPTA_TELECHARGEMENTS")       # essais : un dossier à part
    return Path(d) if d else Path.home() / "Downloads"


def save_to_downloads(url: str):
    """Enregistre un fichier de l'application dans Téléchargements (nom donné par le serveur, sans écraser)."""
    import re
    from pathlib import Path
    from urllib.parse import unquote
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            data = r.read()
            disp = r.headers.get("Content-Disposition") or ""
        m = re.search(r"filename\*=UTF-8''([^;]+)", disp) or re.search(r'filename="?([^";]+)"?', disp)
        name = Path(unquote(m.group(1)) if m else url.rstrip("/").rsplit("/", 1)[-1] or "fichier").name
        folder = downloads_dir()
        folder.mkdir(parents=True, exist_ok=True)
        target, stem, ext, i = folder / name, Path(name).stem, Path(name).suffix, 2
        while target.exists():
            target, i = folder / f"{stem} ({i}){ext}", i + 1
        target.write_bytes(data)
        return target
    except Exception:
        return None


def _menu(f: Fenetres):
    from webview.menu import Menu, MenuAction, MenuSeparator
    return [Menu("Fichier", [MenuAction("Nouvelle fenêtre", f.new_later),
                             MenuAction("Rechercher dans la page…", f.find),
                             MenuSeparator(),
                             MenuAction("Ouvrir dans le navigateur", f.open_in_browser)])]


def _mac_hooks(f: Fenetres, arreter):
    """Mac : clic sur l'icône du Dock quand l'app tourne ; Cmd+Q / « Quitter » / extinction = arrêt propre ; liens
    target=_blank de l'app dans une nouvelle fenêtre (les autres dans le navigateur) ; fichiers « à enregistrer »."""
    import objc
    import WebKit
    from webview.platforms import cocoa

    def applicationShouldHandleReopen_hasVisibleWindows_(self, app, visible):
        if not visible:
            f.show()
        return True

    def applicationWillTerminate_(self, notification):
        arreter()
        if DISCRETE:
            print("arrêt propre (Quitter)", flush=True)

    objc.classAddMethods(cocoa.BrowserView.AppDelegate, [
        objc.selector(applicationShouldHandleReopen_hasVisibleWindows_,
                      selector=b"applicationShouldHandleReopen:hasVisibleWindows:", signature=b"Z@:@Z"),
        objc.selector(applicationWillTerminate_, selector=b"applicationWillTerminate:", signature=b"v@:@")])

    def createWebView(self, webview_, config, action, features):
        url = str(action.request().URL().absoluteString())
        if f.is_ours(url):
            f.new_later(url)
        else:
            webbrowser.open(url)
        return None

    # fichier envoyé « à enregistrer » (Content-Disposition: attachment, ex. le bilan PDF) : « Enregistrer sous… »,
    # même si WebKit sait l'afficher (sinon le PDF remplacerait l'application dans la fenêtre)
    orig = cocoa.BrowserView.BrowserDelegate.webView_decidePolicyForNavigationResponse_decisionHandler_
    orig = getattr(orig, "callable", orig)

    def decide(self, webview_, nav_response, handler):
        resp = nav_response.response()
        headers = dict(resp.allHeaderFields()) if hasattr(resp, "allHeaderFields") else {}
        disp = next((str(v) for k, v in headers.items() if str(k).lower() == "content-disposition"), "")
        if disp.lower().startswith("attachment"):
            handler(getattr(WebKit, "WKNavigationResponsePolicyDownload", 2))
            return
        return orig(self, webview_, nav_response, handler)

    def became_download(self, webview_, nav_response, download):
        download.setDelegate_(cocoa.BrowserView.DownloadDelegate.alloc().init().retain())

    # liens vers une autre application (mailto:, tel:, whatsapp:…) : le moteur web de la fenêtre ne sait pas les ouvrir
    # (dans un navigateur, c'est lui qui passe la main) -> on les confie au système (messagerie…).
    # Téléchargement d'un lien « download » (PDF à joindre à un mail ou à WhatsApp) : directement dans Téléchargements,
    # sans « Enregistrer sous… », comme un navigateur.
    orig_action = cocoa.BrowserView.BrowserDelegate.webView_decidePolicyForNavigationAction_decisionHandler_
    orig_action = getattr(orig_action, "callable", orig_action)

    def decide_action(self, webview_, action, handler):
        url = str(action.request().URL().absoluteString())
        scheme = url.split(":", 1)[0].lower()
        if scheme not in ("http", "https", "about", "data", "blob", "file"):
            webbrowser.open(url)
            handler(getattr(WebKit, "WKNavigationActionPolicyCancel", 0))
            return
        if action.shouldPerformDownload() and f.is_ours(url):
            threading.Thread(target=save_to_downloads, args=(url,), daemon=True).start()
            handler(getattr(WebKit, "WKNavigationActionPolicyCancel", 0))
            return
        return orig_action(self, webview_, action, handler)

    objc.classAddMethods(cocoa.BrowserView.BrowserDelegate, [
        objc.selector(decide_action, selector=b"webView:decidePolicyForNavigationAction:decisionHandler:",
                      signature=b"v@:@@@?"),
        objc.selector(createWebView, selector=b"webView:createWebViewWithConfiguration:forNavigationAction:"
                                              b"windowFeatures:", signature=b"@@:@@@@"),
        objc.selector(decide, selector=b"webView:decidePolicyForNavigationResponse:decisionHandler:",
                      signature=b"v@:@@@?"),
        objc.selector(became_download, selector=b"webView:navigationResponse:didBecomeDownload:",
                      signature=b"v@:@@@")])


def _mac_shortcuts():
    """Raccourcis du menu Fichier (pywebview n'en pose pas) : Cmd+N, Cmd+F."""
    import AppKit
    from PyObjCTools import AppHelper

    def add():
        main = AppKit.NSApp.mainMenu()
        if main is None:
            return
        for i in range(main.numberOfItems()):
            sub = main.itemAtIndex_(i).submenu()
            if sub is None or main.itemAtIndex_(i).title() != "Fichier":
                continue
            for j in range(sub.numberOfItems()):
                it = sub.itemAtIndex_(j)
                key = {"Nouvelle fenêtre": "n", "Rechercher dans la page…": "f"}.get(str(it.title()))
                if key:
                    it.setKeyEquivalent_(key)
    AppHelper.callAfter(add)


def run(app, host: str, port: int, log_level: str = "warning", on_start=None) -> None:
    """Démarre le serveur puis la fenêtre ; rend la main quand la dernière fenêtre est fermée (après l'arrêt
    propre). Sur Mac, Cmd+Q passe par applicationWillTerminate (arrêt propre aussi)."""
    import uvicorn
    import webview

    base = f"http://{host}:{port}/"
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level=log_level))
    th = threading.Thread(target=server.run, daemon=True, name="serveur")
    th.start()
    _ready(base)
    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    f = Fenetres(base)
    app.state.fenetres = f
    arreter = getattr(app.state, "arreter", None) or (lambda: None)
    if sys.platform == "darwin":
        _mac_hooks(f, arreter)
        if DISCRETE:
            from webview.platforms import cocoa
            cocoa.BrowserView.app.setActivationPolicy_(2)     # « prohibited » : jamais au premier plan
    f.new()

    def after_start():
        if sys.platform == "darwin":
            _mac_shortcuts()
        if on_start:                           # tests : piloter la vraie fenêtre
            on_start(f)
    webview.start(after_start, private_mode=False, menu=_menu(f))
    arreter()                                  # dernière fenêtre fermée : enregistre et rend la main
    server.should_exit = True
    th.join(timeout=5)
