"""Défaire et rétablir, « Dernières modifications », la page d'aide, et /ping (l'application répond-elle ?).

Partie de l'application web (voir compta/web.py, qui installe ces pages dans l'ordre).
"""
from __future__ import annotations

import re
import sys
import uuid
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import HTMLResponse

from .. import db as D
from .. import models as M
from ..db import human_when
from ..web import GUIDE_URL


def install(site):
    """Installe ces pages dans l'application. `site` : ce que les pages ont en commun (voir web.py)."""
    app = site.app
    back = site.back
    db = site.db
    refuse = site.refuse
    render = site.render
    upd = site.upd

    # ------------------------------------------------------------------ global
    def _return_path(request: Request) -> str:
        path = urlparse(request.headers.get("referer") or "/").path or "/"
        path = re.sub(r"/(nouvelle|\d+/modifier)$", "", path)
        m = re.match(r"/v/(\d+)", path)
        if m:               # le voyage n'existe plus (création défaite) : retour à l'accueil
            with db.Session() as s:
                if not s.get(M.Trip, int(m.group(1))):
                    path = "/"
        m = re.match(r"/preparer/(\d+)", path)
        if m:               # voyage en préparation : sa page (formulaire d'offre compris), ou l'accueil s'il n'existe plus
            path = f"/preparer/{m.group(1)}"
            with db.Session() as s:
                if not s.get(M.Plan, int(m.group(1))):
                    path = "/"
        return path

    @app.post("/annuler")
    async def undo(request: Request):
        f = await request.form()
        d = f.get("d")
        if d and d != db.undo_token():
            return refuse(_return_path(request), "Cette action a été suivie d'une autre : elle ne peut plus être "
                                                 "défaite d'ici.")
        label = db.undo()
        if not label:
            return back(_return_path(request), "Rien à défaire")
        return back(_return_path(request), f"Défait : {label}.", redo=db.redo_token())

    # ------------------------------------------------------------------ dernières modifications, aide
    @app.get("/historique", response_class=HTMLResponse)
    def history_page(request: Request):
        """« Défaire » toujours accessible : la liste des dernières modifications, avec ce que chaque bouton défait
        (jamais un bouton aveugle : on voit ce qui va être annulé)."""
        items = db.history()
        for i, h in enumerate(items):
            h["ago"] = human_when(h["when"])
            h["also"] = i                    # modifications plus récentes, défaites elles aussi
        return render(request, "history.html", items=items, keep=D.UNDO_KEEP, redo=db.redo_token(),
                      redo_label=db.last_undone(), redo_token=None)

    @app.post("/historique/revenir")
    async def history_back(request: Request):
        f = await request.form()
        done = db.undo_to(str(f.get("jusqua") or ""))
        if not done:
            return refuse("/historique", "Cette modification n'est plus dans la liste : rien n'a été défait.")
        if len(done) == 1:
            return back("/historique", f"Défait : {done[0]}.", redo=db.redo_token())
        return back("/historique", f"{len(done)} modifications défaites, jusqu'à « {done[-1]} ». « Rétablir » les "
                                   f"refait une par une.", redo=db.redo_token())

    @app.get("/aide", response_class=HTMLResponse)
    def help_page(request: Request):
        return render(request, "help.html", in_window=bool(getattr(app.state, "fenetres", None)),
                      mac=sys.platform == "darwin", data_dir=db.home, guide_url=GUIDE_URL, keep_undo=D.UNDO_KEEP, keep_backups=D.BACKUP_KEEP)

    @app.post("/retablir")
    async def redo(request: Request):
        f = await request.form()
        r = f.get("r")
        if r and r != db.redo_token():
            return back(_return_path(request), "Il n'y a plus rien à rétablir.")
        label = db.redo()
        return back(_return_path(request), f"Rétabli : {label}." if label else "Rien à rétablir")

    boot_id = uuid.uuid4().hex[:12]

    @app.get("/ping")
    def ping():
        """Sert à reconnaître Trip Compta (script de redémarrage) et à recharger l'onglet après un redémarrage."""
        out = {"app": "tripcompta", "boot": boot_id, "version": upd.version, "jeton": bool(app.state.session_token)}
        if getattr(app.state, "fenetre_vue", None):          # la vraie fenêtre a affiché sa 1re page
            out["fenetre"] = app.state.fenetre_vue
        return out

    # utilisé par d'autres pages
    site._return_path = _return_path
