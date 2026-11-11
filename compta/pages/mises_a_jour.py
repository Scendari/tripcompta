"""« À propos », fichier de diagnostic, mises à jour, fenêtres, arrêt de l'application.

Partie de l'application web (voir compta/web.py, qui installe ces pages dans l'ordre).
"""
from __future__ import annotations

import datetime as dt
import os

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import diagnostic as DIAG
from .. import journal
from ..web import _diagnostic_response


def install(site):
    """Installe ces pages dans l'application. `site` : ce que les pages ont en commun (voir web.py)."""
    U = site.U
    _return_path = site._return_path
    app = site.app
    back = site.back
    data_source = site.data_source
    db = site.db
    refuse = site.refuse
    render = site.render
    upd = site.upd

    # ------------------------------------------------------------------ à propos, mises à jour
    def _about(request, **ctx):
        latest = upd.latest()
        checked = upd.checked_at()
        ok, why = upd.installable()
        return render(request, "about.html", info=upd, latest=latest, available=upd.available(),
                      checked=dt.datetime.fromtimestamp(checked).strftime("%d/%m/%Y à %H:%M") if checked else None,
                      installable=ok, not_installable=why, page=(latest or {}).get("page") or U.RELEASES_PAGE,
                      help=U.INSTALL_HELP, update_banner=None, **ctx)

    @app.get("/reglages/a-propos", response_class=HTMLResponse)
    def about_page(request: Request):
        return _about(request)

    @app.get("/reglages/diagnostic.txt")
    def diagnostic_file():
        """« Exporter un diagnostic » : état de l'application et des fichiers, sans nom ni montant."""
        locked = bool(site.vault_box is not None and site.vault_box.locked)
        extra = [f"vérification automatique des mises à jour : {upd.auto_state()}",
                 f"mise à jour disponible : {(upd.available() or {}).get('version') or 'non'}",
                 "sauvegarde du jour : " + ("ÉCHEC le " + app.state.backup_failed if app.state.backup_failed else "faite")]
        if app.state.misplaced:
            extra.append(f"application ouverte hors du dossier Applications : {app.state.misplaced}")
        journal.log.info("diagnostic exporté")
        return _diagnostic_response(DIAG.build(version=upd.label, folder=db.home, db=db, source=data_source,
                                               protected=site.vault_box is not None, locked=locked, extra=extra))

    @app.post("/reglages/verifier-maj")
    def check_now(request: Request):
        res = upd.check(force=True)
        if not res["ok"]:
            return refuse("/reglages/a-propos", res["error"])
        a = upd.available()
        if a:
            return back("/reglages/a-propos", f"Une nouvelle version est disponible : {a['version']}.")
        if upd.dev:
            v = (res["latest"] or {}).get("version")
            return back("/reglages/a-propos", f"Dernière version publiée : {v}." if v else
                        "Aucune version publiée pour l'instant.")
        return back("/reglages/a-propos", "Tu as déjà la dernière version.")

    @app.post("/reglages/maj-auto")
    async def toggle_auto(request: Request):
        f = await request.form()
        on = f.get("auto") == "1"
        upd.set_auto(on)
        return back("/reglages/a-propos", "Vérification automatique activée" if on else
                    "Vérification automatique désactivée")

    @app.post("/maj/plus-tard")
    async def update_later(request: Request):
        f = await request.form()
        upd.later(f.get("version") or "")
        return back(_return_path(request), "D'accord : tu retrouveras la mise à jour dans « À propos ».")

    @app.post("/maj/installer", response_class=HTMLResponse)
    def update_install(request: Request):
        ok, why = upd.installable()
        a = upd.available()
        if not a:
            return back("/reglages/a-propos", "Pas de nouvelle version à installer.")
        if not ok:
            return refuse("/reglages/a-propos", why)
        if not (upd.job and upd.job["step"] not in ("erreur",)):
            import threading
            upd.job = dict(step="sauvegarde", percent=0, error=None, version=a["version"])
            port = app.state.port or request.url.port or 8765

            def quit_now():
                import time as _t
                _t.sleep(1.5)                     # laisser la page d'attente voir « redémarrage »
                if site.vault_box is not None:
                    site.vault_box.stop()
                db.engine.dispose()
                os._exit(0)
            threading.Thread(target=upd.run_install, daemon=True, kwargs=dict(
                db=db, port=port, app_args=U.clean_args(app.state.argv), quit_app=quit_now)).start()
        return RedirectResponse("/maj/en-cours", status_code=303)   # page d'attente rechargeable

    @app.get("/maj/en-cours", response_class=HTMLResponse)
    def update_progress(request: Request):
        """Page d'attente de l'installation ; sans installation en cours (ex. rechargée après le redémarrage) :
        retour à « À propos », qui dit si la mise à jour a réussi."""
        if not upd.job:
            return RedirectResponse("/reglages/a-propos", status_code=303)
        return render(request, "update.html", version=upd.job.get("version"))

    @app.get("/maj/installer")
    def update_install_get():
        """Adresse du formulaire rouverte comme une page (rechargement après une mise à jour) : pas d'erreur."""
        return RedirectResponse("/reglages/a-propos", status_code=303)

    @app.get("/maj/etat")
    def update_state():
        j = dict(upd.job or {})
        if j.get("step") == "erreur":
            upd.job = None                         # on pourra réessayer
        return j or {"step": "aucune"}

    stopped = []

    def arreter():
        """Arrêt propre, sans quitter le programme : enregistre (voyages protégés : verrouille). Utilisé par
        « ⏻ Arrêter » et par la fenêtre (dernière fenêtre fermée, Cmd+Q, « Quitter » du Dock, extinction)."""
        if stopped:
            return
        stopped.append(1)
        if site.vault_box is not None:
            site.vault_box.stop()                             # enregistré, chiffré : rien en clair ne reste
        db.engine.dispose()
    app.state.arreter = arreter

    @app.post("/fenetre/nouvelle")
    def new_window():
        """Raccourci Cmd/Ctrl+N dans la fenêtre de l'application (Windows : pas de menu à raccourcis)."""
        f = getattr(app.state, "fenetres", None)
        if f is None:
            return JSONResponse({"ok": False}, status_code=404)
        f.new_later()
        return {"ok": True}

    @app.post("/fenetre/montrer")
    def show_window():
        """Trip Compta relancé alors qu'il tourne déjà (Windows) : ramener sa fenêtre au premier plan."""
        f = getattr(app.state, "fenetres", None)
        if f is None:
            return JSONResponse({"ok": False}, status_code=404)
        f.show()
        return {"ok": True}

    @app.post("/quitter", response_class=HTMLResponse)
    def quit_app():
        import os
        import threading
        arreter()
        threading.Timer(0.5, lambda: os._exit(0)).start()
        return HTMLResponse("<!doctype html><meta charset=utf-8><title>Trip Compta</title>"
                            "<body style='font-family:system-ui;text-align:center;padding:4rem'>"
                            "<h1>Trip Compta est arrêté.</h1><p>Tu peux fermer cet onglet. "
                            "Tes données sont enregistrées dans « " + str(db.home) + " ».</p></body>")
