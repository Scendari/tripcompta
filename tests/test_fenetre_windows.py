"""La vraie fenêtre de Trip Compta sous Windows (Edge WebView2), en mode discret : hors écran, sans bouton dans la barre
des tâches, sans garder le focus de l'utilisateur. Pilotée par CDP (le port de débogage de WebView2), qui ne passe pas
par la souris ni le clavier de Windows. Équivalent Windows de test_fenetre.py::test_real_window_discreet (Mac)."""
import ctypes
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from compta import fenetre

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(sys.platform != "win32" or bool(os.environ.get("CI")) or not fenetre.available(),
                                reason="vraie fenêtre : Windows, hors GitHub")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _windows(pid):
    """Fenêtres « Trip Compta » visibles du processus : (handle, rect, styles étendus, propriétaire)."""
    u32 = ctypes.windll.user32
    out = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(h, _):
        p = ctypes.c_ulong()
        u32.GetWindowThreadProcessId(ctypes.c_void_p(h), ctypes.byref(p))
        b = ctypes.create_unicode_buffer(256)
        u32.GetWindowTextW(ctypes.c_void_p(h), b, 256)
        if p.value == pid and b.value == fenetre.TITLE and u32.IsWindowVisible(ctypes.c_void_p(h)):
            r = (ctypes.c_long * 4)()
            u32.GetWindowRect(ctypes.c_void_p(h), r)
            out.append((h, tuple(r), u32.GetWindowLongW(ctypes.c_void_p(h), -20),
                        u32.GetWindow(ctypes.c_void_p(h), 4)))
        return True
    u32.EnumWindows(proto(cb), 0)
    return out


def _app_pid(launcher_pid):
    """Le venv de Windows lance le vrai python.exe en sous-processus : le processus qui porte les fenêtres."""
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          f"(Get-CimInstance Win32_Process -Filter 'ParentProcessId={launcher_pid}').ProcessId"],
                         capture_output=True, text=True).stdout.split()
    return [int(x) for x in out] + [launcher_pid]


class Premier:
    """Surveille le premier plan toutes les 5 ms : combien de temps une fenêtre de l'app l'a eu."""

    def __init__(self):
        self.pids, self.app_ms, self.stop = set(), 0.0, False
        self.th = threading.Thread(target=self.loop, daemon=True)
        self.th.start()

    def loop(self):
        u32 = ctypes.windll.user32
        while not self.stop:
            p = ctypes.c_ulong()
            u32.GetWindowThreadProcessId(u32.GetForegroundWindow(), ctypes.byref(p))
            if p.value in self.pids:
                self.app_ms += 5
            time.sleep(0.005)


def test_real_window_discreet_windows(tmp_path):
    from playwright.sync_api import sync_playwright
    port, cdp = _free_port(), _free_port()
    dl = tmp_path / "telechargements"
    env = dict(os.environ, TRIPCOMPTA_FENETRE_DISCRETE="1", TRIPCOMPTA_NO_UPDATE_CHECK="1",
               TRIPCOMPTA_CONFIG=str(tmp_path / "reglages.json"), TRIPCOMPTA_TELECHARGEMENTS=str(dl),
               WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=f"--remote-debugging-port={cdp}")
    env.pop("TRIPCOMPTA_DATA_DIR", None)
    watch = Premier()
    p = subprocess.Popen([sys.executable, str(ROOT / "run.py"), "--fenetre", "--demo", "--port", str(port),
                          "--data-dir", str(tmp_path / "donnees")], env=env, cwd=str(ROOT),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        watch.pids.update([p.pid])
        base = f"http://127.0.0.1:{port}/"
        for _ in range(240):                                     # serveur, puis WebView2 et son port CDP
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cdp}/json", timeout=1).read()
                break
            except Exception:
                time.sleep(0.25)
        pids = _app_pid(p.pid)
        watch.pids.update(pids)
        wins = [w for pid in pids for w in _windows(pid)]
        assert len(wins) == 1
        h, rect, ex, owner = wins[0]
        assert rect[2] < 0                                       # entièrement hors écran (à gauche)
        assert ex & 0x80 and not ex & 0x40000                    # WS_EX_TOOLWINDOW : pas de barre des tâches

        with sync_playwright() as pw:
            b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp}")
            # Playwright détourne les téléchargements : on rend la main à WebView2 (donc au code de l'application)
            b.new_browser_cdp_session().send("Browser.setDownloadBehavior", {"behavior": "default"})
            page = b.contexts[0].pages[0]
            page.goto(base)
            page.wait_for_function("() => !!window.pywebview")
            assert page.evaluate("document.visibilityState") == "visible"   # WebView2 dessine
            assert page.screenshot(timeout=10000)

            # Ctrl+F : barre de recherche de l'application
            page.keyboard.press("Control+f")
            page.wait_for_selector("#tc-find", state="visible")
            page.keyboard.press("Escape")

            # Ctrl+N : une 2e fenêtre, elle aussi discrète
            page.keyboard.press("Control+n")
            for _ in range(80):
                if len([w for pid in pids for w in _windows(pid)]) == 2:
                    break
                time.sleep(0.1)
            wins = [w for pid in pids for w in _windows(pid)]
            assert len(wins) == 2 and all(w[1][2] < 0 and w[2] & 0x80 for w in wins)

            # Export : PDF et ZIP directement dans Téléchargements, sans « Enregistrer sous… »
            import json
            tid = json.load(urllib.request.urlopen(base + "api/voyages"))[0]["id"]
            page.goto(base + f"v/{tid}/export")
            page.get_by_text("Télécharger le PDF").click()
            page.get_by_text("Télécharger le ZIP").click()
            for _ in range(100):
                done = [f for f in dl.glob("*") if f.suffix in (".pdf", ".zip")] if dl.exists() else []
                if len(done) == 2:
                    break
                time.sleep(0.1)
            names = sorted(f.name for f in dl.glob("*"))
            assert len(names) == 2 and names[0].startswith("Bilan_") and names[1].startswith("Justificatifs_")
            assert all((dl / n).stat().st_size > 1000 for n in names)
            assert not [w for pid in pids for w in _windows(pid) if w[0] not in {x[0] for x in wins}]  # aucun dialogue

            # Réglages : la carte « Ouvrir dans le navigateur » n'apparaît que dans la fenêtre
            page.goto(base + "reglages")
            assert page.locator("[data-window-only]").is_visible()
            b.close()

        # jamais (durablement) au premier plan : au plus un passage de quelques ms pendant l'ouverture de chaque fenêtre
        assert watch.app_ms <= 100, f"premier plan pris {watch.app_ms} ms"

        # fermer toutes les fenêtres = arrêt propre du programme
        for w in [w for pid in pids for w in _windows(pid)]:
            ctypes.windll.user32.PostMessageW(ctypes.c_void_p(w[0]), 0x10, 0, 0)
        assert p.wait(timeout=20) == 0
        with socket.socket() as s:
            assert s.connect_ex(("127.0.0.1", port)) != 0
    finally:
        watch.stop = True
        if p.poll() is None:
            p.kill()
            p.wait()
