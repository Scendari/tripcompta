"""Voyages protégés par un mot de passe (voir crypto.py) : dans le dossier des voyages, tout est chiffré
(`voyages.chiffre`, `justificatifs/*.chiffre`) ; l'application travaille sur une copie en clair, sur cet ordinateur
seulement, le temps qu'elle est déverrouillée.

- au déverrouillage : on reprend la version du dossier si elle a changé depuis notre dernier enregistrement (un
  autre ordinateur l'a modifiée) ; si la copie de cet ordinateur avait aussi des changements pas encore enregistrés,
  ils ne sont pas perdus : déposés à côté, chiffrés, comme une copie en conflit (exportable) ;
- après chaque modification, la copie est **rechiffrée** dans le dossier d'un coup (fichier temporaire vérifié puis
  remplacement atomique) ; si le dossier a changé entre-temps, rien n'est écrasé : notre version est déposée à côté
  et on reprend celle du dossier ;
- en verrouillant (ou en quittant) : l'historique « Annuler » et les sauvegardes sont chiffrés, la copie en clair
  est effacée.

Pas de verrou entre ordinateurs : un seul ordinateur à la fois (voir l'aide).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
from pathlib import Path

from .dossier import (DB, INBOX, RECEIPTS, _unlink, cloud_only, computer_name, file_hash, is_valid_db, replace,
                      same_content, service_name, sqlite_copy)
from . import dossier as _D
from .db import DataUnavailableError

STATE = "coffre.json"                   # dans la copie de travail : empreintes du dernier enregistrement
PRIVATE = (".annuler", ".retablir", "sauvegardes", "brouillons_ia")   # chiffrés sur l'ordinateur quand verrouillé
LOCKED_FLAG = "VERROUILLE"            # dans la copie de travail : voyages protégés, pas encore déverrouillés


def local_dir_for(folder: Path, base: Path) -> Path:
    """Dossier de la copie de travail (en clair) de ces voyages protégés, sur cet ordinateur."""
    key = hashlib.sha1(str(Path(folder).expanduser()).encode("utf-8")).hexdigest()[:12]
    return base / "copies" / key


class DossierIndisponible(DataUnavailableError):
    """Le dossier des voyages protégés est introuvable ou pas encore téléchargé."""


def _safe(s: str) -> str:
    return re.sub(r"[^\w\- ]+", "", s, flags=re.UNICODE).strip() or "un autre ordinateur"


class Coffre:
    """Copie de travail en clair (`local`) de voyages chiffrés dans `shared`. `host` (tests) : nom de l'ordinateur."""

    def __init__(self, shared: Path, local: Path, host: str | None = None):
        self.shared = Path(shared)
        self.local = Path(local)
        self.host = host or computer_name()
        self.vault = None                        # clé des voyages déverrouillée (crypto.Vault)
        self.notice: str | None = None           # message à montrer une fois (version reprise, conflit…)
        self.published_at: float | None = None
        self.mutex = threading.RLock()
        self.before_replace = None               # rappel : refermer la base avant de remplacer / effacer la copie
        self.on_conflict = None                  # rappel : la version du dossier a été reprise (rouvrir la base)
        self._seen_mtime = None
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------------ chemins
    @property
    def protected(self) -> bool:
        from . import crypto
        return crypto.is_protected(self.shared)

    @property
    def locked(self) -> bool:
        return self.vault is None

    @property
    def shared_db(self) -> Path:
        from . import crypto
        return self.shared / crypto.DB_FILE

    @property
    def local_receipts(self) -> Path:
        return self.local / RECEIPTS

    @property
    def local_db(self) -> Path:
        return self.local / DB

    @property
    def service(self) -> str | None:
        return service_name(self.shared)

    # ------------------------------------------------------------------ empreintes du dernier enregistrement
    def _state(self) -> dict:
        try:
            return json.loads((self.local / STATE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self, **kw) -> None:
        st = self._state()
        st.update(kw)
        self.local.mkdir(parents=True, exist_ok=True)
        tmp = self.local / f".{STATE}.tmp"
        tmp.write_text(json.dumps(st), encoding="utf-8")
        os.replace(tmp, self.local / STATE)

    @property
    def base_db(self) -> Path:
        """La dernière version enregistrée dans le dossier (ou reprise), pour savoir si la copie a vraiment changé."""
        return self.local / ".base.db"

    def _remember_base(self, src: Path) -> None:
        try:
            shutil.copyfile(src, self.base_db)
        except OSError:
            pass

    def local_dirty(self) -> bool:
        """La copie de travail a-t-elle des modifications pas encore enregistrées dans le dossier ? (de vraies
        saisies : une base réécrite à l'identique, ou seulement marquée par la version de l'application, ne compte
        pas)"""
        if not self.local_db.exists():
            return False
        if file_hash(self.local_db) == self._state().get("local"):
            return False
        return not (self.base_db.exists() and same_content(self.local_db, self.base_db))

    def shared_changed(self) -> bool:
        """Le dossier contient-il une autre version que celle qu'on a enregistrée ou reprise en dernier ?"""
        if not self.shared_db.exists() or cloud_only(self.shared_db):
            return False
        h = file_hash(self.shared_db)
        return h is not None and h != self._state().get("shared")

    # ------------------------------------------------------------------ copies en conflit
    def conflicts(self) -> list[dict]:
        return _D.conflicts(self.shared, True, self.conflict_readable)

    def conflict_readable(self, p: Path) -> bool:
        if self.vault is None:
            return True                              # on ne peut pas vérifier tant que c'est verrouillé
        tmp = self.local / ".verif-conflit.db"
        try:
            self.vault.decrypt_file(p, tmp, "base")
            return is_valid_db(tmp)
        except Exception:
            return False
        finally:
            tmp.unlink(missing_ok=True)

    def _deposit_conflict(self, src: Path) -> Path:
        """Notre version, chiffrée, à côté de celle du dossier : « voyages (version de PC-Anne 2026-11-05 14h02).chiffre »."""
        stamp = f"{_safe(self.host)} {dt.datetime.now():%Y-%m-%d %Hh%M}"
        dst = self.shared / f"voyages (version de {stamp}).chiffre"
        n = 2
        while dst.exists():
            dst = self.shared / f"voyages (version de {stamp} {n}).chiffre"
            n += 1
        self.vault.encrypt_file(src, dst, "base")
        return dst

    # ------------------------------------------------------------------ reprendre / enregistrer
    def _snapshot(self) -> Path:
        snap = self.local / ".envoi.db"
        snap.unlink(missing_ok=True)
        sqlite_copy(self.local_db, snap)
        return snap

    def pull(self) -> bool:
        """Reprend la version du dossier si elle a changé. Des changements de cette copie pas encore enregistrés ne
        sont jamais perdus : déposés à côté comme une copie en conflit. Renvoie True si la copie a été remplacée."""
        if self.vault is None or not self.shared_db.exists() or cloud_only(self.shared_db):
            return False
        h = file_hash(self.shared_db)
        if h is None or (h == self._state().get("shared") and self.local_db.exists()):
            return False
        incoming = self.local / ".reception.db"
        try:
            self.vault.decrypt_file(self.shared_db, incoming, "base")
        except Exception:                            # en cours de synchronisation, ou abîmé : on réessaiera
            incoming.unlink(missing_ok=True)
            return False
        if file_hash(self.shared_db) != h or not is_valid_db(incoming):
            incoming.unlink(missing_ok=True)
            return False
        self.receipts_down()
        kept = None
        if self.local_db.exists() and self.local_dirty() and not same_content(self.local_db, incoming):
            snap = self._snapshot()
            try:
                kept = self._deposit_conflict(snap)
            finally:
                snap.unlink(missing_ok=True)
        if self.before_replace:
            self.before_replace()
        self._remember_base(incoming)
        replace(incoming, self.local_db)
        for ext in ("-journal", "-wal", "-shm"):
            (self.local / f"{DB}{ext}").unlink(missing_ok=True)
        self._save_state(shared=h, local=file_hash(self.local_db))
        if kept:
            self.notice = (f"Tes voyages avaient été modifiés sur un autre ordinateur, et cet ordinateur avait aussi "
                           f"des changements pas encore enregistrés. Trip Compta affiche la version du dossier ; "
                           f"celle de cet ordinateur n'est pas perdue : « {kept.name} » (Réglages › Dossier des "
                           f"voyages, pour l'exporter).")
        return True

    def publish(self) -> str:
        """Rechiffre la copie de travail dans le dossier. Renvoie « ok », « inchangé », « conflit » (le dossier
        avait changé : notre version est déposée à côté, celle du dossier reprise), « verrouillé » ou « erreur »."""
        with self.mutex:
            if self.vault is None:
                return "verrouillé"
            if not self.local_db.exists():
                return "inchangé"
            hl = file_hash(self.local_db)
            st = self._state()
            if hl == st.get("local") and self.shared_db.exists():
                return "inchangé"
            try:
                snap = self._snapshot()
            except sqlite3.Error:
                return "erreur"
            try:
                cur = file_hash(self.shared_db) if self.shared_db.exists() else None
                if cur is not None and st.get("shared") is None:
                    # cet ordinateur n'a jamais repris la version du dossier : il la reprend d'abord (une copie de
                    # travail d'origine inconnue est déposée à côté, jamais enregistrée par-dessus)
                    self.pull()
                    return "inchangé"
                if cur is not None and cur != st.get("shared"):
                    kept = self._deposit_conflict(snap)
                    self._remember_base(snap)        # déjà gardée à côté : pas une 2e fois en reprenant le dossier
                    self.pull()
                    self.notice = (f"Tes voyages ont été modifiés sur un autre ordinateur pendant que tu travaillais. "
                                   f"Trip Compta affiche maintenant cette version ; la tienne n'est pas perdue : "
                                   f"« {kept.name} » (Réglages › Dossier des voyages, pour l'exporter).")
                    return "conflit"
                if not is_valid_db(snap):
                    return "erreur"
                self.receipts_up()                   # les justificatifs d'abord : la base n'en cite jamais d'absent
                self.vault.encrypt_file(snap, self.shared_db, "base")
                self._remember_base(snap)
                self._save_state(shared=file_hash(self.shared_db), local=hl)
                import time
                self.published_at = time.time()
                return "ok"
            except OSError:
                return "erreur"
            finally:
                snap.unlink(missing_ok=True)

    def publish_if_changed(self) -> str | None:
        """La copie de travail a été modifiée par un autre programme (l'IA, via le serveur MCP) : on l'enregistre
        dans le dossier sans attendre (sinon un plantage la perdrait : la copie en clair est effacée au démarrage
        suivant). Un simple coup d'œil à la date du fichier ; None si rien n'a bougé."""
        if self.vault is None:
            return None
        try:
            m = os.stat(self.local_db).st_mtime_ns
        except OSError:
            return None
        if m == self._seen_mtime:
            return None
        self._seen_mtime = m
        r = self.publish()
        if r == "conflit" and self.on_conflict:
            self.on_conflict()
        return r

    def watch(self, every: float = 3.0) -> None:
        """Toutes les quelques secondes, tant que l'application tourne : publish_if_changed()."""
        if self._thread is not None:
            return

        def loop():
            while not self._stop.wait(every):
                try:
                    self.publish_if_changed()
                except Exception:
                    pass
        self._thread = threading.Thread(target=loop, daemon=True, name="tripcompta-coffre")
        self._thread.start()

    # ------------------------------------------------------------------ justificatifs
    def _receipt_role(self, name: str) -> str:
        return f"justificatif:{name}"

    def receipts_up(self) -> int:
        """Chiffre dans le dossier les justificatifs ajoutés sur cet ordinateur (une seule fois chacun)."""
        from . import crypto
        if self.vault is None or not self.local_receipts.is_dir():
            return 0
        dst_dir = self.shared / RECEIPTS
        dst_dir.mkdir(exist_ok=True)
        n = 0
        for f in self.local_receipts.iterdir():
            if f.name.startswith(".") or not f.is_file():
                continue
            target = dst_dir / f"{f.name}{crypto.SUFFIX}"
            if not target.exists():
                self.vault.encrypt_file(f, target, self._receipt_role(f.name))
                n += 1
        return n

    def receipts_down(self) -> int:
        """Déchiffre sur l'ordinateur les justificatifs du dossier qui n'y sont pas encore."""
        from . import crypto
        src_dir = self.shared / RECEIPTS
        if self.vault is None or not src_dir.is_dir():
            return 0
        self.local_receipts.mkdir(parents=True, exist_ok=True)
        n = 0
        for f in src_dir.iterdir():
            if f.name.startswith(".") or not f.name.endswith(crypto.SUFFIX) or cloud_only(f):
                continue
            name = f.name[:-len(crypto.SUFFIX)]
            target = self.local_receipts / name
            if not target.exists():
                try:
                    self.vault.decrypt_file(f, target, self._receipt_role(name))
                    n += 1
                except crypto.Damaged:
                    pass                             # signalé à l'affichage (« fichier introuvable »)
        return n

    # ------------------------------------------------------------------ cycle de vie
    def open(self) -> None:
        """Au démarrage : dossier disponible ? clé mémorisée sur cet ordinateur → déverrouillé ; sinon verrouillé
        (et ce qui serait resté en clair d'un arrêt brutal est effacé)."""
        from . import crypto
        if not self.shared.is_dir() or cloud_only(self.shared_db) or cloud_only(self.shared / crypto.KEY_FILE):
            raise DossierIndisponible(self.shared_db)
        self.local.mkdir(parents=True, exist_ok=True)
        (self.shared / RECEIPTS).mkdir(exist_ok=True)
        (self.shared / INBOX).mkdir(exist_ok=True)
        data = crypto.read_key_file(self.shared) or {}
        v = crypto.recall(data.get("id", "")) if data.get("id") else None
        if v is not None and self._key_works(v):
            self.unlock(v)                           # « se souvenir sur cet ordinateur »
        else:
            self.wipe_working_copy()
            self._flag_locked(True)

    def _key_works(self, vault) -> bool:
        if not self.shared_db.exists():
            return True
        try:
            vault.decrypt(self.shared_db.read_bytes(), "base")
            return True
        except Exception:
            return False

    def unlock(self, vault) -> None:
        """Clé vérifiée : historique déchiffré, version du dossier reprise si elle a changé, justificatifs."""
        self.vault = vault
        self._flag_locked(False)
        self.decrypt_private()
        self.pull()
        self.receipts_down()

    def lock(self) -> None:
        """Verrouille : enregistre, chiffre l'historique et les sauvegardes, efface la copie en clair."""
        if self.vault is None:
            return
        with self.mutex:
            if self.local_db.exists() and self.local_dirty():
                self.publish()
            if self.local_db.exists() and self.local_dirty():    # pas pu enregistrer : gardée, chiffrée ci-dessous
                folder = self.local / "sauvegardes"
                folder.mkdir(parents=True, exist_ok=True)
                sqlite_copy(self.local_db, folder / f"pas-encore-enregistree-{dt.datetime.now():%Y-%m-%d-%Hh%M%S}.db")
            self.encrypt_private()
            self.wipe_working_copy()
            self.vault = None
            self._flag_locked(True)

    def wipe_working_copy(self) -> None:
        """Efface ce qui est en clair et redondant avec le dossier : base de travail, justificatifs, vignettes."""
        if self.before_replace:
            try:
                self.before_replace()            # Windows ne supprime pas un fichier encore ouvert
            except Exception:
                pass
        for name in (DB, f"{DB}-journal", f"{DB}-wal", f"{DB}-shm", ".reception.db", ".envoi.db",
                     ".verif-conflit.db", ".base.db", ".protection.db"):
            try:
                _unlink(self.local / name)
            except OSError:                      # Windows : encore ouvert par un autre programme (l'IA) ; au
                pass                             # prochain verrouillage ou démarrage
        for d in (self.local_receipts, self.local / ".vignettes", self.local / ".verrouille"):
            shutil.rmtree(d, ignore_errors=True)

    def _private_files(self):
        for d in PRIVATE:
            folder = self.local / d
            if folder.is_dir():
                for f in sorted(folder.rglob("*")):
                    if f.is_file():
                        yield f

    def encrypt_private(self) -> None:
        from . import crypto
        for f in list(self._private_files()):
            if f.name.endswith(crypto.SUFFIX):
                continue
            rel = f.relative_to(self.local).as_posix()
            self.vault.encrypt_file(f, f.with_name(f.name + crypto.SUFFIX), f"local:{rel}")
            f.unlink()

    def decrypt_private(self) -> None:
        from . import crypto
        for f in list(self._private_files()):
            if not f.name.endswith(crypto.SUFFIX):
                continue
            clear = f.with_name(f.name[:-len(crypto.SUFFIX)])
            rel = clear.relative_to(self.local).as_posix()
            try:
                self.vault.decrypt_file(f, clear, f"local:{rel}")
                f.unlink()
            except crypto.Damaged:
                pass

    def _flag_locked(self, on: bool) -> None:
        f = self.local / LOCKED_FLAG
        try:
            if on:
                self.local.mkdir(parents=True, exist_ok=True)
                f.write_text("Voyages protégés par un mot de passe : Trip Compta est verrouillé.", encoding="utf-8")
            else:
                f.unlink(missing_ok=True)
        except OSError:
            pass

    def stop(self) -> None:
        """En quittant : rien en clair ne reste sur l'ordinateur."""
        self._stop.set()
        if self.vault is not None:
            self.lock()

    def inbox(self) -> list[dict]:
        return _D.inbox(self.shared)
