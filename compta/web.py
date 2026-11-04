"""Application web (FastAPI + Jinja2)."""
from __future__ import annotations

from markupsafe import Markup
import datetime as dt
import io
import os
import mimetypes
import re
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from . import engine as E
from . import models as M
from . import services as S
from .db import Database, NewerDatabaseError, stored_version

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


def _date(d):
    if not d:
        return ""
    if isinstance(d, str):
        try:
            d = dt.date.fromisoformat(d)
        except ValueError:
            return d
    return d.strftime("%d/%m/%Y")


def _amount_input(c):
    """centimes -> texte pour un champ de saisie ('1500,50')."""
    if c is None:
        return ""
    s = f"{abs(c) // 100},{abs(c) % 100:02d}"
    if s.endswith(",00"):
        s = s[:-3]
    return ("-" if c < 0 else "") + s


templates.env.filters["money"] = E.fmt
templates.env.filters["money_sign"] = lambda c: E.fmt(c, sign=True)
templates.env.filters["date"] = _date
templates.env.filters["amount_input"] = _amount_input
templates.env.tests["contains"] = lambda seq, x: x in (seq or ())
templates.env.globals["ROLES"] = S.ROLES
templates.env.filters["role_label"] = S.role_label
templates.env.globals["ROLE_TITLES"] = S.ROLE_TITLES
templates.env.filters["weight"] = lambda w: E.fmt_weight(E.to_weight(w))
templates.env.globals["ASSO"] = E.ASSO
# icône « Arrêter » dessinée : le caractère ⏻ (U+23FB) manque aux polices de Windows 10 (il s'affiche en carré vide)
templates.env.globals["POWER"] = Markup('<svg class="ico-power" viewBox="0 0 24 24" width="1em" height="1em" aria-hidden="true">'
    '<path d="M12 3v8M7.1 6.6a7.5 7.5 0 1 0 9.8 0" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg>')
# Version des fichiers statiques : force le navigateur à recharger le CSS / JS après une mise à jour
templates.env.globals["STATIC_V"] = str(int(max((HERE / "static" / n).stat().st_mtime for n in ("app.css", "app.js"))))
TRANSPARENT_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000100e221bc330000000049454e44ae426082")


import threading as _threading

_PDFIUM = _threading.Lock()   # pypdfium2 n'accepte pas deux PDF à la fois : plusieurs vignettes en même temps
                              # (page Justificatifs, cache vide) faisaient planter tout le processus


def _thumbnail(data: bytes, is_pdf: bool, size: int = 240) -> bytes:
    """Miniature JPEG : la photo réduite (dans le bon sens), ou la 1re page d'un PDF."""
    import io
    from PIL import Image, ImageOps
    if is_pdf:
        import pypdfium2 as pdfium
        with _PDFIUM:
            doc = pdfium.PdfDocument(data)
            try:
                page = doc[0]
                im = page.render(scale=size / max(page.get_width(), page.get_height(), 1) * 1.5).to_pil()
            finally:
                doc.close()
    else:
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    im = im.convert("RGB")
    im.thumbnail((size, size))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=82)
    return out.getvalue()


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_bytes(data)
    try:
        os.replace(tmp, path)
    except PermissionError:                  # Windows : une autre demande vient de l'écrire et la lit déjà
        tmp.unlink(missing_ok=True)


def _capfirst(t):
    if not t:
        return t
    out = str(t)[:1].upper() + str(t)[1:]
    return Markup(out) if isinstance(t, Markup) else out


def _label(name: str) -> Markup:
    """Nom de la caisse prêt pour le HTML : échappé, sauf l'apostrophe (« l'asso » reste lisible dans le source)."""
    return Markup(str(name).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


templates.env.filters["capfirst"] = _capfirst
templates.env.filters["caisse"] = lambda t: _label(S.caisse_label(t))   # nom de la caisse d'un voyage donné


def newer_data_app(err: NewerDatabaseError, updater) -> FastAPI:
    """La base a été ouverte par une version plus récente : on n'y touche pas, on explique quoi faire."""
    from . import updater as U
    from . import version as V
    app = FastAPI(title="Trip Compta", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db = None
    app.state.port = None                 # donnés par run.py (pour relancer après une mise à jour)
    app.state.argv = []
    app.state.updater = updater
    app.state.arreter = lambda: None      # rien d'ouvert : rien à enregistrer
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    boot = uuid.uuid4().hex[:12]

    @app.post("/fenetre/{action}")
    def window_action(action: str):
        f = getattr(app.state, "fenetres", None)
        if f is None or action not in ("nouvelle", "montrer", "navigateur"):
            return JSONResponse({"ok": False}, status_code=404)
        getattr(f, {"nouvelle": "new_later", "montrer": "show", "navigateur": "open_in_browser"}[action])()
        return {"ok": True}

    @app.get("/ping")
    def ping():
        return {"app": "tripcompta", "boot": boot, "version": updater.version}

    @app.post("/quitter", response_class=HTMLResponse)
    def quit_app():
        import threading
        threading.Timer(0.5, lambda: os._exit(0)).start()
        return HTMLResponse("<!doctype html><meta charset=utf-8><title>Trip Compta</title>"
                            "<body style='font-family:system-ui;text-align:center;padding:4rem'>"
                            "<h1>Trip Compta est arrêté.</h1><p>Tu peux fermer cet onglet.</p></body>")

    checked = []

    def fitting():
        """La version publiée, si elle sait ouvrir ces données (au moins aussi récente qu'elles)."""
        if not checked and not updater.dev and not os.environ.get("TRIPCOMPTA_NO_UPDATE_CHECK"):
            checked.append(updater.check(force=True))     # une seule fois : la page reste rapide ensuite
        a = updater.available()
        return a if a and V.base(a["version"]) and V.base(a["version"]) >= V.base(err.stored) else None

    def page(request, name, code=200, **ctx):
        return templates.TemplateResponse(request, name, dict(
            request=request, err=err, page=U.RELEASES_PAGE, help=U.INSTALL_HELP, trip=None, msg=None,
            asso_name="", app_version=updater.label, update_banner=None, STATIC_V=templates.env.globals["STATIC_V"],
            **ctx), status_code=code)

    @app.post("/maj/installer")
    def update_install(request: Request):
        """Mettre à jour directement depuis cet écran : les données ne sont pas ouvertes, rien à sauvegarder."""
        a = fitting()
        ok, _why = updater.installable()
        if a and ok and not (updater.job and updater.job["step"] != "erreur"):
            import threading
            updater.job = dict(step="sauvegarde", percent=0, error=None, version=a["version"])
            port = app.state.port or request.url.port or 8765

            def quit_now():
                import time as _t
                _t.sleep(1.5)                     # laisser la page d'attente voir « redémarrage »
                os._exit(0)
            threading.Thread(target=updater.run_install, daemon=True, kwargs=dict(
                db=None, port=port, app_args=U.clean_args(app.state.argv), quit_app=quit_now)).start()
        return RedirectResponse("/maj/en-cours" if updater.job else "/", status_code=303)

    @app.get("/maj/en-cours", response_class=HTMLResponse)
    def update_progress(request: Request):
        if not updater.job:
            return RedirectResponse("/", status_code=303)
        return page(request, "update.html", version=updater.job.get("version"), back="/")

    @app.get("/maj/etat")
    def update_state():
        j = dict(updater.job or {})
        if j.get("step") == "erreur":
            updater.job = None                    # on pourra réessayer
        return j or {"step": "aucune"}

    @app.get("/{rest:path}", response_class=HTMLResponse)
    def explain(request: Request, rest: str = ""):
        a = fitting()
        ok, why = updater.installable() if a else (False, "")
        return page(request, "newer_data.html", 409, available=a, installable=ok, why=why)
    return app


def _hm(value) -> str:
    """« 2026-11-03T14:02:11 » ou horodatage → « 14 h 02 » (avec la date si ce n'est pas aujourd'hui)."""
    if not value:
        return ""
    try:
        d = dt.datetime.fromtimestamp(value) if isinstance(value, (int, float)) else dt.datetime.fromisoformat(value)
    except (ValueError, TypeError, OSError):
        return ""
    hm = f"{d.hour} h {d.minute:02d}"
    return hm if d.date() == dt.date.today() else f"{d:%d/%m} à {hm}"


def create_app(data_dir=None, restart=None, updater=None, cloud=None) -> FastAPI:
    """restart : fonction qui relance l'application (fournie par run.py) ; sans elle, on demande de relancer.
    updater : gestion des mises à jour (les tests en donnent une simulée).
    cloud (tests) : {"shared": dossier synchronisé, "local": copie de travail, "identity": {"host", "pid"}} ;
    sinon le dossier synchronisé vient des réglages (« Où ranger mes voyages »)."""
    from . import cloud as C
    from . import paths as PATHS
    from . import updater as U
    from . import version as V
    upd = updater or U.Updater()
    if data_dir:
        data_source = "lancement"
    elif os.environ.get("TRIPCOMPTA_DATA_DIR") or os.environ.get("COMPTA_DATA_DIR"):
        data_source = "env"
    elif PATHS.read_config().get("data_dir"):
        data_source = "choisi"
    else:
        data_source = "defaut"
    cfg = PATHS.read_config()
    sync = None
    if cloud is not None:
        shared = Path(cloud["shared"])
        sync = C.CloudSync(shared, Path(cloud.get("local") or C.local_dir_for(shared, PATHS.config_file().parent)),
                           identity=cloud.get("identity"))
        data_source = "choisi"
    elif data_source == "choisi" and cfg.get("partage"):
        shared = Path(cfg["data_dir"]).expanduser()
        sync = C.CloudSync(shared, C.local_dir_for(shared, PATHS.config_file().parent))
    try:
        if sync is not None:
            sdb = sync.shared_db
            stored = stored_version(sdb) if sdb.exists() and not C.cloud_only(sdb) else None
            if stored and V.base(stored) and V.base(upd.version) and V.base(stored) > V.base(upd.version):
                raise NewerDatabaseError(stored, upd.version, sync.shared)
            sync.open()
            if sync.locked:                      # protégé par un mot de passe : rien en clair tant que verrouillé
                db = Database(sync.local / ".verrouille", app_version=upd.version, home=sync.shared,
                              receipts_in_home=False)
            else:
                db = Database(sync.local, app_version=upd.version, home=sync.shared,
                              receipts_in_home=not sync.protected)
                sync.publish()
        else:
            db = Database(data_dir, app_version=upd.version)
    except NewerDatabaseError as err:
        if sync is not None:
            sync.release()
        return newer_data_app(err, upd)
    if sync is None or not sync.locked:
        db.daily_backup()
        with db.Session() as s_:                 # centimes d'arrondi : un tirage pour les dépenses qui n'en ont pas
            if any([S.store_draws(t_) for t_ in s_.query(M.Trip).all()]):
                s_.commit()
    app = FastAPI(title="Trip Compta", docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json",
                  description="API JSON de Trip Compta : une IA peut lire et remplir les voyages. "
                              "Toutes les écritures acceptent \"simulation\": true.")
    app.state.db = db
    app.state.restart = restart
    app.state.updater = upd
    app.state.port = None                 # donnés par run.py (pour relancer après une mise à jour)
    app.state.argv = []
    app.state.update_notice = None if upd.dev else upd.startup_notice()
    app.state.sync = sync
    if sync is not None:
        sync.before_wipe = lambda: db.engine.dispose()
    if updater is None:
        upd.start_background()
    if sync is not None:
        import atexit
        atexit.register(sync.stop)
        if (cloud or {}).get("background", True):
            # battement du verrou + publication de ce qu'aurait écrit l'IA (autre processus) dans la copie de travail
            sync.start(on_tick=lambda: (idle_check(), not sync.readonly and not sync.problem
                                        and sync.local_dirty() and sync.publish()))

    # « Défaire » est proposé dans le message qui suit une action, et ne vaut que pour CETTE action :
    # on note l'action défaisable au début de chaque requête pour savoir si la requête en a créé une.
    import contextvars
    snap_before: contextvars.ContextVar = contextvars.ContextVar("snap_before", default=None)

    @app.middleware("http")
    async def keep_draws(request, call_next):
        """Après chaque modification d'un voyage : tirage des centimes d'arrondi pour les dépenses qui n'en ont
        pas de valable (nouvelle dépense, personnes ou montant changés, désistement…). Jamais refait sinon."""
        response = await call_next(request)
        m = re.match(r"/v/(\d+)/", request.url.path)
        if request.method == "POST" and m and response.status_code < 400 and not request.url.path.endswith("apercu"):
            with db.Session() as s_:
                t_ = s_.get(M.Trip, int(m.group(1)))
                if t_ is not None and S.store_draws(t_):
                    s_.commit()
        return response

    @app.middleware("http")
    async def remember_snapshot(request, call_next):
        # (dernière action défaisable avant la requête, heure de début au format des sauvegardes d'annulation)
        token = snap_before.set((db.undo_token(), dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")))
        try:
            return await call_next(request)
        finally:
            snap_before.reset(token)

    app.state.last_seen = time.time()            # dernier signe d'un onglet ouvert (page, ou /ping?vu=1 d'un onglet)

    def idle_check(now=None) -> bool:
        """Aucun onglet ouvert depuis C.IDLE_AFTER secondes : rendre la main aux autres ordinateurs."""
        if sync is None or (now or time.time()) - app.state.last_seen < C.IDLE_AFTER:
            return False
        return sync.go_idle()
    app.state.idle_check = idle_check

    def is_placeholder() -> bool:
        return db.dir == sync.local / ".verrouille"

    def open_unlocked():
        """Après le déverrouillage : la vraie base (déchiffrée sur l'ordinateur) remplace la base vide d'attente."""
        db.reopen(sync.local, receipts_in_home=False)
        import shutil as _sh
        _sh.rmtree(sync.local / ".verrouille", ignore_errors=True)
        check_newer()
        db.daily_backup()
        with db.Session() as s_:
            if any([S.store_draws(t_) for t_ in s_.query(M.Trip).all()]):
                s_.commit()
        if not sync.readonly:
            sync.publish()

    def to_locked():
        """Verrouiller : base refermée, puis copie de travail effacée et historique chiffré (cloud.py)."""
        db.engine.dispose()
        if sync.vault is not None:
            sync.lock()
        else:                                    # protection apparue depuis un autre ordinateur
            if sync.local_db.exists() and sync.local_dirty():
                sync.keep_local_copy("pas-encore-envoyee")
            sync.wipe_working_copy()
            sync._flag_locked(True)
            sync.release()
        db.reopen(sync.local / ".verrouille", receipts_in_home=False)

    def check_newer():
        stored = stored_version(db.path)
        if stored and V.base(stored) and V.base(upd.version) and V.base(stored) > V.base(upd.version):
            sync.readonly = True
            sync.problem = (f"Ces voyages ont été enregistrés par Trip Compta {stored}, plus récente que celle-ci : "
                            f"mets à jour Trip Compta (ℹ️ À propos) pour pouvoir les modifier.")

    def cloud_refresh():
        """En revenant sur l'app : dossier revenu ? nouvelle version publiée par un autre ordinateur ?"""
        if sync.protected and sync.vault is None:
            if not is_placeholder():             # protection activée depuis un autre ordinateur
                to_locked()
            return
        if not sync.protected and is_placeholder():  # protection retirée depuis un autre ordinateur
            sync._flag_locked(False)
            db.reopen(sync.local, receipts_in_home=True)
            sync.acquire()
        if sync.problem:
            if sync.check_folder() is None:              # le dossier est revenu (cloud relancé, clé rebranchée)
                sync.problem = None
                sync.open()
                db.after_replace()
            elif sync.shared.is_dir() and sync.shared_changed() and sync.pull(before_replace=db.engine.dispose):
                db.after_replace()                       # dossier lisible mais protégé : on suit quand même
            return
        if sync.shared_changed() and sync.pull(before_replace=db.engine.dispose):
            db.after_replace()
            check_newer()

    READ_OK = ("/reglages", "/maj/", "/quitter", "/apercu-fichier", "/deverrouiller", "/verrouiller", "/protection",
               "/fenetre/")
    LOCKED_OK = ("/static", "/ping", "/deverrouiller", "/quitter", "/maj/", "/reglages/a-propos", "/favicon",
                 "/fenetre/")

    def readonly_message() -> str:
        if sync.problem:
            return "Lecture seule : " + sync.problem
        o = sync.owner or {}
        since = _hm(o.get("depuis"))
        if o.get("ordinateur") == sync.host:
            return ("Lecture seule : Trip Compta est déjà ouvert sur cet ordinateur (une autre fenêtre ou une autre "
                    "version). Utilise celle-là, ou ferme-la (bouton « Arrêter ») pour pouvoir modifier ici.")
        txt = (f"Lecture seule : tes voyages sont ouverts sur « {o.get('ordinateur') or 'un autre ordinateur'} »"
               + (f" depuis {since}" if since else "") + ". Ferme Trip Compta là-bas pour pouvoir les modifier ici.")
        if o and sync.lock_is_stale(o):
            txt += " (Cet ordinateur ne donne plus signe de vie : tu peux « Prendre la main ».)"
        return txt

    @app.middleware("http")
    async def cloud_sync(request, call_next):
        if sync is None:
            return await call_next(request)
        path = request.url.path
        tab = (not path.startswith(("/static", "/ping", "/maj/etat", "/api"))
               or (path == "/ping" and request.query_params.get("vu")))
        if tab:                                  # un onglet est ouvert : on reprend la main si on l'avait rendue
            app.state.last_seen = time.time()
            if sync.idle:
                try:
                    sync.wake()
                except Exception:
                    pass
        if request.method == "GET" and not path.startswith(("/static", "/ping", "/maj/etat")):
            try:
                cloud_refresh()
            except Exception:
                pass
        writing = request.method in ("POST", "PUT", "PATCH", "DELETE")
        if sync.locked and not path.startswith(LOCKED_OK):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Trip Compta est verrouillé : saisis le mot de passe de tes voyages "
                                               "dans l'application."}, status_code=423)
            return RedirectResponse("/deverrouiller", status_code=303)
        if writing and sync.readonly and not path.startswith(READ_OK) and not path.endswith("apercu"):
            if path.startswith("/api/"):
                return JSONResponse({"detail": readonly_message()}, status_code=423)
            return back(urlparse(request.headers.get("referer") or "/").path or "/", readonly_message())
        response = await call_next(request)
        if writing and response.status_code < 400 and not sync.readonly and not sync.problem and not sync.locked:
            sync.publish()
        return response

    from .api import install_api
    install_api(app)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def get_s():
        s = db.Session()
        try:
            yield s
        finally:
            s.close()

    # ------------------------------------------------------------------ helpers
    def mode_of(request: Request) -> str:
        """Toujours le montant réel s'il est connu, sinon le prévu (plus de bouton « prévu / réel »)."""
        return E.ACTUAL

    def render(request: Request, name: str, **ctx):
        ctx.setdefault("mode", mode_of(request))
        ctx.setdefault("msg", request.query_params.get("msg"))
        d, r = request.query_params.get("d"), request.query_params.get("r")
        ctx.setdefault("undo_token", d if d and d == db.undo_token() else None)
        ctx.setdefault("undo_label", db.last_action() if ctx["undo_token"] else None)
        ctx.setdefault("redo_token", r if r and r == db.redo_token() else None)
        if ctx.get("trip") is not None and isinstance(ctx["trip"], M.Trip):
            ctx.setdefault("roles", S.roles_map(ctx["trip"]))
            ctx.setdefault("estimated", [e for e in ctx["trip"].expenses if S.is_estimated(e)])
            ctx.setdefault("trip_roles", S.trip_roles(ctx["trip"]))
        if "asso_name" not in ctx:
            if isinstance(ctx.get("trip"), M.Trip):
                ctx["asso_name"] = S.caisse_label(ctx["trip"])
            else:
                with db.Session() as s_:
                    ctx["asso_name"] = S.asso_name(s_)
        ctx["asso_name"] = _label(ctx["asso_name"])
        ctx.setdefault("caisse_title", _capfirst(ctx["asso_name"]) if ctx["asso_name"].startswith("la caisse")
                       else Markup("Caisse de ") + ctx["asso_name"])
        ctx["request"] = request
        ctx.setdefault("cloud", cloud_status())
        if sync is not None and sync.notice and not ctx.get("msg"):
            ctx["msg"], sync.notice = sync.notice, None
        ctx.setdefault("app_version", upd.label)
        ctx.setdefault("update_banner", None if upd.job else upd.banner())
        if not upd.dev and not app.state.update_notice:
            app.state.update_notice = upd.failure_notice()
        if app.state.update_notice and not ctx.get("msg"):
            ctx["msg"], app.state.update_notice = app.state.update_notice, None
        return templates.TemplateResponse(request, name, ctx)

    def cloud_status() -> dict | None:
        """État du dossier synchronisé, pour l'en-tête des pages (None : données rangées sur l'ordinateur)."""
        if sync is None:
            return None
        o = sync.owner or {}
        # dernier enregistrement réel dans le dossier synchronisé (même avant ce lancement, ou depuis un autre
        # ordinateur) : date du fichier ; « stat » ne télécharge rien d'un fichier resté dans le cloud
        try:
            shared_file = sync.shared / (K.DB_FILE if sync.protected else C.DB)
            on_disk = shared_file.stat().st_mtime if shared_file.exists() else None
        except OSError:
            on_disk = None
        # (pas « pulled_at » : c'est l'heure où l'on a récupéré le fichier, au lancement par exemple, pas celle où il
        # a été enregistré ; un fichier récupéré a gardé sa date, comptée dans on_disk)
        saved = max([x for x in (sync.published_at, on_disk) if x] or [0]) or None
        return dict(service=sync.service or ("cet ordinateur" if not PATHS.looks_synced(sync.shared)
                                             else "le dossier synchronisé"), folder=str(sync.shared),
                    readonly=sync.readonly, problem=sync.problem, owner=o.get("ordinateur"),
                    same_host=bool(o) and o.get("ordinateur") == sync.host,
                    protected=sync.protected, locked=sync.locked,
                    local=not PATHS.looks_synced(sync.shared) and sync.service is None,
                    since=_hm(o.get("depuis")), stale=bool(o) and sync.lock_is_stale(o), saved=_hm(saved),
                    conflicts=len(sync.conflicts()) if not sync.problem else 0,
                    inbox=len(sync.inbox()) if not sync.problem else 0)

    def back(url: str, msg: str | None = None, redo: str | None = None):
        if msg:
            url, _, frag = url.partition("#")
            url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
            now = db.undo_token()
            before, started = snap_before.get() or (None, "")
            # « Défaire » seulement si CETTE requête a modifié les données (sauvegarde d'annulation créée pendant
            # elle). Déverrouiller, verrouiller, mot de passe, « Prendre la main », changer de dossier… ne font
            # qu'ouvrir d'autres données : la dernière action change, mais il n'y a rien à défaire.
            if now and now != before and now >= started:
                url += "&d=" + quote(now)
            if redo:
                url += "&r=" + quote(redo)
            if frag:
                url += "#" + frag
        return RedirectResponse(url, status_code=303)

    def trip_or_404(s: Session, tid: int) -> M.Trip:
        t = s.get(M.Trip, tid)
        if not t:
            raise HTTPException(404, "Voyage introuvable")
        return t

    class FormError(Exception):
        pass

    def amount(form, key):
        try:
            v = E.parse_amount(form.get(key))
        except ValueError:
            raise FormError(f"Montant invalide : « {form.get(key)} ». Exemple : 12,50")
        if v is not None and v < 0:
            raise FormError("Le montant doit être positif.")
        return v

    @app.exception_handler(FormError)
    async def form_error(request: Request, exc: FormError):
        ref = request.headers.get("referer") or "/"
        u = urlparse(ref)
        path = u.path + (("?" + re.sub(r"&?msg=[^&]*", "", u.query).lstrip("&")) if u.query else "")
        return back(path.rstrip("?"), str(exc))

    def names(trip):
        return {p.id: p.name for p in trip.participants}

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return templates.TemplateResponse(
            request, "error.html",
            dict(request=request, status=exc.status_code, detail=exc.detail, mode=mode_of(request),
                 msg=None, last_action=db.last_action()),
            status_code=exc.status_code)

    # ------------------------------------------------------------------ global
    @app.get("/mode")
    def set_mode(request: Request, m: str = E.ACTUAL, next: str = "/"):
        if not next.startswith("/"):
            next = "/"
        r = RedirectResponse(next, status_code=303)
        r.set_cookie("mode", m if m in (E.ACTUAL, E.PLANNED) else E.ACTUAL, max_age=3600 * 24 * 365)
        return r

    def _return_path(request: Request) -> str:
        path = urlparse(request.headers.get("referer") or "/").path or "/"
        path = re.sub(r"/(nouvelle|\d+/modifier)$", "", path)
        m = re.match(r"/v/(\d+)", path)
        if m:               # le voyage n'existe plus (création défaite) : retour à l'accueil
            with db.Session() as s:
                if not s.get(M.Trip, int(m.group(1))):
                    path = "/"
        return path

    @app.post("/annuler")
    async def undo(request: Request):
        f = await request.form()
        d = f.get("d")
        if d and d != db.undo_token():
            return back(_return_path(request), "Cette action a été suivie d'une autre : elle ne peut plus être "
                                               "défaite d'ici.")
        label = db.undo()
        if not label:
            return back(_return_path(request), "Rien à défaire")
        return back(_return_path(request), f"Défait : {label}.", redo=db.redo_token())

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
        return {"app": "tripcompta", "boot": boot_id, "version": upd.version}

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

    @app.post("/reglages/verifier-maj")
    def check_now(request: Request):
        res = upd.check(force=True)
        if not res["ok"]:
            return back("/reglages/a-propos", res["error"])
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
            return back("/reglages/a-propos", why)
        if not (upd.job and upd.job["step"] not in ("erreur",)):
            import threading
            upd.job = dict(step="sauvegarde", percent=0, error=None, version=a["version"])
            port = app.state.port or request.url.port or 8765

            def quit_now():
                import time as _t
                _t.sleep(1.5)                     # laisser la page d'attente voir « redémarrage »
                if sync is not None:
                    sync.stop()
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
        """Arrêt propre, sans quitter le programme : enregistre et rend la main aux autres ordinateurs. Utilisé par
        « ⏻ Arrêter » et par la fenêtre (dernière fenêtre fermée, Cmd+Q, « Quitter » du Dock, extinction)."""
        if stopped:
            return
        stopped.append(1)
        if sync is not None:
            if not sync.readonly and not sync.problem:
                sync.publish()
            sync.stop()                                  # libère le verrou : un autre ordinateur peut prendre la main
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

    @app.post("/fenetre/navigateur")
    def window_to_browser():
        """« Ouvrir dans le navigateur » (Réglages), quand Trip Compta tourne dans sa propre fenêtre."""
        f = getattr(app.state, "fenetres", None)
        if f is None:
            return JSONResponse({"ok": False}, status_code=404)
        f.open_in_browser()
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

    # ------------------------------------------------------------------ où ranger mes voyages
    def source_is_default() -> bool:
        try:
            return db.home.resolve() == (PATHS.documents_dir() / PATHS.FOLDER).resolve()
        except OSError:
            return False

    def _count_trips() -> int:
        try:
            with db.Session() as s_:
                return s_.query(M.Trip).count()
        except Exception:
            return 0

    def _data_page(request, **ctx):
        cur = db.home
        default_dir = PATHS.documents_dir() / PATHS.FOLDER
        clouds = C.cloud_roots()
        for c_ in clouds:
            try:
                c_["current"] = sync is not None and Path(c_["target"]).resolve() == sync.shared.resolve()
            except OSError:
                c_["current"] = False
        return render(request, "data_dir.html", data_dir=cur, source=data_source, shared=sync is not None,
                      local_has_data=(default_dir / C.DB).exists(),
                      local_current=sync is None and source_is_default(),
                      default_dir=default_dir, synced=PATHS.looks_synced(cur), clouds=clouds,
                      conflicts=sync.conflicts() if sync is not None and not sync.problem else [],
                      protected=sync is not None and sync.protected,
                      remembered=bool(sync is not None and sync.vault is not None and K.recall(sync.vault.id)),
                      can_remember=K.keyring_available(),
                      can_restart=app.state.restart is not None, cur_trips=_count_trips(), **ctx)

    @app.get("/reglages/donnees", response_class=HTMLResponse)
    def data_dir_page(request: Request):
        return _data_page(request)

    @app.get("/reglages/dossier-info")
    def folder_info(chemin: str = ""):
        """Ce que contient un dossier, AVANT de le choisir : voyages trouvés (nombre, noms), protégé, vide, à créer.
        Lecture seule ; un fichier encore « dans le cloud » (pas téléchargé) n'est jamais ouvert."""
        raw = (chemin or "").strip().strip('"').strip("'")
        if raw == "__local__":
            raw = str(PATHS.documents_dir() / PATHS.FOLDER)
        p = Path(raw).expanduser() if raw else None
        out = dict(chemin=str(p) if p else "", ok=False, existe=False, voyages=None, noms=[], protege=False,
                   cloud_seulement=False, actuel=False)
        if p is None or not p.is_absolute():
            return out
        try:
            out["actuel"] = p.resolve() == db.home.resolve()
        except OSError:
            pass
        out["existe"] = p.is_dir()
        out["ok"] = p.is_dir() or p.parent.is_dir()
        if not p.is_dir():
            return out
        if K.is_protected(p):
            out["protege"] = True
            return out
        if (p / f".{C.DB}.icloud").exists() and not (p / C.DB).exists():
            out["cloud_seulement"] = True
            return out
        if (p / C.DB).exists():
            import sqlite3
            try:
                con = sqlite3.connect(f"file:{p / C.DB}?mode=ro", uri=True, timeout=2)
                try:
                    rows = con.execute("SELECT name FROM trips ORDER BY created DESC").fetchall()
                finally:
                    con.close()
                out["voyages"], out["noms"] = len(rows), [r[0] for r in rows[:3]]
            except Exception:
                out["voyages"] = -1                      # des voyages, mais illisibles d'ici
        return out

    @app.get("/reglages", response_class=HTMLResponse)
    def settings_page(request: Request):
        """Réglages : où sont les voyages, mot de passe, version et mises à jour — l'état de chacun d'un coup d'œil."""
        checked = upd.checked_at()
        return render(request, "settings.html", data_dir=db.home, source=data_source, shared=sync is not None,
                      protected=sync is not None and sync.protected,
                      remembered=bool(sync is not None and sync.vault is not None and K.recall(sync.vault.id)),
                      info=upd, available=upd.available(), latest=upd.latest(),
                      checked=dt.datetime.fromtimestamp(checked).strftime("%d/%m/%Y") if checked else None)

    @app.get("/reglages/protection", response_class=HTMLResponse)
    def protection_settings(request: Request):
        return render(request, "protection_settings.html", source=data_source,
                      protected=sync is not None and sync.protected,
                      remembered=bool(sync is not None and sync.vault is not None and K.recall(sync.vault.id)),
                      can_remember=K.keyring_available())

    @app.post("/reglages/ouvrir-dossier")
    def data_dir_open(request: Request):
        import subprocess
        import sys as _sys
        target = str(db.home)
        try:
            if _sys.platform == "darwin":
                subprocess.Popen(["open", target])
            elif _sys.platform == "win32":
                os.startfile(target)                     # Explorateur Windows
            else:
                subprocess.Popen(["xdg-open", target])
        except Exception:
            return back(urlparse(request.headers.get("referer") or "/").path, f"Impossible d'ouvrir le dossier : {target}")
        return back(urlparse(request.headers.get("referer") or "/").path, "Dossier ouvert")

    @app.post("/reglages/parcourir")
    def data_dir_browse():
        """Fenêtre « choisir un dossier » du système ; renvoie le chemin choisi (ou rien si annulé)."""
        f = getattr(app.state, "fenetres", None)
        if f is not None and sys.platform == "win32":    # dans la fenêtre : le choix de dossier moderne, devant elle
            return {"path": f.pick_folder()}
        return {"path": C.pick_folder()}

    def _restart_page(request, message):
        if sync is not None:
            sync.stop()
        if app.state.restart is not None:
            import threading
            threading.Timer(0.6, app.state.restart).start()
        return render(request, "restart.html", message=message, can_restart=app.state.restart is not None)

    def _copy_voyages(target: Path, receipts_to: Path, drafts_to: Path | None):
        """Copie complète et vérifiée de la base (fichier temporaire puis remplacement d'un coup) et des
        justificatifs vers `target`. Le dossier d'origine n'est pas touché."""
        import shutil
        target.mkdir(parents=True, exist_ok=True)
        tmp = target / ".compta.db.copie"
        tmp.unlink(missing_ok=True)
        C.sqlite_copy(db.path, tmp)
        if not C.is_valid_db(tmp):
            tmp.unlink(missing_ok=True)
            raise OSError("la copie de la base est incomplète")
        C.replace(tmp, target / C.DB)
        if db.receipts_dir.is_dir():
            shutil.copytree(db.receipts_dir, receipts_to, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(".vignettes", ".*"))
        if drafts_to is not None and (db.dir / "brouillons_ia").is_dir():
            shutil.copytree(db.dir / "brouillons_ia", drafts_to, dirs_exist_ok=True)

    @app.post("/reglages/donnees", response_class=HTMLResponse)
    async def data_dir_change(request: Request):
        f = await request.form()
        action = f.get("action") or "ranger"
        if f.get("dossier") == "__local__":     # l'option « 💻 Sur cet ordinateur » de la liste
            action = "local"
        if data_source in ("lancement", "env"):
            return _data_page(request, error="Le dossier est imposé au lancement de l'application : il ne se change "
                                              "pas ici.")
        if sync is not None and sync.protected and action in ("local", "defaut", "ranger"):
            return _data_page(request, error="Tes voyages sont protégés par un mot de passe : pour les ranger "
                                              "ailleurs, retire d'abord la protection (plus bas), change de dossier, "
                                              "puis protège-les de nouveau. Sinon, une copie lisible serait créée.")
        if action == "proteger":                 # ancien réglage : dossier choisi, synchronisé, sans protection
            PATHS.write_config(partage=True)
            return _restart_page(request, "Trip Compta protège maintenant ce dossier synchronisé (verrou et "
                                          "enregistrements sûrs).")
        if action in ("local", "defaut"):        # retour au dossier sur cet ordinateur (Documents/TripCompta)
            target = PATHS.documents_dir() / PATHS.FOLDER
            try:
                same = target.resolve() == db.home.resolve()
            except OSError:
                same = False
            if f.get("mode") == "utiliser":   # reprendre les voyages déjà rangés sur cet ordinateur
                if not (target / C.DB).exists():
                    return _data_page(request, error=f"Il n'y a pas encore de voyages Trip Compta dans « {target} ». "
                                                      "Choisis « Copier mes voyages dans ce dossier ».", mode="utiliser")
                PATHS.write_config(data_dir=None, partage=False)
                return _restart_page(request, f"Trip Compta va utiliser les voyages rangés sur cet ordinateur, dans "
                                              f"« {target} ».")
            if not same:
                try:
                    if (target / C.DB).exists():  # ce qui y était (l'ancien dossier) est gardé dans ses sauvegardes
                        (target / "sauvegardes").mkdir(parents=True, exist_ok=True)
                        C.sqlite_copy(target / C.DB, target / "sauvegardes" /
                                      f"avant-retour-{dt.datetime.now():%Y-%m-%d-%Hh%M%S}.db")
                    _copy_voyages(target, target / "justificatifs", target / "brouillons_ia")
                except (OSError, Exception) as e:
                    return _data_page(request, error=f"Impossible de copier les voyages vers « {target} » : {e}")
            PATHS.write_config(data_dir=None, partage=False)
            return _restart_page(request, f"Tes voyages sont de nouveau rangés sur cet ordinateur, dans « {target} »."
                                 + (f" Le dossier « {db.home} » n'a pas été touché." if sync is not None else ""))
        raw = (f.get("dossier") or "").strip().strip('"').strip("'")
        if raw == "__autre__":
            raw = (f.get("autre") or "").strip().strip('"').strip("'")
        if not raw:
            return _data_page(request, error="Choisis un dossier.", value=raw)
        target = Path(raw).expanduser()
        if not target.is_absolute():
            return _data_page(request, error="Indique le chemin complet du dossier (par exemple "
                                              f"{PATHS.documents_dir() / 'Asso' / PATHS.FOLDER}).", value=raw)
        try:
            same = target.resolve() == db.home.resolve()
        except OSError:
            same = False
        if same:
            return _data_page(request, error="Tes voyages sont déjà rangés dans ce dossier.", value=raw)
        if not target.parent.is_dir() and not target.is_dir():
            return _data_page(request, error=f"Le dossier « {target.parent} » est introuvable.", value=raw)
        has_data = (target / C.DB).exists() or (target / f".{C.DB}.icloud").exists() or K.is_protected(target)
        if f.get("mode") == "utiliser":
            if not has_data:
                return _data_page(request, error="Il n'y a pas encore de voyages Trip Compta dans ce dossier (fichier "
                                                  "« compta.db » introuvable). Choisis « Copier mes voyages dans ce dossier ».",
                                  value=raw, mode="utiliser")
            PATHS.write_config(data_dir=str(target), partage=True)
            return _restart_page(request, f"Trip Compta va utiliser les voyages rangés dans « {target} ».")
        if has_data:
            return _data_page(request, error="Ce dossier contient déjà des voyages Trip Compta (sans doute rangés "
                                              "depuis un autre ordinateur). Choisis « Ouvrir les voyages de ce "
                                              "dossier », ou un autre dossier.", value=raw)
        try:
            new_local = C.local_dir_for(target, PATHS.config_file().parent)
            _copy_voyages(target, target / C.RECEIPTS, new_local / "brouillons_ia")
            (target / C.INBOX).mkdir(exist_ok=True)
        except (OSError, Exception) as e:
            return _data_page(request, error=f"Impossible de copier les voyages vers ce dossier : {e}", value=raw)
        PATHS.write_config(data_dir=str(target), partage=True)
        svc = C.service_name(target)
        return _restart_page(request, f"Tes voyages ont été copiés dans « {target} »"
                             + (f" ({svc})" if svc else "") + f". L'ancien dossier « {db.home} » n'a pas été "
                             f"touché : tu pourras le supprimer quand tu auras vérifié que tout est là.")

    @app.post("/reglages/prendre-la-main")
    def take_over(request: Request):
        ref = urlparse(request.headers.get("referer") or "/").path or "/"
        if sync is None or sync.problem:
            return back(ref, "Rien à faire : tes voyages ne sont pas ouverts ailleurs.")
        o = sync.read_lock()
        if not o or sync._mine(o):
            return back(ref, "Tu as déjà la main.")
        # l'autre ordinateur passera en lecture seule à son prochain battement (dès que le cloud lui aura
        # apporté notre verrou) ; s'il enregistre entre-temps, la copie en conflit sera signalée, rien n'est perdu
        sync.acquire(force=True)
        if sync.pull(before_replace=db.engine.dispose):       # la dernière version publiée par l'autre ordinateur
            db.after_replace()
        who = o.get("ordinateur") or "l'autre ordinateur"
        return back(ref, f"Tu as la main : tu peux modifier tes voyages. Sur « {who} », ils passent en lecture seule "
                         f"(dans les minutes qui viennent, le temps que {sync.service or 'ton cloud'} le prévienne).")

    # ------------------------------------------------------------------ protection par un mot de passe
    from . import crypto as K
    app.state.pending_protection = None      # en cours d'activation (en mémoire seulement, jamais sur le disque)

    def _folder() -> Path:
        return db.home

    def _unlock_page(request, **ctx):
        return render(request, "unlock.html", folder=str(_folder()), can_remember=K.keyring_available(), **ctx)

    @app.get("/deverrouiller", response_class=HTMLResponse)
    def unlock_page(request: Request):
        if sync is None or not sync.locked:
            return RedirectResponse("/", status_code=303)
        return _unlock_page(request)

    def _after_unlock(vault, remember: bool, request):
        if not sync._key_works(vault):
            return _unlock_page(request, error="Les voyages de ce dossier sont abîmés ou incomplets (synchronisation "
                                               "en cours ?). Attends un peu, puis réessaie.")
        if remember:
            K.remember(vault)
        else:
            K.forget(vault.id)
        sync.unlock(vault)
        open_unlocked()
        return None

    @app.post("/deverrouiller", response_class=HTMLResponse)
    async def unlock_post(request: Request):
        if sync is None or not sync.locked:
            return RedirectResponse("/", status_code=303)
        f = await request.form()
        try:
            vault = K.unlock(_folder(), password=f.get("mot_de_passe") or "")
        except K.WrongPassword:
            return _unlock_page(request, error="Mot de passe incorrect.")
        except K.Damaged:
            return _unlock_page(request, error="Le fichier de clé de ce dossier (cle.tripcompta) est introuvable ou "
                                               "abîmé.")
        err = _after_unlock(vault, bool(f.get("souvenir")), request)
        return err or back("/", "Voyages déverrouillés")

    @app.post("/deverrouiller/secours", response_class=HTMLResponse)
    async def unlock_recovery(request: Request):
        """Mot de passe oublié : la clé de secours ouvre les voyages et permet d'en choisir un nouveau."""
        if sync is None or not sync.locked:
            return RedirectResponse("/", status_code=303)
        f = await request.form()
        new, again = f.get("nouveau") or "", f.get("confirmation") or ""
        if len(new) < K.MIN_PASSWORD:
            return _unlock_page(request, error_rec=f"Le nouveau mot de passe doit faire au moins {K.MIN_PASSWORD} "
                                                   "caractères.", show_rec=True)
        if new != again:
            return _unlock_page(request, error_rec="Les deux mots de passe ne sont pas identiques.", show_rec=True)
        try:
            vault = K.unlock(_folder(), recovery=f.get("secours") or "")
        except K.WrongPassword:
            return _unlock_page(request, error_rec="Clé de secours incorrecte.", show_rec=True)
        K.change_password(_folder(), vault, new)
        err = _after_unlock(vault, bool(f.get("souvenir")), request)
        return err or back("/", "Nouveau mot de passe enregistré : voyages déverrouillés")

    @app.post("/verrouiller")
    def lock_now():
        if sync is not None and sync.protected and sync.vault is not None:
            to_locked()
        return RedirectResponse("/deverrouiller", status_code=303)

    def _protection_page(request, **ctx):
        return render(request, "protection.html", folder=str(_folder()), can_remember=K.keyring_available(),
                      protected=sync is not None and sync.protected, **ctx)

    def _need_main(request):
        if sync is not None and (sync.readonly or sync.problem):
            return _protection_page(request, step="erreur", error=readonly_message())
        if data_source in ("lancement", "env"):
            return _protection_page(request, step="erreur", error="Le dossier est imposé au lancement de "
                                                                   "l'application : la protection ne se règle pas ici.")
        return None

    @app.get("/protection/activer", response_class=HTMLResponse)
    def protect_start(request: Request):
        if sync is not None and sync.protected:
            return RedirectResponse("/reglages/protection", status_code=303)
        return _need_main(request) or _protection_page(request, step="mot_de_passe")

    @app.post("/protection/activer", response_class=HTMLResponse)
    async def protect_password(request: Request):
        import secrets as _secrets
        if sync is not None and sync.protected:
            return RedirectResponse("/reglages/protection", status_code=303)
        bad = _need_main(request)
        if bad:
            return bad
        f = await request.form()
        pw, again = f.get("mot_de_passe") or "", f.get("confirmation") or ""
        if len(pw) < K.MIN_PASSWORD:
            return _protection_page(request, step="mot_de_passe",
                                    error=f"Le mot de passe doit faire au moins {K.MIN_PASSWORD} caractères.")
        if pw != again:
            return _protection_page(request, step="mot_de_passe", error="Les deux mots de passe ne sont pas identiques.")
        rec = K.new_recovery_key()
        app.state.pending_protection = dict(kind="activer", password=pw, recovery=rec,
                                            remember=bool(f.get("souvenir")), group=_secrets.randbelow(8))
        return _protection_page(request, step="secours", recovery=rec, group=app.state.pending_protection["group"],
                                action="/protection/confirmer")

    @app.post("/protection/confirmer", response_class=HTMLResponse)
    async def protect_confirm(request: Request):
        pend = app.state.pending_protection
        if not pend or pend["kind"] != "activer":
            return RedirectResponse("/reglages/protection", status_code=303)
        bad = _need_main(request)
        if bad:
            return bad
        f = await request.form()
        if not K.check_group(pend["recovery"], pend["group"], f.get("groupe") or ""):
            return _protection_page(request, step="secours", recovery=pend["recovery"], group=pend["group"],
                                    action="/protection/confirmer",
                                    error=f"Ce n'est pas le groupe n° {pend['group'] + 1} de ta clé de secours. "
                                          "Vérifie ce que tu as noté.")
        try:
            vault = _activate(pend["password"], pend["recovery"])
        except Exception as e:                   # rien n'a été effacé : la protection n'est pas activée
            app.state.pending_protection = None
            return _protection_page(request, step="erreur", error=f"La protection n'a pas pu être activée ({e}). "
                                                                   "Tes voyages n'ont pas été modifiés.")
        app.state.pending_protection = None
        if pend["remember"]:
            K.remember(vault)
        return _restart_page(request, "Tes voyages sont maintenant protégés par ton mot de passe. Pense à vider la "
                                      "corbeille et les anciennes versions de ton cloud (par exemple Google Drive les "
                                      "garde 30 jours) : elles contiennent encore les fichiers d'avant, lisibles.")

    def _activate(password: str, recovery: str):
        """Chiffre la base et les justificatifs dans le dossier, vérifie TOUT, puis écrit cle.tripcompta (c'est le
        moment où la protection devient effective), et seulement ensuite efface les fichiers en clair."""
        import shutil as _sh
        F = _folder()
        local = sync.local if sync is not None else C.local_dir_for(F, PATHS.config_file().parent)
        local.mkdir(parents=True, exist_ok=True)
        vault, keydata = K.create(F, password, recovery)
        snap = local / ".protection.db"
        snap.unlink(missing_ok=True)
        C.sqlite_copy(db.path, snap)
        if not C.is_valid_db(snap):
            raise OSError("copie de la base incomplète")
        rdir = F / C.RECEIPTS
        rdir.mkdir(exist_ok=True)
        receipts = [p_ for p_ in (db.receipts_dir.iterdir() if db.receipts_dir.is_dir() else [])
                    if p_.is_file() and not p_.name.startswith(".")]
        vault.encrypt_file(snap, F / K.DB_FILE, "base")
        for p_ in receipts:
            vault.encrypt_file(p_, rdir / f"{p_.name}{K.SUFFIX}", f"justificatif:{p_.name}")
        # vérification complète avant de toucher à quoi que ce soit
        if vault.decrypt((F / K.DB_FILE).read_bytes(), "base") != snap.read_bytes():
            raise OSError("vérification de la base chiffrée")
        for p_ in receipts:
            if vault.decrypt((rdir / f"{p_.name}{K.SUFFIX}").read_bytes(), f"justificatif:{p_.name}") != p_.read_bytes():
                raise OSError(f"vérification du justificatif {p_.name}")
        K.write_key_file(F, keydata)                 # ← la protection est effective
        # ce qui était en clair dans le dossier : effacé ; l'historique et les sauvegardes partent sur l'ordinateur
        db.engine.dispose()
        if sync is None:                              # dossier « sur cet ordinateur » : on passe au mode dossier
            for name in C.PRIVATE:
                src = F / name
                if src.is_dir():
                    _sh.copytree(src, local / name, dirs_exist_ok=True)
                    _sh.rmtree(src, ignore_errors=True)
            _sh.rmtree(F / "imports_en_attente", ignore_errors=True)
        for name in (C.DB, f"{C.DB}-journal", f"{C.DB}-wal", f"{C.DB}-shm"):
            (F / name).unlink(missing_ok=True)
        for p_ in receipts:
            if p_.parent == rdir:
                p_.unlink(missing_ok=True)
        _sh.rmtree(rdir / ".vignettes", ignore_errors=True)
        snap.unlink(missing_ok=True)
        helper = C.CloudSync(F, local)                # l'historique sur l'ordinateur : chiffré lui aussi
        helper.vault = vault
        helper.encrypt_private()
        helper.wipe_working_copy()
        if sync is not None:
            sync.vault = None
        PATHS.write_config(data_dir=str(F), partage=True)
        # jusqu'au redémarrage : une base vide d'attente (surtout ne rien recréer en clair dans le dossier)
        db.reopen(local / ".verrouille", receipts_in_home=False)
        return vault

    def _check_current(request, f):
        try:
            K.unlock(_folder(), password=f.get("actuel") or "")
            return None
        except K.WrongPassword:
            return _protection_page(request, step="erreur", error="Mot de passe actuel incorrect.", back_link=True)

    @app.post("/protection/mot-de-passe", response_class=HTMLResponse)
    async def protect_change(request: Request):
        if sync is None or sync.vault is None:
            return RedirectResponse("/reglages/protection", status_code=303)
        f = await request.form()
        bad = _check_current(request, f)
        if bad:
            return bad
        new, again = f.get("nouveau") or "", f.get("confirmation") or ""
        if len(new) < K.MIN_PASSWORD or new != again:
            return _protection_page(request, step="erreur", back_link=True,
                                    error=f"Le nouveau mot de passe doit faire au moins {K.MIN_PASSWORD} caractères, "
                                          "et les deux saisies doivent être identiques.")
        K.change_password(_folder(), sync.vault, new)
        return back("/reglages/protection", "Mot de passe changé (sur tous tes ordinateurs, dès que ton cloud aura "
                                          "recopié le dossier)")

    @app.post("/protection/secours", response_class=HTMLResponse)
    async def protect_new_recovery(request: Request):
        import secrets as _secrets
        if sync is None or sync.vault is None:
            return RedirectResponse("/reglages/protection", status_code=303)
        f = await request.form()
        bad = _check_current(request, f)
        if bad:
            return bad
        rec = K.new_recovery_key()
        app.state.pending_protection = dict(kind="secours", recovery=rec, group=_secrets.randbelow(8))
        return _protection_page(request, step="secours", recovery=rec, group=app.state.pending_protection["group"],
                                action="/protection/secours/confirmer", renew=True)

    @app.post("/protection/secours/confirmer", response_class=HTMLResponse)
    async def protect_new_recovery_confirm(request: Request):
        pend = app.state.pending_protection
        if not pend or pend["kind"] != "secours" or sync is None or sync.vault is None:
            return RedirectResponse("/reglages/protection", status_code=303)
        f = await request.form()
        if not K.check_group(pend["recovery"], pend["group"], f.get("groupe") or ""):
            return _protection_page(request, step="secours", recovery=pend["recovery"], group=pend["group"],
                                    action="/protection/secours/confirmer", renew=True,
                                    error=f"Ce n'est pas le groupe n° {pend['group'] + 1} de ta clé de secours.")
        K.set_recovery(_folder(), sync.vault, pend["recovery"])
        app.state.pending_protection = None
        return back("/reglages/protection", "Nouvelle clé de secours enregistrée : l'ancienne ne marche plus")

    @app.post("/protection/oublier")
    def protect_forget():
        if sync is not None and sync.vault is not None:
            K.forget(sync.vault.id)
        return back("/reglages/protection", "Cet ordinateur ne se souvient plus du mot de passe")

    @app.post("/protection/retirer", response_class=HTMLResponse)
    async def protect_remove(request: Request):
        """Retirer la protection : tout redevient lisible (vérifié), puis la clé est effacée."""
        import shutil as _sh
        if sync is None or sync.vault is None:
            return RedirectResponse("/reglages/protection", status_code=303)
        bad = _need_main(request)
        if bad:
            return bad
        f = await request.form()
        bad = _check_current(request, f)
        if bad:
            return bad
        F = _folder()
        sync.publish()
        sync.receipts_down()
        tmp = F / ".compta.db.copie"
        tmp.unlink(missing_ok=True)
        C.sqlite_copy(db.path, tmp)
        if not C.is_valid_db(tmp):
            tmp.unlink(missing_ok=True)
            return _protection_page(request, step="erreur", error="La copie lisible de la base est incomplète : rien "
                                                                   "n'a été changé.", back_link=True)
        rdir = F / C.RECEIPTS
        for p_ in (db.receipts_dir.iterdir() if db.receipts_dir.is_dir() else []):
            if p_.is_file() and not p_.name.startswith("."):
                _sh.copyfile(p_, rdir / p_.name)
                if (rdir / p_.name).read_bytes() != p_.read_bytes():
                    return _protection_page(request, step="erreur", back_link=True,
                                            error=f"Copie du justificatif {p_.name} incomplète : rien n'a été changé.")
        C.replace(tmp, F / C.DB)
        vault_id = sync.vault.id
        (F / K.KEY_FILE).unlink()                    # ← la protection est retirée
        (F / K.README).unlink(missing_ok=True)
        (F / K.DB_FILE).unlink(missing_ok=True)
        for p_ in rdir.glob(f"*{K.SUFFIX}"):
            p_.unlink(missing_ok=True)
        K.forget(vault_id)
        sync.vault = None
        # ce qu'on sait du dossier : exactement ce qui vient d'y être écrit (sinon le battement suivant croirait
        # que le dossier a changé dans notre dos, et déposerait une fausse « copie en conflit »)
        sync._save_state(shared=C.file_hash(F / C.DB), local=C.file_hash(db.path))
        sync._seen_sig = sync._sig()
        return _restart_page(request, "La protection est retirée : tes voyages sont de nouveau des fichiers normaux.")

    @app.post("/reglages/conflit")
    async def resolve_conflict(request: Request):
        """Une copie en conflit (laissée par le service de cloud) : la garder comme version des voyages, ou la ranger
        de côté (dossier « Anciennes versions »). Rien n'est jamais effacé."""
        f = await request.form()
        if sync is None or sync.readonly or sync.problem:
            return back("/reglages/donnees", "Impossible pour l'instant : les voyages sont en lecture seule ici.")
        name = f.get("nom") or ""
        src = sync.shared / name
        pattern = C.CONFLICT_RE_PROTECTED if sync.protected else C.CONFLICT_RE
        if name.startswith(".") or "/" in name or "\\" in name or not pattern.match(name) or not src.is_file():
            return back("/reglages/donnees#conflits", "Version introuvable.")
        side = sync.shared / C.OLD_VERSIONS
        side.mkdir(exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y-%m-%d %Hh%M%S")
        if f.get("choix") == "utiliser":
            if not sync.conflict_readable(src):
                return back("/reglages/donnees#conflits", "Cette version est abîmée : elle ne peut pas être utilisée.")
            db.snapshot(f"choix d'une autre version des voyages ({name})")
            db.engine.dispose()
            if sync.protected:                   # versions chiffrées : on garde l'actuelle telle quelle, chiffrée
                import shutil as _sh
                _sh.copyfile(sync.shared_db, side / f"voyages (remplacée le {stamp}).chiffre")
                tmp_ = sync.local / ".choix.db"
                sync.decrypt_db(src, tmp_)
                C.sqlite_copy(tmp_, db.path)
                tmp_.unlink(missing_ok=True)
            else:
                C.sqlite_copy(sync.shared_db, side / f"compta (remplacée le {stamp}).db")
                C.sqlite_copy(src, db.path)
            db.after_replace()
            sync.publish()
            os.replace(src, side / name)
            return back("/reglages/donnees", "Cette version est maintenant celle de tes voyages. L'autre est rangée "
                                              f"dans « {C.OLD_VERSIONS} ». « Défaire » revient en arrière.")
        os.replace(src, side / name)
        return back("/reglages/donnees", f"Version rangée dans le dossier « {C.OLD_VERSIONS} » (rien n'est effacé).")

    # ------------------------------------------------------------------ IA
    def mcp_conf(request: Request) -> dict:
        import json
        import sys
        root = str(HERE.parent)
        frozen = getattr(sys, "frozen", False)
        if frozen:
            cmd, args = sys.executable, ["--mcp", "--data-dir", str(db.dir)]
        else:
            cmd, args = sys.executable, ["-m", "compta.mcp_server", "--data-dir", str(db.dir)]
        if db.home != db.dir and not (sync is not None and sync.protected):   # dossier synchronisé : justificatifs
            args += ["--home", str(db.home)]
        conf = {"mcpServers": {"tripcompta": {"command": cmd, "args": args,
                                                  **({} if frozen else {"cwd": root})}}}
        if not frozen:
            conf["mcpServers"]["tripcompta"]["env"] = {"PYTHONPATH": root}
        cli = "claude mcp add tripcompta -- " + " ".join(
            [f'"{cmd}"'] + [f'"{a}"' if " " in a else a for a in args])
        return dict(conf=json.dumps(conf, indent=2, ensure_ascii=False), cli=cli,
                    api=str(request.base_url) + "api/docs")

    def drafts() -> Path:
        """Dossier des brouillons IA : recalculé à chaque fois (la base peut être rouverte ailleurs, par exemple
        après le déverrouillage d'un dossier protégé)."""
        d_ = db.dir / "brouillons_ia"
        d_.mkdir(exist_ok=True)
        return d_
    INIT, UPDATE = "initialisation", "mise-a-jour"
    # Brouillon unique des versions précédentes (avant l'IA par voyage) : gardé à part s'il contenait quelque chose.
    for legacy in ("brouillon_ia.txt", "brouillon_ia.md", "brouillon_ia.precedent.txt", "brouillon_ia.precedent.md",
                   "brouillon_ia.ancien-markdown.txt"):
        f_ = db.dir / legacy
        if f_.exists():
            from . import importer as _imp
            txt = f_.read_text(encoding="utf-8")
            if _imp.is_old_markdown_draft(txt):
                txt = _imp.markdown_draft_to_text(txt)
            if txt.strip() and txt.strip() != _imp.template().strip() and "Nom du voyage :\n" not in txt[:600]:
                (drafts() / f"ancien-{legacy.replace('.md', '.txt')}").write_text(txt, encoding="utf-8")
            f_.unlink()

    def draft_paths(tid: int, mode: str):
        base = drafts() / f"voyage-{tid}-{mode}"
        return base.with_suffix(".txt"), Path(str(base) + ".precedent.txt")

    def ai_mode(t: M.Trip, value) -> str:
        if value in (INIT, UPDATE):
            return value
        return UPDATE if (t.participants or t.expenses) else INIT

    def trip_state_for_ai(t: M.Trip) -> str:
        """État actuel du voyage, joint au message pour l'IA (compact, lisible)."""
        import json
        from .ops import trip_detail
        d = trip_detail(t)
        keep = dict(
            voyage_id=t.id, nom=d["nom"], lieu=d["lieu"], debut=d["debut"], fin=d["fin"], note=d["note"],
            equipe=d["equipe"], categories=d["categories"],
            participants=[{k: v for k, v in p.items() if k in ("nom", "famille", "accompagnant", "roles", "desiste",
                                                              "sa_part", "a_verse", "a_avance", "reste", "situation")
                           and v not in (None, [], False, "")} for p in d["participants"]],
            familles=d["familles"],
            depenses=[{k: v for k, v in e.items() if k in ("libelle", "type", "a_venir", "categorie", "sous_categorie", "date", "prevu",
                                                          "reel", "rembourse", "rembourse_a", "paye_par", "pour")
                       and v not in (None, [], "", False, "depense")} for e in d["depenses"]],
            mouvements=[{k: v for k, v in m.items() if k != "id" and v not in (None, "")} for m in d["mouvements"]],
            ajustements=[{k: v for k, v in a.items() if k != "id" and v not in (None, "", False)}
                         for a in d["ajustements"]],
            desistements=d["desistements"], entre_nous=[x["nom"] for x in d["entre_nous"]],
            cout_total=d["cout_total"], deja_recu=d["deja_recu"], reste_a_recevoir=d["reste_a_recevoir"],
        )
        return json.dumps(keep, ensure_ascii=False, indent=1)

    def ai_render(request: Request, s: Session, t: M.Trip, mode: str, **ctx):
        from . import importer
        ctx.setdefault("texte", "")
        dfile, dprev = draft_paths(t.id, mode)
        blank = importer.template() if mode == INIT else importer.update_template()
        draft = dfile.read_text(encoding="utf-8") if dfile.exists() else None
        if mode == UPDATE and draft and draft.lstrip().startswith("=== METTRE À JOUR LE VOYAGE ===") and not any(
                l.strip().startswith("- ") and l.strip()[2:].strip() for l in draft.splitlines()):
            draft = None            # ancien modèle à rubriques, jamais rempli : on repart d'un texte libre
        saved = dt.datetime.fromtimestamp(dfile.stat().st_mtime) if draft is not None else None
        legacy = sorted(p_.name for p_ in drafts().glob("ancien-*.txt"))
        return render(request, "ai.html", trip=t, ia_mode=mode, INIT=INIT, UPDATE=UPDATE, modele=blank,
                      guide=importer.guide(), state=trip_state_for_ai(t), draft=draft, draft_saved=saved,
                      has_prev=dprev.exists(), legacy=legacy, cur=f"/v/{t.id}/ia", **mcp_conf(request), **ctx)

    @app.get("/ia")
    def ai_global():
        return back("/", "L'IA travaille sur un voyage : ouvre-le, puis clique sur « 🤖 IA » dans son menu.")

    @app.get("/v/{tid}/ia", response_class=HTMLResponse)
    def ai_page(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        return ai_render(request, s, t, ai_mode(t, request.query_params.get("modele")))

    @app.get("/ia/guide_ia.md")
    def ai_guide():
        from . import importer
        return Response(importer.guide(), media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="guide_ia_tripcompta.md"'})

    @app.get("/ia/modele-{mode}.txt")
    def ai_template(mode: str):
        from . import importer
        if mode not in (INIT, UPDATE):
            raise HTTPException(404, "Modèle inconnu")
        text = importer.template() if mode == INIT else importer.update_template()
        return Response(text, media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="modele_{mode}.txt"'})

    @app.get("/ia/ancien-brouillon/{name}")
    def ai_legacy(name: str):
        f_ = drafts() / name
        if not name.startswith("ancien-") or "/" in name or not f_.exists():
            raise HTTPException(404, "Brouillon introuvable")
        return Response(f_.read_text(encoding="utf-8"), media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.put("/v/{tid}/ia/brouillon/{mode}")
    @app.post("/v/{tid}/ia/brouillon/{mode}")
    async def ai_draft_save(tid: int, mode: str, request: Request, s: Session = Depends(get_s)):
        """Enregistrement à la volée du modèle rempli (dans le dossier de données, écriture atomique)."""
        trip_or_404(s, tid)
        if mode not in (INIT, UPDATE):
            raise HTTPException(404, "Modèle inconnu")
        text = (await request.body()).decode("utf-8")
        if len(text) > 2_000_000:
            raise HTTPException(413, "Texte trop long")
        dfile, _ = draft_paths(tid, mode)
        tmp = dfile.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, dfile)
        return {"enregistre": dt.datetime.now().strftime("%H:%M:%S")}

    @app.post("/v/{tid}/ia/brouillon/{mode}/vider")
    def ai_draft_reset(tid: int, mode: str, s: Session = Depends(get_s)):
        """Remet le modèle au propre ; l'ancien brouillon est gardé (un seul niveau) pour pouvoir le récupérer."""
        from . import importer
        trip_or_404(s, tid)
        if mode not in (INIT, UPDATE):
            raise HTTPException(404, "Modèle inconnu")
        blank = importer.template() if mode == INIT else importer.update_template()
        dfile, dprev = draft_paths(tid, mode)
        if dfile.exists() and dfile.read_text(encoding="utf-8").strip() != blank.strip():
            os.replace(dfile, dprev)
        dfile.unlink(missing_ok=True)
        return {"modele": blank}

    @app.post("/v/{tid}/ia/brouillon/{mode}/recuperer")
    def ai_draft_restore(tid: int, mode: str, s: Session = Depends(get_s)):
        trip_or_404(s, tid)
        if mode not in (INIT, UPDATE):
            raise HTTPException(404, "Modèle inconnu")
        dfile, dprev = draft_paths(tid, mode)
        if not dprev.exists():
            raise HTTPException(404, "Pas de brouillon précédent")
        text = dprev.read_text(encoding="utf-8")
        if dfile.exists():   # échange : rien n'est perdu
            swap = dfile.with_suffix(".swap")
            os.replace(dfile, swap)
            os.replace(dprev, dfile)
            os.replace(swap, dprev)
        else:
            os.replace(dprev, dfile)
        return {"modele": text}

    @app.post("/v/{tid}/ia/apercu", response_class=HTMLResponse)
    async def ai_preview(tid: int, request: Request, s: Session = Depends(get_s)):
        from .ops import Ops, OpsError
        t = trip_or_404(s, tid)
        f = await request.form()
        texte, mode = f.get("texte") or "", ai_mode(t, f.get("modele"))
        try:
            res = Ops(db).import_trip(texte, str(tid), simulation=True, verifier_id=True)
            return ai_render(request, s, t, mode, texte=texte, res=res, focus="apercu")
        except OpsError as e:
            errors = getattr(e, "errors", None) or [str(e)]
            return ai_render(request, s, t, mode, texte=texte, errors=errors, focus="apercu")

    @app.post("/v/{tid}/ia/importer")
    async def ai_import(tid: int, request: Request, s: Session = Depends(get_s)):
        from .ops import Ops, OpsError
        t = trip_or_404(s, tid)
        f = await request.form()
        texte, mode = f.get("texte") or "", ai_mode(t, f.get("modele"))
        try:
            res = Ops(db).import_trip(texte, str(tid), verifier_id=True)
        except OpsError as e:
            errors = getattr(e, "errors", None) or [str(e)]
            return ai_render(request, s, t, mode, texte=texte, errors=errors, focus="apercu")
        return back(f"/v/{tid}", res["message"] + "")

    # ------------------------------------------------------------------ voyages
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, s: Session = Depends(get_s)):
        trips = s.query(M.Trip).order_by(M.Trip.created.desc(), M.Trip.id.desc()).all()   # le plus récent d'abord
        cards = []
        for t in trips:
            r = S.compute_trip(t, mode_of(request))
            cards.append((t, r))
        q = request.query_params
        return render(request, "index.html", cards=cards, data_dir=db.home,
                      open_new=q.get("nouveau") == "1", open_import=q.get("importer") == "1")

    @app.post("/voyages")
    async def create_trip(request: Request, s: Session = Depends(get_s)):
        f = await request.form()
        name = (f.get("name") or "").strip()
        if not name:
            return back("/", "Donne un nom au voyage")
        db.snapshot(f"création du voyage « {name} »")
        t = M.Trip(name=name, place=f.get("place", "").strip(), organizer=f.get("organizer", "").strip(),
                   start=S.parse_date(f.get("start")), end=S.parse_date(f.get("end")))
        t.categories = S.new_trip_categories(s)
        s.add(t)
        s.commit()
        return back(f"/v/{t.id}/participants", "Voyage créé. Ajoute maintenant les participants.")

    @app.post("/v/{tid}/modifier")
    async def edit_trip(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        db.snapshot(f"modification du voyage « {t.name} »")
        t.name = (f.get("name") or t.name).strip()
        t.place = f.get("place", "").strip()
        t.organizer = f.get("organizer", "").strip()
        if "note" in f:
            t.note = (f.get("note") or "").strip()
        t.start = S.parse_date(f.get("start"))
        t.end = S.parse_date(f.get("end"))
        s.commit()
        return back(f"/v/{tid}", "Voyage modifié")

    @app.post("/v/{tid}/supprimer")
    def delete_trip(tid: int, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        db.snapshot(f"suppression du voyage « {t.name} »")
        _delete_trip(s, t)
        s.commit()
        # ses brouillons IA sont mis de côté : un nouveau voyage peut recevoir le même numéro, il ne doit pas en hériter
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        for f_ in drafts().glob(f"voyage-{tid}-*"):
            try:
                f_.rename(drafts() / f"supprime-{stamp}-{f_.name}")
            except OSError:
                pass
        return back("/", f"Voyage « {t.name} » supprimé (tu peux encore annuler)")

    def _delete_trip(s, t):
        for e in t.expenses:
            for l in list(e.receipt_links):
                s.delete(l)
        s.flush()
        for a in t.adjustments:
            s.delete(a)
        s.flush()
        for sub in t.subaccounts:
            s.delete(sub)
        s.flush()
        for e in t.expenses:
            s.delete(e)
        s.flush()
        s.delete(t)

    @app.get("/v/{tid}", response_class=HTMLResponse)
    def dashboard(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        r = S.compute_trip(t, mode_of(request))
        asso_paid = sum(c for e in t.expenses if e.actual is not None and not e.is_income
                        for pid, c in e.splits() if pid is None)
        cat_totals = {}
        for e in t.expenses:
            cn = e.category.name if e.category else "Divers"
            cat_totals[cn] = cat_totals.get(cn, 0) + r.expense_totals.get(e.id, 0)
        return render(request, "dashboard.html", trip=t, r=r, alerts=S.alerts(t, r), msum=S.money_summary(t, r),
                      asso_paid=asso_paid, cat_totals=cat_totals,
                      adv_total=sum(a["amount"] for a in S.advances(t) if not a["income"]),
                      withdrawn={w.participant_id for w in t.withdrawals})

    # ------------------------------------------------------------------ participants
    @app.get("/v/{tid}/participants", response_class=HTMLResponse)
    def participants(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        r = S.compute_trip(t, mode_of(request))
        groups = S.trip_groups(t)
        known = S.known_people(t)
        return render(request, "participants.html", trip=t, r=r, groups=groups, export_cols=S.EXPORT_COLS,
                      known=known, known_trips=S.known_trips(t, known),
                      known_fill={S.person_key(x["name"]): dict(phone=x["phone"], email=x["email"],
                                                                companion=x["companion"]) for x in known},
                      open_add=request.query_params.get("ajout") == "1",
                      open_edit=int(request.query_params.get("modifier") or 0) if (request.query_params.get("modifier") or "").isdigit() else 0,
                      pgroups={p.id: [g["name"] for g in groups if p.id in g["ids"]] for p in t.participants},
                      withdrawn={w.participant_id for w in t.withdrawals},
                      withdrawn_dates={w.participant_id: w.date for w in t.withdrawals},
                      has_expenses=bool(t.expenses), shared=S.shared_expenses(t),
                      join_default={e.id: (S.default_join(e, False, t), S.default_join(e, True, t))
                                    for e in t.expenses})

    @app.get("/v/{tid}/participants/avances", response_class=HTMLResponse)
    def people_advances(tid: int, request: Request, s: Session = Depends(get_s)):
        """Ce que des participants ont payé eux-mêmes (avances) ou reçu à la place de l'asso."""
        t = trip_or_404(s, tid)
        refunds = [dict(date=m.date, label=m.note or "Remboursement", mid=m.id, pid=m.participant_id,
                        name=m.participant.name if m.participant else "?", amount=m.amount)
                   for m in t.movements if m.kind == "remboursement"]
        return render(request, "avances.html", trip=t, advances=S.advances(t), refunds=refunds)

    @app.get("/v/{tid}/participants/organisation", response_class=HTMLResponse)
    def organisation(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        return render(request, "organisation.html", trip=t, groups=S.trip_groups(t),
                      role_members={rk: S.with_role(t, rk) for rk in S.trip_roles(t)})

    @app.post("/v/{tid}/roles-du-voyage")
    async def add_role_route(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        name = str((await request.form()).get("name") or "")
        db.snapshot(f"nouveau rôle {name}")
        try:
            key = S.add_trip_role(t, name)
        except ValueError as e:
            return back(f"/v/{tid}/participants/organisation#roles", str(e))
        s.commit()
        return back(f"/v/{tid}/participants/organisation#roles", f"Rôle « {S.role_label(key)} » ajouté")

    @app.post("/v/{tid}/roles-du-voyage/supprimer")
    async def remove_role_route(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        key = str((await request.form()).get("role") or "")
        db.snapshot(f"suppression du rôle {S.role_label(key)}")
        try:
            S.remove_trip_role(t, key)
        except ValueError as e:
            return back(f"/v/{tid}/participants/organisation#roles", str(e))
        s.commit()
        return back(f"/v/{tid}/participants/organisation#roles", f"Rôle « {S.role_label(key)} » supprimé de ce voyage")

    @app.post("/v/{tid}/roles/{role}")
    async def save_role_members(tid: int, role: str, request: Request, s: Session = Depends(get_s)):
        """Onglet Organisation : les membres d'un rôle (organisateur : un seul ; plongeur / accompagnant : l'un ou l'autre)."""
        t = trip_or_404(s, tid)
        if role not in S.trip_roles(t):
            raise HTTPException(404, "Rôle inconnu")
        ids = {int(x) for x in (await request.form()).getlist("members") if str(x).isdigit()}
        if role == S.ORGANIZER and len(ids) > 1:
            return back(f"/v/{tid}/participants/organisation#roles", "Un seul organisateur : choisis une personne")
        db.snapshot(f"rôle {S.role_label(role)}")
        other = {S.DIVER: S.COMPANION, S.COMPANION: S.DIVER}.get(role)
        for p in t.participants:
            rs = [r for r in p.role_list if r != role]
            if p.id in ids:
                rs.append(role)
                if other:
                    rs = [r for r in rs if r != other]
            S.set_roles(t, p, rs)
        s.commit()
        n = len(ids)
        return back(f"/v/{tid}/participants/organisation#roles",
                    f"{S.role_label(role)} : {n} personne{'s' if n > 1 else ''}")

    @app.post("/v/{tid}/familles")
    async def create_family(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        db.snapshot(f"famille {f.get('name')}")
        try:
            fam = S.save_family(s, t, f.get("name"), f.getlist("members"), f.get("referent"))
        except ValueError as e:
            s.rollback()
            return back(f"/v/{tid}/participants/organisation#familles", str(e))
        s.commit()
        return back(f"/v/{tid}/participants/organisation#familles", f"Famille {fam.name} créée")

    @app.post("/v/{tid}/familles/{fid}/supprimer")
    def remove_family(tid: int, fid: int, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        fam = next((x for x in t.families if x.id == fid), None)
        if fam is None:
            raise HTTPException(404, "Famille introuvable")
        name = fam.name
        db.snapshot(f"suppression de la famille {name}")
        S.delete_family(s, t, fam)
        s.commit()
        return back(f"/v/{tid}/participants/organisation#familles", f"Famille {name} supprimée : chacun a son compte")

    @app.post("/v/{tid}/groupes")
    async def save_group_route(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        old = f.get("old_name") or None
        db.snapshot(f"groupe « {f.get('name') or old} »")
        try:
            S.save_group(t, f.get("name") or "", f.getlist("members"), old)
        except ValueError as e:
            return back(f"/v/{tid}/participants/organisation#groupes", str(e))
        s.commit()
        g = S.find_group(t, f.get("name"))
        return back(f"/v/{tid}/participants/organisation#groupes",
                    f"Groupe « {g['name']} » enregistré ({len(g['ids'])} personne{'s' if len(g['ids']) > 1 else ''})")

    @app.post("/v/{tid}/groupes/supprimer")
    async def delete_group_route(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        name = (await request.form()).get("name") or ""
        db.snapshot(f"suppression du groupe « {name} »")
        S.delete_group(t, name)
        s.commit()
        return back(f"/v/{tid}/participants/organisation#groupes", f"Groupe « {name} » supprimé (les dépenses ne changent pas)")

    def _roles_from_form(f) -> list:
        """Rôles cochés (pastilles r_<rôle>) ; l'ancien choix « role » = plongeur / accompagnant reste accepté.
        Sans pastilles (ancien formulaire, liste de noms) : plongeur par défaut."""
        rs = [r for r in S.ROLES if f.get(f"r_{r}")] + [r for r in f.keys() if r.startswith("r_" + S.CUSTOM) and f.get(r)]
        rs = [r[2:] if r.startswith("r_") else r for r in rs]
        for v in f.getlist("role"):
            if v in (S.DIVER, S.COMPANION) and v not in rs:
                rs.append(v)
        if "roles_form" not in f and S.DIVER not in rs and S.COMPANION not in rs:
            rs.append(S.DIVER)
        return rs

    def _family_from_form(f):
        """Famille choisie : pastilles (« Aucune », une existante, « Nouvelle… » + nom) ou ancien champ texte."""
        if "family_pick" in f:
            pick = f.get("family_pick") or ""
            return (f.get("family_new") or "").strip() if pick == "__new__" else pick
        return f.get("family") if "family" in f else None

    @app.post("/v/{tid}/participants/reprendre")
    async def take_known_people(tid: int, request: Request, s: Session = Depends(get_s)):
        """Ajoute des personnes déjà venues sur d'autres voyages : nom, téléphone, e-mail, plongeur ou accompagnant
        (pas la famille, les rôles, les groupes ni la note, propres à chaque voyage)."""
        t = trip_or_404(s, tid)
        f = await request.form()
        wanted = set(f.getlist("known"))
        people = [x for x in S.known_people(t) if x["key"] in wanted]
        if not people:
            return back(f"/v/{tid}/participants?ajout=1#ajout", "Coche au moins une personne")
        db.snapshot(f"reprise de {len(people)} participant(s) d'autres voyages")
        added = [M.Participant(name=x["name"], phone=x["phone"], email=x["email"], companion=x["companion"])
                 for x in people]
        t.participants.extend(added)
        s.flush()
        for p in added:
            S.set_roles(t, p, [S.COMPANION if p.companion else S.DIVER])
        s.commit()
        msg = f"{added[0].name} ajouté" if len(added) == 1 else f"{len(added)} participants ajoutés"
        if t.expenses:
            msg += " — pense à les cocher dans « Pour qui ? » des dépenses qui les concernent"
        return back(f"/v/{tid}/participants", msg)

    @app.post("/v/{tid}/participants")
    async def add_participants(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        raw = f.get("names") or f.get("name") or ""
        new = []
        existing = {E.sort_key(p.name) for p in t.participants}
        for line in re.split(r"[\n;]", raw):
            n = line.strip().strip(",").strip()
            if n and E.sort_key(n) not in existing:
                new.append(n)
                existing.add(E.sort_key(n))
        if not new:
            return back(f"/v/{tid}/participants?ajout=1#ajout", "Ce nom est vide ou déjà dans la liste")
        roles_in = _roles_from_form(f)
        companion = S.COMPANION in roles_in
        family = (_family_from_form(f) or "").strip()
        db.snapshot(f"ajout de {len(new)} participant(s)")
        shared = S.shared_expenses(t)
        if "join_form" in f:
            join = [e for e in shared if f.get(f"join_{e.id}")]
        else:  # ancien formulaire : case unique
            join = shared if f.get("join_all") else []
        kp = S.known_person(t, new[0]) if len(new) == 1 else None   # déjà venu sur un autre voyage ?
        added = [M.Participant(name=n, companion=companion) for n in new]
        t.participants.extend(added)
        s.flush()
        for p in added:
            S.set_roles(t, p, roles_in)
            if family:
                S.set_family(s, t, p, family)
            for e in join:
                e.shares.append(M.ExpenseShare(participant_id=p.id, weight=1.0))
        if len(added) == 1 and "name" in f:      # formulaire « un participant » : toutes ses infos
            p = added[0]
            p.phone, p.email, p.note = (f.get(k, "").strip() for k in ("phone", "email", "note"))
            if kp:                               # déjà venu sur un autre voyage : ses contacts s'il n'en a pas saisi
                p.phone, p.email = p.phone or kp["phone"], p.email or kp["email"]
            if f.getlist("groups"):
                S.set_person_groups(t, p.id, f.getlist("groups"))
        s.commit()
        if len(added) == 1:
            msg = f"{added[0].name} ajouté{' (accompagnant)' if companion else ''}"
        else:
            msg = f"{len(new)} {'accompagnant' if companion else 'participant'}(s) ajouté(s)"
        if family:
            msg += f" dans la famille {S.find_family(t, family).name}"
        if join:
            msg += f", inclus dans {len(join)} dépense(s) de groupe"
        elif t.expenses and "name" in f:
            msg += " — pense à le cocher dans « Pour qui ? » des dépenses qui le concernent"
        return back(f"/v/{tid}/participants#pe{added[0].id}" if len(added) == 1 else f"/v/{tid}/participants", msg)

    @app.post("/v/{tid}/participants/{pid}")
    async def edit_participant(tid: int, pid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        p = s.get(M.Participant, pid)
        if not p or p.trip_id != tid:
            raise HTTPException(404, "Participant introuvable")
        f = await request.form()
        db.snapshot(f"modification de {p.name}")
        p.name = (f.get("name") or p.name).strip()
        p.phone = f.get("phone", "").strip()
        p.email = f.get("email", "").strip()
        p.note = f.get("note", "").strip()
        fam_ = _family_from_form(f)
        if fam_ is not None:
            S.set_family(s, t, p, fam_)
        if "role" in f and "roles_form" not in f:        # ancien formulaire : plongeur / accompagnant seulement
            keep = [r for r in p.role_list if r not in (S.DIVER, S.COMPANION)]
            S.set_roles(t, p, keep + [S.COMPANION if f.get("role") == "accompagnant" else S.DIVER])
        if "roles_form" in f:
            S.set_roles(t, p, _roles_from_form(f))
        if "groups_form" in f:
            S.set_person_groups(t, p.id, f.getlist("groups"))
        s.commit()
        return back(f"/v/{tid}/participants", f"{p.name} modifié")

    @app.post("/v/{tid}/roles")
    async def save_roles(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        org = f.get("organisateur") or ""
        db.snapshot("rôles du groupe")
        for p in t.participants:
            rs = [r for r in p.role_list if r in (S.DIVER, S.COMPANION)]
            if str(p.id) == org:
                rs.append(S.ORGANIZER)
            if f.get(f"co_{p.id}"):
                rs.append(S.CO_ORGANIZER)
            if f.get(f"dp_{p.id}"):
                rs.append(S.DP)
            keep_custom = [r for r in p.role_list if r.startswith(S.CUSTOM)]
            p.roles = ",".join([r for r in S.ROLES if r in rs] + keep_custom)
        s.commit()
        return back(f"/v/{tid}/participants/organisation#equipe", "Équipe enregistrée")

    @app.post("/v/{tid}/familles/{fid}")
    async def edit_family(tid: int, fid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        fam = next((x for x in t.families if x.id == fid), None)
        if fam is None:
            raise HTTPException(404, "Famille introuvable")
        f = await request.form()
        if "members" in f:                      # onglet Organisation : nom, membres, qui paie
            db.snapshot(f"modification de la famille {fam.name}")
            try:
                S.save_family(s, t, f.get("name"), f.getlist("members"), f.get("referent"), fam)
            except ValueError as e:
                s.rollback()
                return back(f"/v/{tid}/participants/organisation#familles", str(e))
            s.commit()
            return back(f"/v/{tid}/participants/organisation#familles", f"Famille {fam.name} modifiée")
        name = (f.get("name") or "").strip()
        if name.lower().startswith("famille "):
            name = name[8:].strip()
        other = S.find_family(t, name) if name else None
        if other is not None and other.id != fam.id:
            return back(f"/v/{tid}/participants", f"Il y a déjà une famille {other.name}")
        db.snapshot(f"modification de la famille {fam.name}")
        if name:
            fam.name = name
        ref = f.get("referent")
        if ref and int(ref) in {m.id for m in fam.members}:
            fam.referent_id = int(ref)
        s.commit()
        return back(f"/v/{tid}/participants", f"Famille {fam.name} modifiée")

    @app.post("/v/{tid}/participants/{pid}/supprimer")
    def delete_participant(tid: int, pid: int, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        p = s.get(M.Participant, pid)
        if not p or p.trip_id != tid:
            raise HTTPException(404, "Participant introuvable")
        used = (any(m.participant_id == pid for m in t.movements)
                or any(e.payer_id == pid or any(x.payer_id == pid for x in e.payments) for e in t.expenses)
                or any(pid in sub.members for sub in t.subaccounts))
        if used:
            return back(f"/v/{tid}/participants",
                        f"{p.name} a déjà des versements ou des avances : utilise plutôt « Quelqu'un se désiste »")
        db.snapshot(f"suppression de {p.name}")
        S.set_family(s, t, p, None)
        s.query(M.ExpenseShare).filter_by(participant_id=pid).delete()
        s.query(M.Adjustment).filter((M.Adjustment.participant_id == pid) |
                                     (M.Adjustment.compensator_id == pid)).delete()
        s.query(M.Withdrawal).filter_by(participant_id=pid).delete()
        s.delete(p)
        s.commit()
        return back(f"/v/{tid}/participants", f"{p.name} retiré du voyage")

    # ------------------------------------------------------------------ comptes par personne
    @app.get("/v/{tid}/p/{pid}")
    def person(tid: int, pid: int, request: Request, s: Session = Depends(get_s)):
        """Ancienne fiche : l'onglet Comptes, filtré sur cette personne (message et « Défaire » conservés)."""
        t = trip_or_404(s, tid)
        p = s.get(M.Participant, pid)
        if not p or p.trip_id != tid:
            raise HTTPException(404, "Participant introuvable")
        extra = "".join(f"&{k}={quote(v)}" for k, v in request.query_params.items() if k in ("msg", "d", "r"))
        return RedirectResponse(f"/v/{tid}/participants/comptes?p={pid}{extra}#p{pid}", status_code=303)

    @app.get("/v/{tid}/participants/comptes", response_class=HTMLResponse)
    def people_accounts(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        mode = mode_of(request)
        r = S.compute_trip(t, mode)
        rp = S.compute_trip(t, E.PLANNED)
        sends = _send_rows(t, r)
        wds = {w.participant_id: w for w in t.withdrawals}
        blocks = []
        for p in sorted(t.participants, key=lambda x: E.sort_key(x.name)):
            subs = [sub for sub in t.subaccounts if p.id in sub.members]
            blocks.append(dict(
                p=p, acc=r.account_of(p.id), me=r.people[p.id], account=S.person_account(t, r, p.id),
                adjs=[a for a in t.adjustments if a.participant_id == p.id], wd=wds.get(p.id), subs=subs,
                sub_bal={sub.id: S.sub_view(sub)["balances"].get(p.id, 0) for sub in subs},
                send=next(x for x in sends if any(m.id == p.id for m in x["acc"].members)),
                planned_share=rp.people[p.id].share, actual_share=r.people[p.id].share))
        wanted = {int(x) for x in request.query_params.getlist("p") if x.isdigit()}
        selected = wanted or {p.id for p in t.participants}
        return render(request, "comptes.html", trip=t, r=r, blocks=blocks, selected=selected,
                      from_url=bool(wanted), today=dt.date.today().isoformat())

    # ------------------------------------------------------------------ dépenses
    @app.get("/v/{tid}/depenses", response_class=HTMLResponse)
    def expenses(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        r = S.compute_trip(t, mode_of(request))
        groups = []
        order = lambda e: (not e.subcategory, E.sort_key(e.subcategory or ""), e.id)    # « Autres » en dernier
        for c in t.categories:
            es = sorted([e for e in t.expenses if e.category_id == c.id], key=order)
            groups.append((c, es))
        orphans = sorted([e for e in t.expenses if e.category_id is None], key=order)
        if orphans:
            groups.append((None, orphans))
        # sous-totaux par sous-catégorie ; dans une catégorie qui en a, les dépenses sans sous-catégorie forment « Autres »
        with_subs = {e.category_id for e in t.expenses if e.subcategory}
        subtotals = {}
        for e in t.expenses:
            if e.subcategory or e.category_id in with_subs:
                k = (e.category_id, e.subcategory or "")
                subtotals[k] = subtotals.get(k, 0) + r.expense_totals.get(e.id, 0)
        # contrôle : une dépense dont la répartition ne tombe pas juste (rare) est signalée sur sa ligne
        bad = {x["id"] for x in S.ledger(t, r)["expenses"] if not x["ok"] and not x["pending"]}
        return render(request, "expenses.html", trip=t, r=r, groups=groups, nb=len(t.participants), bad=bad,
                      who_label=S.who_label, who_detail=S.who_detail, payer_label=S.payer_label, names=names(t),
                      subtotals=subtotals,
                      with_subs=with_subs)

    def _expense_form_ctx(t, e=None):
        weights = {s_.participant_id: s_.weight for s_ in e.shares} if e else {p.id: 1.0 for p in t.participants}
        fixed = {s_.participant_id: s_.fixed for s_ in e.shares if s_.fixed is not None} if e else {}
        split_mode = "egal"
        total = e.best_known() if e else 0
        free = {p: w for p, w in weights.items() if p not in fixed}
        rest = total - sum(fixed.values())
        amounts = dict(fixed)
        if rest > 0 and free:
            amounts.update(E.split_exact(rest, free))
        linked = {l.receipt_id for l in e.receipt_links} if e else set()
        rank = {S.ORGANIZER: 0, S.CO_ORGANIZER: 1}
        payers = sorted(t.participants, key=lambda p: min([rank.get(r, 9) for r in p.role_list] or [9]))
        from sqlalchemy.orm import object_session
        subcats = S.subcategories_for(object_session(t), t)
        def chip(label, members):
            return dict(label=label, ids=[p.id for p in members], names=", ".join(p.name for p in members))
        who_roles = [chip(v, S.with_role(t, rk)) for rk, v in S.trip_roles(t).items() if S.with_role(t, rk)]
        who_families = [chip(f_.name, f_.members) for f_ in t.families if f_.members]
        who_groups = [chip(g["name"], g["members"]) for g in S.trip_groups(t) if g["members"]]
        # centimes d'arrondi : le tirage enregistré, et combien chacun en a déjà payé ailleurs (tirage équitable)
        import json as _json
        r_ = S.compute_trip(t)
        inf = r_.cent_info.get(e.id) if e else None
        cents = dict(mode=(e.cents_mode or "") if e else "", who=inf["winners"] if inf and inf["drawn"] else [],
                     counts=_json.dumps({str(k): v for k, v in S.cent_counts(r_, skip=e.id if e else None).items()}))
        if inf and inf["drawn"] and e:
            amounts = {p: r_.share(p, e.id) for p in weights}          # exactement ce que donne le calcul
        return dict(trip=t, e=e, weights=weights, linked=linked, groups=S.trip_groups(t),
                    who_roles=who_roles, who_families=who_families, who_groups=who_groups, payers=payers, subcats=subcats,
                    split_mode=split_mode, amounts=amounts, fixed=fixed, cents=cents)

    @app.get("/v/{tid}/depenses/nouvelle", response_class=HTMLResponse)
    def new_expense(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        if not t.participants:
            return back(f"/v/{tid}/participants", "Ajoute d'abord les participants")
        return render(request, "expense_form.html", **_expense_form_ctx(t),
                      today=dt.date.today().isoformat())

    @app.get("/v/{tid}/depenses/{eid}/modifier", response_class=HTMLResponse)
    def edit_expense_form(tid: int, eid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        e = s.get(M.Expense, eid)
        if not e or e.trip_id != tid:
            raise HTTPException(404, "Dépense introuvable")
        return render(request, "expense_form.html", **_expense_form_ctx(t, e),
                      today=dt.date.today().isoformat())

    def _parse_shares(t, f) -> tuple:
        """Personnes cochées -> (parts, montants fixés 🔒 en centimes).
        Une case « fx_<id> » cochée = le montant « m_<id> » est fixé ; les autres se partagent le reste.
        (« split=montants » : ancien formulaire, tous les montants sont fixés.)"""
        weights, fixed = {}, {}
        all_fixed = f.get("split") == "montants"
        for p in t.participants:
            if not f.get(f"inc_{p.id}"):
                continue
            try:   # nombre de parts (1 par défaut ; 2 = double part…)
                w = float(E.to_weight(f.get(f"w_{p.id}") or "1"))
            except Exception:
                w = 1.0
            if all_fixed or f.get(f"fx_{p.id}"):
                try:
                    c_ = E.parse_amount(f.get(f"m_{p.id}"))
                except ValueError:
                    raise FormError(f"Montant invalide pour {p.name} : « {f.get(f'm_{p.id}')} ». Exemple : 12,50")
                if c_ is not None and c_ >= 0:
                    fixed[p.id] = c_
                    w = w if w > 0 else 1.0
            if w > 0:
                weights[p.id] = w
        return weights, fixed

    def _parse_weights(t, f) -> dict:
        return _parse_shares(t, f)[0]

    def weights_ok(t, f):
        return bool(_parse_weights(t, f))

    def _category_id(s, t, name):
        name = clean_cat_name(name) or "Divers"
        for c in t.categories:
            if E.sort_key(c.name) == E.sort_key(name):
                return c.id
        c = M.Category(name=name, audience="", position=len(t.categories))
        t.categories.append(c)
        S.add_to_library(s, name)
        s.flush()
        return c.id

    # ------------------------------------------------------------------ catégories
    def cat_or_404(t, cid):
        c = next((c for c in t.categories if c.id == cid), None)
        if c is None:
            raise HTTPException(404, "Catégorie introuvable")
        return c

    def clean_cat_name(name):
        return re.sub(r"\s+", " ", (name or "").strip())[:100]

    def add_cat_to_trip(t, name):
        t.categories.append(M.Category(name=name, audience="", position=len(t.categories)))

    @app.get("/v/{tid}/categories", response_class=HTMLResponse)
    def categories(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        have = {E.sort_key(c.name) for c in t.categories}
        available = [n for n in S.category_library(s) if E.sort_key(n) not in have]
        custom = {E.sort_key(n) for n in S.category_library(s) if S.is_custom_category(s, n)}
        return render(request, "categories.html", trip=t, available=available, custom=custom, sort_key=E.sort_key,
                      subcats=S.subcategories_for(s, t))

    @app.post("/v/{tid}/categories/{cid}/sous-categories")
    async def add_subcat(tid: int, cid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        c = cat_or_404(t, cid)
        name = clean_cat_name((await request.form()).get("name"))
        if not name:
            return back(f"/v/{tid}/categories#c{cid}", "Donne un nom à la sous-catégorie")
        if E.sort_key(name) in {E.sort_key(x) for x in S.subcategories_for(s, t).get(c.name, [])}:
            return back(f"/v/{tid}/categories#c{cid}", f"« {name} » existe déjà dans « {c.name} »")
        db.snapshot(f"sous-catégorie « {name} » ajoutée à « {c.name} »")
        S.add_subcategory(s, c.name, name)
        s.commit()
        return back(f"/v/{tid}/categories#c{cid}", f"Sous-catégorie « {name} » ajoutée à « {c.name} »")

    @app.post("/v/{tid}/categories/{cid}/sous-categories/supprimer")
    async def del_subcat(tid: int, cid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        c = cat_or_404(t, cid)
        name = clean_cat_name((await request.form()).get("name"))
        db.snapshot(f"sous-catégorie « {name} » supprimée")
        S.remove_subcategory(s, c.name, name)
        used = [e for e in t.expenses if e.category_id == c.id and E.sort_key(e.subcategory or "") == E.sort_key(name)]
        for e in used:
            e.subcategory = ""
        s.commit()
        msg = f"Sous-catégorie « {name} » supprimée"
        if used:
            msg += f" ; ses {len(used)} dépense(s) restent dans « {c.name} »"
        return back(f"/v/{tid}/categories#c{cid}", msg)

    @app.post("/v/{tid}/categories")
    async def new_category(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        name = clean_cat_name((await request.form()).get("name"))
        if not name:
            return back(f"/v/{tid}/categories", "Donne un nom à la catégorie")
        if any(E.sort_key(c.name) == E.sort_key(name) for c in t.categories):
            return back(f"/v/{tid}/categories", f"« {name} » est déjà dans ce voyage")
        if E.sort_key(name) in {E.sort_key(n) for n in S.category_library(s)}:
            return back(f"/v/{tid}/categories", f"« {name} » est déjà dans les catégories disponibles")
        db.snapshot(f"nouvelle catégorie « {name} »")
        S.add_to_library(s, name)          # dans les disponibles seulement : on l'ajoute au voyage d'un clic
        s.commit()
        return back(f"/v/{tid}/categories",
                    f"« {name} » créée dans les catégories disponibles : clique dessus pour l'ajouter à ce voyage")

    @app.post("/v/{tid}/categories/ajouter")
    async def take_category(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        name = clean_cat_name((await request.form()).get("name"))
        if not name or any(E.sort_key(c.name) == E.sort_key(name) for c in t.categories):
            return back(f"/v/{tid}/categories")
        db.snapshot(f"catégorie « {name} » ajoutée au voyage")
        add_cat_to_trip(t, name)
        s.commit()
        return back(f"/v/{tid}/categories", f"« {name} » ajoutée à ce voyage")

    @app.post("/v/{tid}/categories/{cid}/retirer")
    def drop_category(tid: int, cid: int, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        c = cat_or_404(t, cid)
        used = [e for e in t.expenses if e.category_id == c.id]
        target = None
        if used:
            target = next((o for o in t.categories if E.sort_key(o.name) == "divers" and o.id != c.id), None)
            if target is None:
                if E.sort_key(c.name) == "divers":
                    return back(f"/v/{tid}/categories", "« Divers » est utilisée par des dépenses : change d'abord "
                                                        "leur catégorie")
                db.snapshot(f"catégorie « {c.name} » retirée du voyage")
                target = M.Category(name="Divers", audience="", position=len(t.categories))
                t.categories.append(target)
                s.flush()
            else:
                db.snapshot(f"catégorie « {c.name} » retirée du voyage")
        else:
            db.snapshot(f"catégorie « {c.name} » retirée du voyage")
        for e in used:
            e.category_id = target.id
        t.categories.remove(c)
        s.delete(c)
        for i, o in enumerate(sorted(t.categories, key=lambda x: (x.position, x.id))):
            o.position = i
        s.commit()
        msg = f"« {c.name} » retirée de ce voyage"
        if used:
            msg += f" ; ses {len(used)} dépense(s) sont maintenant dans « Divers »"
        return back(f"/v/{tid}/categories", msg)

    @app.post("/v/{tid}/categories/oublier")
    async def forget_category(tid: int, request: Request, s: Session = Depends(get_s)):
        trip_or_404(s, tid)
        name = clean_cat_name((await request.form()).get("name"))
        if not S.is_custom_category(s, name):
            return back(f"/v/{tid}/categories", "Seules les catégories que tu as créées peuvent être supprimées")
        db.snapshot(f"catégorie « {name} » supprimée de la liste")
        S.remove_from_library(s, name)
        s.commit()
        return back(f"/v/{tid}/categories", f"« {name} » n'est plus proposée (les voyages qui l'utilisent la gardent)")

    @app.post("/v/{tid}/depenses/apercu", response_class=HTMLResponse)
    async def expense_preview(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        weights, fixed = _parse_shares(t, f)
        def total_of(v):          # « 80,12 + 69,10 » accepté (plusieurs tickets)
            parts = [p_ for p_ in str(v or "").split("+") if p_.strip()]
            return sum(E.parse_amount(p_) or 0 for p_ in parts) if parts else None
        try:
            planned = total_of(f.get("planned"))
            actual = total_of(f.get("actual"))
        except ValueError:
            return HTMLResponse('<p class="muted">Montant invalide.</p>')
        amt = actual if actual is not None else planned
        if not amt:
            return HTMLResponse('<p class="muted">Indique un montant pour voir la part de chacun.</p>')
        if not weights:
            return HTMLResponse('<p class="warn-text">Coche au moins une personne.</p>')
        kind = "recette" if f.get("kind") == "recette" else "depense"
        r = E.compute([E.Participant(p.id, p.name, p.family_id, bool(p.companion)) for p in t.participants],
                      [E.Expense(0, f.get("label") or "Cette dépense", "", planned, actual, None, weights, kind,
                                 fixed=fixed)], rounding=t.rounding or E.ROUND_PARTICIPANTS)
        return render(request, "_expense_preview.html", trip=t, r=r, amount=amt, weights=weights)

    @app.post("/v/{tid}/depenses")
    @app.post("/v/{tid}/depenses/{eid}")
    async def save_expense(tid: int, request: Request, eid: int | None = None, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        label = (f.get("label") or "").strip()
        if not label:
            return back(f"/v/{tid}/depenses/nouvelle", "Donne un nom à la dépense")
        # Plusieurs tickets : lignes « ticket_amount / ticket_date », ou « 80,12 + 69,10 » tapé dans le montant
        tickets = []
        for a_, d_ in zip(f.getlist("ticket_amount"), f.getlist("ticket_date")):
            c_ = amount({"x": a_}, "x")
            if c_:
                tickets.append((c_, S.parse_date(d_)))
        raw_actual = str(f.get("actual") or "")
        if not tickets and "+" in raw_actual.lstrip("+"):
            tickets = [(c_, None) for c_ in (amount({"x": p_}, "x") for p_ in raw_actual.split("+")) if c_]
        raw_planned = str(f.get("planned") or "")
        planned = (sum(amount({"x": p_}, "x") or 0 for p_ in raw_planned.split("+")) or None) \
            if "+" in raw_planned.lstrip("+") else amount(f, "planned")
        actual = sum(c_ for c_, _ in tickets) if tickets else amount(f, "actual")
        ref = request.headers.get("referer", f"/v/{tid}/depenses/nouvelle")
        if not weights_ok(t, f):
            return back(ref, "Coche au moins une personne pour « Pour qui ? »")
        if planned is None and actual is None and not f.get("pending"):
            return back(ref, "Indique au moins un montant (prévu ou réel), ou coche « Montant pas encore connu »")
        weights, fixed = _parse_shares(t, f)
        if fixed:
            total_ = (actual if actual is not None else planned) or 0
            if not total_:
                return back(ref, "Indique le montant de la dépense avant de fixer des montants")
            refund_ = (amount(f, "refund") or 0) if f.get("has_refund") else 0
            total_ = max(0, total_ - refund_)          # c'est le coût, remboursement déduit, qui est partagé
            got = sum(fixed.values())
            if got > total_:
                what = "le coût à partager" if refund_ else "la dépense"
                return back(ref, f"Les montants fixés font {E.fmt(got)}, plus que {what} ({E.fmt(total_)})")
            if len(fixed) == len(weights) and got != total_:
                return back(ref, f"Il reste {E.fmt(total_ - got)} à répartir : tous les montants sont fixés. "
                                 f"Laisse au moins une personne sans montant fixé, ou corrige les montants.")
        payments = []
        if f.get("payer_kind") == "multi":
            for i in range(20):
                a_ = amount(f, f"ma_{i}")
                if a_:
                    who = f.get(f"mp_{i}") or "asso"
                    pid_ = None if who == "asso" else int(who)
                    if pid_ is not None and pid_ not in names(t):
                        raise FormError("Payeur inconnu")
                    payments.append((pid_, a_))
            if not payments:
                return back(ref, "Indique combien chaque payeur a payé")
            total_ = (actual if actual is not None else planned) or 0
            if total_ and sum(c for _, c in payments) > total_:
                return back(ref, f"Les payeurs totalisent {E.fmt(sum(c for _, c in payments))}, "
                                 f"plus que le montant ({E.fmt(total_)})")
        if eid:
            e = s.get(M.Expense, eid)
            if not e or e.trip_id != tid:
                raise HTTPException(404, "Dépense introuvable")
            db.snapshot(f"modification de la dépense « {e.label} »")
        else:
            db.snapshot(f"ajout de la dépense « {label} »")
            e = M.Expense(trip_id=tid, label=label)
            s.add(e)
        e.label = label
        cat = f.get("category")
        if cat == "__new__":
            cat = f.get("category_new")
        e.category_id = _category_id(s, t, cat)
        sub = f.get("subcategory") or ""
        if sub == "__new__":
            sub = f.get("subcategory_new") or ""
        e.subcategory = clean_cat_name(sub)
        if e.subcategory:
            s.flush()
            cname = next((c.name for c in t.categories if c.id == e.category_id), None)
            if cname:
                S.add_subcategory(s, cname, e.subcategory)
        e.planned, e.actual = planned, actual
        e.set_tickets(tickets)
        e.date = S.parse_date(f.get("date")) or next((d_ for _, d_ in tickets if d_), None)
        e.kind = "recette" if f.get("kind") == "recette" else "depense"
        refund = amount(f, "refund") if f.get("has_refund") else None
        if refund:
            if refund > ((actual if actual is not None else planned) or 0):
                raise FormError("Le montant remboursé dépasse le montant de la dépense")
            who_ = f.get("refund_to") or "asso"
            e.refund = refund
            e.refund_to_id = None if who_ == "asso" else int(who_)
        else:
            e.refund, e.refund_to_id = None, None
        payer = f.get("payer") or "asso"
        e.payer_id = None if (payer == "asso" or payments) else int(payer)
        e.note = f.get("note", "").strip()
        s.flush()
        for x in list(e.payments):
            e.payments.remove(x)
        if len(payments) == 1 and payments[0][1] == e.best_known():
            e.payer_id = payments[0][0]
        elif payments:
            for pid_, c in payments:
                e.payments.append(M.ExpensePayment(payer_id=pid_, amount=c))
        s.query(M.ExpenseShare).filter_by(expense_id=e.id).delete()
        for pid, w in weights.items():
            s.add(M.ExpenseShare(expense_id=e.id, participant_id=pid, weight=w, fixed=fixed.get(pid)))
        if "cents_mode" in f:          # centimes d'arrondi : le choix et le tirage vus à l'écran
            e.cents_mode = f.get("cents_mode") if f.get("cents_mode") in (E.ROUND_PARTICIPANTS, E.ROUND_ASSO) else ""
            e.cents_who = ",".join(str(x) for x in S.cents_ids(f.get("cents_who")))
        # justificatifs existants
        wanted = {int(x) for x in f.getlist("receipts")}
        s.query(M.ReceiptLink).filter_by(expense_id=e.id).delete()
        for rid in wanted:
            s.add(M.ReceiptLink(receipt_id=rid, expense_id=e.id))
        # nouveaux fichiers
        for up in f.getlist("files"):
            if getattr(up, "filename", None):
                rec = await _store_receipt(s, t, up)
                s.add(M.ReceiptLink(receipt_id=rec.id, expense_id=e.id))
        s.commit()
        if f.get("again"):
            return back(f"/v/{tid}/depenses/nouvelle", f"« {label} » enregistrée. Dépense suivante :")
        return back(f"/v/{tid}/depenses#e{e.id}", f"« {label} » enregistrée")

    @app.post("/v/{tid}/depenses/{eid}/supprimer")
    async def delete_expense(tid: int, eid: int, request: Request, s: Session = Depends(get_s)):
        trip_or_404(s, tid)
        e = s.get(M.Expense, eid)
        if not e or e.trip_id != tid:
            raise HTTPException(404, "Dépense introuvable")
        db.snapshot(f"suppression de la dépense « {e.label} »")
        s.query(M.Adjustment).filter_by(expense_id=eid).delete()
        s.delete(e)
        s.commit()
        nxt = (await request.form()).get("next") or ""
        return back(nxt if nxt.startswith(f"/v/{tid}/") else f"/v/{tid}/depenses", f"« {e.label} » supprimée")

    # ------------------------------------------------------------------ répartition
    @app.get("/v/{tid}/comptes")
    def ledger_moved(tid: int):
        # l'ancien « Tableau des comptes » : son tableau est devenu l'onglet Participants › Comptes
        return RedirectResponse(f"/v/{tid}/participants/soldes", status_code=303)

    @app.get("/v/{tid}/participants/export")
    def participants_export(tid: int, request: Request, s: Session = Depends(get_s)):
        """Liste des participants : format=csv (fichier pour Excel), tsv (à coller dans un tableur),
        txt (une personne par ligne, pour un message). cols=nom,telephone,… ; desistes=0 pour les exclure."""
        t = trip_or_404(s, tid)
        q = request.query_params
        fmt_ = q.get("format", "csv")
        cols = [c for c in (q.get("cols") or "").split(",") if c] or [k for k, _, on in S.EXPORT_COLS if on]
        r = S.compute_trip(t, mode_of(request))
        heads, rows = S.participants_export(t, r, cols, q.get("desistes", "1") != "0")
        money_cols = {i for i, c in enumerate(cols) if c in ("part", "verse", "solde")}

        def cell(i, v):
            if i not in money_cols or isinstance(v, str):
                return str(v)
            if fmt_ == "txt":
                if cols[i] == "solde":
                    return f"doit {E.fmt(v)}" if v > 0 else f"on lui doit {E.fmt(-v)}" if v < 0 else "à jour"
                return E.fmt(v)
            return f"{v / 100:.2f}".replace(".", ",")          # nombre, lisible par Excel en français

        if fmt_ == "txt":
            body = "\n".join(" · ".join(x for x in (cell(i, v) for i, v in enumerate(row)) if x) for row in rows)
            return Response(body, media_type="text/plain; charset=utf-8")
        if "solde" in cols:
            heads = [h + " (négatif = à lui rendre)" if c == "solde" else h for h, c in zip(heads, cols)]
        sep = "\t" if fmt_ == "tsv" else ";"

        def esc(x):
            x = x.replace("\t", " ").replace("\r", " ").replace("\n", " ")
            if sep == ";" and any(ch in x for ch in ';"'):
                x = '"' + x.replace('"', '""') + '"'
            return x
        lines = [sep.join(esc(h) for h in heads)] + [sep.join(esc(cell(i, v)) for i, v in enumerate(row)) for row in rows]
        body = "\r\n".join(lines) + "\r\n"
        if fmt_ == "tsv":
            return Response(body, media_type="text/plain; charset=utf-8")
        name = re.sub(r"[^\w-]+", "_", f"participants {t.name}").strip("_")
        return Response("\ufeff" + body, media_type="text/csv; charset=utf-8",          # BOM : accents corrects dans Excel
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}.csv"})

    @app.get("/v/{tid}/participants/soldes", response_class=HTMLResponse)
    def ledger(tid: int, request: Request, remise: int | None = None, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        if remise is not None:              # anciens liens : l'onglet Remises / suppléments
            return RedirectResponse(f"/v/{tid}/participants/remises?remise={remise}", status_code=303)
        r = S.compute_trip(t, mode_of(request))
        tags = {}
        for a in t.adjustments:
            if a.expense_id is None:
                tags[a.participant_id] = tags.get(a.participant_id, 0) + a.value
        return render(request, "ledger.html", trip=t, r=r, L=S.ledger(t, r), remise_tags=tags)

    @app.get("/v/{tid}/participants/remises", response_class=HTMLResponse)
    def remises_page(tid: int, request: Request, remise: int | None = None, rm: int | None = None,
                     s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        remises = sorted([a for a in t.adjustments if a.expense_id is None], key=lambda a: E.sort_key(a.participant.name))
        old = [a for a in t.adjustments if a.expense_id is not None]
        edit = next((a for a in remises if a.id == rm), None) if rm else None
        return render(request, "remises.html", trip=t, remises=remises, old_adjs=old, remise_edit=edit,
                      remise_for=remise, open_remise=bool(remise or edit or not remises))

    @app.post("/v/{tid}/remises")
    async def remise_save(tid: int, request: Request, s: Session = Depends(get_s)):
        """Remise (−) ou supplément (+) sur tout le voyage d'une personne, compensé par l'asso ou par les autres."""
        t = trip_or_404(s, tid)
        f = await request.form()
        here = f"/v/{tid}/participants/remises"
        pid = int(f.get("participant") or 0) if (f.get("participant") or "").isdigit() else 0
        if pid not in names(t):
            return back(here + "?remise=0", "Choisis la personne")
        try:
            val = E.parse_amount(f.get("value"))
        except ValueError:
            return back(here + f"?remise={pid}", f"Montant invalide : « {f.get('value')} ». Exemple : 50")
        if not val or val <= 0:
            return back(here + f"?remise={pid}", "Indique un montant (ex. 50)")
        aid = f.get("aid") or ""
        a = next((x for x in t.adjustments if str(x.id) == aid and x.expense_id is None), None) if aid else None
        who = names(t)[pid]
        kind = "Supplément" if f.get("kind") == "increase" else "Remise"
        db.snapshot(f"{kind.lower()} pour {who}")
        if a is None:
            a = M.Adjustment(trip_id=tid, expense_id=None, fixed=False)
            s.add(a)
        a.participant_id = pid
        a.value = (1 if kind == "Supplément" else -1) * abs(val)
        a.compensation = E.ASSO if f.get("by") == "asso" else E.OTHERS
        a.compensator_id = None
        a.reason = (f.get("reason") or "").strip()
        s.commit()
        return back(here, f"{kind} de {E.fmt(abs(val))} pour {who} enregistré" + ("e" if kind == "Remise" else ""))

    @app.post("/v/{tid}/remises/{aid}/supprimer")
    def remise_delete(tid: int, aid: int, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        a = next((x for x in t.adjustments if x.id == aid), None)
        if a is None:
            raise HTTPException(404, "Remise introuvable")
        db.snapshot(f"suppression d'une remise de {a.participant.name}")
        s.delete(a)
        s.commit()
        return back(f"/v/{tid}/participants/remises", "Supprimé")

    @app.get("/v/{tid}/en-images", response_class=HTMLResponse)
    def visuals(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        r = S.compute_trip(t, mode_of(request))
        return render(request, "viz.html", trip=t, r=r, V=S.trip_visuals(t, r))

    @app.get("/v/{tid}/asso", response_class=HTMLResponse)
    def asso_page(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        r = S.compute_trip(t, mode_of(request))
        return render(request, "asso.html", trip=t, r=r, A=S.asso_account(t, r), cashier=S.cashier(t),
                      asso_custom=S.trip_asso_name(t) != "l'asso", asso_real=S.trip_asso_name(t))

    @app.post("/v/{tid}/asso/nom")
    async def asso_rename(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        name = str((await request.form()).get("name") or "")
        S.set_trip_asso_name(t, name, s)
        s.commit()
        return back(f"/v/{tid}/asso", f"Dans ce voyage, l'asso s'appelle maintenant « {S.trip_asso_name(t)} »"
                    if name.strip() else "Nom de l'asso effacé pour ce voyage")

    @app.post("/v/{tid}/asso/caisse")
    async def asso_cashier(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        if f.get("cashier") == "p":
            pid = int(f.get("participant") or 0)
            if pid not in {p.id for p in t.participants}:
                return back(f"/v/{tid}/asso", "Choisis le participant qui tient la caisse")
            t.cashier_id = pid
        else:
            t.cashier_id = None
        s.commit()
        c = S.cashier(t)
        return back(f"/v/{tid}/asso", f"C'est {c.name} qui tient la caisse" if c else
                    f"C'est {S.trip_asso_name(t)} qui tient la caisse")

    @app.post("/v/{tid}/asso/arrondi")
    async def asso_rounding(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        v = (await request.form()).get("rounding")
        if v not in (E.ROUND_PARTICIPANTS, E.ROUND_ASSO):
            return back(f"/v/{tid}/asso", "Choix d'arrondi inconnu")
        if v != (t.rounding or E.ROUND_PARTICIPANTS):
            db.snapshot("changement de qui paie les centimes d'arrondi")
            t.rounding = v
            s.commit()
        caisse = S.caisse_label(t)
        return back(f"/v/{tid}/asso", "Les centimes d'arrondi sont tirés au sort parmi les participants" if v == E.ROUND_PARTICIPANTS
                    else f"Les centimes d'arrondi sont pris en charge par {caisse}")

    @app.get("/v/{tid}/repartition", response_class=HTMLResponse)
    def matrix(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        r = S.compute_trip(t, mode_of(request))
        has_global = any(l.expense is None for l in r.lines.values())
        # par défaut : sous-totaux ; « ▸ catégorie » déplie une catégorie (?o=id), « Tout déplier » = ?vue=detail
        all_open = request.query_params.get("vue") == "detail"
        opened = {int(x) for x in request.query_params.getlist("o") if x.isdigit()}
        compact = not all_open
        OTHER = "Autres (sans sous-catégorie)"
        # colonnes : dépenses (détail), « Σ sous-catégorie », « Total catégorie »
        cols = []
        for c in t.categories:
            es = sorted([e for e in t.expenses if e.category_id == c.id],
                        key=lambda e: (not e.subcategory, E.sort_key(e.subcategory or ""), e.id))
            if not es:
                continue
            grouped = any(e.subcategory for e in es)
            compact = not (all_open or c.id in opened)
            if grouped:
                subs = []
                for e in es:
                    k = e.subcategory or OTHER
                    if not subs or subs[-1][0] != k:
                        subs.append((k, []))
                    subs[-1][1].append(e)
                for k, ges in subs:
                    if not compact:
                        cols += [dict(kind="exp", cat=c.name, sub=k, e=x, eids=[x.id], label=x.label) for x in ges]
                    if len(subs) > 1:                     # une seule sous-catégorie : Σ = total, on ne répète pas
                        cols.append(dict(kind="sub", cat=c.name, sub=k, eids=[x.id for x in ges], label=f"Σ {k}"))
                cols.append(dict(kind="cat", cat=c.name, sub=None, eids=[x.id for x in es], label=f"Total {c.name}"))
            else:
                if not compact:
                    cols += [dict(kind="exp", cat=c.name, sub=None, e=x, eids=[x.id], label=x.label) for x in es]
                if compact or len(es) > 1:
                    cols.append(dict(kind="cat", cat=c.name, sub=None, eids=[x.id for x in es],
                                     label=f"Total {c.name}"))
        cat_ids = {c.name: c.id for c in t.categories}
        for col in cols:
            col["total"] = sum(r.expense_totals.get(i, 0) for i in col["eids"])
        # en-têtes regroupés : catégories, puis sous-catégories
        head1, head2 = [], []
        prev = None
        for col in cols:
            col["start"] = col["cat"] != prev          # 1re colonne d'une catégorie : trait de séparation
            prev = col["cat"]
            key = (col["cat"], col["sub"] if col["kind"] != "cat" else "__total__")
            # 1re colonne d'une sous-catégorie (ou du total) dans la catégorie : trait en pointillé
            col["sub_start"] = not col["start"] and bool(head2) and head2[-1]["key"] != key
            if head1 and head1[-1]["cat"] == col["cat"]:
                head1[-1]["span"] += 1
            else:
                cid = cat_ids.get(col["cat"])
                is_open = all_open or cid in opened
                other = sorted(opened ^ {cid})
                ndetail = sum(1 for e in t.expenses if e.category_id == cid)
                head1.append(dict(cat=col["cat"], span=1, open=is_open, band=len(head1) % 2,
                                  toggle=None if all_open or ndetail < 2 else
                                  "?" + "&".join(f"o={i}" for i in other)))
            if head2 and head2[-1]["key"] == key:
                head2[-1]["span"] += 1
            else:
                head2.append(dict(key=key, label=(col["sub"] or "") if col["kind"] != "cat" else "", span=1,
                                  start=col["start"], sub_start=col["sub_start"]))
        return render(request, "matrix.html", trip=t, r=r, cols=cols, head1=head1, head2=head2,
                      all_open=all_open, any_open=bool(opened),
                      has_global=has_global, has_subs=any(c["kind"] == "sub" for c in cols),
                      withdrawn={w.participant_id for w in t.withdrawals})

    # ------------------------------------------------------------------ ajustements
    @app.get("/v/{tid}/ajuster")
    def adjust_moved(tid: int, p: int | None = None, e: str | None = None):
        """L'ancienne page « Ajuster une part » : un montant précis se fixe dans la dépense,
        une remise sur tout le voyage dans Participants › Remises / suppléments."""
        if e and e.isdigit():
            return RedirectResponse(f"/v/{tid}/depenses/{e}/modifier#who", status_code=303)
        if p:
            return RedirectResponse(f"/v/{tid}/participants/remises?remise={p}", status_code=303)
        return RedirectResponse(f"/v/{tid}/participants", status_code=303)

    @app.post("/v/{tid}/ajuster/{aid}/supprimer")
    def adjust_delete(tid: int, aid: int, request: Request, s: Session = Depends(get_s)):
        trip_or_404(s, tid)
        a = s.get(M.Adjustment, aid)
        if not a or a.trip_id != tid:
            raise HTTPException(404, "Ajustement introuvable")
        db.snapshot(f"suppression d'un ajustement de {a.participant.name}")
        s.delete(a)
        s.commit()
        ref = urlparse(request.headers.get("referer") or f"/v/{tid}/participants").path
        return back(ref, "Ajustement supprimé")

    # ------------------------------------------------------------------ désistement
    @app.get("/v/{tid}/desistement", response_class=HTMLResponse)
    def withdraw_page(tid: int, request: Request, p: int | None = None, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        r = S.compute_trip(t, mode_of(request))
        withdrawn = {w.participant_id for w in t.withdrawals}
        person_ = s.get(M.Participant, p) if p else None
        if person_ and person_.trip_id != tid:
            person_ = None
        exps = [e for e in t.expenses if any(sh.participant_id == p for sh in e.shares)] if person_ else []
        cur = next((w for w in t.withdrawals if person_ and w.participant_id == person_.id), None)
        # parts « si elle restait » : calcul sans son désistement (pour montrer ce qu'elle garde ou non)
        inputs = S.engine_inputs(t)
        r_full = E.compute(**{**inputs, "withdrawals": [w for w in inputs["withdrawals"]
                                                         if not person_ or w.participant != person_.id]},
                           mode=mode_of(request)) if person_ else r
        return render(request, "withdraw.html", trip=t, r=r, r_full=r_full, sel=person_, exps=exps,
                      withdrawn=withdrawn, wcur=cur, today=dt.date.today().isoformat())

    def _withdraw_from_form(t, f):
        pid = int(f.get("participant") or 0)
        kept = {int(x) for x in f.getlist("kept")}
        return E.Withdrawal(pid, f.get("absorber", E.GROUP), kept, f.get("date", ""))

    @app.post("/v/{tid}/desistement/apercu", response_class=HTMLResponse)
    async def withdraw_preview(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        w = _withdraw_from_form(t, f)
        if not w.participant:
            return HTMLResponse('<p class="muted">Choisis une personne.</p>')
        mode = mode_of(request)
        inputs = S.engine_inputs(t)
        r0 = E.compute(**inputs, mode=mode)                     # aujourd'hui (avec un éventuel désistement déjà noté)
        others = [x for x in inputs["withdrawals"] if x.participant != w.participant]
        r1 = E.compute(**{**inputs, "withdrawals": others + [w]}, mode=mode)
        return render(request, "_diff.html", trip=t, r0=r0, r1=r1, eid=None, wpid=w.participant)

    @app.post("/v/{tid}/desistement")
    async def withdraw_save(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        w = _withdraw_from_form(t, f)
        p = s.get(M.Participant, w.participant)
        if not p or p.trip_id != tid:
            return back(f"/v/{tid}/desistement", "Choisis une personne")
        cur = next((x for x in t.withdrawals if x.participant_id == p.id), None)
        db.snapshot(f"{'modification du ' if cur else ''}désistement de {p.name}")
        if cur is None:
            cur = M.Withdrawal(trip_id=tid, participant_id=p.id)
            s.add(cur)
        cur.date = S.parse_date(w.date)
        cur.absorber = w.absorber
        cur.kept = ",".join(str(x) for x in sorted(w.kept))
        cur.note = (f.get("note") or "").strip()
        edited = cur.id is not None
        refund = amount(f, "refund")
        if refund and refund > 0:
            s.add(M.Movement(trip_id=tid, participant_id=p.id, kind="remboursement", amount=refund,
                             date=S.parse_date(w.date), note="Remboursement désistement"))
        s.commit()
        return back(f"/v/{tid}/p/{p.id}", f"Désistement de {p.name} {'modifié' if edited else 'enregistré'}")

    @app.post("/v/{tid}/desistement/{pid}/annuler")
    async def withdraw_cancel(tid: int, pid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        nxt = str((await request.form()).get("next") or "")
        w = next((x for x in t.withdrawals if x.participant_id == pid), None)
        if not w:
            return back(f"/v/{tid}/p/{pid}", "Pas de désistement à annuler")
        db.snapshot(f"retour de {w.participant.name} dans le voyage")
        s.delete(w)
        s.commit()
        return back(nxt if nxt.startswith(f"/v/{tid}/") else f"/v/{tid}/p/{pid}",
                    "Désistement annulé : la personne est de nouveau dans le voyage")

    # ------------------------------------------------------------------ argent reçu / rendu
    @app.get("/v/{tid}/argent/mouvements", response_class=HTMLResponse)
    def money_moves(tid: int, request: Request, s: Session = Depends(get_s)):
        return money(tid, request, s, tab="mouvements")

    @app.get("/v/{tid}/argent", response_class=HTMLResponse)
    def money(tid: int, request: Request, s: Session = Depends(get_s), tab: str = "regler"):
        t = trip_or_404(s, tid)
        if request.query_params.get("k") or request.query_params.get("p"):   # lien « Enregistrer un versement de … »
            tab = "mouvements"
        r = S.compute_trip(t, mode_of(request))
        rows = []
        for m in t.movements:
            if m.kind == "versement":
                rows.append(dict(date=m.date, label=f"Versement de {m.participant.name}", inc=m.amount, out=None,
                                 note=m.note, mid=m.id, m=m))
            else:
                rows.append(dict(date=m.date, label=f"Remboursement à {m.participant.name}", inc=None,
                                 out=m.amount, note=m.note, mid=m.id, m=m))
        for e in t.expenses:
            if e.actual is None:
                continue
            asso_part = sum(c for pid, c in e.splits() if pid is None)
            if not asso_part:
                continue
            part = " (part payée par l'asso)" if e.multi_payer and not e.is_income else ""
            if e.is_income:
                rows.append(dict(date=e.date, label=f"Reçu : {e.label}", inc=asso_part, out=None,
                                 note=e.category.name if e.category else "", eid=e.id))
            else:
                rows.append(dict(date=e.date, label=f"Paiement : {e.label}{part}", inc=None, out=asso_part,
                                 note=e.category.name if e.category else "", eid=e.id))
        rows.sort(key=lambda x: (x["date"] or dt.date.min))
        total_in = sum(x["inc"] or 0 for x in rows)
        total_out = sum(x["out"] or 0 for x in rows)
        advances = S.advances(t)
        pending = [e for e in t.expenses if e.pending]
        to_pay = [e for e in t.expenses if e.actual is None and not e.pending and not e.is_income
                  and any(pid is None for pid, _ in e.splits())]
        return render(request, "money.html", trip=t, r=r, rows=rows, total_in=total_in, total_out=total_out,
                      advances=advances, to_pay=to_pay, pending=pending, payer_label=S.payer_label, names=names(t), today=dt.date.today().isoformat(),
                      sel=request.query_params.get("p"), kind=request.query_params.get("k", "versement"),
                      prefill=request.query_params.get("a", ""), tab=tab,
                      open_form=tab == "mouvements" and bool(request.query_params.get("k") or request.query_params.get("p")))

    @app.post("/v/{tid}/argent")
    async def money_add(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        pid = int(f.get("participant") or 0)
        amt = amount(f, "amount")
        kind = f.get("kind", "versement")
        if pid not in names(t) or not amt or amt <= 0 or kind not in ("versement", "remboursement"):
            return back(f"/v/{tid}/argent/mouvements?k={kind}", "Choisis une personne et un montant positif")
        who = names(t)[pid]
        db.snapshot(f"{kind} de {E.fmt(amt)} ({who})")
        s.add(M.Movement(trip_id=tid, participant_id=pid, kind=kind, amount=amt,
                         date=S.parse_date(f.get("date")), note=(f.get("note") or "").strip()))   # pas de date d'office
        s.commit()
        dest = f.get("next") or f"/v/{tid}/argent"
        if not dest.startswith("/"):
            dest = f"/v/{tid}/argent"
        txt = f"Versement de {E.fmt(amt)} par {who} enregistré" if kind == "versement" else \
            f"Remboursement de {E.fmt(amt)} à {who} enregistré"
        return back(dest, txt)

    @app.post("/v/{tid}/argent/{mid}")
    async def money_edit(tid: int, mid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        m = s.get(M.Movement, mid)
        if not m or m.trip_id != tid:
            raise HTTPException(404, "Mouvement introuvable")
        f = await request.form()
        pid = int(f.get("participant") or m.participant_id)
        amt = amount(f, "amount")
        kind = f.get("kind", m.kind)
        if pid not in names(t) or not amt or amt <= 0 or kind not in ("versement", "remboursement"):
            return back(f"/v/{tid}/argent#m{mid}", "Choisis une personne et un montant positif")
        db.snapshot(f"modification d'un {m.kind} de {m.participant.name}")
        m.participant_id, m.amount, m.kind = pid, amt, kind
        m.date = S.parse_date(f.get("date")) or m.date
        m.note = (f.get("note") or "").strip()
        s.commit()
        return back(f"/v/{tid}/argent", f"{'Versement' if kind == 'versement' else 'Remboursement'} de "
                                         f"{E.fmt(amt)} ({names(t)[pid]}) modifié")

    @app.post("/v/{tid}/argent/{mid}/supprimer")
    def money_delete(tid: int, mid: int, request: Request, s: Session = Depends(get_s)):
        trip_or_404(s, tid)
        m = s.get(M.Movement, mid)
        if not m or m.trip_id != tid:
            raise HTTPException(404, "Mouvement introuvable")
        db.snapshot(f"suppression d'un {m.kind} de {m.participant.name}")
        s.delete(m)
        s.commit()
        ref = urlparse(request.headers.get("referer") or f"/v/{tid}/argent").path
        return back(ref, "Mouvement supprimé")

    # ------------------------------------------------------------------ justificatifs
    async def _store_receipt(s, t, up) -> M.Receipt:
        data = await up.read()
        ext = Path(up.filename).suffix.lower()[:10]
        stored = f"{t.id}_{uuid.uuid4().hex}{ext}"
        (db.receipts_dir / stored).write_bytes(data)
        number = max([r.number for r in t.receipts] or [0]) + 1
        rec = M.Receipt(trip_id=t.id, number=number, original_name=Path(up.filename).name, stored_name=stored,
                        content_type=up.content_type or mimetypes.guess_type(up.filename)[0] or "")
        t.receipts.append(rec)
        s.flush()
        return rec

    @app.get("/v/{tid}/justificatifs", response_class=HTMLResponse)
    def receipts(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        missing = [e for e in t.expenses if e.actual is not None and not e.receipt_links]
        inbox = sync.inbox() if sync is not None and not sync.problem else None
        return render(request, "receipts.html", trip=t, missing=missing, inbox=inbox,
                      inbox_folder=str(sync.shared / C.INBOX) if sync is not None else "")

    # « Justificatifs à trier » : photos ou PDF déposés dans le dossier synchronisé depuis un téléphone
    def _inbox_path(name: str) -> Path:
        p_ = C.inbox_file(sync.shared, name) if sync is not None else None
        if p_ is None:
            raise HTTPException(404, "Fichier introuvable")
        return p_

    @app.get("/a-trier/fichier")
    def inbox_file(nom: str):
        p_ = _inbox_path(nom)
        return FileResponse(p_, media_type=mimetypes.guess_type(p_.name)[0] or None,
                            headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(p_.name)}"})

    @app.get("/a-trier/vignette")
    def inbox_thumb(nom: str):
        import hashlib
        p_ = _inbox_path(nom)
        st_ = p_.stat()
        key = hashlib.sha1(f"{p_.name}|{st_.st_size}|{st_.st_mtime_ns}".encode()).hexdigest()[:16]
        cache = db.thumbs_dir / f"a-trier-{key}.jpg"
        if not cache.exists():
            try:
                data = _thumbnail(p_.read_bytes(), p_.suffix.lower() == ".pdf")
                cache.parent.mkdir(exist_ok=True)
                _write_atomic(cache, data)       # deux demandes simultanées : jamais une vignette à moitié écrite
            except Exception:
                return Response(TRANSPARENT_PNG, media_type="image/png", headers={"Cache-Control": "no-store"})
        return FileResponse(cache, media_type="image/jpeg")

    @app.post("/v/{tid}/justificatifs/a-trier")
    async def inbox_attach(tid: int, request: Request, s: Session = Depends(get_s)):
        """Ajoute au voyage les fichiers cochés de « Justificatifs à trier » (et les lie aux dépenses cochées) ;
        les originaux sont rangés dans « Justificatifs à trier/Rangés » (jamais effacés)."""
        import shutil
        t = trip_or_404(s, tid)
        f = await request.form()
        names_ = [n for n in f.getlist("nom") if n]
        if not names_:
            return back(f"/v/{tid}/justificatifs#a-trier", "Coche au moins un fichier")
        files = [_inbox_path(n) for n in names_]
        db.snapshot(f"ajout de {len(files)} justificatif(s) à trier")
        link_to = {int(x) for x in f.getlist("expenses")}
        done = sync.shared / C.INBOX / C.INBOX_DONE
        if not sync.protected:
            done.mkdir(exist_ok=True)
        added = []
        for src in files:
            ext = src.suffix.lower()[:10]
            stored = f"{t.id}_{uuid.uuid4().hex}{ext}"
            shutil.copyfile(src, db.receipts_dir / stored)
            number = max([r.number for r in t.receipts] or [0]) + 1
            ctype = mimetypes.guess_type(src.name)[0] or ("image/heic" if ext in (".heic", ".heif") else "")
            rec = M.Receipt(trip_id=t.id, number=number, original_name=src.name, stored_name=stored, content_type=ctype)
            t.receipts.append(rec)
            s.flush()
            for eid in link_to:
                s.add(M.ReceiptLink(receipt_id=rec.id, expense_id=eid))
            added.append(rec)
        s.commit()
        for src in files:                        # après l'enregistrement : rangés, pas effacés
            if sync.protected:                   # dossier protégé : l'original en clair ne doit pas rester
                src.unlink(missing_ok=True)
                continue
            dst = done / src.name
            n_ = 2
            while dst.exists():
                dst = done / f"{src.stem} ({n_}){src.suffix}"
                n_ += 1
            try:
                os.replace(src, dst)
            except OSError:
                pass
        codes = ", ".join(r.code for r in added)
        return back(f"/v/{tid}/justificatifs", f"{len(added)} justificatif(s) ajouté(s) : {codes}")

    @app.post("/v/{tid}/justificatifs")
    async def receipts_upload(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        files = [u for u in f.getlist("files") if getattr(u, "filename", None)]
        if not files:
            return back(f"/v/{tid}/justificatifs", "Choisis au moins un fichier")
        db.snapshot(f"ajout de {len(files)} justificatif(s)")
        link_to = {int(x) for x in f.getlist("expenses")}
        for up in files:
            rec = await _store_receipt(s, t, up)
            for eid in link_to:
                s.add(M.ReceiptLink(receipt_id=rec.id, expense_id=eid))
        s.commit()
        return back(f"/v/{tid}/justificatifs", f"{len(files)} justificatif(s) ajouté(s)")

    @app.post("/v/{tid}/justificatifs/{rid}")
    async def receipt_links(tid: int, rid: int, request: Request, s: Session = Depends(get_s)):
        trip_or_404(s, tid)
        rec = s.get(M.Receipt, rid)
        if not rec or rec.trip_id != tid:
            raise HTTPException(404, "Justificatif introuvable")
        f = await request.form()
        db.snapshot(f"liens du justificatif {rec.code}")
        s.query(M.ReceiptLink).filter_by(receipt_id=rid).delete()
        for eid in {int(x) for x in f.getlist("expenses")}:
            s.add(M.ReceiptLink(receipt_id=rid, expense_id=eid))
        s.commit()
        return back(f"/v/{tid}/justificatifs", f"Justificatif {rec.code} mis à jour")

    @app.post("/v/{tid}/justificatifs/{rid}/supprimer")
    def receipt_delete(tid: int, rid: int, s: Session = Depends(get_s)):
        trip_or_404(s, tid)
        rec = s.get(M.Receipt, rid)
        if not rec or rec.trip_id != tid:
            raise HTTPException(404, "Justificatif introuvable")
        db.snapshot(f"suppression du justificatif {rec.code}")
        s.delete(rec)   # le fichier reste sur le disque : « Annuler » reste possible
        s.commit()
        return back(f"/v/{tid}/justificatifs", f"Justificatif {rec.code} supprimé")

    @app.post("/apercu-fichier")
    async def file_preview(request: Request):
        """Miniature d'un fichier choisi, avant l'envoi (rien n'est enregistré)."""
        up = (await request.form()).get("file")
        if not getattr(up, "filename", None):
            raise HTTPException(400, "Aucun fichier")
        data = await up.read()
        try:
            out = _thumbnail(data, (up.content_type or "").endswith("pdf") or up.filename.lower().endswith(".pdf"))
        except Exception:
            return Response(TRANSPARENT_PNG, media_type="image/png")
        return Response(out, media_type="image/jpeg")

    @app.get("/v/{tid}/justificatifs/{rid}/vignette")
    def receipt_thumb(tid: int, rid: int, s: Session = Depends(get_s)):
        """Miniature (JPEG, 240 px) : la photo réduite, ou la 1re page d'un PDF. Gardée en cache sur le disque."""
        rec = s.get(M.Receipt, rid)
        if not rec or rec.trip_id != tid:
            raise HTTPException(404, "Justificatif introuvable")
        src = db.receipts_dir / rec.stored_name
        cache = db.thumbs_dir / f"{Path(rec.stored_name).stem}.jpg"
        if not cache.exists():
            if not src.exists():
                raise HTTPException(404, "Fichier introuvable sur le disque")
            try:
                data = _thumbnail(src.read_bytes(), (rec.content_type or "").endswith("pdf") or src.suffix.lower() == ".pdf")
                cache.parent.mkdir(exist_ok=True)
                _write_atomic(cache, data)       # deux demandes simultanées : jamais une vignette à moitié écrite
            except Exception:      # fichier illisible : image transparente, la case affiche « PDF » / rien
                return Response(TRANSPARENT_PNG, media_type="image/png", headers={"Cache-Control": "no-store"})
        return FileResponse(cache, media_type="image/jpeg")

    @app.get("/v/{tid}/justificatifs/{rid}/fichier")
    def receipt_file(tid: int, rid: int, s: Session = Depends(get_s)):
        rec = s.get(M.Receipt, rid)
        if not rec or rec.trip_id != tid:
            raise HTTPException(404, "Justificatif introuvable")
        path = db.receipts_dir / rec.stored_name
        if not path.exists():
            raise HTTPException(404, "Fichier introuvable sur le disque")
        return FileResponse(path, media_type=rec.content_type or None,
                            headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(rec.original_name)}"})

    # ------------------------------------------------------------------ Entre nous
    @app.get("/v/{tid}/entre-nous", response_class=HTMLResponse)
    def subs(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        views = {sub.id: S.sub_view(sub) for sub in t.subaccounts}
        return render(request, "subaccounts.html", trip=t, views=views, names=names(t))

    @app.post("/v/{tid}/entre-nous")
    async def sub_create(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        name = (f.get("name") or "").strip()
        members = [int(x) for x in f.getlist("members")]
        if not name or len(members) < 2:
            return back(f"/v/{tid}/entre-nous", "Donne un nom et choisis au moins 2 personnes")
        db.snapshot(f"création du compte « {name} »")
        sub = M.SubAccount(trip_id=tid, name=name, member_ids=",".join(map(str, members)))
        s.add(sub)
        s.commit()
        return back(f"/v/{tid}/entre-nous/{sub.id}", f"Compte « {name} » créé")

    def _sub_or_404(s, tid, sid):
        sub = s.get(M.SubAccount, sid)
        if not sub or sub.trip_id != tid:
            raise HTTPException(404, "Compte introuvable")
        return sub

    @app.get("/v/{tid}/entre-nous/{sid}", response_class=HTMLResponse)
    def sub_detail(tid: int, sid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        sub = _sub_or_404(s, tid, sid)
        return render(request, "subaccount.html", trip=t, sub=sub, v=S.sub_view(sub), names=names(t),
                      today=dt.date.today().isoformat())

    @app.post("/v/{tid}/entre-nous/{sid}/depense")
    async def sub_expense(tid: int, sid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        sub = _sub_or_404(s, tid, sid)
        f = await request.form()
        label = (f.get("label") or "").strip()
        amt = amount(f, "amount")
        payer = int(f.get("payer") or 0)
        bens = [int(x) for x in f.getlist("for") if int(x) in sub.members]
        if not label or not amt or amt <= 0 or payer not in sub.members or not bens:
            return back(f"/v/{tid}/entre-nous/{sid}", "Remplis le libellé, le montant, qui a payé et pour qui")
        db.snapshot(f"dépense « {label} » dans « {sub.name} »")
        s.add(M.SubExpense(sub_id=sid, label=label, amount=amt, payer_id=payer,
                           date=S.parse_date(f.get("date")), weights=",".join(f"{b}:1" for b in bens)))
        s.commit()
        return back(f"/v/{tid}/entre-nous/{sid}", f"« {label} » ajoutée")

    @app.post("/v/{tid}/entre-nous/{sid}/regler")
    async def sub_settle(tid: int, sid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        sub = _sub_or_404(s, tid, sid)
        f = await request.form()
        a, b = int(f.get("sender")), int(f.get("receiver"))
        amt = amount(f, "amount")
        if not amt or amt <= 0:
            return back(f"/v/{tid}/entre-nous/{sid}", "Montant invalide")
        n = names(t)
        db.snapshot(f"{n[a]} a remboursé {n[b]}")
        s.add(M.SubTransfer(sub_id=sid, sender_id=a, receiver_id=b, amount=amt, date=dt.date.today()))
        s.commit()
        return back(f"/v/{tid}/entre-nous/{sid}", f"{n[a]} a remboursé {E.fmt(amt)} à {n[b]}")

    @app.post("/v/{tid}/entre-nous/{sid}/supprimer-ligne")
    async def sub_delete_line(tid: int, sid: int, request: Request, s: Session = Depends(get_s)):
        trip_or_404(s, tid)
        _sub_or_404(s, tid, sid)
        f = await request.form()
        db.snapshot("suppression d'une ligne « Entre nous »")
        if f.get("expense"):
            obj = s.get(M.SubExpense, int(f.get("expense")))
        else:
            obj = s.get(M.SubTransfer, int(f.get("transfer")))
        if obj and obj.sub_id == sid:
            s.delete(obj)
            s.commit()
        return back(f"/v/{tid}/entre-nous/{sid}", "Ligne supprimée")

    @app.post("/v/{tid}/entre-nous/{sid}/supprimer")
    def sub_delete(tid: int, sid: int, s: Session = Depends(get_s)):
        trip_or_404(s, tid)
        sub = _sub_or_404(s, tid, sid)
        db.snapshot(f"suppression du compte « {sub.name} »")
        s.delete(sub)
        s.commit()
        return back(f"/v/{tid}/entre-nous", f"Compte « {sub.name} » supprimé")

    @app.get("/v/{tid}/entre-nous/{sid}/export.pdf")
    def sub_pdf(tid: int, sid: int, s: Session = Depends(get_s)):
        from .export import subaccount_pdf
        t = trip_or_404(s, tid)
        sub = _sub_or_404(s, tid, sid)
        data = subaccount_pdf(t, sub)
        return Response(data, media_type="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(_slug(sub.name))}.pdf"})

    # ------------------------------------------------------------------ export
    @app.get("/v/{tid}/export", response_class=HTMLResponse)
    def export_page(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        r = S.compute_trip(t, mode_of(request))
        subject, body = S.treasurer_mail(t, r)
        return render(request, "export.html", trip=t, r=r, alerts=S.alerts(t, r), mail_subject=subject, mail_body=body,
                      mail_link=f"mailto:?subject={quote(subject)}&body={quote(body)}")

    # ------------------------------------------------------------------ exporter / importer des voyages
    from . import transfer as TR
    imports_dir = db.dir / "imports_en_attente"

    def _zip_name(label):
        return re.sub(r"[^\w-]+", "_", label).strip("_") or "export"

    @app.get("/v/{tid}/transfert", response_class=HTMLResponse)
    def transfer_page(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        return render(request, "transfer.html", trip=t, n_trips=s.query(M.Trip).count())

    @app.get("/v/{tid}/voyage.zip")
    def transfer_export(tid: int, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        data = TR.export_zip(s, [t], db.receipts_dir)
        name = _zip_name(f"voyage {t.name}") + ".zip"
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    @app.get("/tous-les-voyages.zip")
    def transfer_export_all(s: Session = Depends(get_s)):
        trips = s.query(M.Trip).order_by(M.Trip.id).all()
        data = TR.export_zip(s, trips, db.receipts_dir)
        name = f"tripcompta_{dt.date.today().isoformat()}.zip"
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    @app.post("/importer", response_class=HTMLResponse)
    async def transfer_preview(request: Request, s: Session = Depends(get_s)):
        """1re étape : on lit le fichier et on montre ce qui va arriver ; rien n'est encore enregistré."""
        f = await request.form()
        up = f.get("fichier")
        content = await up.read() if up is not None and hasattr(up, "read") else b""
        if not content:
            return back("/?importer=1", "Choisis le fichier .zip exporté depuis l'autre ordinateur")
        try:
            data, _ = TR.read_zip(content)
        except TR.TransferError as e:
            return back("/?importer=1", str(e))
        imports_dir.mkdir(exist_ok=True)
        for old in imports_dir.glob("*.zip"):            # ménage : les aperçus abandonnés depuis plus d'un jour
            if old.stat().st_mtime < dt.datetime.now().timestamp() - 86400:
                old.unlink(missing_ok=True)
        token = uuid.uuid4().hex
        (imports_dir / f"{token}.zip").write_bytes(content)
        existing = {t.name for t in s.query(M.Trip).all()}
        return render(request, "import_preview.html", token=token, voyages=TR.summary(data), existing=existing,
                      fichier=getattr(up, "filename", "") or "", reglages=sorted((data.get("reglages") or {}).keys()))

    @app.post("/importer/{token}")
    def transfer_confirm(token: str, s: Session = Depends(get_s)):
        if not re.fullmatch(r"[0-9a-f]{32}", token) or not (imports_dir / f"{token}.zip").exists():
            return back("/?importer=1", "Cet aperçu a expiré : choisis à nouveau le fichier")
        path = imports_dir / f"{token}.zip"
        db.snapshot("import de voyage(s)")
        try:
            trips, _ = TR.import_zip(s, path.read_bytes(), db.receipts_dir)
        except (TR.TransferError, ValueError, KeyError, TypeError) as e:
            s.rollback()
            return back("/?importer=1", f"Import impossible : {e}")
        s.commit()
        path.unlink(missing_ok=True)
        if len(trips) == 1:
            return back(f"/v/{trips[0].id}", f"Voyage « {trips[0].name} » importé")
        return back("/", f"{len(trips)} voyages importés")

    # ------------------------------------------------------------------ envoyer à chacun son compte
    def _send_rows(t, r):
        rows = []
        for a in r.accounts:
            ref = next(p for p in t.participants if p.id == a.referent)
            msg = S.account_message(t, r, a)
            num = S.whatsapp_number(ref.phone)
            subject = (f"Compte de la {a.name[:1].lower() + a.name[1:]}" if a.is_family else "Ton compte") + \
                f" — voyage « {t.name} »"
            rows.append(dict(acc=a, ref=ref, msg=msg, num=num, subject=subject, key=a.key,
                             wa=f"https://wa.me/{num}?text={quote(msg)}" if num else f"https://wa.me/?text={quote(msg)}",
                             mail=f"mailto:{quote(ref.email or '', safe='@.+-_')}?subject={quote(subject)}&body={quote(msg)}",
                             pdf=f"/v/{t.id}/compte/{a.key}.pdf"))
        return rows

    @app.get("/v/{tid}/envoyer", response_class=HTMLResponse)
    def send_page(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        r = S.compute_trip(t, mode_of(request))
        return render(request, "send.html", trip=t, r=r, rows=_send_rows(t, r))

    @app.get("/v/{tid}/compte/{key}.pdf")
    def account_pdf_route(tid: int, key: str, request: Request, s: Session = Depends(get_s)):
        from .export import account_pdf
        t = trip_or_404(s, tid)
        r = S.compute_trip(t, mode_of(request))
        if key not in {a.key for a in r.accounts}:
            raise HTTPException(404, "Compte introuvable")
        data, name = account_pdf(t, key, mode_of(request))
        return Response(data, media_type="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    @app.get("/v/{tid}/comptes-personnels.zip")
    def accounts_zip_route(tid: int, request: Request, s: Session = Depends(get_s)):
        from .export import accounts_zip
        t = trip_or_404(s, tid)
        return Response(accounts_zip(t, mode_of(request)), media_type="application/zip",
                        headers={"Content-Disposition":
                                 f"attachment; filename*=UTF-8''{quote('Comptes_' + _slug(t.name))}.zip"})

    @app.get("/v/{tid}/export.pdf")
    def export_pdf(tid: int, request: Request, s: Session = Depends(get_s)):
        from .export import trip_pdf
        t = trip_or_404(s, tid)
        data = trip_pdf(t, mode_of(request))
        return Response(data, media_type="application/pdf",
                        headers={"Content-Disposition":
                                 f"attachment; filename*=UTF-8''{quote('Bilan_' + _slug(t.name))}.pdf"})

    @app.get("/v/{tid}/export.zip")
    def export_zip(tid: int, request: Request, s: Session = Depends(get_s)):
        from .export import trip_zip
        t = trip_or_404(s, tid)
        data = trip_zip(t, mode_of(request), db.receipts_dir)
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition":
                                 f"attachment; filename*=UTF-8''{quote('Justificatifs_' + _slug(t.name))}.zip"})

    return app


def _slug(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")
    return s[:60] or "export"
