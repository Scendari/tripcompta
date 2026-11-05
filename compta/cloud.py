"""Voyages rangés dans un dossier synchronisé (Dropbox, Google Drive, OneDrive, iCloud Drive, kDrive, Proton Drive…).

Aucun serveur ni API de cloud : Trip Compta écrit dans un dossier, le logiciel du cloud le synchronise entre les
ordinateurs. Pour que la base ne s'abîme jamais et qu'aucune saisie ne se perde :

- dans le dossier synchronisé (« partagé ») : seulement `compta.db` (la version publiée), `justificatifs/`,
  `Justificatifs à trier/` (photos déposées depuis un téléphone) et le verrou `tripcompta-verrou.json` ;
- sur l'ordinateur (« copie de travail ») : la base sur laquelle l'app travaille, `.annuler`, `.retablir`,
  `sauvegardes`, `brouillons_ia`, les vignettes ;
- après chaque modification, la copie de travail est **publiée** d'un coup : copie complète vérifiée dans un
  fichier temporaire du dossier, puis remplacement atomique (`os.replace`) : jamais de fichier à moitié écrit, ni
  de fichiers -journal / -wal / -shm dans le cloud ;
- au démarrage et en revenant sur l'app, la version du dossier est reprise si elle a changé depuis la dernière
  fois (un autre ordinateur l'a publiée) ;
- un **verrou** dit quel ordinateur a la main ; il est rafraîchi régulièrement (« battement »). Un 2e ordinateur
  ouvre en lecture seule ; si le verrou est ancien (ordinateur éteint ou planté), il peut « prendre la main ».
"""
from __future__ import annotations

import datetime as dt
import getpass
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path

from .dossier import (CONFLICT_RE, CONFLICT_RE_PROTECTED, DB, INBOX, INBOX_DONE, INBOX_TYPES, RECEIPTS,  # noqa: F401
                      SERVICES, _account, _unlink, atomic_copy, cloud_only, cloud_roots, computer_name, file_hash,
                      inbox_file, is_valid_db, pick_folder, replace, same_content, service_name, sqlite_copy)
from . import dossier as _D

LOCK = "tripcompta-verrou.json"
OLD_VERSIONS = "Anciennes versions"
STATE = "synchro.json"                  # dans la copie de travail : ce qu'on sait du dossier partagé
READONLY_FLAG = "LECTURE-SEULE"         # dans la copie de travail : l'IA (autre processus) ne doit pas écrire
BEAT = 30                               # secondes entre deux battements du verrou
STALE = 180                             # verrou sans battement depuis 3 min : ordinateur éteint ou planté
IDLE_AFTER = 120                        # aucun onglet ouvert depuis 2 min : on rend la main aux autres ordinateurs

PRIVATE = (".annuler", ".retablir", "sauvegardes", "brouillons_ia")   # chiffrés sur l'ordinateur quand verrouillé
LOCKED_FLAG = "VERROUILLE"            # dans la copie de travail : voyages protégés, pas encore déverrouillés


def local_dir_for(shared: Path, base: Path) -> Path:
    """Dossier de la copie de travail de ce dossier partagé, sur cet ordinateur."""
    key = hashlib.sha1(str(shared.expanduser()).encode("utf-8")).hexdigest()[:12]
    return base / "copies" / key


class CloudSync:
    """Synchronisation de la copie de travail avec le dossier partagé. `identity` (tests) : remplace le nom de
    l'ordinateur et le numéro de processus, pour simuler deux ordinateurs dans un même test."""

    def __init__(self, shared: Path, local: Path, identity: dict | None = None, clock=time.time):
        self.shared = Path(shared)
        self.local = Path(local)
        ident = identity or {}
        self.host = ident.get("host") or computer_name()
        self.user = ident.get("user") or _user()
        self.pid = ident.get("pid") or os.getpid()
        self.id = uuid.uuid4().hex
        self.clock = clock
        self.readonly = False
        self.owner: dict | None = None          # verrou d'un autre ordinateur (si lecture seule)
        self.problem: str | None = None          # dossier introuvable, illisible…
        self.notice: str | None = None           # message à montrer une fois (version reprise, conflit…)
        self.published_at: float | None = None
        self.pulled_at: float | None = None
        self.lock_mutex = threading.RLock()
        self._seen_sig = None
        self._stop = threading.Event()
        self._thread = None
        self.on_pulled = None                    # rappel : la copie de travail a été remplacée (rouvrir la base)
        self.vault = None                        # clé des voyages déverrouillée (dossier protégé, voir crypto.py)
        self.idle = False                        # main rendue faute d'onglet ouvert (reprise au prochain onglet)
        self.before_wipe = None                  # rappel : refermer la base avant d'effacer la copie (Windows)

    # ------------------------------------------------------------------ chemins
    @property
    def protected(self) -> bool:
        """Dossier protégé par un mot de passe (fichier cle.tripcompta présent)."""
        from . import crypto
        return crypto.is_protected(self.shared)

    @property
    def locked(self) -> bool:
        return self.protected and self.vault is None

    @property
    def shared_db(self) -> Path:
        if self.protected:
            from . import crypto
            return self.shared / crypto.DB_FILE
        return self.shared / DB

    @property
    def local_receipts(self) -> Path:
        """Dossier protégé : justificatifs en clair sur l'ordinateur, le temps que l'app est déverrouillée."""
        return self.local / RECEIPTS

    @property
    def local_db(self) -> Path:
        return self.local / DB

    @property
    def lock_path(self) -> Path:
        return self.shared / LOCK

    @property
    def service(self) -> str | None:
        return service_name(self.shared)

    # ------------------------------------------------------------------ état mémorisé
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

    # ------------------------------------------------------------------ vérifications du dossier
    def check_folder(self) -> str | None:
        """Problème empêchant d'utiliser le dossier partagé (message clair), ou None."""
        if not self.shared.is_dir():
            return (f"Le dossier « {self.shared} » est introuvable. Vérifie que le logiciel de ton cloud est lancé "
                    f"(ou que la clé USB est branchée), puis relance Trip Compta.")
        if cloud_only(self.shared_db):
            svc = self.service or "ton cloud"
            return (f"Le fichier des voyages n'est pas encore téléchargé sur cet ordinateur ({svc} le garde « en "
                    f"ligne seulement »). Dans le Finder ou l'Explorateur, fais un clic droit sur le dossier "
                    f"« {self.shared.name} » et coche « Toujours garder sur cet appareil » (ou « Rendre disponible "
                    f"hors connexion »), attends la fin du téléchargement, puis relance Trip Compta.")
        probe = self.shared / f".tripcompta-test-{uuid.uuid4().hex[:6]}"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError:
            return (f"Trip Compta ne peut pas écrire dans « {self.shared} » (dossier protégé, en lecture seule ou "
                    f"disque plein). Tes voyages s'ouvrent en lecture seule.")
        return None

    def conflicts(self) -> list[dict]:
        """Copies en conflit laissées par le service de cloud, à côté de compta.db."""
        return _D.conflicts(self.shared, self.protected, self.conflict_readable)

    def conflict_readable(self, p: Path) -> bool:
        if not self.protected:
            return is_valid_db(p)
        if self.vault is None:
            return True                              # on ne peut pas vérifier tant que c'est verrouillé
        tmp = self.local / ".verif-conflit.db"
        try:
            self.decrypt_db(p, tmp)
            return is_valid_db(tmp)
        except Exception:
            return False
        finally:
            tmp.unlink(missing_ok=True)

    def decrypt_db(self, src: Path, dst: Path) -> None:
        """Base chiffrée du dossier → base en clair (copie de travail)."""
        self.vault.decrypt_file(src, dst, "base")

    # ------------------------------------------------------------------ verrou
    def read_lock(self) -> dict | None:
        try:
            return json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _mine(self, lk: dict | None) -> bool:
        return bool(lk) and lk.get("id") == self.id

    def lock_age(self, lk: dict) -> float:
        try:
            return self.clock() - float(lk.get("battement_ts", 0))
        except (TypeError, ValueError):
            return float("inf")

    def lock_is_stale(self, lk: dict) -> bool:
        if self.lock_age(lk) > STALE:
            return True
        # même ordinateur : le processus qui l'a posé n'existe plus (plantage) → on peut le reprendre
        return lk.get("ordinateur") == self.host and lk.get("pid") not in (None, self.pid) and not _alive(lk["pid"])

    def _write_lock(self, since: str | None = None) -> None:
        now = self.clock()
        data = dict(id=self.id, ordinateur=self.host, utilisateur=self.user, pid=self.pid,
                    depuis=since or _iso(now), battement=_iso(now), battement_ts=now)
        tmp = self.shared / f".{LOCK}.{self.id[:8]}.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.lock_path)

    def acquire(self, force: bool = False) -> bool:
        """Prend la main si personne ne l'a (ou si le verrou est ancien, ou si `force`). Sinon : lecture seule."""
        with self.lock_mutex:
            lk = self.read_lock()
            if lk and not self._mine(lk) and not force and not self.lock_is_stale(lk) \
                    and not (lk.get("ordinateur") == self.host and lk.get("pid") == self.pid):
                self.readonly, self.owner = True, lk
                self._flag_readonly(True)
                return False
            try:
                self._write_lock()
            except OSError:
                self.readonly, self.owner = True, None
                self._flag_readonly(True)
                return False
            self.readonly, self.owner = False, None
            self._flag_readonly(False)
            return True

    def beat(self) -> None:
        """Battement : rafraîchit notre verrou. Si un autre ordinateur a pris la main entre-temps : lecture seule."""
        with self.lock_mutex:
            if self.idle:                            # main rendue (aucun onglet) : on ne la reprend pas tout seul
                return
            lk = self.read_lock()
            if self.readonly:
                if lk is None:                       # libéré proprement : on reprend la main tout seul
                    before = self.owner
                    if self.acquire():
                        self.notice = f"{self._owner_label(before)} a fermé Trip Compta : tu as de nouveau la main."
                else:
                    self.owner = lk
                return
            if lk and not self._mine(lk):
                self.readonly, self.owner = True, lk
                self._flag_readonly(True)
                self.notice = (f"{self._owner_label(lk)} a pris la main sur les voyages : ils sont maintenant en "
                               f"lecture seule ici.")
                return
            try:
                self._write_lock(since=(lk or {}).get("depuis"))
            except OSError:
                pass

    def go_idle(self) -> bool:
        """Plus aucun onglet ouvert : on enregistre, puis on rend la main (le verrou est libéré) pour que l'autre
        ordinateur puisse modifier sans rien faire. Renvoie True si la main a été rendue."""
        with self.lock_mutex:
            if self.idle or self.readonly or self.problem or self.locked:
                return False
            if self.local_dirty():
                self.publish()
            self.release()
            self.idle = True
            self.readonly = True
            self.owner = None
            self._flag_readonly(True)
            return True

    def wake(self) -> None:
        """Un onglet est de nouveau ouvert : on reprend la main si personne ne l'a prise entre-temps (sinon : lecture
        seule, avec le nom de l'ordinateur qui l'a)."""
        with self.lock_mutex:
            if not self.idle:
                return
            self.idle = False
            if self.acquire():
                self.pull()

    def release(self) -> None:
        with self.lock_mutex:
            try:
                if self._mine(self.read_lock()):
                    self.lock_path.unlink()
            except OSError:
                pass

    def _flag_readonly(self, on: bool) -> None:
        f = self.local / READONLY_FLAG
        try:
            if on:
                self.local.mkdir(parents=True, exist_ok=True)
                f.write_text("Voyages ouverts en lecture seule sur cet ordinateur.", encoding="utf-8")
            else:
                f.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _owner_label(lk: dict | None) -> str:
        if not lk:
            return "Un autre ordinateur"
        return f"« {lk.get('ordinateur') or 'un autre ordinateur'} »"

    # ------------------------------------------------------------------ récupérer / publier
    def _sig(self):
        try:
            st = os.stat(self.shared_db)
            return (st.st_size, st.st_mtime_ns)
        except OSError:
            return None

    def shared_changed(self) -> bool:
        """Le dossier contient-il une autre version que celle qu'on a reprise ou publiée en dernier ?"""
        sig = self._sig()
        if sig is None or sig == self._seen_sig:
            return False
        if cloud_only(self.shared_db):               # le lire forcerait un téléchargement (page bloquée) : on attend
            return False
        h = file_hash(self.shared_db)
        self._seen_sig = sig
        return h is not None and h != self._state().get("shared")

    @property
    def base_db(self) -> Path:
        """La dernière version synchronisée (publiée ou reprise), pour savoir si la copie de travail a vraiment changé."""
        return self.local / ".base.db"

    def _remember_base(self, src: Path) -> None:
        try:
            shutil.copyfile(src, self.base_db)
        except OSError:
            pass

    def local_dirty(self) -> bool:
        """La copie de travail contient-elle des modifications pas encore publiées ? (vraies saisies : une base
        réécrite à l'identique, ou seulement marquée par la version de l'application, ne compte pas)"""
        if not self.local_db.exists():
            return False
        if file_hash(self.local_db) == self._state().get("local"):
            return False
        return not (self.base_db.exists() and same_content(self.local_db, self.base_db))

    def _snapshot(self) -> Path:
        snap = self.local / ".envoi.db"
        snap.unlink(missing_ok=True)
        sqlite_copy(self.local_db, snap)
        return snap

    def pull(self, before_replace=None) -> bool:
        """Reprend la version du dossier si elle a changé. Des modifications locales pas encore publiées ne sont
        jamais perdues : elles sont gardées dans les sauvegardes et signalées. Renvoie True si la copie de travail
        a été remplacée."""
        if not self.shared_db.exists() or cloud_only(self.shared_db):
            return False
        h = file_hash(self.shared_db)
        st = self._state()
        if h is None or (h == st.get("shared") and self.local_db.exists()):
            self._seen_sig = self._sig()
            return False
        if not self.protected and self.local_db.exists() and file_hash(self.local_db) == h:   # déjà la même
            self._save_state(shared=h, local=h)
            self._seen_sig = self._sig()
            return False
        incoming = self.local / ".reception.db"
        if self.protected:
            if self.vault is None:
                return False
            try:
                self.decrypt_db(self.shared_db, incoming)
            except Exception:                        # en cours de synchronisation, ou abîmé : on réessaiera
                incoming.unlink(missing_ok=True)
                return False
            if file_hash(self.shared_db) != h or not is_valid_db(incoming):
                incoming.unlink(missing_ok=True)
                return False
            self.receipts_down()
        else:
            shutil.copyfile(self.shared_db, incoming)
            if file_hash(incoming) != h or not is_valid_db(incoming):
                incoming.unlink(missing_ok=True)     # fichier en cours de synchronisation : on réessaiera
                return False
        kept = None
        if self.local_db.exists() and self.local_dirty():
            kept = self.keep_local_copy("pas-encore-envoyee")     # de vraies saisies : gardées, jamais perdues
        if before_replace:
            before_replace()
        self._remember_base(incoming)
        replace(incoming, self.local_db)
        for ext in ("-journal", "-wal", "-shm"):
            (self.local / f"{DB}{ext}").unlink(missing_ok=True)
        self._save_state(shared=h, local=file_hash(self.local_db))
        self._seen_sig = self._sig()
        self.pulled_at = self.clock()
        if kept:
            where = self.service or "le dossier"
            self.notice = (f"Attention : des modifications faites sur cet ordinateur n'avaient pas pu être envoyées "
                           f"dans {where}, et une autre version y est arrivée entre-temps. Trip Compta affiche "
                           f"maintenant la version de {where}. Rien n'est perdu : tes modifications sont gardées dans "
                           f"une copie de secours (« {kept.name} »).")
        return True

    def keep_local_copy(self, why: str) -> Path:
        folder = self.local / "sauvegardes"
        folder.mkdir(parents=True, exist_ok=True)
        dst = folder / f"{why}-{dt.datetime.now():%Y-%m-%d-%Hh%M%S}.db"
        sqlite_copy(self.local_db, dst)
        return dst

    # ------------------------------------------------------------------ justificatifs d'un dossier protégé
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

    def publish(self) -> str:
        """Publie la copie de travail dans le dossier partagé, d'un coup. Renvoie « ok », « inchangé »,
        « lecture seule », « conflit » (quelqu'un d'autre a publié entre-temps : rien n'est écrasé) ou « erreur »."""
        with self.lock_mutex:
            if self.readonly or not self.local_db.exists():
                return "lecture seule" if self.readonly else "inchangé"
            lk = self.read_lock()
            if lk and not self._mine(lk):            # un autre ordinateur a pris la main
                self.beat()
                return "lecture seule"
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
                prot = self.protected
                if prot and self.vault is None:
                    return "lecture seule"
                if cur is not None and cur != st.get("shared"):
                    # le dossier a changé dans notre dos (synchronisation tardive d'un autre ordinateur) :
                    # on n'écrase pas ; notre version est déposée à côté, comme une copie en conflit à trancher
                    stamp = f"{_safe(self.host)} {dt.datetime.now():%Y-%m-%d %Hh%M}"
                    if prot:
                        self.vault.encrypt_file(snap, self.shared / f"voyages (version de {stamp}).chiffre", "base")
                    else:
                        atomic_copy(snap, self.shared / f"compta (version de {stamp}).db")
                    self._save_state(local=hl)
                    self.notice = ("Une autre version des voyages est arrivée dans le dossier partagé pendant que tu "
                                   "travaillais. Rien n'est perdu : les deux versions sont gardées ; choisis laquelle "
                                   "garder dans Réglages › Où ranger mes voyages.")
                    return "conflit"
                if not is_valid_db(snap):
                    return "erreur"
                if prot:
                    self.receipts_up()               # les justificatifs d'abord : la base n'en cite jamais d'absent
                    self.vault.encrypt_file(snap, self.shared_db, "base")
                    hs = file_hash(self.shared_db)
                else:
                    hs = file_hash(snap)
                    atomic_copy(snap, self.shared_db)
                self._remember_base(snap)            # seulement une fois l'envoi réussi
                self._save_state(shared=hs, local=hl)
                self._seen_sig = self._sig()
                self.published_at = self.clock()
                return "ok"
            except OSError:
                return "erreur"
            finally:
                snap.unlink(missing_ok=True)

    # ------------------------------------------------------------------ cycle de vie
    def open(self) -> None:
        """Au démarrage : vérifie le dossier, prend la main (ou lecture seule), reprend la version du dossier."""
        self.local.mkdir(parents=True, exist_ok=True)
        self.problem = self.check_folder()
        if self.problem:
            self.readonly = True
            self._flag_readonly(True)
            if self.shared.is_dir():                 # protégé en écriture : on peut quand même lire les voyages
                try:
                    self.pull()
                except OSError:
                    pass
            return
        (self.shared / RECEIPTS).mkdir(exist_ok=True)
        (self.shared / INBOX).mkdir(exist_ok=True)
        if self.protected:
            from . import crypto
            data = crypto.read_key_file(self.shared) or {}
            v = crypto.recall(data.get("id", "")) if data.get("id") else None
            if v is not None and self._key_works(v):
                self.unlock(v)                       # « se souvenir sur cet ordinateur »
            else:
                self.wipe_working_copy()             # restes en clair d'un arrêt brutal : effacés
                self._flag_locked(True)
            return
        self.acquire()
        self.pull()

    def _key_works(self, vault) -> bool:
        if not self.shared_db.exists():
            return True
        try:
            vault.decrypt(self.shared_db.read_bytes(), "base")
            return True
        except Exception:
            return False

    def unlock(self, vault) -> None:
        """Clé des voyages vérifiée : on reprend la main, déchiffre la base, les justificatifs et l'historique."""
        self.vault = vault
        self._flag_locked(False)
        self.decrypt_private()
        self.acquire()
        self._save_state(shared=None, local=None)    # reprendre la base chiffrée quoi qu'il arrive
        self.pull()
        self.receipts_down()

    def lock(self) -> None:
        """Verrouille : chiffre l'historique et les sauvegardes, efface la copie de travail en clair."""
        if self.vault is None:
            return
        if self.local_db.exists() and self.local_dirty() and not self.readonly:
            self.publish()
        if self.local_db.exists() and self.local_dirty():        # pas pu publier : gardée, chiffrée
            self.keep_local_copy("pas-encore-envoyee")
        self.encrypt_private()
        self.wipe_working_copy()
        self.vault = None
        self._flag_locked(True)
        self.release()

    def wipe_working_copy(self) -> None:
        """Efface ce qui est en clair et redondant avec le dossier : base de travail, justificatifs, vignettes."""
        if self.before_wipe:
            try:
                self.before_wipe()               # Windows ne supprime pas un fichier encore ouvert
            except Exception:
                pass
        for name in (DB, f"{DB}-journal", f"{DB}-wal", f"{DB}-shm", ".reception.db", ".envoi.db",
                     ".verif-conflit.db", ".base.db", ".protection.db", STATE):
            _unlink(self.local / name)
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

    def start(self, every: float = BEAT, on_tick=None) -> None:
        def loop():
            while not self._stop.wait(every):
                try:
                    if not self.problem:
                        self.beat()
                    if on_tick:
                        on_tick()
                except Exception:
                    pass
        self._thread = threading.Thread(target=loop, daemon=True, name="tripcompta-synchro")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.vault is not None:
            self.lock()                              # rien en clair ne reste sur l'ordinateur
        self.release()

    # ------------------------------------------------------------------ justificatifs à trier
    def inbox(self) -> list[dict]:
        return _D.inbox(self.shared)


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _safe(s: str) -> str:
    return re.sub(r"[^\w\- ]+", "", s, flags=re.UNICODE).strip() or "un autre ordinateur"


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return ""


def _alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(h)
            return code.value == 259                                     # STILL_ACTIVE
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


