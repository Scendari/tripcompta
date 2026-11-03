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
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

DB = "compta.db"
LOCK = "tripcompta-verrou.json"
RECEIPTS = "justificatifs"
INBOX = "Justificatifs à trier"
INBOX_DONE = "Rangés"
OLD_VERSIONS = "Anciennes versions"
STATE = "synchro.json"                  # dans la copie de travail : ce qu'on sait du dossier partagé
READONLY_FLAG = "LECTURE-SEULE"         # dans la copie de travail : l'IA (autre processus) ne doit pas écrire
BEAT = 30                               # secondes entre deux battements du verrou
STALE = 180                             # verrou sans battement depuis 3 min : ordinateur éteint ou planté
INBOX_TYPES = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".pdf"}

# copies en conflit créées par les services : « compta (conflicted copy 2026-…).db », « compta (copie en
# conflit de …).db », « compta-PC-DE-PAUL.db » (OneDrive), « compta (1).db » (Google Drive), « compta 2.db » (iCloud)…
CONFLICT_RE = re.compile(r"^compta(?!\.db$).*\.db$", re.I)

SERVICES = [   # (motif dans le chemin, nom affiché)
    ("dropbox", "Dropbox"), ("googledrive", "Google Drive"), ("google drive", "Google Drive"),
    ("mon drive", "Google Drive"), ("my drive", "Google Drive"), ("onedrive", "OneDrive"),
    ("mobile documents", "iCloud Drive"), ("icloud", "iCloud Drive"), ("kdrive", "kDrive"),
    ("proton", "Proton Drive"), ("pcloud", "pCloud"), ("nextcloud", "Nextcloud"), ("box", "Box"),
    ("synology", "Synology Drive"), ("tresorit", "Tresorit"),
]


def service_name(path: Path | str) -> str | None:
    low = str(path).lower().replace("\\", "/")
    for key, name in SERVICES:
        if key == "box":
            if re.search(r"(^|/)box(/|$)", low):
                return name
        elif key in low:
            return name
    return None


def computer_name() -> str:
    """Nom lisible de cet ordinateur (« MacBook Pro de Paul », « PC-BUREAU »)."""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=3)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        if sys.platform == "win32" and os.environ.get("COMPUTERNAME"):
            return os.environ["COMPUTERNAME"]
    except Exception:
        pass
    return socket.gethostname().split(".")[0] or "cet ordinateur"


def file_hash(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def is_valid_db(path: Path) -> bool:
    """Base SQLite complète et lisible (sans rien écrire à côté : ni journal ni verrou)."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        try:
            return con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            con.close()
    except sqlite3.Error:
        return False


def sqlite_copy(src: Path, dst: Path) -> None:
    """Copie cohérente d'une base ouverte (API de sauvegarde de SQLite), sans journal laissé derrière."""
    s = sqlite3.connect(src)
    d = sqlite3.connect(dst)
    try:
        with d:
            s.backup(d)
        d.execute("PRAGMA journal_mode=DELETE")
    finally:
        s.close()
        d.close()


def replace(src: Path, dst: Path, tries: int = 8) -> None:
    """os.replace, en réessayant un peu si Windows bloque momentanément le fichier (antivirus, indexation…)."""
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.25 * (i + 1))


def atomic_copy(src: Path, dst: Path, tries: int = 8) -> None:
    """Copie `src` vers `dst` d'un coup : fichier temporaire dans le même dossier, écrit et vidé sur le disque,
    puis remplacement atomique. Réessaie un peu si le fichier est momentanément bloqué (Windows, antivirus…)."""
    tmp = dst.with_name(f".{dst.name}.{uuid.uuid4().hex[:8]}.envoi")
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            shutil.copyfileobj(fi, fo, 1024 * 1024)
            fo.flush()
            os.fsync(fo.fileno())
        replace(tmp, dst, tries)
    finally:
        tmp.unlink(missing_ok=True)


def cloud_only(path: Path) -> bool:
    """Fichier présent seulement dans le cloud (pas encore téléchargé sur cet ordinateur) ?
    iCloud : « .compta.db.icloud » à la place du fichier ; Mac (File Provider) : fichier « sans données » ;
    Windows (OneDrive, Dropbox…) : attributs « hors connexion » / « rappel à l'accès »."""
    if (path.parent / f".{path.name}.icloud").exists() and not path.exists():
        return True
    try:
        st = os.stat(path)
    except OSError:
        return False
    if sys.platform == "darwin" and getattr(st, "st_flags", 0) & 0x40000000:     # SF_DATALESS
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & (0x1000 | 0x400000 | 0x40000))   # OFFLINE | RECALL_ON_DATA_ACCESS | RECALL_ON_OPEN


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

    # ------------------------------------------------------------------ chemins
    @property
    def shared_db(self) -> Path:
        return self.shared / DB

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
        out = []
        try:
            names = os.listdir(self.shared)
        except OSError:
            return out
        for n in sorted(names):
            if n.startswith(".") or n == DB or not CONFLICT_RE.match(n):
                continue
            p = self.shared / n
            try:
                st = p.stat()
            except OSError:
                continue
            out.append(dict(name=n, size=st.st_size, date=dt.datetime.fromtimestamp(st.st_mtime),
                            valid=is_valid_db(p)))
        return out

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

    def local_dirty(self) -> bool:
        """La copie de travail contient-elle des modifications pas encore publiées ?"""
        if not self.local_db.exists():
            return False
        return file_hash(self.local_db) != self._state().get("local")

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
        if self.local_db.exists() and file_hash(self.local_db) == h:      # déjà la même (1re ouverture)
            self._save_state(shared=h, local=h)
            self._seen_sig = self._sig()
            return False
        incoming = self.local / ".reception.db"
        shutil.copyfile(self.shared_db, incoming)
        if file_hash(incoming) != h or not is_valid_db(incoming):
            incoming.unlink(missing_ok=True)         # fichier en cours de synchronisation : on réessaiera
            return False
        kept = None
        if self.local_db.exists() and self.local_dirty():
            kept = self.keep_local_copy("pas-encore-envoyee")
        if before_replace:
            before_replace()
        replace(incoming, self.local_db)
        for ext in ("-journal", "-wal", "-shm"):
            (self.local / f"{DB}{ext}").unlink(missing_ok=True)
        self._save_state(shared=h, local=h)
        self._seen_sig = self._sig()
        self.pulled_at = self.clock()
        if kept:
            self.notice = (f"La version du dossier partagé a été reprise. Tes dernières modifications faites ici "
                           f"n'avaient pas pu être enregistrées dans le dossier : elles sont gardées dans "
                           f"« {kept.name} » (sauvegardes de cet ordinateur).")
        return True

    def keep_local_copy(self, why: str) -> Path:
        folder = self.local / "sauvegardes"
        folder.mkdir(parents=True, exist_ok=True)
        dst = folder / f"{why}-{dt.datetime.now():%Y-%m-%d-%Hh%M%S}.db"
        sqlite_copy(self.local_db, dst)
        return dst

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
                if cur is not None and cur != st.get("shared"):
                    # le dossier a changé dans notre dos (synchronisation tardive d'un autre ordinateur) :
                    # on n'écrase pas ; notre version est déposée à côté, comme une copie en conflit à trancher
                    side = self.shared / f"compta (version de {_safe(self.host)} {dt.datetime.now():%Y-%m-%d %Hh%M}).db"
                    atomic_copy(snap, side)
                    self._save_state(local=hl)
                    self.notice = ("Une autre version des voyages est arrivée dans le dossier partagé pendant que tu "
                                   "travaillais. Rien n'est perdu : les deux versions sont gardées ; choisis laquelle "
                                   "garder dans Réglages › Où ranger mes voyages.")
                    return "conflit"
                if not is_valid_db(snap):
                    return "erreur"
                hs = file_hash(snap)
                atomic_copy(snap, self.shared_db)
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
        self.acquire()
        self.pull()

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
        self.release()

    # ------------------------------------------------------------------ justificatifs à trier
    def inbox(self) -> list[dict]:
        folder = self.shared / INBOX
        out = []
        try:
            entries = sorted(folder.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return out
        for p in entries:
            if p.name.startswith(".") or not p.is_file() or p.suffix.lower() not in INBOX_TYPES:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            out.append(dict(name=p.name, size=st.st_size, date=dt.datetime.fromtimestamp(st.st_mtime),
                            pdf=p.suffix.lower() == ".pdf", offline=cloud_only(p)))
        return out


def inbox_file(shared: Path, name: str) -> Path | None:
    """Un fichier de « Justificatifs à trier » (nom contrôlé : pas de sous-dossier ni de « .. »)."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    p = Path(shared) / INBOX / name
    return p if p.is_file() else None


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


# ---------------------------------------------------------------------- dossiers cloud de cet ordinateur
def cloud_roots(home: Path | None = None, env: dict | None = None, platform: str | None = None) -> list[dict]:
    """Dossiers synchronisés trouvés sur cet ordinateur : [{name, root}] (racine du service ; Trip Compta y
    proposera un sous-dossier « TripCompta »). Paramètres : pour les tests."""
    home = Path(home or Path.home())
    env = os.environ if env is None else env
    platform = platform or sys.platform
    found: list[tuple[str, Path]] = []

    def add(name, p):
        p = Path(p)
        if p.is_dir() and all(p != q for _, q in found):
            found.append((name, p))

    cs = home / "Library" / "CloudStorage"                      # Mac : services modernes (File Provider)
    if platform == "darwin" and cs.is_dir():
        for d in sorted(cs.iterdir(), key=lambda x: x.name.lower()):
            low = d.name.lower()
            if low.startswith("googledrive"):
                for sub in ("Mon Drive", "My Drive"):
                    if (d / sub).is_dir():
                        add("Google Drive" + _account(d.name), d / sub)
                        break
            elif d.is_dir():
                name = service_name(d.name) or d.name.split("-")[0]
                add(name + _account(d.name), d)
    if platform == "darwin":
        add("iCloud Drive", home / "Library" / "Mobile Documents" / "com~apple~CloudDocs")
    add("Dropbox", home / "Dropbox")
    for k in ("OneDriveConsumer", "OneDriveCommercial", "OneDrive"):
        if env.get(k):
            add("OneDrive", env[k])
    add("OneDrive", home / "OneDrive")
    add("iCloud Drive", home / "iCloudDrive")                   # iCloud pour Windows
    add("kDrive", home / "kDrive")
    add("pCloud", home / "pCloud Drive")
    add("pCloud", home / "pCloudDrive")
    add("Nextcloud", home / "Nextcloud")
    pd = home / "Proton Drive"
    if pd.is_dir():
        for acc in sorted(pd.iterdir()):
            for sub in ("My files", "Mes fichiers"):
                if (acc / sub).is_dir():
                    add("Proton Drive", acc / sub)
    if platform == "win32":                                      # Google Drive pour ordinateur : lecteur G:, H:…
        for letter in "GHIJKLMNOPQRSTUVWXYZ":
            for sub in ("Mon Drive", "My Drive"):
                p = Path(f"{letter}:/") / sub
                try:
                    if p.is_dir():
                        add("Google Drive", p)
                except OSError:
                    pass
    out = []
    for name, root in found:
        target = root / "TripCompta"
        out.append(dict(name=name, root=str(root), target=str(target), has_data=(target / DB).exists()
                        or (target / f".{DB}.icloud").exists()))
    return out


def _account(folder_name: str) -> str:
    """« GoogleDrive-paul@exemple.fr » → « (paul@exemple.fr) » ; « Dropbox » → « »."""
    _, _, acc = folder_name.partition("-")
    return f" ({acc})" if acc else ""


def pick_folder(prompt: str = "Choisis le dossier où ranger tes voyages") -> str | None:
    """Fenêtre « choisir un dossier » du système (Mac, Windows, Linux). None si annulé ou impossible."""
    try:
        if sys.platform == "darwin":
            r = subprocess.run(["osascript", "-e", f'POSIX path of (choose folder with prompt "{prompt}")'],
                               capture_output=True, text=True, timeout=600)
        elif sys.platform == "win32":
            ps = ("Add-Type -AssemblyName System.Windows.Forms;"
                  "$f=New-Object System.Windows.Forms.FolderBrowserDialog;"
                  f"$f.Description='{prompt}';$f.ShowNewFolderButton=$true;"
                  "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                  "if($f.ShowDialog() -eq 'OK'){$f.SelectedPath}")
            r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps], capture_output=True,
                               text=True, encoding="utf-8", timeout=600)
        else:
            cmd = (["zenity", "--file-selection", "--directory", f"--title={prompt}"] if shutil.which("zenity")
                   else ["kdialog", "--getexistingdirectory", str(Path.home())] if shutil.which("kdialog") else None)
            if cmd is None:
                return None
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError):
        return None
    path = (r.stdout or "").strip()
    return path.rstrip("/\\") or None if r.returncode == 0 and path else None
