"""Réglages : où sont rangés les voyages (choisir, changer de dossier), revenir à une sauvegarde.

Partie de l'application web (voir compta/web.py, qui installe ces pages dans l'ordre).
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import HTMLResponse

from .. import db as D
from .. import models as M
from .. import journal
from ..db import human_when


def install(site):
    """Installe ces pages dans l'application. `site` : ce que les pages ont en commun (voir web.py)."""
    C = site.C
    K = site.K
    PATHS = site.PATHS
    app = site.app
    back = site.back
    data_source = site.data_source
    db = site.db
    refuse = site.refuse
    render = site.render
    upd = site.upd

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
        w = db.last_writer() if site.vault_box is None or not site.vault_box.locked else None
        return render(request, "data_dir.html", data_dir=cur, source=data_source,
                      local_current=source_is_default(), default_dir=default_dir,
                      conflicts=(site.vault_box.conflicts() if site.vault_box is not None else C.conflicts(cur)),
                      writer=dict(ordinateur=w["ordinateur"], quand=human_when(w["quand"])) if w else None,
                      protected=site.vault_box is not None,
                      remembered=bool(site.vault_box is not None and site.vault_box.vault is not None
                                      and K.recall(site.vault_box.vault.id)),
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
                      protected=site.vault_box is not None,
                      remembered=bool(site.vault_box is not None and site.vault_box.vault is not None
                                      and K.recall(site.vault_box.vault.id)),
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
        if site.vault_box is not None and not site.vault_box.locked:
            site.vault_box.receipts_down()                # voyages protégés : les justificatifs chiffrés du dossier
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
                      protected=site.vault_box is not None,
                      remembered=bool(site.vault_box is not None and site.vault_box.vault is not None
                                      and K.recall(site.vault_box.vault.id)),
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
        if site.vault_box is not None:
            site.vault_box.stop()
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
        if site.vault_box is not None:
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

    # utilisé par d'autres pages
    site._restart_page = _restart_page
