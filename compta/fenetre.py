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
OFFSCREEN_X = -20000                           # Windows, essais : bien à gauche de tous les écrans


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

    def __init__(self, base: str, app=None):
        self.base = base                       # http://127.0.0.1:8765/
        self.app = app
        self._watched = False

    def is_ours(self, url: str) -> bool:
        return str(url).startswith(self.base)

    def new(self, url: str | None = None, html: str | None = None):
        import webview
        n = len(webview.windows)
        x, y = (None, None) if not n else (80 + 30 * n, 60 + 30 * n)
        hidden = DISCRETE
        if DISCRETE and sys.platform == "win32":
            # Windows : une fenêtre masquée ne dessine pas (WebView2 en pause) -> visible mais HORS de l'écran,
            # sans activation ni bouton dans la barre des tâches (voir _win_discreet)
            x, y, hidden = OFFSCREEN_X - 40 * n, 100, False
        w = webview.create_window(TITLE, None if html else (url or self.base), html=html, width=1280, height=860,
                                  min_size=(820, 560), x=x, y=y, text_select=True, zoomable=True, hidden=hidden,
                                  focus=not DISCRETE)
        if sys.platform == "win32" and not DISCRETE:
            w.events.shown += lambda: _win_front()     # sinon elle s'ouvre souvent derrière l'Explorateur
        if not self._watched:
            self._watched = True
            w.events.loaded += lambda: self._first_load(w)
        return w

    def _first_load(self, w):
        """La 1re page vraiment affichée par la fenêtre (titre, moteur), donnée par /ping : l'essai de l'application
        fabriquée (build_tools/smoke.py --fenetre) vérifie ainsi la vraie fenêtre, WebView2 compris. Avec
        TRIPCOMPTA_ESSAI_FENETRE (essais seulement) : la page crée aussi un voyage, comme un clic (contrôle d'origine)."""
        try:
            if not self.is_ours(w.get_current_url() or ""):     # « Chargement… » (run_early) : pas encore l'app
                return
            info = w.evaluate_js("document.title + '\\n' + navigator.userAgent") or ""
            title, _, ua = str(info).partition("\n")
            engine = "WebView2" if "Edg/" in ua else "WebKit" if "AppleWebKit" in ua else ua[:60]
            if self.app is not None:
                self.app.state.fenetre_vue = {"titre": title, "moteur": engine}
            if os.environ.get("TRIPCOMPTA_ESSAI_FENETRE"):
                w.evaluate_js("fetch('/api/voyages', {method: 'POST', headers: {'Content-Type': 'application/json'},"
                              " body: JSON.stringify({nom: 'Essai de la fenêtre'})}); 1")
        except Exception:
            pass

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
        if sys.platform == "win32":
            _win_front()

    def current(self):
        import webview
        return webview.active_window() or (webview.windows[-1] if webview.windows else None)

    def pick_folder(self):
        """Choix d'un dossier devant la fenêtre (Windows : le sélecteur moderne de l'Explorateur). None si annulé."""
        import webview
        from pathlib import Path
        w = self.current()
        if w is None:
            return None
        r = w.create_file_dialog(webview.FileDialog.FOLDER, directory=str(Path.home()))
        return str(r[0]).rstrip("/\\") if r else None

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
    from webview.menu import Menu, MenuAction
    return [Menu("Fichier", [MenuAction("Nouvelle fenêtre", f.new_later),
                             MenuAction("Rechercher dans la page…", f.find)])]


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


def _win_hooks(f: Fenetres):
    """Windows (Edge WebView2) : un fichier de l'application (PDF, ZIP) va directement dans Téléchargements, comme
    dans Edge (pywebview demanderait « Enregistrer sous… » à chaque fois) ; la bulle « Téléchargements » de WebView2
    confirme et permet de l'ouvrir. Les liens target=_blank de l'application s'ouvrent dans une nouvelle fenêtre (les
    autres dans le navigateur)."""
    from webview.platforms import edgechromium as ec
    orig_new = ec.EdgeChrome.on_new_window_request

    def on_download_starting(self, sender, args):
        from pathlib import Path
        from webview import settings
        if not settings["ALLOW_DOWNLOADS"]:
            args.Cancel = True
            return
        # WebView2 a déjà choisi Téléchargements\<nom donné par le serveur>, sans écraser (« nom (1).pdf »)
        d = os.environ.get("TRIPCOMPTA_TELECHARGEMENTS")         # essais : un dossier à part
        if d:
            import re
            folder = Path(d)
            folder.mkdir(parents=True, exist_ok=True)
            name = re.sub(r" \(\d+\)(?=\.[^.]*$|$)", "", Path(str(args.ResultFilePath)).name) or "fichier"
            target, stem, ext, i = folder / name, Path(name).stem, Path(name).suffix, 2
            while target.exists():
                target, i = folder / f"{stem} ({i}){ext}", i + 1
            args.ResultFilePath = str(target)

    def on_new_window_request(self, sender, args):
        url = str(args.get_Uri())
        if f.is_ours(url):
            args.set_Handled(True)
            f.new_later(url)
            return
        return orig_new(self, sender, args)

    ec.EdgeChrome.on_download_starting = on_download_starting
    ec.EdgeChrome.on_new_window_request = on_new_window_request


def _win_front() -> bool:
    """Windows : mettre la fenêtre de Trip Compta au premier plan. Windows le refuse souvent à une application qui a
    mis quelques secondes à démarrer (elle restait derrière l'Explorateur) ; on s'attache un instant à la file
    d'entrée de la fenêtre active, ce qui l'autorise. Renvoie True si la fenêtre est bien devant."""
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.WinDLL("user32")             # instance à part : ses argtypes ne touchent pas ctypes.windll
    u32.GetForegroundWindow.restype = wintypes.HWND
    u32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    u32.GetWindowThreadProcessId.restype = wintypes.DWORD
    for name in ("IsWindowVisible", "IsIconic", "GetWindowTextLengthW", "BringWindowToTop", "SetForegroundWindow"):
        getattr(u32, name).argtypes = (wintypes.HWND,)
    u32.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
    u32.GetWindow.restype = wintypes.HWND
    u32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    u32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
    pid, found = os.getpid(), []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(h, _):
        p = wintypes.DWORD()
        u32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid and u32.IsWindowVisible(h) and u32.GetWindowTextLengthW(h) and not u32.GetWindow(h, 4):
            found.append(h)                    # fenêtre principale visible de CE processus (GW_OWNER = 4)
        return True
    u32.EnumWindows(each, 0)
    if not found:
        return False
    h = found[0]                               # la plus haute des nôtres (EnumWindows suit l'ordre d'empilement)
    if u32.IsIconic(h):
        u32.ShowWindow(h, 9)                   # SW_RESTORE
    fg = u32.GetForegroundWindow()
    mine = u32.GetWindowThreadProcessId(h, None)
    other = u32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = bool(other and other != mine and u32.AttachThreadInput(other, mine, True))
    try:
        u32.BringWindowToTop(h)
        u32.SetForegroundWindow(h)
    finally:
        if attached:
            u32.AttachThreadInput(other, mine, False)
    return u32.GetForegroundWindow() == h


def _win_discreet():
    """Windows, essais automatiques : fenêtres hors écran, sans bouton dans la barre des tâches, qui ne gardent pas le
    premier plan (Form.Show() active la fenêtre : ShowWithoutActivation, TopMost sans activation ; s'il reste un
    passage au premier plan pendant Show() — ~10 ms, cause non trouvée — il est rendu aussitôt)."""
    # Surcharger une propriété .NET demande une classe pythonnet « __namespace__ » ; or une telle classe dérivée de
    # BrowserForm (classe Python) perd ses méthodes (sa MRO saute BrowserForm). On la dérive donc de Form et on y
    # recopie les méthodes de BrowserForm (seul __init__ appelle super() : on le rattache à la nouvelle classe).
    import ctypes
    import types
    import clr
    from webview.platforms import winforms as wf
    base = wf.BrowserView.BrowserForm
    if getattr(base, "tc_discrete", False):    # déjà fait (2e lancement dans le même processus)
        return

    init = base.__init__
    cls_cell = types.CellType()                # rempli plus bas avec la nouvelle classe (pour super())
    cells = tuple(cls_cell if name == "__class__" else cell
                  for name, cell in zip(init.__code__.co_freevars, init.__closure__ or ()))
    base_init = types.FunctionType(init.__code__, init.__globals__, init.__name__, init.__defaults__, cells)

    # pywebview fait « self.TopMost = window.on_top » dès que le handle existe : le TopMost de WinForms appelle alors
    # SetWindowPos SANS SWP_NOACTIVATE, ce qui met la fenêtre (même pas encore affichée) au premier plan.
    def _get_top(self):
        return bool(ctypes.windll.user32.GetWindowLongW(self.Handle.ToInt64(), -20) & 0x8)   # WS_EX_TOPMOST

    def _set_top(self, value):
        ctypes.windll.user32.SetWindowPos(self.Handle.ToInt64(), -1 if value else -2, 0, 0, 0, 0,
                                          0x0001 | 0x0002 | 0x0010)                         # NOSIZE|NOMOVE|NOACTIVATE

    class FenetreDiscrete(wf.WinForms.Form):
        __namespace__ = "TripCompta.Essais"
        tc_discrete = True

        def __init__(self, window, cache_dir):
            self.tc_avant = ctypes.windll.user32.GetForegroundWindow()   # à qui rendre le premier plan
            base_init(self, window, cache_dir)
            # pas de bouton dans la barre des tâches : WS_EX_TOOLWINDOW (et sans WS_EX_APPWINDOW), avant l'affichage
            h = self.Handle.ToInt64()
            ex = ctypes.windll.user32.GetWindowLongW(h, -20)
            ctypes.windll.user32.SetWindowLongW(h, -20, (ex | 0x80 | 0x08000000) & ~0x40000)

        def _sans_activation(self):
            return True
        ShowWithoutActivation = clr.clrproperty(bool, _sans_activation)
        TopMost = clr.clrproperty(bool, _get_top, _set_top)

    def on_shown(self, *_):                    # sans webview.Focus() : SetFocus activerait la fenêtre
        # malgré tout, la 1re fenêtre de pywebview passe parfois au premier plan pendant Show() : on le rend aussitôt
        # (pendant Show, ce processus a encore le droit de choisir le premier plan)
        u32 = ctypes.windll.user32
        if self.tc_avant and u32.GetForegroundWindow() == self.Handle.ToInt64():
            u32.SetForegroundWindow(self.tc_avant)
        self.shown.set()

    cls_cell.cell_contents = FenetreDiscrete
    for k, v in vars(base).items():
        if k not in {"__dict__", "__weakref__", "__module__", "__qualname__", "__doc__", "__init__", "__str__"}:
            setattr(FenetreDiscrete, k, v)
    FenetreDiscrete.on_shown = on_shown
    wf.BrowserView.BrowserForm = FenetreDiscrete


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


def edge_path(reg=None, exists=os.path.exists, env=None) -> str | None:
    """Windows : msedge.exe (inscrit dans « App Paths », sinon les dossiers habituels). None s'il est absent (Edge
    est désinstallable dans l'Union européenne). reg, exists, env : pour les essais."""
    env = os.environ if env is None else env
    found = []
    if reg is None:
        def reg(root_name, key):
            import winreg
            root = getattr(winreg, root_name)
            with winreg.OpenKey(root, key) as k:
                return winreg.QueryValue(k, None)
    for root in ("HKEY_CURRENT_USER", "HKEY_LOCAL_MACHINE"):
        try:
            found.append(reg(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"))
        except Exception:
            pass
    for var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        if env.get(var):
            found.append(os.path.join(env[var], "Microsoft", "Edge", "Application", "msedge.exe"))
    for p in found:
        p = (p or "").strip().strip('"')
        if p and exists(p):
            return p
    return None


def open_app_window(url: str) -> str:
    """Repli quand la vraie fenêtre est impossible : Windows → Edge « en mode application » (une fenêtre sans barre
    d'adresse ni onglets, presque comme la vraie) ; sinon, ou sans Edge → le navigateur par défaut. Renvoie
    « edge » ou « navigateur »."""
    if DISCRETE:                               # essais : rien ne doit s'ouvrir sur l'écran de l'utilisateur
        print(f"(essai) aurait ouvert : {url}", flush=True)
        return "navigateur"
    if sys.platform == "win32":
        edge = edge_path()
        if edge:
            import subprocess
            try:
                subprocess.Popen([edge, f"--app={url}"], close_fds=True,
                                 creationflags=0x00000008 | 0x00000200)   # DETACHED_PROCESS | NEW_PROCESS_GROUP
                return "edge"
            except OSError:
                pass
        try:
            os.startfile(url)                    # le navigateur par défaut, sans passer par une console
            return "navigateur"
        except OSError:
            pass
    webbrowser.open(url)
    return "navigateur"


def _browser_instead(app, base: str, th: threading.Thread, why: str) -> None:
    """La fenêtre ne peut pas s'ouvrir sur ce PC : Trip Compta marche quand même, dans le navigateur. Cause la plus
    fréquente sous Windows (avant l'installeur) : un zip téléchargé non « débloqué », et .NET refuse alors de
    charger Python.Runtime.dll ; la page d'accueil explique comment faire (app.state.fenetre_impossible)."""
    import traceback
    print(f"fenêtre impossible ({why}) : ouverture dans le navigateur", flush=True)
    traceback.print_exc()
    app.state.fenetre_impossible = why
    app.state.fenetres = None
    if not DISCRETE:
        app.state.fenetre_repli = open_app_window(base)
    th.join()                                  # « Arrêter » termine le processus


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
    f = Fenetres(base, app)
    app.state.fenetres = f
    arreter = getattr(app.state, "arreter", None) or (lambda: None)
    if sys.platform == "darwin":
        _mac_hooks(f, arreter)
        if DISCRETE:
            from webview.platforms import cocoa
            cocoa.BrowserView.app.setActivationPolicy_(2)     # « prohibited » : jamais au premier plan
    elif sys.platform == "win32":
        try:
            _win_hooks(f)                      # charge .NET (pythonnet) : peut échouer sur certains PC
        except Exception:
            return _browser_instead(app, base, th, "dotnet")
        if DISCRETE:
            _win_discreet()
    f.new()

    started = []

    def after_start():
        started.append(True)
        if sys.platform == "darwin":
            _mac_shortcuts()
        if on_start:                           # tests : piloter la vraie fenêtre
            on_start(f)
    # Mac : menu Fichier dans la barre du haut de l'écran. Windows : pywebview le mettrait DANS la fenêtre, sous le
    # titre (ça fait étranger) ; Ctrl+N / Ctrl+F marchent sans lui (app.js).
    # Windows, essais : un dossier WebView2 à part. Deux WebView2 qui partagent le leur partagent aussi le navigateur
    # (le 2e ignore alors son port de débogage) : l'essai rejoindrait le Trip Compta ouvert par l'utilisateur.
    storage = None
    if DISCRETE and sys.platform == "win32":
        import tempfile
        storage = tempfile.mkdtemp(prefix="tripcompta-webview2-")
    try:
        webview.start(after_start, private_mode=False, storage_path=storage,
                      menu=[] if sys.platform == "win32" else _menu(f))
    except Exception:
        if started:
            raise
        return _browser_instead(app, base, th, "webview")
    arreter()                                  # dernière fenêtre fermée : enregistre et rend la main
    server.should_exit = True
    th.join(timeout=5)
    if sys.platform == "win32":                # tout est enregistré : ne pas attendre la fin de .NET / WebView2 (~5 s)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)


LOADING_HTML = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>Trip Compta</title><style>
:root { --bg: #faf9f5; --text: #22201b; --muted: #6b665c; --teal: #1f8a7a; }
@media (prefers-color-scheme: dark) { :root { --bg: #07111f; --text: #e8f4f8; --muted: #8ab0c8; --teal: #1fada8; } }
html, body { height: 100%; margin: 0; }
body { background: var(--bg); color: var(--text); display: flex; align-items: center; justify-content: center;
       font: 16px/1.4 "Segoe UI", system-ui, sans-serif; cursor: progress; }
.box { text-align: center; }
h1 { margin: 0 0 1.2rem; font-size: 1.6rem; font-weight: 600; }
.dots { display: inline-flex; gap: .5rem; }
.dots i { width: .6rem; height: .6rem; border-radius: 50%; background: var(--teal); animation: b 1s infinite ease-in-out; }
.dots i:nth-child(2) { animation-delay: .15s; } .dots i:nth-child(3) { animation-delay: .3s; }
p { color: var(--muted); margin: 1rem 0 0; }
@keyframes b { 0%, 80%, 100% { opacity: .25; transform: scale(.8); } 40% { opacity: 1; transform: scale(1); } }
</style></head><body><div class="box"><h1>Trip Compta</h1><div class="dots"><i></i><i></i><i></i></div>
<p>Chargement…</p></div></body></html>"""

FAILED_HTML = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>Trip Compta</title></head>
<body style="font: 16px/1.5 'Segoe UI', sans-serif; max-width: 40rem; margin: 4rem auto; padding: 0 1rem">
<h1>Trip Compta n'a pas pu démarrer</h1><p>Le détail est dans le fichier tripcompta.log, à côté des réglages :</p>
<pre style="white-space: pre-wrap; font-size: 13px">{}</pre></body></html>"""


def run_early(prepare, host: str, port: int, log_level: str = "warning") -> None:
    """Windows : la fenêtre s'ouvre tout de suite sur « Chargement… » ; prepare() (bibliothèques, base, création de
    l'app) et le serveur démarrent pendant ce temps, puis la fenêtre va sur l'app. WebView2 (un autre processus) se
    lance ainsi en même temps que Python, au lieu d'après (~1 s de moins). Même suite que run() ensuite."""
    import html as _html
    import traceback
    import webview

    base = f"http://{host}:{port}/"
    box, ready = {}, threading.Event()
    f = Fenetres(base, None)

    def boot():
        try:
            import uvicorn
            app = prepare()
            # avant que le serveur réponde : un 2e lancement qui le trouve doit pouvoir ramener CETTE fenêtre
            # (sinon il ouvrirait Edge à côté)
            f.app = app
            app.state.fenetres = f
            server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level=log_level))
            th = threading.Thread(target=server.run, daemon=True, name="serveur")
            th.start()
            box.update(app=app, server=server, th=th)
            _ready(base)
        except Exception:
            traceback.print_exc()
            sys.stdout.flush()
            box["error"] = traceback.format_exc()
        finally:
            ready.set()
    threading.Thread(target=boot, daemon=True, name="demarrage").start()

    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True

    def fallback(why):
        ready.wait()
        if "app" not in box:
            raise SystemExit(1)
        return _browser_instead(box["app"], base, box["th"], why)
    try:
        _win_hooks(f)                          # charge .NET (pythonnet) : peut échouer sur certains PC
    except Exception:
        return fallback("dotnet")
    if DISCRETE:
        _win_discreet()
    w = f.new(html=LOADING_HTML)

    started = []

    def after_start():
        started.append(True)
        ready.wait()
        if "error" in box:
            w.load_html(FAILED_HTML.format(_html.escape(box["error"])))
            return
        w.load_url(base)
    storage = None
    if DISCRETE:                               # essais : un dossier WebView2 à part (voir run())
        import tempfile
        storage = tempfile.mkdtemp(prefix="tripcompta-webview2-")
    try:
        webview.start(after_start, private_mode=False, storage_path=storage, menu=[])
    except Exception:
        if started:
            raise
        return fallback("webview")
    ready.wait(timeout=30)                     # fermée pendant « Chargement… » : laisser finir l'ouverture de la base
    if "app" in box:
        (getattr(box["app"].state, "arreter", None) or (lambda: None))()   # enregistre et rend la main
        box["server"].should_exit = True
        box["th"].join(timeout=5)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
