"""Application web (FastAPI + Jinja2)."""
from __future__ import annotations

from markupsafe import Markup
import datetime as dt
import os
import re
import secrets
import sys
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from . import db as D
from . import diagnostic as DIAG
from . import engine as E
from . import fichiers
from . import journal
from . import models as M
from . import services as S
from . import writes as W
from .db import (Database, DamagedDatabaseError, DataUnavailableError, NewerDatabaseError, human_when,
                 stored_version)

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
GUIDE_URL = "https://tripcompta.plongee.bio/guide"       # le guide en ligne (site de présentation)


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


# ---------------------------------------------------------------------- seules les pages de Trip Compta
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}     # testserver : le client des essais (TestClient)
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _host_name(netloc: str) -> str:
    """« 127.0.0.1:8765 » → « 127.0.0.1 » ; « [::1]:8765 » → « ::1 »."""
    netloc = (netloc or "").strip().lower()
    if netloc.startswith("["):
        return netloc[1:].split("]", 1)[0]
    return netloc.rsplit(":", 1)[0] if netloc.count(":") == 1 else netloc


def request_refusal(method: str, headers) -> tuple[int, str] | None:
    """Trip Compta n'écoute que sur cet ordinateur, mais un site ouvert dans le navigateur pourrait lui envoyer des
    requêtes (formulaire caché, script) ou se faire passer pour lui (« DNS rebinding »). Refusé :
    - toute requête dont l'adresse demandée (Host) n'est pas cet ordinateur ;
    - une modification venue d'une autre page que Trip Compta (Origin, ou à défaut Sec-Fetch-Site du navigateur).
    Les programmes sans navigateur (IA via l'API, relance de Trip Compta) n'envoient ni l'un ni l'autre : acceptés."""
    host = headers.get("host") or ""
    if _host_name(host) not in LOCAL_HOSTS:
        return 400, "Adresse refusée : Trip Compta ne répond qu'à cet ordinateur (127.0.0.1)."
    if method.upper() not in WRITE_METHODS:
        return None
    origin = headers.get("origin")
    if origin is not None:
        from urllib.parse import urlsplit
        o = urlsplit(origin)
        if origin == "null" or o.scheme != "http" or o.netloc.lower() != host.lower():
            return 403, "Requête refusée : elle ne vient pas d'une page de Trip Compta."
        return None
    if (headers.get("sec-fetch-site") or "").lower() in ("cross-site", "same-site"):
        return 403, "Requête refusée : elle ne vient pas d'une page de Trip Compta."
    return None


# ---------------------------------------------------------------------- jeton de session
# Trip Compta n'écoute que sur cet ordinateur, mais un autre programme, ou une autre personne qui a une session
# ouverte sur le même ordinateur, pourrait lui parler à la même adresse. À chaque lancement, run.py tire donc un
# jeton au hasard et le donne à create_app(token=…) ; il est exigé partout, sauf pour /ping (qui ne dit que « je
# suis Trip Compta, version tant ») et les fichiers d'apparence. La fenêtre (ou l'onglet) le reçoit dans la première
# adresse qu'elle ouvre (?jeton=…) : il est aussitôt rangé dans un témoin (cookie) et retiré de l'adresse. Un
# programme (l'IA par l'API, un script) le donne dans l'en-tête « Authorization: Bearer … » ou « X-TripCompta-Jeton ».
# Sans jeton (application créée directement, par les essais) : pas de contrôle.
TOKEN_PARAM = "jeton"
TOKEN_HEADER = "x-tripcompta-jeton"
TOKEN_FREE = ("/static/", "/favicon")
NO_TOKEN = ("Cette page de Trip Compta ne s'ouvre que depuis l'application. Ferme cet onglet et ouvre Trip Compta "
            "par son icône : s'il est déjà ouvert, il revient devant.")
NO_TOKEN_PAGE = ("<!doctype html><html lang=fr><meta charset=utf-8><title>Trip Compta</title>"
                 "<link rel=stylesheet href=/static/app.css>"
                 "<main class=bare-refusal style='max-width:34rem;margin:15vh auto;padding:0 1.2rem;"
                 "font-family:system-ui,sans-serif;line-height:1.5'><h1 style='font-size:1.4rem'>Trip Compta</h1>"
                 f"<p>{NO_TOKEN}</p></main></html>")


def token_cookie(port) -> str:
    """Un témoin par adresse : deux Trip Compta ouverts (deux dossiers de voyages) ne se marchent pas dessus."""
    return f"tc_jeton_{port or 0}"


def token_response(request, token: str):
    """None si la requête porte le jeton (ou n'en a pas besoin) ; sinon la réponse à renvoyer à sa place."""
    path = request.url.path
    if path == "/ping" or path.startswith(TOKEN_FREE):
        return None

    def right(value) -> bool:
        return bool(value) and secrets.compare_digest(str(value).encode("utf-8"), token.encode("utf-8"))
    name = token_cookie(request.url.port)
    auth = request.headers.get("authorization") or ""
    known = (right(request.cookies.get(name)) or right(request.headers.get(TOKEN_HEADER))
             or (auth[:7].lower() == "bearer " and right(auth[7:].strip())))
    given = request.query_params.get(TOKEN_PARAM)
    if request.method == "GET" and given is not None and (known or right(given)):
        # première adresse ouverte par la fenêtre ou l'onglet : le jeton passe dans un témoin et quitte l'adresse
        rest = [(k, v) for k, v in request.query_params.multi_items() if k != TOKEN_PARAM]
        resp = RedirectResponse(path + ("?" + urlencode(rest) if rest else ""), status_code=303)
        if right(given):
            resp.set_cookie(name, token, httponly=True, samesite="strict", path="/")
        return resp
    if known:
        return None
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Jeton de session absent ou incorrect : en-tête « Authorization: Bearer "
                                       "<jeton> » (le jeton est affiché sur la page IA de Trip Compta ; il change "
                                       "à chaque lancement)."}, status_code=403)
    return HTMLResponse(NO_TOKEN_PAGE, status_code=403)


def install_guard(app: FastAPI, token: str | None = None) -> None:
    """À ajouter en dernier : le plus extérieur, il passe avant tout le reste. `token` : le jeton de session exigé
    (None : aucun)."""
    app.state.session_token = token or None

    @app.middleware("http")
    async def only_trip_compta(request, call_next):
        refused = request_refusal(request.method, request.headers)
        if refused:
            code, why = refused
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail": why}, status_code=code)
            return HTMLResponse(f"<!doctype html><meta charset=utf-8><title>Trip Compta</title><p>{why}</p>",
                                status_code=code)
        if token:
            stop = token_response(request, token)
            if stop is not None:
                return stop
        return await call_next(request)


def _bare_app(updater, token: str | None = None) -> tuple[FastAPI, str]:
    """Application réduite (données pas ouvertes) : fenêtre, ping, quitter, fichiers statiques."""
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
        if f is None or action not in ("nouvelle", "montrer"):
            return JSONResponse({"ok": False}, status_code=404)
        getattr(f, {"nouvelle": "new_later", "montrer": "show"}[action])()
        return {"ok": True}

    @app.get("/ping")
    def ping():
        return {"app": "tripcompta", "boot": boot, "version": updater.version,
                "jeton": bool(getattr(app.state, "session_token", None))}

    app.state.problem = None              # (dossier, explication) : donné par l'écran qui explique le problème

    @app.get("/reglages/diagnostic.txt")
    def diagnostic_file():
        folder, why = app.state.problem or (D.default_data_dir(), None)
        return _diagnostic_response(DIAG.build(version=updater.label, folder=folder, problem=why))

    @app.post("/quitter", response_class=HTMLResponse)
    def quit_app():
        import threading
        threading.Timer(0.5, lambda: os._exit(0)).start()
        return HTMLResponse("<!doctype html><meta charset=utf-8><title>Trip Compta</title>"
                            "<body style='font-family:system-ui;text-align:center;padding:4rem'>"
                            "<h1>Trip Compta est arrêté.</h1><p>Tu peux fermer cet onglet.</p></body>")
    return app, boot


def _diagnostic_response(text: str) -> Response:
    return Response(text, media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{DIAG.file_name()}"'})


def _bare_page(request, updater, name, code=200, **ctx):
    return templates.TemplateResponse(request, name, dict(
        request=request, trip=None, msg=None, asso_name="", app_version=updater.label, update_banner=None,
        STATIC_V=templates.env.globals["STATIC_V"], **ctx), status_code=code)


def data_problem_app(err: Exception, updater, restart=None, token: str | None = None) -> FastAPI:
    """Le fichier des voyages est indisponible (pas encore téléchargé, dossier déconnecté) ou abîmé sans sauvegarde
    lisible : on n'y touche pas, on explique quoi faire, et on propose de réessayer."""
    app, _ = _bare_app(updater, token)
    app.state.restart = restart
    damaged = isinstance(err, DamagedDatabaseError)
    app.state.problem = (Path(err.path).parent, "le fichier des voyages est abîmé, sans sauvegarde lisible" if damaged
                         else "le fichier des voyages est là, mais illisible pour l'instant (pas téléchargé ?)")

    @app.post("/reessayer", response_class=HTMLResponse)
    def retry(request: Request):
        r = app.state.restart
        if r is None:
            return _bare_page(request, updater, "data_problem.html", 503, err=err, damaged=damaged, retry=False)
        import threading
        threading.Timer(0.3, r).start()
        return _bare_page(request, updater, "restart.html", can_restart=True,
                          message="Trip Compta essaie de nouveau d'ouvrir tes voyages.")

    @app.get("/{rest:path}", response_class=HTMLResponse)
    def explain(request: Request, rest: str = ""):
        return _bare_page(request, updater, "data_problem.html", 503, err=err, damaged=damaged,
                          folder=str(Path(err.path).parent), retry=app.state.restart is not None)
    install_guard(app, token)
    return app


def newer_data_app(err: NewerDatabaseError, updater, token: str | None = None) -> FastAPI:
    """La base a été ouverte par une version plus récente : on n'y touche pas, on explique quoi faire."""
    from . import updater as U
    from . import version as V
    app, _ = _bare_app(updater, token)
    app.state.problem = (err.path, f"voyages enregistrés par Trip Compta {err.stored}, plus récente que celle-ci")

    checked = []

    def fitting():
        """La version publiée, si elle sait ouvrir ces données (au moins aussi récente qu'elles)."""
        if not checked and not updater.dev and not os.environ.get("TRIPCOMPTA_NO_UPDATE_CHECK"):
            checked.append(updater.check(force=True))     # une seule fois : la page reste rapide ensuite
        a = updater.available()
        return a if a and V.base(a["version"]) and V.base(a["version"]) >= V.base(err.stored) else None

    def page(request, name, code=200, **ctx):
        return _bare_page(request, updater, name, code, err=err, page=U.RELEASES_PAGE, help=U.INSTALL_HELP, **ctx)

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
    install_guard(app, token)
    return app


def writer_notice(db, me: str | None = None, now: dt.datetime | None = None) -> str | None:
    """À l'ouverture : les voyages ont été modifiés en dernier sur un autre ordinateur ? Avertissement s'il y a moins
    d'une heure (Trip Compta y est peut-être encore ouvert) ; sinon simple information, une fois par ordinateur
    (qui alterne portable et fixe ne doit pas voir le même message à chaque lancement)."""
    from . import paths as PATHS
    from .dossier import computer_name
    w = db.last_writer()
    me = me or computer_name()
    if not w or not w.get("ordinateur") or w["ordinateur"] == me:
        return None
    now = now or dt.datetime.now()
    other = w["ordinateur"]
    seen = PATHS.read_config().get("ecrivains_vus") or {}
    key = str(db.home)
    recent = dt.timedelta(0) <= now - w["quand"] < dt.timedelta(hours=1)
    if not recent and seen.get(key) == other:
        return None
    try:
        PATHS.write_config(ecrivains_vus={**seen, key: other})
    except OSError:
        pass
    when = human_when(w["quand"], now)
    if recent:
        return (f"Tes voyages ont été modifiés {when} sur « {other} ». Si Trip Compta est encore ouvert là-bas, "
                f"ferme-le avant de continuer : sinon, les changements d'un ordinateur effaceraient ceux de l'autre.")
    return f"Dernière modification de tes voyages : sur « {other} », {when}."


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


def create_app(data_dir=None, restart=None, updater=None, coffre=None, token: str | None = None) -> FastAPI:
    """restart : fonction qui relance l'application (fournie par run.py) ; sans elle, on demande de relancer.
    updater : gestion des mises à jour (les tests en donnent une simulée).
    coffre (tests) : {"local": copie de travail des voyages protégés, "host": nom de l'ordinateur}.
    Trip Compta lit et écrit directement son dossier ; voyages protégés par un mot de passe : voir coffre.py."""
    from . import coffre as CF
    from . import crypto as K
    from . import dossier as C
    from . import paths as PATHS
    from . import updater as U
    from . import version as V
    upd = updater or U.Updater()
    if data_dir:
        data_source = "lancement"
    elif os.environ.get("TRIPCOMPTA_DATA_DIR"):
        data_source = "env"
    elif PATHS.read_config().get("data_dir"):
        data_source = "choisi"
    else:
        data_source = "defaut"
    folder = Path(data_dir).expanduser() if data_dir else PATHS.default_data_dir()
    vault_box = None                             # voyages protégés par un mot de passe (coffre.py)
    opts = coffre or {}
    box_local = Path(opts.get("local") or CF.local_dir_for(folder, PATHS.config_file().parent))
    try:
        if K.is_protected(folder):
            vault_box = CF.Coffre(folder, box_local, host=opts.get("host"))
            vault_box.open()
            if vault_box.locked:                 # rien en clair tant que c'est verrouillé : une base vide d'attente
                db = Database(vault_box.local / ".verrouille", app_version=upd.version, home=folder,
                              receipts_in_home=False)
            else:
                db = Database(vault_box.local, app_version=upd.version, home=folder, receipts_in_home=False)
                vault_box.publish()
        else:
            db = Database(folder, app_version=upd.version)
    except NewerDatabaseError as err:
        return newer_data_app(err, upd, token)
    except (DataUnavailableError, DamagedDatabaseError) as err:
        return data_problem_app(err, upd, restart, token)
    backup_failed = None
    if vault_box is None or not vault_box.locked:
        try:
            db.daily_backup()
        except Exception as exc:                 # la copie du jour est un filet : son échec n'empêche pas d'ouvrir
            backup_failed = (dt.date.today().isoformat(), exc)
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
    # Mac : application ouverte hors du dossier Applications (Téléchargements…) : on le dit sur l'accueil
    app.state.misplaced = None if upd.dev else U.misplaced_app()
    # jour où la copie de secours quotidienne a échoué (bandeau sur les pages, jusqu'à la prochaine copie réussie)
    app.state.backup_failed = backup_failed[0] if backup_failed else None
    if journal.setup():
        if backup_failed:
            journal.log.error("copie de secours du jour impossible", exc_info=backup_failed[1])
        journal.log.info("démarrage : Trip Compta %s, %s, Python %s", upd.label, sys.platform,
                         ".".join(map(str, sys.version_info[:3])))
        if db.recovered:
            journal.log.warning("fichier des voyages abîmé au démarrage : dernière copie intacte reprise")
    app.state.coffre = vault_box
    app.state.startup_notices = [n for n in (db.recovered_notice(), writer_notice(db)) if n]
    if vault_box is not None:
        vault_box.before_replace = lambda: db.engine.dispose()
        vault_box.on_conflict = lambda: db.after_replace()
        import atexit
        atexit.register(vault_box.stop)
        if (coffre or {}).get("watch", True):
            vault_box.watch()                    # saisies de l'IA (autre programme) : enregistrées sans attendre
    if updater is None:
        upd.start_background()
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

    CLOSED_OK = re.compile(r"^/v/\d+/(rouvrir|dupliquer|supprimer|exemple/\w+|[\w/-]*apercu)$")

    @app.middleware("http")
    async def closed_trip_guard(request, call_next):
        """Voyage clôturé : plus aucune modification depuis l'écran (sauf le rouvrir, le dupliquer, le supprimer)."""
        if request.method == "POST":
            m = re.match(r"^/v/(\d+)/", request.url.path)
            if m and not CLOSED_OK.match(request.url.path) and not (vault_box is not None and vault_box.locked):
                with db.Session() as s_:
                    t_ = s_.get(M.Trip, int(m.group(1)))
                    closed = S.is_closed(t_)
                if closed:
                    return refuse(f"/v/{m.group(1)}", S.CLOSED_MSG)
        return await call_next(request)

    @app.middleware("http")
    async def remember_snapshot(request, call_next):
        # (dernière action défaisable avant la requête, heure de début au format des sauvegardes d'annulation)
        token = snap_before.set((db.undo_token(), dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")))
        try:
            return await call_next(request)
        finally:
            snap_before.reset(token)

    def open_unlocked():
        """Après le déverrouillage : la vraie base (déchiffrée sur l'ordinateur) remplace la base vide d'attente."""
        db.reopen(vault_box.local, receipts_in_home=False)
        import shutil as _sh
        _sh.rmtree(vault_box.local / ".verrouille", ignore_errors=True)
        app.state.startup_notices += [n for n in (db.recovered_notice(), writer_notice(db)) if n]
        db.daily_backup()
        with db.Session() as s_:
            if any([S.store_draws(t_) for t_ in s_.query(M.Trip).all()]):
                s_.commit()
        vault_box.publish()

    def to_locked():
        """Verrouiller : base refermée, enregistrée dans le dossier, puis copie en clair effacée (coffre.py)."""
        db.engine.dispose()
        vault_box.lock()
        db.reopen(vault_box.local / ".verrouille", receipts_in_home=False)

    def newer_stored() -> str | None:
        """Voyages protégés enregistrés par une version plus récente de Trip Compta (vu seulement une fois
        déchiffrés) : cette version ne doit pas y toucher."""
        stored = stored_version(db.path)
        if stored and V.base(stored) and V.base(upd.version) and V.base(stored) > V.base(upd.version):
            return stored
        return None

    LOCKED_OK = ("/static", "/ping", "/deverrouiller", "/quitter", "/maj/", "/reglages/a-propos", "/favicon",
                 "/fenetre/", "/reglages/diagnostic.txt", "/aide")

    @app.middleware("http")
    async def daily_backup(request, call_next):
        """Sauvegarde du jour aussi quand Trip Compta reste ouvert plusieurs jours (une copie par jour au plus)."""
        response = await call_next(request)
        if (request.method in ("POST", "PUT", "PATCH", "DELETE") and response.status_code < 400
                and not (vault_box is not None and vault_box.locked)):
            try:
                db.daily_backup()
                app.state.backup_failed = None
            except Exception as exc:
                # dit une fois dans le journal, et affiché sur les pages tant que la copie du jour n'existe pas
                if not app.state.backup_failed:
                    journal.log.error("copie de secours du jour impossible", exc_info=exc)
                app.state.backup_failed = dt.date.today().isoformat()
        return response

    @app.middleware("http")
    async def protected_folder(request, call_next):
        """Voyages protégés : rien tant que c'est verrouillé ; après chaque modification, rechiffrés dans le dossier."""
        if vault_box is None:
            return await call_next(request)
        path = request.url.path
        if vault_box.locked and not path.startswith(LOCKED_OK):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Trip Compta est verrouillé : saisis le mot de passe de tes voyages "
                                               "dans l'application."}, status_code=423)
            return RedirectResponse("/deverrouiller", status_code=303)
        response = await call_next(request)
        # (la requête a pu activer ou retirer la protection : on relit vault_box)
        if (request.method in ("POST", "PUT", "PATCH", "DELETE") and response.status_code < 400
                and vault_box is not None and not vault_box.locked):
            if vault_box.publish() == "conflit":     # un autre ordinateur avait enregistré : sa version est reprise
                db.after_replace()
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
    def render(request: Request, name: str, **ctx):
        ctx.setdefault("mode", E.ACTUAL)
        ctx.setdefault("msg", request.query_params.get("msg"))
        # message de refus (rouge) : seulement celui de l'adresse, pas un avis posé ensuite à sa place
        ctx.setdefault("msg_err", bool(ctx["msg"]) and ctx["msg"] == request.query_params.get("msg")
                       and request.query_params.get("e") == "1")
        d, r = request.query_params.get("d"), request.query_params.get("r")
        ctx.setdefault("undo_token", d if d and d == db.undo_token() else None)
        ctx.setdefault("undo_label", db.last_action() if ctx["undo_token"] else None)
        ctx.setdefault("redo_token", r if r and r == db.redo_token() else None)
        if ctx.get("trip") is not None and isinstance(ctx["trip"], M.Trip):
            ctx.setdefault("roles", S.roles_map(ctx["trip"]))
            ctx.setdefault("estimated", [e for e in ctx["trip"].expenses if S.is_estimated(e)])
            ctx.setdefault("trip_roles", S.trip_roles(ctx["trip"]))
            ctx.setdefault("checks", S.checks_of(ctx["trip"]))       # dépenses « à vérifier » (bandeau, liste)
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
        ctx["full_app"] = True               # voyages ouverts : « Défaire » et « Aide » dans la barre du haut
        ctx.setdefault("dossier", folder_status())
        ctx.setdefault("backup_failed", bool(app.state.backup_failed))
        ctx.setdefault("misplaced", app.state.misplaced)
        if vault_box is not None and vault_box.notice and not ctx.get("msg"):
            ctx["msg"], vault_box.notice = vault_box.notice, None
        ctx.setdefault("app_version", upd.label)
        ctx.setdefault("update_banner", None if upd.job else upd.banner())
        if not upd.dev and not app.state.update_notice:
            app.state.update_notice = upd.failure_notice()
        if app.state.update_notice and not ctx.get("msg"):
            ctx["msg"], app.state.update_notice = app.state.update_notice, None
        if app.state.startup_notices and not ctx.get("msg"):
            ctx["msg"] = app.state.startup_notices.pop(0)
        return templates.TemplateResponse(request, name, ctx)

    def folder_status() -> dict:
        """Pour l'en-tête des pages : heure du dernier enregistrement (automatique), protégé / verrouillé, copies en
        conflit laissées à côté de la base. « stat » ne télécharge rien d'un fichier resté dans un cloud."""
        F = db.home
        try:
            main = vault_box.shared_db if vault_box is not None else db.path
            on_disk = main.stat().st_mtime if main.exists() else None
        except OSError:
            on_disk = None
        published = vault_box.published_at if vault_box is not None else None
        saved = max([x for x in (published, on_disk) if x] or [0]) or None
        return dict(folder=str(F), protected=vault_box is not None,
                    locked=vault_box is not None and vault_box.locked, saved=_hm(saved),
                    conflicts=len(C.conflicts(F, vault_box is not None, readable=lambda p: True)))

    def refuse(url: str, msg: str, field: str | None = None):
        """Retour avec un message de refus (saisie à corriger, action impossible) : affiché en rouge. `field` : le
        nom du champ en cause ; la page y remet le message, à côté, et le formulaire revient avec la saisie."""
        return back(url, msg, err=True, field=field)

    def back(url: str, msg: str | None = None, redo: str | None = None, err: bool = False,
             field: str | None = None):
        if msg:
            url, _, frag = url.partition("#")
            if "?" in url:                         # l'adresse d'où l'on vient portait déjà un message : on le retire
                base, _, q = url.partition("?")
                q = "&".join(p_ for p_ in q.split("&") if p_ and p_.split("=")[0] not in ("msg", "e", "f", "d", "r"))
                url = base + ("?" + q if q else "")
            url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
            if err:
                url += "&e=1" + (f"&f={quote(field)}" if field else "")
            now = db.undo_token()
            before, started = snap_before.get() or (None, "")
            # « Défaire » seulement si CETTE requête a modifié les données (sauvegarde d'annulation créée pendant
            # elle). Déverrouiller, verrouiller, mot de passe, changer de dossier… ne font
            # qu'ouvrir d'autres données : la dernière action change, mais il n'y a rien à défaire.
            if now and now != before and now >= started:
                url += "&d=" + quote(now)
            if redo:
                url += "&r=" + quote(redo)
            if frag:
                url += "#" + frag
        return RedirectResponse(url, status_code=303)

    def _then_join(t: M.Trip, before: dict, url: str, msg: str):
        """Après un ajout de personne, d'étiquette, de groupe ou de famille : les dépenses où cet ensemble est allumé
        sont proposées (rien de coché) ; celles à parts personnalisées sont marquées « à vérifier ». Sinon, retour."""
        from sqlalchemy.orm import object_session
        sug = S.join_suggestions(t, before)
        if not sug:
            return back(url, msg)
        if S.flag_checks(t, sug):
            object_session(t).commit()
        texts = list(dict.fromkeys([x["why"] for x in sug] + [x["joined"] for x in sug]
                                   + [", ".join(x["others"]) for x in sug if x["others"]]))
        kinds = {"groupes": "g", "autres": "a", "verifier": "v"}
        q = "s=" + quote(",".join(
            f"{x['pid']}.{x['eid']}.{texts.index(x['why'])}.{texts.index(x['joined'])}.{kinds[x['kind']]}."
            f"{texts.index(', '.join(x['others'])) if x['others'] else -1}" for x in sug)) \
            + "&w=" + quote("|".join(w.replace("|", " ") for w in texts)) + "&next=" + quote(url)
        return back(f"/v/{t.id}/rejoindre?{q}", msg)

    def trip_or_404(s: Session, tid: int) -> M.Trip:
        t = s.get(M.Trip, tid)
        if not t:
            raise HTTPException(404, "Voyage introuvable")
        return t

    class FormError(Exception):
        """Saisie refusée : retour au formulaire. `field` : le nom du champ en cause."""

        def __init__(self, msg: str, field: str | None = None):
            super().__init__(msg)
            self.field = field

    def whole(v, default: int = 0) -> int:
        """Nombre entier venu d'un formulaire (une personne ou une ligne choisie) ; autre chose : la valeur par défaut."""
        v = str(v if v is not None else "").strip()
        return int(v) if v.isdigit() else default

    def wholes(values) -> list:
        return [int(x) for x in values if str(x).strip().isdigit()]

    def amount(form, key, field: str | None = None):
        try:
            v = E.parse_amount(form.get(key))
        except ValueError:
            raise FormError(f"Montant invalide : « {form.get(key)} ». Exemple : 12,50", field or key)
        if v is not None and v < 0:
            raise FormError("Le montant doit être positif.", field or key)
        return v

    @app.exception_handler(FormError)
    async def form_error(request: Request, exc: Exception):
        ref = request.headers.get("referer") or "/"
        u = urlparse(ref)
        path = u.path + (("?" + re.sub(r"&?(msg|e)=[^&]*", "", u.query).lstrip("&")) if u.query else "")
        return refuse(path.rstrip("?"), str(exc), getattr(exc, "field", None))

    app.state.form_refusal = form_error        # une écriture refusée (writes.WriteError) depuis un formulaire

    def names(trip):
        return {p.id: p.name for p in trip.participants}

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return templates.TemplateResponse(
            request, "error.html",
            dict(request=request, status=exc.status_code, detail=exc.detail, mode=E.ACTUAL,
                 msg=None, last_action=db.last_action()),
            status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def bad_address(request: Request, exc: RequestValidationError):
        """Adresse mal formée (« /v/abc ») : une page en français, pas un message technique. L'API garde le sien."""
        if request.url.path.startswith("/api/"):
            from fastapi.exception_handlers import request_validation_exception_handler
            return await request_validation_exception_handler(request, exc)
        return await http_error(request, HTTPException(404, "Cette page n'existe pas."))

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        """Erreur que l'application n'avait pas prévue : notée dans le journal (avec l'heure, pour la retrouver), et
        une page en français qui dit quoi faire. Rien de ce qui était enregistré n'est touché."""
        when = dt.datetime.now().strftime("%H:%M:%S")
        journal.log.error("erreur imprévue : %s %s", request.method, request.url.path, exc_info=exc)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Trip Compta a rencontré un problème imprévu. Rien de ce qui était "
                                           "enregistré n'est perdu."}, status_code=500)
        return templates.TemplateResponse(
            request, "error.html",
            dict(request=request, status=500, detail="", unexpected=True, when=when, journal_file=str(journal.file()),
                 mode=E.ACTUAL, msg=None, last_action=None),
            status_code=500)

    # ------------------------------------------------------------------ global
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
        locked = bool(vault_box is not None and vault_box.locked)
        extra = [f"vérification automatique des mises à jour : {'oui' if upd.auto else 'non'}",
                 f"mise à jour disponible : {(upd.available() or {}).get('version') or 'non'}",
                 "sauvegarde du jour : " + ("ÉCHEC le " + app.state.backup_failed if app.state.backup_failed else "faite")]
        if app.state.misplaced:
            extra.append(f"application ouverte hors du dossier Applications : {app.state.misplaced}")
        journal.log.info("diagnostic exporté")
        return _diagnostic_response(DIAG.build(version=upd.label, folder=db.home, db=db, source=data_source,
                                               protected=vault_box is not None, locked=locked, extra=extra))

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
                if vault_box is not None:
                    vault_box.stop()
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
        if vault_box is not None:
            vault_box.stop()                             # enregistré, chiffré : rien en clair ne reste
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
        w = db.last_writer() if vault_box is None or not vault_box.locked else None
        return render(request, "data_dir.html", data_dir=cur, source=data_source,
                      local_current=source_is_default(), default_dir=default_dir,
                      conflicts=(vault_box.conflicts() if vault_box is not None else C.conflicts(cur)),
                      writer=dict(ordinateur=w["ordinateur"], quand=human_when(w["quand"])) if w else None,
                      protected=vault_box is not None,
                      remembered=bool(vault_box is not None and vault_box.vault is not None
                                      and K.recall(vault_box.vault.id)),
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
        try:                                             # le dossier par défaut est créé au besoin (Documents compris)
            default = p.resolve() == (PATHS.documents_dir() / PATHS.FOLDER).resolve()
        except OSError:
            default = False
        out["ok"] = p.is_dir() or p.parent.is_dir() or default
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
        with db.Session() as s_:
            demo = s_.query(M.Trip).filter(M.Trip.is_demo.is_(True)).first()
            demo_trip = {"id": demo.id, "name": demo.name} if demo else None
        saved = db.backups()
        return render(request, "settings.html", data_dir=db.home, source=data_source,
                      demo_trip=demo_trip, backups_count=len(saved),
                      last_backup=(human_when(saved[0]["when"]) if saved[0]["timed"]
                                   else f"le {saved[0]['when']:%d/%m/%Y}") if saved else None,
                      protected=vault_box is not None,
                      remembered=bool(vault_box is not None and vault_box.vault is not None and K.recall(vault_box.vault.id)),
                      info=upd, available=upd.available(), latest=upd.latest(),
                      checked=dt.datetime.fromtimestamp(checked).strftime("%d/%m/%Y") if checked else None)

    # ------------------------------------------------------------------ revenir à une sauvegarde
    @app.get("/reglages/sauvegardes", response_class=HTMLResponse)
    def backups_page(request: Request):
        items = db.backups()
        for b in items:
            b["ago"] = human_when(b["when"]) if b["timed"] else None
            if b["ago"] and b["kind"] == "jour" and b["ago"].startswith("le "):     # le jour est déjà dans le titre
                b["ago"] = f"à {b['when'].hour} h {b['when'].minute:02d}"
        with db.Session() as s_:
            now = dict(trips=s_.query(M.Trip).count(), expenses=s_.query(M.Expense).count(),
                       receipts=s_.query(M.Receipt).count())
        return render(request, "backups.html", backups=items, now=now, where=db.backups_dir,
                      complete=db.complete_backups, keep=D.BACKUP_KEEP)

    @app.post("/reglages/sauvegardes/revenir")
    async def backup_restore(request: Request):
        f = await request.form()
        try:
            res = db.restore_backup(str(f.get("nom") or ""))
        except D.BackupError as err:
            return refuse("/reglages/sauvegardes", str(err))
        if vault_box is not None and not vault_box.locked:
            vault_box.receipts_down()                # voyages protégés : les justificatifs chiffrés du dossier
        journal.log.info("retour à la sauvegarde « %s »", f.get("nom"))
        msg = f"Tes voyages sont revenus à l'état du {res['when']:%d/%m/%Y}"
        msg += f" à {res['when'].hour} h {res['when'].minute:02d}." if res["timed"] else "."
        if res["receipts_back"]:
            n = res["receipts_back"]
            msg += f" {n} justificatif{'s' if n > 1 else ''} remis à sa place." if n == 1 else f" {n} justificatifs remis à leur place."
        if res["receipts_missing"]:
            n = res["receipts_missing"]
            msg += (f" ⚠️ {n} justificatif{'s' if n > 1 else ''} de cette sauvegarde "
                    f"{'sont introuvables' if n > 1 else 'est introuvable'} : à rajouter depuis les originaux.")
        return back("/reglages/sauvegardes", msg + " Tu peux encore défaire ce retour.")

    @app.post("/reglages/sauvegardes/ouvrir")
    def backups_open(request: Request):
        import subprocess
        import sys as _sys
        target = str(db.backups_dir)
        try:
            if _sys.platform == "darwin":
                subprocess.Popen(["open", target])
            elif _sys.platform == "win32":
                os.startfile(target)                     # Explorateur Windows
            else:
                subprocess.Popen(["xdg-open", target])
        except Exception:
            return refuse("/reglages/sauvegardes", f"Impossible d'ouvrir le dossier : {target}")
        return back("/reglages/sauvegardes", "Dossier des sauvegardes ouvert")

    @app.get("/reglages/protection", response_class=HTMLResponse)
    def protection_settings(request: Request):
        return render(request, "protection_settings.html", source=data_source,
                      protected=vault_box is not None,
                      remembered=bool(vault_box is not None and vault_box.vault is not None and K.recall(vault_box.vault.id)),
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
            return refuse(urlparse(request.headers.get("referer") or "/").path, f"Impossible d'ouvrir le dossier : {target}")
        return back(urlparse(request.headers.get("referer") or "/").path, "Dossier ouvert")

    @app.post("/reglages/parcourir")
    def data_dir_browse():
        """Fenêtre « choisir un dossier » du système ; renvoie le chemin choisi (ou rien si annulé)."""
        f = getattr(app.state, "fenetres", None)
        if f is not None and sys.platform == "win32":    # dans la fenêtre : le choix de dossier moderne, devant elle
            return {"path": f.pick_folder()}
        return {"path": C.pick_folder()}

    def _restart_page(request, message):
        if vault_box is not None:
            vault_box.stop()
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
        """Changer de dossier : un seul chemin. Le dossier contient déjà des voyages → on les ouvre (jamais de copie
        par-dessus) ; sinon → on y copie les voyages actuels (vérifiés), l'ancien dossier n'est pas touché."""
        f = await request.form()
        if data_source in ("lancement", "env"):
            return _data_page(request, error="Le dossier est imposé au lancement de l'application : il ne se change "
                                              "pas ici.")
        if vault_box is not None:
            return _data_page(request, error="Tes voyages sont protégés par un mot de passe : pour les ranger "
                                              "ailleurs, retire d'abord la protection (plus bas), change de dossier, "
                                              "puis protège-les de nouveau. Sinon, une copie lisible serait créée.")
        raw = (f.get("dossier") or "").strip().strip('"').strip("'")
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
        try:
            default = target.resolve() == (PATHS.documents_dir() / PATHS.FOLDER).resolve()
        except OSError:
            default = False
        if not default and not target.parent.is_dir() and not target.is_dir():
            return _data_page(request, error=f"Le dossier « {target.parent} » est introuvable.", value=raw)
        conf = None if default else str(target)       # le dossier par défaut : rien à retenir
        has_data = (target / C.DB).exists() or (target / f".{C.DB}.icloud").exists() or K.is_protected(target)
        if has_data:                                   # des voyages y sont déjà : on les ouvre, rien n'est copié
            PATHS.write_config(data_dir=conf)
            return _restart_page(request, f"Trip Compta va ouvrir les voyages rangés dans « {target} ». Ceux de "
                                          f"« {db.home} » restent où ils sont.")
        try:
            _copy_voyages(target, target / C.RECEIPTS, target / "brouillons_ia")
        except (OSError, Exception) as e:
            return _data_page(request, error=f"Impossible de copier les voyages vers ce dossier : {e}", value=raw)
        try:                                           # les sauvegardes (sur l'ordinateur) suivent les voyages
            import shutil as _sh
            _sh.copytree(db.backups_dir, D.backups_dir_for(target), dirs_exist_ok=True,
                         ignore=_sh.ignore_patterns("A LIRE.txt"))
        except OSError as exc:
            journal.log.warning("sauvegardes pas recopiées pour le nouveau dossier : %s", exc)
        PATHS.write_config(data_dir=conf)
        return _restart_page(request, f"Tes voyages ont été copiés dans « {target} ». L'ancien dossier "
                                      f"« {db.home} » n'a pas été touché : tu pourras le supprimer quand tu auras "
                                      f"vérifié que tout est là.")

    # ------------------------------------------------------------------ protection par un mot de passe
    app.state.pending_protection = None      # en cours d'activation (en mémoire seulement, jamais sur le disque)

    def _folder() -> Path:
        return db.home

    def _unlock_page(request, **ctx):
        return render(request, "unlock.html", folder=str(_folder()), can_remember=K.keyring_available(), **ctx)

    @app.get("/deverrouiller", response_class=HTMLResponse)
    def unlock_page(request: Request):
        if vault_box is None or not vault_box.locked:
            return RedirectResponse("/", status_code=303)
        return _unlock_page(request)

    def _after_unlock(vault, remember: bool, request):
        if not vault_box._key_works(vault):
            return _unlock_page(request, error="Les voyages de ce dossier sont abîmés ou incomplets (synchronisation "
                                               "en cours ?). Attends un peu, puis réessaie.")
        if remember:
            K.remember(vault)
        else:
            K.forget(vault.id)
        vault_box.unlock(vault)
        db.reopen(vault_box.local, receipts_in_home=False)
        newer = newer_stored()
        if newer:                                # une version plus récente les a enregistrés : on n'y touche pas
            db.engine.dispose()
            vault_box.lock()
            db.reopen(vault_box.local / ".verrouille", receipts_in_home=False)
            return _unlock_page(request, error=f"Ces voyages ont été enregistrés par Trip Compta {newer}, plus récente "
                                               f"que celle-ci : mets à jour Trip Compta (ℹ️ À propos), puis réessaie. "
                                               f"Rien n'a été modifié.")
        open_unlocked()
        return None

    @app.post("/deverrouiller", response_class=HTMLResponse)
    async def unlock_post(request: Request):
        if vault_box is None or not vault_box.locked:
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
        if vault_box is None or not vault_box.locked:
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
        if vault_box is not None and vault_box.vault is not None:
            to_locked()
        return RedirectResponse("/deverrouiller", status_code=303)

    def _protection_page(request, **ctx):
        return render(request, "protection.html", folder=str(_folder()), can_remember=K.keyring_available(),
                      protected=vault_box is not None, **ctx)

    @app.get("/protection/activer", response_class=HTMLResponse)
    def protect_start(request: Request):
        if vault_box is not None:
            return RedirectResponse("/reglages/protection", status_code=303)
        return _protection_page(request, step="mot_de_passe")

    @app.post("/protection/activer", response_class=HTMLResponse)
    async def protect_password(request: Request):
        import secrets as _secrets
        if vault_box is not None:
            return RedirectResponse("/reglages/protection", status_code=303)
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
        return _restart_page(request, "Tes voyages sont maintenant protégés par ton mot de passe. Si ce dossier est "
                                      "sauvegardé ou synchronisé ailleurs, ses anciennes copies sont encore lisibles : "
                                      "pense à les supprimer (corbeille, anciennes versions).")

    def _activate(password: str, recovery: str):
        """Chiffre la base et les justificatifs dans le dossier, vérifie TOUT, puis écrit cle.tripcompta (c'est le
        moment où la protection devient effective), et seulement ensuite efface les fichiers en clair."""
        nonlocal vault_box
        import shutil as _sh
        F = _folder()
        local = box_local
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
        # historique, sauvegardes, brouillons : sur l'ordinateur, chiffrés (comme la copie de travail)
        for name in CF.PRIVATE:
            src = F / name
            if src.is_dir():
                _sh.copytree(src, local / name, dirs_exist_ok=True)
                _sh.rmtree(src, ignore_errors=True)
        # les sauvegardes complètes (en clair, à part sur l'ordinateur) : les bases rejoignent la copie de travail
        # (chiffrées juste après), la copie en clair des justificatifs est effacée (ils sont chiffrés dans le dossier)
        if db.complete_backups and db.backups_dir.is_dir():
            (local / "sauvegardes").mkdir(parents=True, exist_ok=True)
            for p_ in db.backups_dir.glob("*.db"):
                _sh.copy2(p_, local / "sauvegardes" / p_.name)
            _sh.rmtree(db.backups_dir, ignore_errors=True)
        _sh.rmtree(F / "imports_en_attente", ignore_errors=True)
        for name in (C.DB, f"{C.DB}-journal", f"{C.DB}-wal", f"{C.DB}-shm"):
            (F / name).unlink(missing_ok=True)
        for p_ in receipts:
            if p_.parent == rdir:
                p_.unlink(missing_ok=True)
        _sh.rmtree(rdir / ".vignettes", ignore_errors=True)
        snap.unlink(missing_ok=True)
        helper = CF.Coffre(F, local)                  # l'historique sur l'ordinateur : chiffré lui aussi
        helper.vault = vault
        helper.encrypt_private()
        helper.wipe_working_copy()
        # jusqu'au redémarrage : verrouillé, avec une base vide d'attente (surtout rien de recréé en clair dans le dossier)
        vault_box = app.state.coffre = CF.Coffre(F, local, host=opts.get("host"))
        vault_box.before_replace = lambda: db.engine.dispose()
        vault_box.on_conflict = lambda: db.after_replace()
        if opts.get("watch", True):
            vault_box.watch()
        vault_box._flag_locked(True)
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
        if vault_box is None or vault_box.vault is None:
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
        K.change_password(_folder(), vault_box.vault, new)
        return back("/reglages/protection", "Mot de passe changé")

    @app.post("/protection/secours", response_class=HTMLResponse)
    async def protect_new_recovery(request: Request):
        import secrets as _secrets
        if vault_box is None or vault_box.vault is None:
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
        if not pend or pend["kind"] != "secours" or vault_box is None or vault_box.vault is None:
            return RedirectResponse("/reglages/protection", status_code=303)
        f = await request.form()
        if not K.check_group(pend["recovery"], pend["group"], f.get("groupe") or ""):
            return _protection_page(request, step="secours", recovery=pend["recovery"], group=pend["group"],
                                    action="/protection/secours/confirmer", renew=True,
                                    error=f"Ce n'est pas le groupe n° {pend['group'] + 1} de ta clé de secours.")
        K.set_recovery(_folder(), vault_box.vault, pend["recovery"])
        app.state.pending_protection = None
        return back("/reglages/protection", "Nouvelle clé de secours enregistrée : l'ancienne ne marche plus")

    @app.post("/protection/oublier")
    def protect_forget():
        if vault_box is not None and vault_box.vault is not None:
            K.forget(vault_box.vault.id)
        return back("/reglages/protection", "Cet ordinateur ne se souvient plus du mot de passe")

    @app.post("/protection/retirer", response_class=HTMLResponse)
    async def protect_remove(request: Request):
        """Retirer la protection : tout redevient lisible (vérifié), puis la clé est effacée."""
        nonlocal vault_box
        import shutil as _sh
        if vault_box is None or vault_box.vault is None:
            return RedirectResponse("/reglages/protection", status_code=303)
        f = await request.form()
        bad = _check_current(request, f)
        if bad:
            return bad
        F = _folder()
        vault_box.publish()
        vault_box.receipts_down()
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
        vault_id = vault_box.vault.id
        (F / K.KEY_FILE).unlink()                    # ← la protection est retirée
        (F / K.README).unlink(missing_ok=True)
        (F / K.DB_FILE).unlink(missing_ok=True)
        for p_ in rdir.glob(f"*{K.SUFFIX}"):
            p_.unlink(missing_ok=True)
        K.forget(vault_id)
        # l'historique « Défaire », les sauvegardes et les brouillons (en clair sur l'ordinateur) rejoignent le dossier,
        # puis la copie de travail est effacée : les voyages sont maintenant directement dans le dossier
        db.engine.dispose()
        for name in CF.PRIVATE:
            src = vault_box.local / name
            if src.is_dir():
                dst = F / name
                if name == "sauvegardes":            # voyages ordinaires : sauvegardes à part, sur l'ordinateur
                    dst = D.backups_dir_for(F)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                _sh.copytree(src, dst, dirs_exist_ok=True)
        vault_box.vault = None
        vault_box.wipe_working_copy()
        for name in CF.PRIVATE:
            _sh.rmtree(vault_box.local / name, ignore_errors=True)
        (vault_box.local / CF.STATE).unlink(missing_ok=True)
        vault_box = app.state.coffre = None
        db.reopen(F, receipts_in_home=True)          # jusqu'au redémarrage : directement dans le dossier
        try:
            db.mirror_receipts(force=True)           # les sauvegardes redeviennent complètes (avec les justificatifs)
        except OSError:
            pass
        return _restart_page(request, "La protection est retirée : tes voyages sont de nouveau des fichiers normaux.")

    OLD_VERSIONS = "Anciennes versions"

    def _conflict_file(name: str) -> Path:
        """Une copie en conflit du dossier (nom contrôlé : pas de sous-dossier ni de « .. »)."""
        F = db.home
        pattern = C.CONFLICT_RE_PROTECTED if vault_box is not None else C.CONFLICT_RE
        if not name or name.startswith(".") or "/" in name or "\\" in name or not pattern.match(name):
            raise HTTPException(404, "Version introuvable")
        src = F / name
        if not src.is_file():
            raise HTTPException(404, "Version introuvable")
        return src

    @app.get("/reglages/conflit/export")
    def export_conflict(nom: str = ""):
        """L'autre version, exportée comme des voyages (.zip) : « Importer un voyage » les ajoute à côté des voyages
        actuels, sans rien écraser. Rien n'est modifié dans le dossier."""
        import shutil as _sh
        import tempfile
        src = _conflict_file(nom)
        with tempfile.TemporaryDirectory(prefix="tripcompta-version-") as tmp:
            work = Path(tmp)
            try:
                if vault_box is not None:
                    if vault_box.vault is None:
                        raise HTTPException(423, "Trip Compta est verrouillé")
                    vault_box.vault.decrypt_file(src, work / C.DB, "base")
                else:
                    C.sqlite_copy(src, work / C.DB)
                if not C.is_valid_db(work / C.DB):
                    raise ValueError("abîmée")
                other = Database(work, app_version=upd.version)      # mise à niveau de la copie, pas de l'original
                with other.Session() as s_:
                    trips = s_.query(M.Trip).order_by(M.Trip.id).all()
                    data = TR.export_zip(s_, trips, db.receipts_dir)
                other.engine.dispose()
            except HTTPException:
                raise
            except Exception:
                return refuse("/reglages/donnees#conflits", "Cette version est abîmée : elle ne peut pas être exportée.")
            finally:
                _sh.rmtree(work, ignore_errors=True)
        stamp = dt.datetime.fromtimestamp(src.stat().st_mtime).strftime("%Y-%m-%d %Hh%M")
        name = f"autre version des voyages {stamp}.zip"
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    @app.post("/reglages/conflit")
    async def resolve_conflict(request: Request):
        """Ranger une copie en conflit dans « Anciennes versions » (jamais effacée)."""
        f = await request.form()
        src = _conflict_file(f.get("nom") or "")
        side = db.home / OLD_VERSIONS
        side.mkdir(exist_ok=True)
        dst = side / src.name
        n_ = 2
        while dst.exists():
            dst = side / f"{src.stem} ({n_}){src.suffix}"
            n_ += 1
        os.replace(src, dst)
        return back("/reglages/donnees", f"Version rangée dans le dossier « {OLD_VERSIONS} » (rien n'est effacé).")

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
        conf = {"mcpServers": {"tripcompta": {"command": cmd, "args": args,
                                                  **({} if frozen else {"cwd": root})}}}
        if not frozen:
            conf["mcpServers"]["tripcompta"]["env"] = {"PYTHONPATH": root}
        cli = "claude mcp add tripcompta -- " + " ".join(
            [f'"{cmd}"'] + [f'"{a}"' if " " in a else a for a in args])
        return dict(conf=json.dumps(conf, indent=2, ensure_ascii=False), cli=cli,
                    api=str(request.base_url) + "api/docs", api_token=app.state.session_token)

    def drafts() -> Path:
        """Dossier des brouillons IA : recalculé à chaque fois (la base peut être rouverte ailleurs, par exemple
        après le déverrouillage d'un dossier protégé)."""
        d_ = db.dir / "brouillons_ia"
        d_.mkdir(exist_ok=True)
        return d_
    INIT, UPDATE = "initialisation", "mise-a-jour"
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
        saved = dt.datetime.fromtimestamp(dfile.stat().st_mtime) if draft is not None else None
        return render(request, "ai.html", trip=t, ia_mode=mode, INIT=INIT, UPDATE=UPDATE, modele=blank,
                      guide=importer.guide(), state=trip_state_for_ai(t), draft=draft, draft_saved=saved,
                      has_prev=dprev.exists(), cur=f"/v/{t.id}/ia", ai_notes=S.ai_notes(t),
                      **mcp_conf(request), **ctx)

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
        n = len(res.get("a_verifier") or [])
        more = (f" {n} point{'s' if n > 1 else ''} à vérifier {'sont gardés' if n > 1 else 'est gardé'} "
                "plus bas, dans « À regarder ».") if n else ""
        return back(f"/v/{tid}", res["message"] + more)

    @app.post("/v/{tid}/ia/point-verifie")
    async def ai_note_done(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        dest = f"/v/{tid}/ia" if f.get("retour") == "ia" else f"/v/{tid}"
        if not any(n["texte"] == str(f.get("texte") or "").strip() for n in S.ai_notes(t)):
            return back(dest, "Ce point était déjà retiré")
        db.snapshot("point de l'IA vérifié")
        S.drop_ai_note(t, f.get("texte"))
        s.commit()
        return back(dest, "Point vérifié : il est retiré de la liste")

    # ------------------------------------------------------------------ voyages
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, s: Session = Depends(get_s)):
        trips = s.query(M.Trip).order_by(M.Trip.is_demo, M.Trip.created.desc(), M.Trip.id.desc()).all()   # récents
        # d'abord ; le voyage d'exemple en dernier
        cards = []
        for t in trips:
            r = S.compute_trip(t)
            cards.append((t, r))
        q = request.query_params
        stored = S.asso_name(s)
        return render(request, "index.html", cards=cards, data_dir=db.home,
                      asso_default="" if stored == "l'asso" else stored,
                      diving_default=bool(trips) and any(S.is_diving(t_) for t_ in trips[:1] if not t_.is_demo),
                      open_new=q.get("nouveau") == "1", open_import=q.get("importer") == "1")

    @app.post("/voyages")
    async def create_trip(request: Request, s: Session = Depends(get_s)):
        f = await request.form()
        name = (f.get("name") or "").strip()
        if not name:
            return refuse("/?nouveau=1", "Donne un nom au voyage", "name")
        holder = " ".join(str(f.get("cashier_name") or "").split())[:200]
        if f.get("caisse") == "personne" and not holder:
            return refuse("/?nouveau=1", "Écris le nom de la personne qui tient la caisse", "cashier_name")
        db.snapshot(f"création du voyage « {name} »")
        t = W.create_trip(s, name, place=f.get("place", ""), organizer=f.get("organizer", ""),
                          start=S.parse_date(f.get("start")), end=S.parse_date(f.get("end")),
                          diving=bool(f.get("plongee")))
        who = ""
        if f.get("caisse") == "personne":           # pas d'association : une personne du voyage tient la caisse
            p = W.add_participant(s, t, holder, roles=[S.DIVER] if S.is_diving(t) else [])
            t.cashier_id = p.id
            who = f" {holder} tient la caisse."
        elif "asso_name" in f and str(f.get("asso_name") or "").strip():
            S.set_trip_asso_name(t, str(f.get("asso_name")), s)
        s.commit()
        return back(f"/v/{t.id}/participants?ajout=1#ajout", f"Voyage créé.{who} Ajoute maintenant les participants.")

    @app.post("/v/{tid}/jeu-plongee")
    def add_diving_set(tid: int, s: Session = Depends(get_s)):
        """Ajoute au voyage les rôles Plongeur / Accompagnant et les catégories de plongée."""
        t = trip_or_404(s, tid)
        if S.is_diving(t) and all(E.sort_key(n) in {E.sort_key(c.name) for c in t.categories}
                                  for n, _ in S.DIVING_CATEGORIES):
            return back(f"/v/{tid}/participants/organisation#roles", "Ce voyage a déjà les réglages de plongée")
        db.snapshot("réglages de plongée ajoutés")
        added = S.add_diving_set(t)
        s.commit()
        return back(f"/v/{tid}/participants/organisation#roles",
                    "Réglages de plongée ajoutés : rôles Plongeur et Accompagnant"
                    + (f", catégories {', '.join(added)}" if added else ""))

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

    # ------------------------------------------------------------------ clôturer, rouvrir, dupliquer
    @app.post("/v/{tid}/cloturer")
    def trip_close(tid: int, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        db.snapshot(f"clôture du voyage « {t.name} »")
        S.close_trip(t)
        s.commit()
        return back(f"/v/{tid}", "Voyage clôturé : ses comptes sont figés. Tu peux toujours le consulter, "
                                 "l'exporter, et le rouvrir si besoin.")

    @app.post("/v/{tid}/rouvrir")
    def trip_reopen(tid: int, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        if not S.is_closed(t):
            return back(f"/v/{tid}", "Ce voyage est déjà ouvert")
        db.snapshot(f"réouverture du voyage « {t.name} »")
        S.reopen_trip(t)
        s.commit()
        return back(f"/v/{tid}", "Voyage rouvert : tu peux de nouveau le modifier")

    @app.get("/v/{tid}/dupliquer", response_class=HTMLResponse)
    def trip_duplicate_page(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        wd = {w.participant_id for w in t.withdrawals}
        return render(request, "duplicate.html", trip=t, r=S.compute_trip(t),
                      proposed=S.next_trip_name(t.name, [x.name for x in s.query(M.Trip).all()]),
                      n_people=len(t.participants), n_withdrawn=len(wd), n_expenses=len(t.expenses))

    @app.post("/v/{tid}/dupliquer")
    async def trip_duplicate(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        name = (f.get("name") or "").strip()[:200]
        if not name:
            return refuse(f"/v/{tid}/dupliquer", "Donne un nom au nouveau voyage", "name")
        if any(E.sort_key(x.name) == E.sort_key(name) for x in s.query(M.Trip).all()):
            return refuse(f"/v/{tid}/dupliquer", f"Un voyage s'appelle déjà « {name} » : choisis un autre nom", "name")
        start, end = S.parse_date(f.get("start")), S.parse_date(f.get("end"))
        if start and end and end < start:
            return refuse(f"/v/{tid}/dupliquer", "La date de fin est avant la date de début", "end")
        people, expenses = bool(f.get("participants")), bool(f.get("depenses"))
        db.snapshot(f"voyage « {name} » créé à partir de « {t.name} »")
        new = TR.duplicate_trip(s, t, name, start, end, with_people=people, with_expenses=expenses)
        s.commit()
        S.store_draws(new)
        s.commit()
        parts = []
        if people:
            n = len(new.participants)
            parts.append(f"{n} participant{'s' if n > 1 else ''}")
        if expenses:
            n = len(new.expenses)
            parts.append(f"{n} dépense{'s' if n > 1 else ''} reprise{'s' if n > 1 else ''} comme montant{'s' if n > 1 else ''} prévu{'s' if n > 1 else ''}")
        return back(f"/v/{new.id}", f"Voyage « {new.name} » créé à partir de « {t.name} »"
                    + (" : " + ", ".join(parts) if parts else "") + ". Rien n'a changé dans l'ancien voyage.")

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

    # ------------------------------------------------------------------ voyage d'exemple (Hendaye 2026 – démo)
    @app.post("/exemple/ouvrir")
    def example_open(s: Session = Depends(get_s)):
        from . import transfer as TR
        t = s.query(M.Trip).filter(M.Trip.is_demo.is_(True)).first()
        if t is not None:
            return back(f"/v/{t.id}", "Le voyage d'exemple est déjà là")
        db.snapshot("ouverture du voyage d'exemple")
        t = TR.import_example(s, db.receipts_dir)
        s.commit()
        return back(f"/v/{t.id}", "Voici un voyage d'exemple, entièrement fictif : explore-le et modifie-le librement.")

    def _demo_or_404(s, tid):
        t = trip_or_404(s, tid)
        if not t.is_demo:
            raise HTTPException(404, "Ce n'est pas le voyage d'exemple")
        return t

    @app.post("/v/{tid}/exemple/retirer")
    def example_remove(tid: int, s: Session = Depends(get_s)):
        t = _demo_or_404(s, tid)
        db.snapshot("retrait du voyage d'exemple")
        _delete_trip(s, t)
        s.commit()
        return back("/", "Voyage d'exemple retiré. Tu pourras le rouvrir depuis ⚙️ Réglages.")

    @app.post("/v/{tid}/exemple/recommencer")
    def example_reset(tid: int, s: Session = Depends(get_s)):
        from . import transfer as TR
        t = _demo_or_404(s, tid)
        db.snapshot("voyage d'exemple remis à neuf")
        _delete_trip(s, t)
        s.flush()
        t = TR.import_example(s, db.receipts_dir)
        s.commit()
        return back(f"/v/{t.id}", "Voyage d'exemple remis à neuf.")

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
        r = S.compute_trip(t)
        asso_paid = sum(c for e in t.expenses if e.actual is not None and not e.is_income
                        for pid, c in e.splits() if pid is None)
        cat_totals = {}
        for e in t.expenses:
            cn = e.category.name if e.category else "Divers"
            cat_totals[cn] = cat_totals.get(cn, 0) + r.expense_totals.get(e.id, 0)
        return render(request, "dashboard.html", trip=t, r=r, alerts=S.alerts(t, r), msum=S.money_summary(t, r),
                      ai_notes=S.ai_notes(t), start_steps=S.getting_started(t),
                      asso_paid=asso_paid, cat_totals=cat_totals,
                      adv_total=sum(a["amount"] for a in S.advances(t) if not a["income"]),
                      withdrawn={w.participant_id for w in t.withdrawals})

    # ------------------------------------------------------------------ participants
    @app.get("/v/{tid}/participants", response_class=HTMLResponse)
    def participants(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        r = S.compute_trip(t)
        groups = S.trip_groups(t)
        known = S.known_people(t)
        return render(request, "participants.html", trip=t, r=r, groups=groups, export_cols=S.EXPORT_COLS,
                      known=known, known_trips=S.known_trips(t, known),
                      known_fill={S.person_key(x["name"]): dict(phone=x["phone"], email=x["email"],
                                                                companion=x["companion"]) for x in known},
                      open_add=request.query_params.get("ajout") == "1",
                      open_list=request.query_params.get("liste") == "1", list_text="",
                      open_edit=int(request.query_params.get("modifier") or 0) if (request.query_params.get("modifier") or "").isdigit() else 0,
                      pgroups={p.id: [g["name"] for g in groups if p.id in g["ids"]] for p in t.participants},
                      withdrawn={w.participant_id for w in t.withdrawals},
                      withdrawn_dates={w.participant_id: w.date for w in t.withdrawals},
                      has_expenses=bool(t.expenses))

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
        return render(request, "organisation.html", trip=t, groups=S.trip_groups(t), diving=S.is_diving(t),
                      role_members={rk: S.with_role(t, rk) for rk in S.trip_roles(t)})

    @app.post("/v/{tid}/roles-du-voyage")
    async def add_role_route(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        name = str((await request.form()).get("name") or "")
        db.snapshot(f"nouveau rôle {name}")
        try:
            key = S.add_trip_role(t, name)
        except ValueError as e:
            return refuse(f"/v/{tid}/participants/organisation#roles", str(e))
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
            return refuse(f"/v/{tid}/participants/organisation#roles", str(e))
        s.commit()
        return back(f"/v/{tid}/participants/organisation#roles", f"Rôle « {S.role_label(key)} » supprimé de ce voyage")

    @app.post("/v/{tid}/roles/{role}")
    async def save_role_members(tid: int, role: str, request: Request, s: Session = Depends(get_s)):
        """Onglet Organisation : les membres d'un rôle (organisateur : un seul). Ceux qui y entrent se voient proposer
        les dépenses qui concernaient exactement ce rôle."""
        t = trip_or_404(s, tid)
        if role not in S.trip_roles(t):
            raise HTTPException(404, "Rôle inconnu")
        ids = {int(x) for x in (await request.form()).getlist("members") if str(x).isdigit()}
        if role == S.ORGANIZER and len(ids) > 1:
            return refuse(f"/v/{tid}/participants/organisation#roles", "Un seul organisateur : choisis une personne")
        db.snapshot(f"rôle {S.role_label(role)}")
        before = S.label_members(t)
        for p in t.participants:
            rs = [r for r in p.role_list if r != role]
            if p.id in ids:
                rs.append(role)
            S.set_roles(t, p, rs)
        s.commit()
        n = len(ids)
        return _then_join(t, before, f"/v/{tid}/participants/organisation#roles",
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
            return refuse(f"/v/{tid}/participants/organisation#familles", str(e))
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
        before = S.label_members(t)
        try:
            S.save_group(t, f.get("name") or "", f.getlist("members"), old)
        except ValueError as e:
            return refuse(f"/v/{tid}/participants/organisation#groupes", str(e))
        s.commit()
        g = S.find_group(t, f.get("name"))
        return _then_join(t, before, f"/v/{tid}/participants/organisation#groupes",
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
            return refuse(f"/v/{tid}/participants?ajout=1#ajout", "Coche au moins une personne")
        db.snapshot(f"reprise de {len(people)} participant(s) d'autres voyages")
        before = S.label_members(t)
        added = []
        for x in people:
            dflt = S.COMPANION if x["companion"] else S.DIVER
            added.append(W.add_participant(s, t, x["name"], roles=[dflt] if dflt in S.trip_roles(t) else [],
                                           companion=x["companion"], phone=x["phone"], email=x["email"]))
        s.commit()
        msg = f"{added[0].name} ajouté" if len(added) == 1 else f"{len(added)} participants ajoutés"
        return _then_join(t, before, f"/v/{tid}/participants", msg)

    @app.post("/v/{tid}/participants")
    async def add_participants(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        raw = f.get("names") or f.get("name") or ""
        many = "names" in f                                  # « Plusieurs personnes d'un coup » : un nom par ligne
        new, known_already = [], 0
        existing = {E.sort_key(p.name) for p in t.participants}
        for line in re.split(r"[\n;]", raw):
            n = re.sub(r"\s+", " ", line).strip().strip(",").strip()   # tabulations d'un tableur : des espaces
            if n and E.sort_key(n) not in existing:
                new.append(n)
                existing.add(E.sort_key(n))
            elif n:
                known_already += 1
        if not new and many:
            return refuse(f"/v/{tid}/participants?ajout=1&liste=1",
                          "Tous ces noms sont déjà dans la liste" if known_already else "Écris au moins un nom, un par ligne")
        if not new:
            return refuse(f"/v/{tid}/participants?ajout=1#ajout", "Ce nom est vide ou déjà dans la liste", "name")
        roles_in = _roles_from_form(f)
        companion = S.COMPANION in roles_in
        family = (_family_from_form(f) or "").strip()
        db.snapshot(f"ajout de {len(new)} participant(s)")
        before = S.label_members(t)
        kp = S.known_person(t, new[0]) if len(new) == 1 else None   # déjà venu sur un autre voyage ?
        details = {}
        if len(new) == 1 and "name" in f:        # formulaire « un participant » : toutes ses infos
            details = {k: f.get(k, "").strip() for k in ("phone", "email", "note")}
            if kp:                               # déjà venu sur un autre voyage : ses contacts s'il n'en a pas saisi
                details["phone"], details["email"] = details["phone"] or kp["phone"], details["email"] or kp["email"]
        added = [W.add_participant(s, t, n, roles=roles_in, companion=companion, family=family, **details)
                 for n in new]
        if details and f.getlist("groups"):
            S.set_person_groups(t, added[0].id, f.getlist("groups"))
        s.commit()
        if len(added) == 1:
            msg = f"{added[0].name} ajouté{' (accompagnant)' if companion else ''}"
        else:
            msg = f"{len(new)} {'accompagnants' if companion else 'participants'} ajoutés"
        if known_already and many:
            msg += f" ({known_already} déjà dans la liste)"
        if family:
            msg += f" dans la famille {S.find_family(t, family).name}"
        if "join_form" in f or f.get("join_all"):   # ancien envoi (scripts) : les dépenses à rejoindre sont données
            if "join_form" in f:
                joined = [e for e in t.expenses if f.get(f"join_{e.id}")]
                for e in joined:
                    for p in added:
                        W.join_expense(e, p.id)
            else:
                joined = W.join_group_expenses(t, added, before)
            s.commit()
            n = len({e.id for e in joined})
            if n:
                msg += f", inclus dans {n} dépense(s) de groupe"
            return back(f"/v/{tid}/participants#pe{added[0].id}" if len(added) == 1 else f"/v/{tid}/participants", msg)
        if f.get("again"):                                   # « Ajouter et continuer » : le formulaire reste ouvert
            return _then_join(t, before, f"/v/{tid}/participants?ajout=1#ajout", msg)
        return _then_join(t, before, f"/v/{tid}/participants#pe{added[0].id}" if len(added) == 1
                          else f"/v/{tid}/participants", msg)

    @app.post("/v/{tid}/participants/{pid}")
    async def edit_participant(tid: int, pid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        p = s.get(M.Participant, pid)
        if not p or p.trip_id != tid:
            raise HTTPException(404, "Participant introuvable")
        f = await request.form()
        db.snapshot(f"modification de {p.name}")
        before = S.label_members(t)
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
        return _then_join(t, before, f"/v/{tid}/participants", f"{p.name} modifié")

    def _safe_next(tid: int, url: str) -> str:
        url = str(url or "")
        return url if url.startswith(f"/v/{tid}/") or url == f"/v/{tid}" else f"/v/{tid}/participants"

    @app.get("/v/{tid}/rejoindre", response_class=HTMLResponse)
    def join_page(tid: int, request: Request, s: Session = Depends(get_s)):
        """« Julie est maintenant dans Plongeurs : l'ajouter aussi à ces dépenses ? » (pré-coché)."""
        t = trip_or_404(s, tid)
        q = request.query_params
        nxt = _safe_next(tid, q.get("next"))
        whys = (q.get("w") or "").split("|")
        people = {p.id: p for p in t.participants}
        exps = {e.id: e for e in t.expenses}
        r = S.compute_trip(t)
        rows: dict = {}
        for item in (q.get("s") or "").split(","):
            parts = item.split(".")
            try:
                pid, eid, wi = (int(x) for x in parts[:3])
                ji = int(parts[3]) if len(parts) > 3 else wi
                kind = {"g": "groupes", "a": "autres", "v": "verifier"}.get(parts[4] if len(parts) > 4 else "g")
                oi = int(parts[5]) if len(parts) > 5 else -1
            except (ValueError, IndexError):
                continue
            p, e = people.get(pid), exps.get(eid)
            if p is None or e is None or kind is None or pid in {x.participant_id for x in e.shares}:
                continue
            rows.setdefault(pid, dict(p=p, whys=[], items=[], checks=[]))
            text = (lambda i: whys[i] if 0 <= i < len(whys) else "")
            if text(ji) and text(ji) not in rows[pid]["whys"]:
                rows[pid]["whys"].append(text(ji))
            it = dict(e=e, why=text(wi), others=text(oi), kind=kind, total=r.expense_totals.get(e.id, 0),
                      n=len(e.shares))
            rows[pid]["checks" if kind == "verifier" else "items"].append(it)
        if not rows:
            return RedirectResponse(nxt, status_code=303)
        n_checks = sum(len(x["checks"]) for x in rows.values())
        return render(request, "join.html", trip=t, rows=list(rows.values()), next_url=nxt, n_checks=n_checks,
                      any_items=any(x["items"] for x in rows.values()))

    @app.post("/v/{tid}/rejoindre")
    async def join_save(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        nxt = _safe_next(tid, f.get("next"))
        people = {p.id: p for p in t.participants}
        exps = {e.id: e for e in t.expenses}
        wanted = []
        for v in f.getlist("add"):
            try:
                pid, eid = (int(x) for x in str(v).split("."))
            except ValueError:
                continue
            e = exps.get(eid)
            if pid in people and e is not None and pid not in {x.participant_id for x in e.shares} \
                    and not any(x.weight != 1 or x.fixed is not None for x in e.shares):   # parts personnalisées : non
                wanted.append((pid, e))
        if not wanted:
            return back(nxt, "Aucune dépense changée")
        db.snapshot("ajout aux dépenses")
        for pid, e in wanted:
            W.join_expense(e, pid)
        s.commit()
        names = list(dict.fromkeys(people[pid].name for pid, _ in wanted))
        n = len({e.id for _, e in wanted})
        who = names[0] if len(names) == 1 else f"{len(names)} personnes"
        return back(nxt, f"{who} ajouté{'s' if len(names) > 1 else ''} à {n} dépense{'s' if n > 1 else ''}")

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
            keep_custom = [r for r in p.role_list if r.startswith(S.CUSTOM)]
            if f.get(f"dp_{p.id}"):                    # ancien formulaire : DP est devenu une étiquette ordinaire
                keep_custom += [r for r in S.parse_roles(["dp"], t) if r not in keep_custom]
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
            before = S.label_members(t)
            try:
                S.save_family(s, t, f.get("name"), f.getlist("members"), f.get("referent"), fam)
            except ValueError as e:
                s.rollback()
                return refuse(f"/v/{tid}/participants/organisation#familles", str(e))
            s.commit()
            return _then_join(t, before, f"/v/{tid}/participants/organisation#familles", f"Famille {fam.name} modifiée")
        name = (f.get("name") or "").strip()
        if name.lower().startswith("famille "):
            name = name[8:].strip()
        other = S.find_family(t, name) if name else None
        if other is not None and other.id != fam.id:
            return refuse(f"/v/{tid}/participants", f"Il y a déjà une famille {other.name}")
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
            return refuse(f"/v/{tid}/participants",
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
        extra = "".join(f"&{k}={quote(v)}" for k, v in request.query_params.items() if k in ("msg", "e", "f", "d", "r"))
        return RedirectResponse(f"/v/{tid}/participants/comptes?p={pid}{extra}#p{pid}", status_code=303)

    @app.get("/v/{tid}/participants/comptes", response_class=HTMLResponse)
    def people_accounts(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        mode = E.ACTUAL
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
    def expenses(tid: int, request: Request, s: Session = Depends(get_s), tri: str = ""):
        t = trip_or_404(s, tid)
        r = S.compute_trip(t)
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
        # tri : par catégorie (comme d'habitude), ou toutes les dépenses en une seule liste
        flat = {"date": ("Toutes les dépenses, les plus récentes en haut",
                         lambda e: (e.date is None, -(e.date.toordinal() if e.date else 0), E.sort_key(e.label), e.id)),
                "montant": ("Toutes les dépenses, les plus grosses en haut",
                            lambda e: (-abs(r.expense_totals.get(e.id, 0)), E.sort_key(e.label), e.id)),
                "nom": ("Toutes les dépenses, de A à Z", lambda e: (E.sort_key(e.label), e.id))}.get(tri)
        if flat:
            groups, with_subs = [(None, sorted(t.expenses, key=flat[1]))], set()
        paid_by = {pid for e in t.expenses for pid, _ in e.splits()}
        return render(request, "expenses.html", trip=t, r=r, groups=groups, nb=len(t.participants), bad=bad,
                      who_label=S.who_label, who_detail=S.who_detail, payer_label=S.payer_label, names=names(t),
                      subtotals=subtotals, sort=tri if flat else "", flat_title=flat[0] if flat else None,
                      payers=[p for p in t.participants if p.id in paid_by], asso_pays=None in paid_by,
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
        # plus de « parts » : une part ≠ 1 (dépense d'avant, ou IA sans montant connu) s'affiche comme un montant
        # fixé 🔒 ; enregistrer la dépense la convertit pour de bon
        equal = E.split_exact(total, {p: 1 for p in weights}) if total else {}
        for p, w in list(weights.items()):
            if w != 1 and p not in fixed and p in amounts and abs(amounts[p] - equal.get(p, -10)) > 1:
                fixed[p] = amounts[p]
            weights[p] = 1.0
        return dict(trip=t, e=e, weights=weights, linked=linked, groups=S.trip_groups(t),
                    who_roles=who_roles, who_families=who_families, who_groups=who_groups, payers=payers, subcats=subcats,
                    split_mode=split_mode, amounts=amounts, fixed=fixed, cents=cents)

    @app.get("/v/{tid}/depenses/nouvelle", response_class=HTMLResponse)
    def new_expense(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        if not t.participants:
            return refuse(f"/v/{tid}/participants", "Ajoute d'abord les participants")
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
                    raise FormError(f"Montant invalide pour {p.name} : « {f.get(f'm_{p.id}')} ». Exemple : 12,50",
                                    f"m_{p.id}")
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
        diving = S.is_diving(t)                      # les catégories de plongée : seulement pour un voyage de plongée
        available = [n for n in S.category_library(s) if E.sort_key(n) not in have
                     and (diving or not S.is_diving_category(n))]
        custom = {E.sort_key(n) for n in S.category_library(s) if S.is_custom_category(s, n)}
        return render(request, "categories.html", trip=t, available=available, custom=custom, sort_key=E.sort_key,
                      subcats=S.subcategories_for(s, t))

    @app.post("/v/{tid}/categories/{cid}/sous-categories")
    async def add_subcat(tid: int, cid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        c = cat_or_404(t, cid)
        name = clean_cat_name((await request.form()).get("name"))
        if not name:
            return refuse(f"/v/{tid}/categories#c{cid}", "Donne un nom à la sous-catégorie")
        if E.sort_key(name) in {E.sort_key(x) for x in S.subcategories_for(s, t).get(c.name, [])}:
            return refuse(f"/v/{tid}/categories#c{cid}", f"« {name} » existe déjà dans « {c.name} »")
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
            return refuse(f"/v/{tid}/categories", "Donne un nom à la catégorie", "name")
        if any(E.sort_key(c.name) == E.sort_key(name) for c in t.categories):
            return refuse(f"/v/{tid}/categories", f"« {name} » est déjà dans ce voyage")
        if E.sort_key(name) in {E.sort_key(n) for n in S.category_library(s)}:
            return refuse(f"/v/{tid}/categories", f"« {name} » est déjà dans les catégories disponibles")
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
                    return refuse(f"/v/{tid}/categories", "« Divers » est utilisée par des dépenses : change d'abord "
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
            return refuse(f"/v/{tid}/categories", "Seules les catégories que tu as créées peuvent être supprimées")
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
            return refuse(request.headers.get("referer") or f"/v/{tid}/depenses/nouvelle",
                          "Donne un nom à la dépense", "label")
        # Plusieurs tickets : lignes « ticket_amount / ticket_date », ou « 80,12 + 69,10 » tapé dans le montant
        tickets = []
        for a_, d_ in zip(f.getlist("ticket_amount"), f.getlist("ticket_date")):
            c_ = amount({"x": a_}, "x")
            if c_:
                tickets.append((c_, S.parse_date(d_)))
        raw_actual = str(f.get("actual") or "")
        if not tickets and "+" in raw_actual.lstrip("+"):
            tickets = [(c_, None) for c_ in (amount({"x": p_}, "x", "actual") for p_ in raw_actual.split("+")) if c_]
        raw_planned = str(f.get("planned") or "")
        planned = (sum(amount({"x": p_}, "x", "planned") or 0 for p_ in raw_planned.split("+")) or None) \
            if "+" in raw_planned.lstrip("+") else amount(f, "planned")
        actual = sum(c_ for c_, _ in tickets) if tickets else amount(f, "actual")
        ref = request.headers.get("referer", f"/v/{tid}/depenses/nouvelle")
        if not weights_ok(t, f):
            return refuse(ref, "Coche au moins une personne pour « Pour qui ? »")
        if planned is None and actual is None and not f.get("pending"):
            return refuse(ref, "Indique au moins un montant (prévu ou réel), ou coche « Montant pas encore connu »",
                          "actual")
        weights, fixed = _parse_shares(t, f)
        if fixed:
            total_ = (actual if actual is not None else planned) or 0
            if not total_:
                return refuse(ref, "Indique le montant de la dépense avant de fixer des montants")
            refund_ = (amount(f, "refund") or 0) if f.get("has_refund") else 0
            total_ = max(0, total_ - refund_)          # c'est le coût, remboursement déduit, qui est partagé
            got = sum(fixed.values())
            if got > total_:
                what = "le coût à partager" if refund_ else "la dépense"
                return refuse(ref, f"Les montants fixés font {E.fmt(got)}, plus que {what} ({E.fmt(total_)})")
            if len(fixed) == len(weights) and got != total_:
                return refuse(ref, f"Il reste {E.fmt(total_ - got)} à répartir : tous les montants sont fixés. "
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
                return refuse(ref, "Indique combien chaque payeur a payé")
            total_ = (actual if actual is not None else planned) or 0
            if total_ and sum(c for _, c in payments) > total_:
                return refuse(ref, f"Les payeurs totalisent {E.fmt(sum(c for _, c in payments))}, "
                                   f"plus que le montant ({E.fmt(total_)})")
        cat = f.get("category")
        if cat == "__new__":
            cat = f.get("category_new")
        sub = f.get("subcategory") or ""
        if sub == "__new__":
            sub = f.get("subcategory_new") or ""
        refund = (amount(f, "refund") if f.get("has_refund") else None) or None
        if refund and refund > ((actual if actual is not None else planned) or 0):
            raise FormError("Le montant remboursé dépasse le montant de la dépense", "refund")
        who_ = f.get("refund_to") or "asso"
        payer = f.get("payer") or "asso"
        values = dict(label=label, category=cat, subcategory=sub,
                      kind="recette" if f.get("kind") == "recette" else "depense", planned=planned, actual=actual,
                      tickets=tickets, date=S.parse_date(f.get("date")),
                      payer_id=None if (payer == "asso" or payments) else whole(payer, -1), payments=payments,
                      weights=weights, fixed=fixed, refund=refund,
                      refund_to_id=None if (who_ == "asso" or not refund) else whole(who_, -1),
                      note=f.get("note", ""), pending=bool(f.get("pending")), reviewed=True)
        W.check_expense(t, **values)             # avant la copie « Défaire » : refusé, rien n'est écrit
        same_exp, e = None, None
        if eid:
            e = s.get(M.Expense, eid)
            if not e or e.trip_id != tid:
                raise HTTPException(404, "Dépense introuvable")
            db.snapshot(f"modification de la dépense « {e.label} »")
        else:
            same_exp = S.duplicate_expense(t, label, actual if actual is not None else planned)
            db.snapshot(f"ajout de la dépense « {label} »")
        e = W.save_expense(s, t, e, **values)     # le même chemin que l'IA : mêmes règles, mêmes lignes
        if "cents_mode" in f:          # centimes d'arrondi : le choix et le tirage vus à l'écran
            e.cents_mode = f.get("cents_mode") if f.get("cents_mode") in (E.ROUND_PARTICIPANTS, E.ROUND_ASSO) else ""
            e.cents_who = ",".join(str(x) for x in S.cents_ids(f.get("cents_who")))
        # justificatifs existants
        wanted = set(wholes(f.getlist("receipts")))
        s.query(M.ReceiptLink).filter_by(expense_id=e.id).delete()
        for rid in wanted:
            s.add(M.ReceiptLink(receipt_id=rid, expense_id=e.id))
        # nouveaux fichiers
        not_joined = []
        for up in f.getlist("files"):
            if getattr(up, "filename", None):
                rec = await _store_receipt(s, t, up)
                if rec is None:                  # ni photo ni PDF : la dépense est enregistrée, sans ce fichier
                    not_joined.append(Path(up.filename).name)
                    continue
                s.add(M.ReceiptLink(receipt_id=rec.id, expense_id=e.id))
        s.commit()
        if not_joined:
            return back(f"/v/{tid}/depenses/{e.id}/modifier", f"« {label} » enregistrée, mais " + _refused_files(not_joined),
                        err=True)
        twice = ""
        if same_exp is not None:                # même nom, même montant : sans doute saisie deux fois
            twice = (f". ⚠️ Attention : une dépense « {same_exp.label} » de {E.fmt(same_exp.best_known())} existait "
                     "déjà. Si c'est la même, clique sur « Défaire »")
        if f.get("again"):
            return back(f"/v/{tid}/depenses/nouvelle", f"« {label} » enregistrée{twice}. Dépense suivante :")
        return back(f"/v/{tid}/depenses#e{e.id}", f"« {label} » enregistrée{twice}")

    @app.post("/v/{tid}/depenses/{eid}/verifiee")
    async def expense_checked(tid: int, eid: int, s: Session = Depends(get_s)):
        """« C'est bon » : la dépense à vérifier ne concerne pas la (les) personne(s) signalée(s)."""
        trip_or_404(s, tid)
        e = s.get(M.Expense, eid)
        if not e or e.trip_id != tid:
            raise HTTPException(404, "Dépense introuvable")
        e.checks.clear()
        s.commit()
        return back(f"/v/{tid}/depenses#e{eid}", f"« {e.label} » vérifiée")

    @app.post("/v/{tid}/depenses/{eid}/supprimer")
    async def delete_expense(tid: int, eid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        e = s.get(M.Expense, eid)
        if not e or e.trip_id != tid:
            raise HTTPException(404, "Dépense introuvable")
        db.snapshot(f"suppression de la dépense « {e.label} »")
        label = W.delete_expense(s, t, e)
        s.commit()
        nxt = (await request.form()).get("next") or ""
        return back(nxt if nxt.startswith(f"/v/{tid}/") else f"/v/{tid}/depenses", f"« {label} » supprimée")

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
        r = S.compute_trip(t)
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
        r = S.compute_trip(t)
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
            return refuse(here + "?remise=0", "Choisis la personne", "participant")
        try:
            val = E.parse_amount(f.get("value"))
        except ValueError:
            return refuse(here + f"?remise={pid}", f"Montant invalide : « {f.get('value')} ». Exemple : 50", "value")
        if not val or val <= 0:
            return refuse(here + f"?remise={pid}", "Indique un montant (ex. 50)", "value")
        by = f.get("by") if f.get("by") in ("asso", "person") else "others"
        comp = whole(f.get("compensator")) if by == "person" else 0
        if by == "person" and comp not in names(t):
            return refuse(here + f"?remise={pid}", "Choisis la personne qui compense", "compensator")
        if by == "person" and comp == pid:
            return refuse(here + f"?remise={pid}", "La personne qui compense ne peut pas être celle qui a la remise",
                          "compensator")
        aid = f.get("aid") or ""
        a = next((x for x in t.adjustments if str(x.id) == aid and x.expense_id is None), None) if aid else None
        who = names(t)[pid]
        kind = "Supplément" if f.get("kind") == "increase" else "Remise"
        db.snapshot(f"{kind.lower()} pour {who}")
        W.save_discount(s, t, pid, (1 if kind == "Supplément" else -1) * abs(val),
                        {"asso": E.ASSO, "person": E.PERSON}.get(by, E.OTHERS), comp if by == "person" else None,
                        f.get("reason"), adjustment=a)
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
        r = S.compute_trip(t)
        return render(request, "viz.html", trip=t, r=r, V=S.trip_visuals(t, r))

    @app.get("/v/{tid}/asso", response_class=HTMLResponse)
    def asso_page(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        r = S.compute_trip(t)
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
            pid = whole(f.get("participant"))
            if pid not in {p.id for p in t.participants}:
                return refuse(f"/v/{tid}/asso", "Choisis le participant qui tient la caisse")
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
            return refuse(f"/v/{tid}/asso", "Choix d'arrondi inconnu")
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
        r = S.compute_trip(t)
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
        r = S.compute_trip(t)
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
                           mode=E.ACTUAL) if person_ else r
        return render(request, "withdraw.html", trip=t, r=r, r_full=r_full, sel=person_, exps=exps,
                      withdrawn=withdrawn, wcur=cur, today=dt.date.today().isoformat())

    def _withdraw_from_form(t, f):
        pid = whole(f.get("participant"))
        kept = set(wholes(f.getlist("kept")))
        return E.Withdrawal(pid, f.get("absorber", E.GROUP), kept, f.get("date", ""))

    @app.post("/v/{tid}/desistement/apercu", response_class=HTMLResponse)
    async def withdraw_preview(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        w = _withdraw_from_form(t, f)
        if not w.participant:
            return HTMLResponse('<p class="muted">Choisis une personne.</p>')
        mode = E.ACTUAL
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
            return refuse(f"/v/{tid}/desistement", "Choisis une personne")
        cur = next((x for x in t.withdrawals if x.participant_id == p.id), None)
        refund = amount(f, "refund")
        db.snapshot(f"{'modification du ' if cur else ''}désistement de {p.name}")
        _, new = W.save_withdrawal(s, t, p.id, date=S.parse_date(w.date), absorber=w.absorber, kept=w.kept,
                                   note=f.get("note") or "", refund=refund or None)
        s.commit()
        return back(f"/v/{tid}/p/{p.id}", f"Désistement de {p.name} {'enregistré' if new else 'modifié'}")

    @app.post("/v/{tid}/desistement/{pid}/annuler")
    async def withdraw_cancel(tid: int, pid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        nxt = str((await request.form()).get("next") or "")
        w = next((x for x in t.withdrawals if x.participant_id == pid), None)
        if not w:
            return refuse(f"/v/{tid}/p/{pid}", "Pas de désistement à annuler")
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
        r = S.compute_trip(t)
        rows = []
        for m in t.movements:
            if m.kind == "versement":
                rows.append(dict(date=m.date, label=f"Versement de {m.participant.name}", inc=m.amount, out=None,
                                 note=m.note, mid=m.id, m=m, method=S.pay_method_label(m.method)))
            else:
                rows.append(dict(date=m.date, label=f"Remboursement à {m.participant.name}", inc=None,
                                 out=m.amount, note=m.note, mid=m.id, m=m, method=S.pay_method_label(m.method)))
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
        # sans date d'abord (à compléter), puis la plus récente en haut
        rows.sort(key=lambda x: (x["date"] is not None, -(x["date"].toordinal() if x["date"] else 0)))
        total_in = sum(x["inc"] or 0 for x in rows)
        total_out = sum(x["out"] or 0 for x in rows)
        advances = S.advances(t)
        pending = [e for e in t.expenses if e.pending]
        to_pay = [e for e in t.expenses if e.actual is None and not e.pending and not e.is_income
                  and any(pid is None for pid, _ in e.splits())]
        return render(request, "money.html", trip=t, r=r, rows=rows, total_in=total_in, total_out=total_out,
                      advances=advances, to_pay=to_pay, pending=pending, payer_label=S.payer_label, names=names(t), today=dt.date.today().isoformat(),
                      sel=request.query_params.get("p"), kind=request.query_params.get("k", "versement"),
                      prefill=request.query_params.get("a", ""), tab=tab, methods=S.PAY_METHODS,
                      cheques=sorted(S.cheques_to_deposit(t), key=lambda m: (m.date is None, m.date or dt.date.min, m.id)),
                      open_form=tab == "mouvements" and bool(request.query_params.get("k") or request.query_params.get("p")))

    @app.post("/v/{tid}/argent")
    async def money_add(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        pid = whole(f.get("participant"))
        amt = amount(f, "amount")
        kind = f.get("kind", "versement")
        if pid not in names(t) or not amt or amt <= 0 or kind not in ("versement", "remboursement"):
            ref = urlparse(request.headers.get("referer") or "")      # retour là où le formulaire a été rempli
            here = (ref.path + ("?" + ref.query if ref.query else "")) if ref.path.startswith(f"/v/{tid}/argent") \
                else f"/v/{tid}/argent/mouvements?k={kind}"
            return refuse(here, "Choisis une personne et un montant positif", "amount")
        who = names(t)[pid]
        date = S.parse_date(f.get("date"))
        same = S.duplicate_movement(t, pid, kind, amt, date)
        db.snapshot(f"{kind} de {E.fmt(amt)} ({who})")
        W.add_movement(t, pid, kind, amt, date=date, note=f.get("note"),     # pas de date d'office
                       method=S.pay_method(f.get("method")) or "")
        s.commit()
        dest = f.get("next") or f"/v/{tid}/argent"
        if not dest.startswith("/"):
            dest = f"/v/{tid}/argent"
        txt = f"Versement de {E.fmt(amt)} par {who} enregistré" if kind == "versement" else \
            f"Remboursement de {E.fmt(amt)} à {who} enregistré"
        if same is not None:                    # sans doute saisi deux fois : on prévient, « Défaire » est juste à côté
            txt += (f". ⚠️ Attention : un {kind} de {E.fmt(amt)} était déjà noté pour {who}"
                    + (f" (le {same.date:%d/%m/%Y})" if same.date else "")
                    + ". Si c'est le même, clique sur « Défaire »")
        return back(dest, txt)

    @app.post("/v/{tid}/argent/lot")
    async def money_batch(tid: int, request: Request, s: Session = Depends(get_s)):
        """Plusieurs lignes cochées dans « Reste à régler » : un versement (ou un remboursement) par ligne, du
        montant qu'il reste exactement à régler. Les montants sont recalculés ici, jamais lus dans le formulaire."""
        t = trip_or_404(s, tid)
        f = await request.form()
        kind = f.get("kind", "versement")
        if kind not in ("versement", "remboursement"):
            raise HTTPException(400, "Type de mouvement inconnu")
        r = S.compute_trip(t)
        todo = S.settle_amounts(r, kind, [whole(x) for x in f.getlist("qui")])
        if not todo:
            return refuse(f"/v/{tid}/argent", "Coche d'abord au moins une ligne" if not f.getlist("qui") else
                          "Ces lignes sont déjà réglées : rien n'a été enregistré")
        total = sum(c for _, c in todo)
        received = kind == "versement"
        db.snapshot(f"{len(todo)} {kind}s notés d'un coup ({E.fmt(total)})" if len(todo) > 1 else
                    f"{kind} de {E.fmt(total)} ({todo[0][0].name})")
        W.settle_batch(t, r, kind, [acc.referent for acc, _ in todo], date=S.parse_date(f.get("date")),
                       method=S.pay_method(f.get("method")) or "", note=(f.get("note") or "").strip())
        s.commit()
        who = ", ".join(f"{acc.name} {E.fmt(c)}" for acc, c in todo[:6]) + ("…" if len(todo) > 6 else "")
        if len(todo) == 1:
            return back(f"/v/{tid}/argent", f"{'Versement' if received else 'Remboursement'} de {E.fmt(total)} "
                                             f"{'par' if received else 'à'} {todo[0][0].name} enregistré")
        return back(f"/v/{tid}/argent", f"{len(todo)} {'versements reçus' if received else 'remboursements faits'} "
                                         f"enregistrés, {E.fmt(total)} en tout : {who}")

    @app.post("/v/{tid}/argent/cheques-deposes")
    def cheques_all_deposited(tid: int, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        todo = S.cheques_to_deposit(t)
        if not todo:
            return back(f"/v/{tid}/argent/mouvements", "Aucun chèque à déposer")
        db.snapshot(f"{len(todo)} chèque{'s' if len(todo) > 1 else ''} déposé{'s' if len(todo) > 1 else ''} à la banque")
        for m in todo:
            m.deposited = True
        s.commit()
        return back(f"/v/{tid}/argent/mouvements",
                    f"{len(todo)} chèques notés comme déposés" if len(todo) > 1 else "Chèque noté comme déposé")

    @app.post("/v/{tid}/argent/{mid}/depose")
    async def cheque_deposited(tid: int, mid: int, request: Request, s: Session = Depends(get_s)):
        trip_or_404(s, tid)
        m = s.get(M.Movement, mid)
        if not m or m.trip_id != tid:
            raise HTTPException(404, "Mouvement introuvable")
        f = await request.form()
        done = f.get("etat", "1") != "0"
        if m.method != "cheque" or m.kind != "versement":
            return refuse(f"/v/{tid}/argent/mouvements", "Ce mouvement n'est pas un chèque reçu")
        db.snapshot(f"chèque de {m.participant.name} ({E.fmt(m.amount)}) "
                    + ("déposé à la banque" if done else "plus noté comme déposé"))
        m.deposited = done
        s.commit()
        return back(f"/v/{tid}/argent/mouvements", f"Chèque de {m.participant.name} ({E.fmt(m.amount)}) "
                    + ("noté comme déposé" if done else "remis dans les chèques à déposer"))

    @app.post("/v/{tid}/argent/{mid}")
    async def money_edit(tid: int, mid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        m = s.get(M.Movement, mid)
        if not m or m.trip_id != tid:
            raise HTTPException(404, "Mouvement introuvable")
        f = await request.form()
        pid = whole(f.get("participant"), m.participant_id)
        amt = amount(f, "amount")
        kind = f.get("kind", m.kind)
        if pid not in names(t) or not amt or amt <= 0 or kind not in ("versement", "remboursement"):
            return refuse(f"/v/{tid}/argent#m{mid}", "Choisis une personne et un montant positif", "amount")
        db.snapshot(f"modification d'un {m.kind} de {m.participant.name}")
        m.participant_id, m.amount, m.kind = pid, amt, kind
        m.date = S.parse_date(f.get("date")) or m.date
        m.note = (f.get("note") or "").strip()
        if "method" in f:
            m.method = S.pay_method(f.get("method")) or ""
        if "deposited_shown" in f:                       # la case « déposé » était affichée (chèque reçu)
            m.deposited = bool(f.get("deposited"))
        if m.method != "cheque" or m.kind != "versement":
            m.deposited = False
        s.commit()
        return back(f"/v/{tid}/argent", f"{'Versement' if kind == 'versement' else 'Remboursement'} de "
                                         f"{E.fmt(amt)} ({names(t)[pid]}) modifié")

    @app.post("/v/{tid}/argent/{mid}/supprimer")
    def money_delete(tid: int, mid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        m = s.get(M.Movement, mid)
        if not m or m.trip_id != tid:
            raise HTTPException(404, "Mouvement introuvable")
        db.snapshot(f"suppression d'un {m.kind} de {m.participant.name}")
        W.delete_movement(s, t, m)
        s.commit()
        ref = urlparse(request.headers.get("referer") or f"/v/{tid}/argent").path
        return back(ref, "Mouvement supprimé")

    # ------------------------------------------------------------------ justificatifs
    async def _store_receipt(s, t, up) -> M.Receipt | None:
        """Enregistre une photo ou un PDF ; None pour tout autre fichier (reconnu à son contenu, pas à son nom)."""
        data = await up.read()
        kind = fichiers.sniff(data[:fichiers.SNIFF_BYTES])
        if kind is None:
            return None
        ctype, ext = kind
        stored = f"{t.id}_{uuid.uuid4().hex}{ext}"
        (db.receipts_dir / stored).write_bytes(data)
        number = max([r.number for r in t.receipts] or [0]) + 1
        rec = M.Receipt(trip_id=t.id, number=number, original_name=Path(up.filename).name, stored_name=stored,
                        content_type=ctype)
        t.receipts.append(rec)
        s.flush()
        return rec

    def _refused_files(names_: list) -> str:
        """« x.html » n'a pas été joint… : phrase pour les fichiers qui ne sont ni une photo ni un PDF."""
        if not names_:
            return ""
        shown = ", ".join(f"« {n} »" for n in names_[:3]) + ("…" if len(names_) > 3 else "")
        return (f"{shown} {'n’a pas été joint' if len(names_) == 1 else 'n’ont pas été joints'} : "
                f"un justificatif doit être {fichiers.WHAT}.")

    @app.get("/v/{tid}/justificatifs", response_class=HTMLResponse)
    def receipts(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        missing = [e for e in t.expenses if e.actual is not None and not e.receipt_links]
        return render(request, "receipts.html", trip=t, missing=missing)

    @app.post("/v/{tid}/justificatifs")
    async def receipts_upload(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        files = [u for u in f.getlist("files") if getattr(u, "filename", None)]
        if not files:
            return refuse(f"/v/{tid}/justificatifs", "Choisis au moins un fichier")
        db.snapshot(f"ajout de {len(files)} justificatif(s)")
        link_to = set(wholes(f.getlist("expenses")))
        added, not_joined = 0, []
        for up in files:
            rec = await _store_receipt(s, t, up)
            if rec is None:
                not_joined.append(Path(up.filename).name)
                continue
            added += 1
            for eid in link_to:
                s.add(M.ReceiptLink(receipt_id=rec.id, expense_id=eid))
        s.commit()
        if not added:
            return refuse(f"/v/{tid}/justificatifs", _refused_files(not_joined))
        done = "1 justificatif ajouté" if added == 1 else f"{added} justificatifs ajoutés"
        if not_joined:
            return refuse(f"/v/{tid}/justificatifs", f"{done}, mais " + _refused_files(not_joined))
        return back(f"/v/{tid}/justificatifs", done)

    @app.post("/v/{tid}/justificatifs/{rid}")
    async def receipt_links(tid: int, rid: int, request: Request, s: Session = Depends(get_s)):
        trip_or_404(s, tid)
        rec = s.get(M.Receipt, rid)
        if not rec or rec.trip_id != tid:
            raise HTTPException(404, "Justificatif introuvable")
        f = await request.form()
        db.snapshot(f"liens du justificatif {rec.code}")
        s.query(M.ReceiptLink).filter_by(receipt_id=rid).delete()
        for eid in set(wholes(f.getlist("expenses"))):
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
        # Le type vient du contenu du fichier, jamais de ce qui a été annoncé à l'envoi ou écrit dans un voyage
        # importé. Photo ou PDF : affiché. Tout le reste : téléchargement seulement, jamais ouvert dans l'application.
        kind = fichiers.sniff_file(path)
        name = quote(rec.original_name)
        if kind is None:
            return FileResponse(path, media_type="application/octet-stream",
                                headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}",
                                         "X-Content-Type-Options": "nosniff"})
        return FileResponse(path, media_type=kind[0],
                            headers={"Content-Disposition": f"inline; filename*=UTF-8''{name}",
                                     "X-Content-Type-Options": "nosniff"})

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
        members = wholes(f.getlist("members"))
        if not name or len(members) < 2:
            return refuse(f"/v/{tid}/entre-nous", "Donne un nom et choisis au moins 2 personnes", "name")
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
        payer = whole(f.get("payer"))
        bens = [x for x in wholes(f.getlist("for")) if x in sub.members]
        if not label or not amt or amt <= 0 or payer not in sub.members or not bens:
            return refuse(f"/v/{tid}/entre-nous/{sid}", "Remplis le libellé, le montant, qui a payé et pour qui")
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
        a, b = whole(f.get("sender")), whole(f.get("receiver"))
        amt = amount(f, "amount")
        if not amt or amt <= 0:
            return refuse(f"/v/{tid}/entre-nous/{sid}", "Montant invalide", "amount")
        if a not in sub.members or b not in sub.members or a == b:
            return refuse(f"/v/{tid}/entre-nous/{sid}", "Choisis qui rembourse et qui reçoit, parmi les personnes de ce compte")
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
            obj = s.get(M.SubExpense, whole(f.get("expense")))
        else:
            obj = s.get(M.SubTransfer, whole(f.get("transfer")))
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
        r = S.compute_trip(t)
        subject, body = S.treasurer_mail(t, r)
        return render(request, "export.html", trip=t, r=r, alerts=S.alerts(t, r), mail_subject=subject, mail_body=body,
                      mail_link=f"mailto:?subject={quote(subject)}&body={quote(body)}")

    # ------------------------------------------------------------------ exporter / importer des voyages
    from . import transfer as TR
    def imports_dir() -> Path:                   # suit la base (déverrouillage d'un dossier protégé)
        return db.dir / "imports_en_attente"

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
            return refuse("/?importer=1", "Choisis le fichier .zip exporté depuis l'autre ordinateur")
        try:
            data, _ = TR.read_zip(content)
        except TR.TransferError as e:
            return refuse("/?importer=1", str(e))
        imports_dir().mkdir(exist_ok=True)
        for old in imports_dir().glob("*.zip"):            # ménage : les aperçus abandonnés depuis plus d'un jour
            if old.stat().st_mtime < dt.datetime.now().timestamp() - 86400:
                old.unlink(missing_ok=True)
        token = uuid.uuid4().hex
        (imports_dir() / f"{token}.zip").write_bytes(content)
        existing = {t.name for t in s.query(M.Trip).all()}
        return render(request, "import_preview.html", token=token, voyages=TR.summary(data), existing=existing,
                      fichier=getattr(up, "filename", "") or "", reglages=sorted((data.get("reglages") or {}).keys()))

    @app.post("/importer/{token}")
    def transfer_confirm(token: str, s: Session = Depends(get_s)):
        if not re.fullmatch(r"[0-9a-f]{32}", token) or not (imports_dir() / f"{token}.zip").exists():
            return refuse("/?importer=1", "Cet aperçu a expiré : choisis à nouveau le fichier")
        path = imports_dir() / f"{token}.zip"
        db.snapshot("import de voyage(s)")
        try:
            trips, _ = TR.import_zip(s, path.read_bytes(), db.receipts_dir)
        except (TR.TransferError, ValueError, KeyError, TypeError) as e:
            s.rollback()
            return refuse("/?importer=1", f"Import impossible : {e}")
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
        r = S.compute_trip(t)
        return render(request, "send.html", trip=t, r=r, rows=_send_rows(t, r), pay_info=t.pay_info or "",
                      pay_max=S.PAY_INFO_MAX, owing=sum(1 for a in r.accounts if S.owes_money(a)))

    @app.post("/v/{tid}/envoyer/comment-payer")
    async def send_pay_info(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        text = str((await request.form()).get("pay_info") or "")
        if len(text.strip()) > S.PAY_INFO_MAX:
            return refuse(f"/v/{tid}/envoyer", f"Ce texte est trop long : {S.PAY_INFO_MAX} caractères au plus", "pay_info")
        if text.replace("\r", "").strip() == (t.pay_info or ""):
            return back(f"/v/{tid}/envoyer", "Rien à changer")
        db.snapshot("« Comment payer »")
        S.set_pay_info(t, text)
        s.commit()
        return back(f"/v/{tid}/envoyer", "C'est noté : « Comment payer » est ajouté au message et au PDF de ceux qui "
                    "doivent encore de l'argent" if t.pay_info else "« Comment payer » est effacé")

    @app.get("/v/{tid}/compte/{key}.pdf")
    def account_pdf_route(tid: int, key: str, request: Request, s: Session = Depends(get_s)):
        from .export import account_pdf
        t = trip_or_404(s, tid)
        r = S.compute_trip(t)
        if key not in {a.key for a in r.accounts}:
            raise HTTPException(404, "Compte introuvable")
        data, name = account_pdf(t, key, E.ACTUAL)
        return Response(data, media_type="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    @app.get("/v/{tid}/comptes-personnels.zip")
    def accounts_zip_route(tid: int, request: Request, s: Session = Depends(get_s)):
        from .export import accounts_zip
        t = trip_or_404(s, tid)
        return Response(accounts_zip(t, E.ACTUAL), media_type="application/zip",
                        headers={"Content-Disposition":
                                 f"attachment; filename*=UTF-8''{quote('Comptes_' + _slug(t.name))}.zip"})

    @app.get("/v/{tid}/export.pdf")
    def export_pdf(tid: int, request: Request, s: Session = Depends(get_s)):
        from .export import trip_pdf
        t = trip_or_404(s, tid)
        data = trip_pdf(t, E.ACTUAL)
        return Response(data, media_type="application/pdf",
                        headers={"Content-Disposition":
                                 f"attachment; filename*=UTF-8''{quote('Bilan_' + _slug(t.name))}.pdf"})

    @app.get("/v/{tid}/export.xlsx")
    def export_xlsx(tid: int, s: Session = Depends(get_s)):
        """Tableur : dépenses, argent reçu et rendu, qui paie quoi (trois feuilles)."""
        from .tableur import trip_workbook
        t = trip_or_404(s, tid)
        return Response(trip_workbook(t, s),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition":
                                 f"attachment; filename*=UTF-8''{quote('Tableur_' + _slug(t.name))}.xlsx"})

    @app.get("/v/{tid}/export.zip")
    def export_zip(tid: int, request: Request, s: Session = Depends(get_s)):
        from .export import trip_zip
        t = trip_or_404(s, tid)
        data = trip_zip(t, E.ACTUAL, db.receipts_dir)
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition":
                                 f"attachment; filename*=UTF-8''{quote('Justificatifs_' + _slug(t.name))}.zip"})

    install_guard(app, token)
    return app


def _slug(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")
    return s[:60] or "export"
