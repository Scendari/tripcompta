"""Protection des voyages par un mot de passe : activer, déverrouiller, clé de secours, changer, retirer ;
et le cas où deux ordinateurs ont enregistré en même temps.

Partie de l'application web (voir compta/web.py, qui installe ces pages dans l'ordre).
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import db as D
from .. import models as M
from .. import transfer as TR
from ..db import Database


def install(site):
    """Installe ces pages dans l'application. `site` : ce que les pages ont en commun (voir web.py)."""
    C = site.C
    CF = site.CF
    K = site.K
    _restart_page = site._restart_page
    app = site.app
    back = site.back
    box_local = site.box_local
    db = site.db
    newer_stored = site.newer_stored
    open_unlocked = site.open_unlocked
    opts = site.opts
    refuse = site.refuse
    render = site.render
    to_locked = site.to_locked
    upd = site.upd

    # ------------------------------------------------------------------ protection par un mot de passe
    app.state.pending_protection = None      # en cours d'activation (en mémoire seulement, jamais sur le disque)

    def _folder() -> Path:
        return db.home

    def _unlock_page(request, **ctx):
        return render(request, "unlock.html", folder=str(_folder()), can_remember=K.keyring_available(), **ctx)

    @app.get("/deverrouiller", response_class=HTMLResponse)
    def unlock_page(request: Request):
        if site.vault_box is None or not site.vault_box.locked:
            return RedirectResponse("/", status_code=303)
        return _unlock_page(request)

    def _after_unlock(vault, remember: bool, request):
        if not site.vault_box._key_works(vault):
            return _unlock_page(request, error="Les voyages de ce dossier sont abîmés ou incomplets (synchronisation "
                                               "en cours ?). Attends un peu, puis réessaie.")
        if remember:
            K.remember(vault)
        else:
            K.forget(vault.id)
        site.vault_box.unlock(vault)
        db.reopen(site.vault_box.local, receipts_in_home=False)
        newer = newer_stored()
        if newer:                                # une version plus récente les a enregistrés : on n'y touche pas
            db.engine.dispose()
            site.vault_box.lock()
            db.reopen(site.vault_box.local / ".verrouille", receipts_in_home=False)
            return _unlock_page(request, error=f"Ces voyages ont été enregistrés par Trip Compta {newer}, plus récente "
                                               f"que celle-ci : mets à jour Trip Compta (ℹ️ À propos), puis réessaie. "
                                               f"Rien n'a été modifié.")
        open_unlocked()
        return None

    @app.post("/deverrouiller", response_class=HTMLResponse)
    async def unlock_post(request: Request):
        if site.vault_box is None or not site.vault_box.locked:
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
        if site.vault_box is None or not site.vault_box.locked:
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
        if site.vault_box is not None and site.vault_box.vault is not None:
            to_locked()
        return RedirectResponse("/deverrouiller", status_code=303)

    def _protection_page(request, **ctx):
        return render(request, "protection.html", folder=str(_folder()), can_remember=K.keyring_available(),
                      protected=site.vault_box is not None, **ctx)

    @app.get("/protection/activer", response_class=HTMLResponse)
    def protect_start(request: Request):
        if site.vault_box is not None:
            return RedirectResponse("/reglages/protection", status_code=303)
        return _protection_page(request, step="mot_de_passe")

    @app.post("/protection/activer", response_class=HTMLResponse)
    async def protect_password(request: Request):
        import secrets as _secrets
        if site.vault_box is not None:
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
        site.vault_box = app.state.coffre = CF.Coffre(F, local, host=opts.get("host"))
        site.vault_box.before_replace = lambda: db.engine.dispose()
        site.vault_box.on_conflict = lambda: db.after_replace()
        if opts.get("watch", True):
            site.vault_box.watch()
        site.vault_box._flag_locked(True)
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
        if site.vault_box is None or site.vault_box.vault is None:
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
        K.change_password(_folder(), site.vault_box.vault, new)
        return back("/reglages/protection", "Mot de passe changé")

    @app.post("/protection/secours", response_class=HTMLResponse)
    async def protect_new_recovery(request: Request):
        import secrets as _secrets
        if site.vault_box is None or site.vault_box.vault is None:
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
        if not pend or pend["kind"] != "secours" or site.vault_box is None or site.vault_box.vault is None:
            return RedirectResponse("/reglages/protection", status_code=303)
        f = await request.form()
        if not K.check_group(pend["recovery"], pend["group"], f.get("groupe") or ""):
            return _protection_page(request, step="secours", recovery=pend["recovery"], group=pend["group"],
                                    action="/protection/secours/confirmer", renew=True,
                                    error=f"Ce n'est pas le groupe n° {pend['group'] + 1} de ta clé de secours.")
        K.set_recovery(_folder(), site.vault_box.vault, pend["recovery"])
        app.state.pending_protection = None
        return back("/reglages/protection", "Nouvelle clé de secours enregistrée : l'ancienne ne marche plus")

    @app.post("/protection/oublier")
    def protect_forget():
        if site.vault_box is not None and site.vault_box.vault is not None:
            K.forget(site.vault_box.vault.id)
        return back("/reglages/protection", "Cet ordinateur ne se souvient plus du mot de passe")

    @app.post("/protection/retirer", response_class=HTMLResponse)
    async def protect_remove(request: Request):
        """Retirer la protection : tout redevient lisible (vérifié), puis la clé est effacée."""
        import shutil as _sh
        if site.vault_box is None or site.vault_box.vault is None:
            return RedirectResponse("/reglages/protection", status_code=303)
        f = await request.form()
        bad = _check_current(request, f)
        if bad:
            return bad
        F = _folder()
        site.vault_box.publish()
        site.vault_box.receipts_down()
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
        vault_id = site.vault_box.vault.id
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
            src = site.vault_box.local / name
            if src.is_dir():
                dst = F / name
                if name == "sauvegardes":            # voyages ordinaires : sauvegardes à part, sur l'ordinateur
                    dst = D.backups_dir_for(F)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                _sh.copytree(src, dst, dirs_exist_ok=True)
        site.vault_box.vault = None
        site.vault_box.wipe_working_copy()
        for name in CF.PRIVATE:
            _sh.rmtree(site.vault_box.local / name, ignore_errors=True)
        (site.vault_box.local / CF.STATE).unlink(missing_ok=True)
        site.vault_box = app.state.coffre = None
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
        pattern = C.CONFLICT_RE_PROTECTED if site.vault_box is not None else C.CONFLICT_RE
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
                if site.vault_box is not None:
                    if site.vault_box.vault is None:
                        raise HTTPException(423, "Trip Compta est verrouillé")
                    site.vault_box.vault.decrypt_file(src, work / C.DB, "base")
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
