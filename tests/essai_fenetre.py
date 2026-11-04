"""Pilote la VRAIE fenêtre de Trip Compta (pywebview, WebKit) en mode discret (aucune fenêtre visible, pas de
focus pris) : menus, raccourcis, liens, téléchargements, choix de fichier, Dock, fermeture / Cmd+Q.

    python tests/essai_fenetre.py <dossier de travail> <fin : fermer | quitter>

Écrit <dossier>/resultats.json. Lancé par tests/test_fenetre.py (Mac seulement, hors GitHub)."""
import json
import os
import sys
import threading
import time
import types
import webbrowser
from pathlib import Path

WORK = Path(sys.argv[1])
END = sys.argv[2]
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["TRIPCOMPTA_CONFIG"] = str(WORK / "conf.json")
os.environ["TRIPCOMPTA_NO_UPDATE_CHECK"] = "1"
os.environ["TRIPCOMPTA_FENETRE_DISCRETE"] = "1"
os.environ["TRIPCOMPTA_TELECHARGEMENTS"] = str(WORK / "dossier-telechargements")

from compta import fenetre                     # noqa: E402
from compta.demo import seed_demo              # noqa: E402
from compta.web import create_app              # noqa: E402

OUT = WORK / "telechargements"
OUT.mkdir(parents=True, exist_ok=True)
RES = {}


def save():
    (WORK / "resultats.json").write_text(json.dumps(RES, ensure_ascii=False, indent=1), encoding="utf-8")


app = create_app(WORK / "donnees")
tid = seed_demo(app.state.db, replace=True)
port = int(os.environ.get("ESSAI_PORT", "8851"))
app.state.port = port
real_stop = app.state.arreter


def stop_and_note():
    RES["arret_propre"] = True
    save()
    real_stop()


app.state.arreter = stop_and_note

# réponses toutes prêtes aux dialogues macOS (« Enregistrer sous… », choix de fichier)
import AppKit                                  # noqa: E402
from webview.platforms import cocoa            # noqa: E402

saved = []


class FakePanel:
    def __init__(self):
        self.name = "fichier"

    def setTitle_(self, t): pass
    def setDirectoryURL_(self, u): pass
    def setNameFieldStringValue_(self, n): self.name = str(n); saved.append(self.name)
    def runModal(self): return AppKit.NSFileHandlingPanelOKButton
    def filename(self): return str(OUT / self.name)


class AppKitProxy(types.ModuleType):
    def __getattr__(self, k):
        return getattr(AppKit, k)


proxy = AppKitProxy("AppKit")
proxy.NSSavePanel = types.SimpleNamespace(savePanel=FakePanel)
cocoa.AppKit = proxy

opened = []
webbrowser.open = lambda url, *a, **k: opened.append(url) or True
fenetre.webbrowser.open = webbrowser.open
cocoa.webbrowser.open = webbrowser.open

RECEIPT = WORK / "ticket-essai.jpg"
RECEIPT.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 200)


def wait(cond, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


def send_apple_event(event_id: bytes):
    """Envoie à CE programme un Apple Event du système (classe « aevt ») : « rapp » = clic sur l'icône du Dock quand
    l'app tourne, « quit » = Quitter du Dock / Cmd+Q / extinction. À soi-même : aucune autorisation demandée."""
    import struct
    import Foundation
    me = Foundation.NSAppleEventDescriptor.descriptorWithProcessIdentifier_(os.getpid())
    code = lambda b: struct.unpack(">I", b)[0]
    ev = Foundation.NSAppleEventDescriptor.appleEventWithEventClass_eventID_targetDescriptor_returnID_transactionID_(
        code(b"aevt"), code(event_id), me, -1, 0)
    ev.sendEventWithOptions_timeout_error_(1, 5.0, None)          # 1 = kAENoReply


def menu_item(title):
    from PyObjCTools import AppHelper
    done = threading.Event()

    def go():
        m = AppKit.NSApp.mainMenu()
        for i in range(m.numberOfItems()):
            sub = m.itemAtIndex_(i).submenu()
            if sub is None or m.itemAtIndex_(i).title() != "Fichier":
                continue
            items = {str(sub.itemAtIndex_(j).title()): j for j in range(sub.numberOfItems())}
            RES.setdefault("menu_fichier", [[str(sub.itemAtIndex_(j).title()), str(sub.itemAtIndex_(j).keyEquivalent())]
                                            for j in range(sub.numberOfItems())])
            sub.performActionForItemAtIndex_(items[title])
        done.set()
    AppHelper.callAfter(go)
    done.wait(5)


def scenario(f):
    import webview
    from PyObjCTools import AppHelper
    try:
        w = webview.windows[0]
        w.events.loaded.wait(15)
        time.sleep(0.8)
        RES["fenetre_url"] = w.get_current_url()
        RES["visible"] = bool(w.native.isVisible()) if getattr(w, "native", None) else None
        RES["app_au_premier_plan"] = bool(AppKit.NSApp.isActive())

        # Cmd+N (menu Fichier) puis Ctrl/Cmd+N depuis la page (route du serveur, utilisé sous Windows)
        menu_item("Nouvelle fenêtre")
        RES["cmd_n_menu"] = wait(lambda: len(webview.windows) == 2) and len(webview.windows)
        w.evaluate_js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'n', ctrlKey: true}))")
        RES["ctrl_n_page"] = wait(lambda: len(webview.windows) == 3) and len(webview.windows)
        for x in list(webview.windows[1:]):      # on revient à une seule fenêtre (celle où l'on travaille)
            x.destroy()
        RES["une_fenetre_fermee_app_continue"] = wait(lambda: len(webview.windows) == 1) and \
            (w.get_current_url() or "").startswith(f.base)

        # Cmd+F (menu) : barre de recherche, recherche d'un mot de la page
        w.load_url(f.base + f"v/{tid}/depenses")
        wait(lambda: (w.get_current_url() or "").endswith("/depenses"))
        time.sleep(1)
        menu_item("Rechercher dans la page…")
        time.sleep(0.5)
        RES["cmd_f_barre"] = w.evaluate_js("(function(){var b=document.querySelector('.find-bar');"
                                           "return b && !b.hidden && document.activeElement && document.activeElement.id})()")
        RES["cmd_f_trouve"] = w.evaluate_js("(function(){var i=document.getElementById('tc-find');i.value='Gîte';"
                                            "i.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter'}));"
                                            "return String(window.getSelection())})()")
        RES["cmd_f_introuvable"] = w.evaluate_js("(function(){var i=document.getElementById('tc-find');i.value='zzzqqqxxx';"
                                                 "i.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter'}));"
                                                 "return document.querySelector('.find-bar').classList.contains('none')})()")

        # retour arrière
        w.evaluate_js("history.back()")
        RES["retour"] = wait(lambda: (w.get_current_url() or "").endswith(":%d/" % port))

        # lien interne target=_blank -> nouvelle fenêtre de l'app ; lien externe -> navigateur
        n = len(webview.windows)
        w.evaluate_js(f"var a=document.createElement('a');a.href='{f.base}v/{tid}/justificatifs';a.target='_blank';"
                      "document.body.appendChild(a);a.click();")
        RES["lien_interne_nouvelle_fenetre"] = wait(lambda: len(webview.windows) == n + 1) and \
            webview.windows[-1].get_current_url()
        w.evaluate_js("var b=document.createElement('a');b.href='https://github.com/Scendari/tripcompta';"
                      "b.target='_blank';document.body.appendChild(b);b.click();")
        wait(lambda: opened)
        menu_item("Ouvrir dans le navigateur")
        wait(lambda: len(opened) >= 2)
        RES["ouvert_dans_navigateur"] = list(opened)

        # téléchargements : ZIP et bilan PDF (Content-Disposition: attachment) -> « Enregistrer sous… »
        w.load_url(f.base + f"v/{tid}/export")
        wait(lambda: (w.get_current_url() or "").endswith("/export"))
        time.sleep(0.8)
        for href in (f"/v/{tid}/voyage.zip", f"/v/{tid}/export.pdf"):
            w.evaluate_js("var a=document.createElement('a');a.href='%s';document.body.appendChild(a);a.click();" % href)
            wait(lambda: len(list(OUT.iterdir())) >= (1 if href.endswith(".zip") else 2), 10)
        time.sleep(1)
        RES["enregistrer_sous"] = list(saved)
        RES["fichiers_enregistres"] = {p.name: p.stat().st_size for p in OUT.iterdir()}
        RES["page_apres_pdf"] = w.get_current_url()

        # « Envoyer » → E-mail : le PDF va dans Téléchargements (sans dialogue), la messagerie s'ouvre (mailto:)
        n_open = len(opened)
        w.load_url(f.base + f"v/{tid}/envoyer")
        wait(lambda: (w.get_current_url() or "").endswith("/envoyer"))
        time.sleep(0.8)
        RES["mail_lien"] = w.evaluate_js("(function(){var a=document.querySelector('a[href^=mailto][data-send-pdf]');"
                                         "if(!a) return null; a.click(); return a.getAttribute('href').slice(0,7)})()")
        wait(lambda: len(opened) > n_open)
        RES["mail_ouvert"] = [u[:7] for u in opened[n_open:]]
        dl = Path(os.environ["TRIPCOMPTA_TELECHARGEMENTS"])
        wait(lambda: dl.exists() and any(dl.iterdir()), 10)
        RES["mail_pdf_telechargements"] = sorted(p.name for p in dl.iterdir()) if dl.exists() else []
        RES["mail_message"] = w.evaluate_js("(function(){var t=document.querySelector('.toast');return t&&!t.hidden?t.textContent:null})()")
        RES["mail_page_inchangee"] = (w.get_current_url() or "").endswith("/envoyer")
        RES["enregistrer_sous_apres_mail"] = list(saved)

        # choix d'un fichier (justificatif) : le dialogue répond avec un ticket
        for inst in cocoa.BrowserView.instances.values():
            inst.create_file_dialog = lambda *a, **k: [str(RECEIPT)]
        w.load_url(f.base + f"v/{tid}/justificatifs")
        wait(lambda: (w.get_current_url() or "").endswith("/justificatifs"))
        time.sleep(0.8)
        w.evaluate_js("document.querySelector('input[type=file]').click()")
        time.sleep(1.5)
        RES["fichier_choisi"] = w.evaluate_js("Array.from(document.querySelector('input[type=file]').files)"
                                              ".map(function(x){return x.name+' '+x.size})")

        # clic sur l'icône du Dock alors que l'app tourne (macOS envoie « rouvrir »)
        done = threading.Event()

        def reopen():
            d = AppKit.NSApp.delegate()
            RES["dock_reponse"] = bool(d.applicationShouldHandleReopen_hasVisibleWindows_(AppKit.NSApp, False))
            done.set()
        AppHelper.callAfter(reopen)
        done.wait(5)
        RES["dock_fenetre_ramenee"] = getattr(f, "shown", 0)
        # le VRAI message que macOS envoie quand on clique sur l'icône du Dock (Apple Event « rouvrir »)
        before = getattr(f, "shown", 0)
        send_apple_event(b"rapp")
        RES["dock_apple_event_rouvrir"] = wait(lambda: getattr(f, "shown", 0) == before + 1)

        # Windows : un 2e lancement demande à celui-ci de montrer sa fenêtre
        import urllib.request
        with urllib.request.urlopen(urllib.request.Request(f.base + "fenetre/montrer", data=b"", method="POST"),
                                    timeout=3) as r:
            RES["deuxieme_lancement"] = r.status
        RES["dock_fenetre_ramenee_2"] = getattr(f, "shown", 0)
        RES["toujours_invisible"] = not any(x.native.isVisible() for x in webview.windows if getattr(x, "native", None))
    except Exception as e:                       # noqa: BLE001
        import traceback
        RES["erreur"] = traceback.format_exc()
    RES["fin"] = END
    save()
    if END == "quitter":                         # « Quitter » du Dock / Cmd+Q / extinction : Apple Event « quitter »
        from PyObjCTools import AppHelper
        AppHelper.callAfter(send_apple_event, b"quit")     # depuis la boucle principale, comme le Dock
    else:                                        # fermer toutes les fenêtres
        for x in list(webview.windows):
            x.destroy()


fenetre.run(app, "127.0.0.1", port, on_start=lambda f: threading.Thread(target=scenario, args=(f,), daemon=True).start())
RES["programme_rend_la_main"] = True
save()
