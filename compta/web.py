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

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from . import db as D
from . import diagnostic as DIAG
from . import engine as E
from . import journal
from . import models as M
from . import services as S
from .db import (Database, DamagedDatabaseError, DataUnavailableError, NewerDatabaseError, human_when, long_date,
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
templates.env.filters["longdate"] = lambda d: long_date(d) if d else ""     # « samedi 1er mai 2027 »
templates.env.filters["shortdate"] = S.short_date                                 # « lun. 3 mai »
templates.env.filters["amount_input"] = _amount_input
templates.env.tests["contains"] = lambda seq, x: x in (seq or ())
templates.env.globals["ROLES"] = S.ROLES
templates.env.filters["role_label"] = S.role_label
templates.env.globals["ROLE_TITLES"] = S.ROLE_TITLES
templates.env.filters["weight"] = lambda w: E.fmt_weight(E.to_weight(w))
templates.env.globals["ASSO"] = E.ASSO
templates.env.globals["pay_plans"] = S.pay_plans      # où en est chaque compte par rapport aux dates de paiement
# Un compte qui ne doit plus rien : « soldé » quand le voyage a des dates de paiement (à jour = pas en retard)
templates.env.filters["settled_word"] = lambda t: "soldé" if S.has_pay_dates(t) else "à jour"
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
    from . import pages
    from . import coffre as CF
    from . import crypto as K
    from . import dossier as C
    from . import paths as PATHS
    from . import updater as U
    from . import version as V
    site = pages.Site()                          # ce que les pages ont en commun
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
    site.vault_box = None                             # voyages protégés par un mot de passe (coffre.py)
    opts = coffre or {}
    box_local = Path(opts.get("local") or CF.local_dir_for(folder, PATHS.config_file().parent))
    try:
        if K.is_protected(folder):
            site.vault_box = CF.Coffre(folder, box_local, host=opts.get("host"))
            site.vault_box.open()
            if site.vault_box.locked:            # rien en clair tant que c'est verrouillé : une base vide d'attente
                db = Database(site.vault_box.local / ".verrouille", app_version=upd.version, home=folder,
                              receipts_in_home=False)
            else:
                db = Database(site.vault_box.local, app_version=upd.version, home=folder, receipts_in_home=False)
                site.vault_box.publish()
        else:
            db = Database(folder, app_version=upd.version)
    except NewerDatabaseError as err:
        return newer_data_app(err, upd, token)
    except (DataUnavailableError, DamagedDatabaseError) as err:
        return data_problem_app(err, upd, restart, token)
    backup_failed = None
    if site.vault_box is None or not site.vault_box.locked:
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
    app.state.coffre = site.vault_box
    app.state.startup_notices = [n for n in (db.recovered_notice(), writer_notice(db)) if n]
    if site.vault_box is not None:
        site.vault_box.before_replace = lambda: db.engine.dispose()
        site.vault_box.on_conflict = lambda: db.after_replace()
        import atexit
        atexit.register(site.vault_box.stop)
        if (coffre or {}).get("watch", True):
            site.vault_box.watch()                    # saisies de l'IA (autre programme) : enregistrées sans attendre
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
            if (m and not CLOSED_OK.match(request.url.path)
                    and not (site.vault_box is not None and site.vault_box.locked)):
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
        db.reopen(site.vault_box.local, receipts_in_home=False)
        import shutil as _sh
        _sh.rmtree(site.vault_box.local / ".verrouille", ignore_errors=True)
        app.state.startup_notices += [n for n in (db.recovered_notice(), writer_notice(db)) if n]
        db.daily_backup()
        with db.Session() as s_:
            if any([S.store_draws(t_) for t_ in s_.query(M.Trip).all()]):
                s_.commit()
        site.vault_box.publish()

    def to_locked():
        """Verrouiller : base refermée, enregistrée dans le dossier, puis copie en clair effacée (coffre.py)."""
        db.engine.dispose()
        site.vault_box.lock()
        db.reopen(site.vault_box.local / ".verrouille", receipts_in_home=False)

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
                and not (site.vault_box is not None and site.vault_box.locked)):
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
        if site.vault_box is None:
            return await call_next(request)
        path = request.url.path
        if site.vault_box.locked and not path.startswith(LOCKED_OK):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Trip Compta est verrouillé : saisis le mot de passe de tes voyages "
                                               "dans l'application."}, status_code=423)
            return RedirectResponse("/deverrouiller", status_code=303)
        response = await call_next(request)
        # (la requête a pu activer ou retirer la protection : on relit site.vault_box)
        if (request.method in ("POST", "PUT", "PATCH", "DELETE") and response.status_code < 400
                and site.vault_box is not None and not site.vault_box.locked):
            if site.vault_box.publish() == "conflit":  # un autre ordinateur avait enregistré : sa version est reprise
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
        if site.vault_box is not None and site.vault_box.notice and not ctx.get("msg"):
            ctx["msg"], site.vault_box.notice = site.vault_box.notice, None
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
            main = site.vault_box.shared_db if site.vault_box is not None else db.path
            on_disk = main.stat().st_mtime if main.exists() else None
        except OSError:
            on_disk = None
        published = site.vault_box.published_at if site.vault_box is not None else None
        saved = max([x for x in (published, on_disk) if x] or [0]) or None
        return dict(folder=str(F), protected=site.vault_box is not None,
                    locked=site.vault_box is not None and site.vault_box.locked, saved=_hm(saved),
                    conflicts=len(C.conflicts(F, site.vault_box is not None, readable=lambda p: True)))

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

    # ------------------------------------------------------------------ les pages, par domaine
    # (compta/pages/*.py). Elles reçoivent `site` : l'application, la base, et les fonctions ci-dessus.
    vars(site).update(C=C, CF=CF, FormError=FormError, K=K, PATHS=PATHS, U=U, _then_join=_then_join, amount=amount,
                      app=app, back=back, box_local=box_local, data_source=data_source, db=db, get_s=get_s,
                      names=names, newer_stored=newer_stored, open_unlocked=open_unlocked, opts=opts,
                      refuse=refuse, render=render, to_locked=to_locked, trip_or_404=trip_or_404, upd=upd,
                      whole=whole, wholes=wholes)
    pages.install_all(site)

    install_guard(app, token)
    return app


def _slug(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")
    return s[:60] or "export"
