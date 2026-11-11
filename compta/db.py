"""Dossier de données, base SQLite, sauvegardes et « Annuler »."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from .engine import sort_key
from .models import Base

UNDO_KEEP = 30
BACKUP_KEEP = 10
BEFORE_RETURN_KEEP = 3                 # états gardés « juste avant un retour à une sauvegarde »
RECEIPTS_POOL = "justificatifs"        # dans le dossier des sauvegardes : la copie des justificatifs
VERSION_KEY = "app_version"
# Numéro de la structure de la base (tables, colonnes), gardé dans le fichier lui-même (PRAGMA user_version).
# À augmenter de 1 à chaque changement de structure : une application qui connaît un numéro plus petit refuse
# d'ouvrir la base (elle ne saurait pas la lire sans l'abîmer) ; deux versions au même numéro s'entendent.
SCHEMA = 5
WRITER_KEY = "dernier_ecrivain"        # {"ordinateur", "quand"} : le dernier ordinateur qui a modifié les voyages


class NewerDatabaseError(Exception):
    """La base a été ouverte par une version plus récente de Trip Compta : une version plus ancienne ne doit pas
    y toucher (sa structure a pu changer)."""

    def __init__(self, stored: str, current: str, path: Path):
        self.stored, self.current, self.path = stored, current, path
        super().__init__(f"Ces données ont été utilisées par Trip Compta {stored}, plus récente que cette version "
                         f"({current}).")


class DamagedDatabaseError(Exception):
    """La base est abîmée et aucune sauvegarde lisible ne permet de la reprendre."""

    def __init__(self, path: Path):
        self.path = path
        super().__init__(f"Le fichier des voyages « {path} » est abîmé, et aucune sauvegarde lisible n'a été trouvée.")


class DataUnavailableError(Exception):
    """Le fichier des voyages est là mais pas lisible maintenant (pas encore téléchargé du cloud, disque ou dossier
    synchronisé déconnecté…) : ce n'est PAS une base abîmée, on ne touche à rien."""

    def __init__(self, path: Path):
        self.path = path
        super().__init__(f"Le fichier des voyages « {path} » n'est pas disponible pour l'instant.")


def db_state(path: Path) -> str:
    """« ok », « abimee » (lisible mais abîmée, ou pas une base SQLite) ou « indisponible » (fichier encore dans le
    cloud, disque débranché, accès refusé…). Lecture seule, rien n'est créé à côté."""
    from .dossier import cloud_only
    try:
        if cloud_only(path):
            return "indisponible"
        with open(path, "rb") as fh:             # lisible ? (sinon : indisponible, pas abîmé)
            head = fh.read(16)
    except OSError:
        return "indisponible"
    if head and head != b"SQLite format 3\x00":
        return "abimee"
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = con.execute("PRAGMA quick_check").fetchone()
        finally:
            con.close()
    except sqlite3.DatabaseError as e:
        msg = str(e).lower()
        if "unable to open" in msg or "disk i/o" in msg or "locked" in msg or "readonly" in msg:
            return "indisponible"
        return "abimee"
    return "ok" if row and row[0] == "ok" else "abimee"


def db_intact(path: Path) -> bool:
    """Vérification complète (integrity_check) : pour choisir une sauvegarde à reprendre."""
    if db_state(path) != "ok":
        return False
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        try:
            ok = con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            # une vraie base Trip Compta (pas un fichier SQLite quelconque)
            return ok and bool(con.execute("SELECT 1 FROM sqlite_master WHERE name='trips'").fetchone())
        finally:
            con.close()
    except sqlite3.Error:
        return False


def human_when(d: dt.datetime, now: dt.datetime | None = None) -> str:
    """« il y a 3 min », « aujourd'hui à 14 h 02 », « hier à 18 h 30 », « le 03/11 à 9 h 05 »."""
    now = now or dt.datetime.now()
    secs = (now - d).total_seconds()
    if 0 <= secs < 60:
        return "il y a moins d'une minute"
    if 0 <= secs < 3600:
        return f"il y a {int(secs // 60)} min"
    hm = f"{d.hour} h {d.minute:02d}"
    if d.date() == now.date():
        return f"aujourd'hui à {hm}"
    if d.date() == now.date() - dt.timedelta(days=1):
        return f"hier à {hm}"
    return f"le {d:%d/%m/%Y} à {hm}" if d.year != now.year else f"le {d:%d/%m} à {hm}"


def long_date(d: dt.date) -> str:
    """« mardi 3 novembre 2026 »"""
    days = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
    months = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
              "novembre", "décembre")
    return f"{days[d.weekday()]} {d.day}{'er' if d.day == 1 else ''} {months[d.month - 1]} {d.year}"


def backups_dir_for(folder: Path | str) -> Path:
    """Où sont rangées les sauvegardes des voyages de `folder` : sur cet ordinateur, à côté des réglages de Trip
    Compta, JAMAIS dans le dossier des voyages. Un dossier effacé, déplacé ou mal recopié n'emporte donc pas ses
    sauvegardes avec lui."""
    from . import paths
    key = hashlib.sha1(os.path.abspath(Path(folder).expanduser()).encode("utf-8")).hexdigest()[:12]
    return paths.config_file().parent / "sauvegardes" / key


class BackupError(Exception):
    """Cette sauvegarde ne peut pas être reprise (introuvable, abîmée, faite par une version plus récente)."""


def stored_version(path: Path) -> str | None:
    """La version de Trip Compta qui a ouvert cette base en dernier (lecture seule, sans rien créer)."""
    if not path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT value FROM settings WHERE key=?", (VERSION_KEY,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def stored_schema(path: Path) -> int | None:
    """Le numéro de structure écrit dans cette base (lecture seule, sans rien créer) ; None si illisible ou absente."""
    if not path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return int(con.execute("PRAGMA user_version").fetchone()[0])
        finally:
            con.close()
    except (sqlite3.Error, TypeError, ValueError):
        return None


def default_data_dir() -> Path:
    from .paths import default_data_dir as _d
    return _d()


SQLITE_SETTINGS = ("foreign_keys=ON", "journal_mode=DELETE", "synchronous=EXTRA", "busy_timeout=5000")


def _column_ddl(col, engine) -> str:
    """« nom TYPE [NOT NULL DEFAULT valeur] » pour ajouter à une table existante une colonne du modèle."""
    ddl = f'"{col.name}" {col.type.compile(engine.dialect)}'
    default = col.default.arg if col.default is not None and col.default.is_scalar else None
    if default is None:
        return ddl                                 # sans valeur par défaut : la colonne reste vide (NULL)
    if isinstance(default, bool):
        lit = "1" if default else "0"
    elif isinstance(default, (int, float)):
        lit = repr(default)
    else:
        lit = "'" + str(default).replace("'", "''") + "'"
    return f"{ddl} NOT NULL DEFAULT {lit}"


class Database:
    """`data_dir` : où l'app travaille (le dossier des voyages). `home` (voyages protégés, voir coffre.py) : le
    dossier des voyages, quand `data_dir` est la copie de travail déchiffrée sur cet ordinateur. Sans `home`, tout est
    dans `data_dir`."""

    def __init__(self, data_dir: Path | str | None = None, app_version: str | None = None,
                 home: Path | str | None = None, receipts_in_home: bool = True, schema: int | None = None):
        """receipts_in_home=False (dossier protégé par un mot de passe) : les justificatifs en clair restent dans
        `data_dir`, sur l'ordinateur ; le dossier ne contient que leurs versions chiffrées (voir coffre.py)."""
        from . import version as V
        self._args = (data_dir, app_version, home, receipts_in_home, schema)
        self.schema = schema or SCHEMA
        self.dir = Path(data_dir) if data_dir else default_data_dir()
        self.home = Path(home) if home else self.dir
        self.app_version = app_version or V.build_info()["version"]
        # AVANT toute écriture : une application qui ne connaît pas la structure de cette base n'y touche pas
        found = stored_schema(self.dir / "compta.db")
        if found is not None and found > self.schema:
            raise NewerDatabaseError(stored_version(self.dir / "compta.db") or "plus récente", self.app_version,
                                     self.dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.receipts_dir = (self.home if receipts_in_home else self.dir) / "justificatifs"
        self.thumbs_dir = (self.receipts_dir if self.home == self.dir else self.dir) / ".vignettes"
        # Sauvegardes. Voyages ordinaires : complètes (base + justificatifs) et rangées à part, sur l'ordinateur.
        # Voyages protégés par un mot de passe : `data_dir` est déjà la copie de travail sur l'ordinateur ; les
        # sauvegardes y restent (chiffrées au verrouillage), et les justificatifs chiffrés du dossier ne sont
        # jamais effacés : on ne les recopie pas en clair ailleurs.
        self.complete_backups = self.home == self.dir
        self.backups_dir = self.dir / "sauvegardes"
        if self.complete_backups:
            try:
                apart = backups_dir_for(self.dir)
                apart.mkdir(parents=True, exist_ok=True)
                note = apart / "A LIRE.txt"
                if not note.exists():
                    note.write_text("Sauvegardes de Trip Compta pour les voyages rangés dans :\n"
                                    f"{self.dir}\n\nPour revenir à l'une d'elles : dans Trip Compta, Réglages, "
                                    "« Sauvegardes ».\n", encoding="utf-8")
                self.backups_dir = apart
            except OSError:                      # réglages de l'ordinateur inaccessibles : à côté de la base
                pass
        self._mirror_sig = None
        self.undo_dir = self.dir / ".annuler"
        self.redo_dir = self.dir / ".retablir"
        for d in (self.backups_dir, self.undo_dir, self.redo_dir):
            d.mkdir(parents=True, exist_ok=True)
        try:                                     # dossier synchronisé introuvable : ne rien y créer
            if self.home == self.dir or self.home.is_dir() or not receipts_in_home:
                self.receipts_dir.mkdir(exist_ok=True)
        except OSError:
            pass
        self.path = self.dir / "compta.db"
        # base abîmée (coupure pendant un enregistrement, cloud qui l'a mal recopiée…) : on reprend la sauvegarde
        # lisible la plus récente ; le fichier abîmé est gardé à côté, jamais effacé
        self.recovered = None
        if self.path.exists() and self.path.stat().st_size:
            state = db_state(self.path)
            if state == "indisponible":
                raise DataUnavailableError(self.path)
            if state == "abimee":
                self.recovered = self._recover()
        self.engine = create_engine(f"sqlite:///{self.path}", connect_args={"check_same_thread": False})

        @event.listens_for(self.engine, "connect")
        def _settings(conn, _):
            # Réglages écrits noir sur blanc (ne pas dépendre de ceux par défaut de la bibliothèque SQLite) :
            #  - un seul fichier « compta.db » au repos (pas de fichiers -wal / -shm à côté, qu'un dossier
            #    synchronisé recopierait séparément) ;
            #  - chaque enregistrement est écrit pour de bon sur le disque avant de rendre la main ;
            #  - si la base est occupée un instant, on patiente 5 secondes au lieu d'échouer.
            for pragma in SQLITE_SETTINGS:
                conn.execute(f"PRAGMA {pragma}").fetchall()
            conn.create_function("unaccent", 1, lambda v: sort_key(v or ""), deterministic=True)

        self._open_structure()
        self._remember_version()
        self.Session = sessionmaker(self.engine, expire_on_commit=False)

    def reopen(self, data_dir=None, receipts_in_home=None):
        """Rouvre la base ailleurs (déverrouillage / verrouillage d'un dossier protégé) : tout le code qui tient
        `db` suit, puisqu'il passe par db.Session, db.receipts_dir… à chaque appel."""
        try:
            self.engine.dispose()
        except Exception:
            pass
        d, v, h, r, sc = self._args
        self.__init__(data_dir if data_dir is not None else d, v, h,
                      receipts_in_home if receipts_in_home is not None else r, sc)

    def _remember_version(self):
        with self.engine.begin() as c:              # écrire seulement si elle change (sinon la base « bouge » pour rien)
            c.exec_driver_sql("INSERT INTO settings (key, value) VALUES (?, ?) "
                              "ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE value != excluded.value",
                              (VERSION_KEY, self.app_version))

    # ---------------------------------------------------------- dernier ordinateur qui a écrit
    def mark_writer(self):
        """Note dans la base quel ordinateur la modifie (appelé avec chaque instantané « Annuler », donc à chaque
        modification, et après Défaire / Rétablir). Hors de l'instantané : « Défaire » n'annule que l'action."""
        from .dossier import computer_name
        value = json.dumps({"ordinateur": computer_name(),
                            "quand": dt.datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False)
        try:
            with self.engine.begin() as c:
                c.exec_driver_sql("INSERT INTO settings (key, value) VALUES (?, ?) "
                                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (WRITER_KEY, value))
        except Exception:                          # jamais au prix de l'action elle-même
            pass

    def last_writer(self) -> dict | None:
        try:
            with self.engine.connect() as c:
                row = c.exec_driver_sql("SELECT value FROM settings WHERE key=?", (WRITER_KEY,)).fetchone()
            w = json.loads(row[0]) if row else None
            w["quand"] = dt.datetime.fromisoformat(w["quand"])
            return w
        except Exception:
            return None

    def _open_structure(self):
        """Crée les tables qui manquent, ajoute les colonnes qui manquent, puis écrit le numéro de structure.
        Rien de propre à une ancienne version ici : la base est simplement alignée sur les modèles (models.py)."""
        Base.metadata.create_all(self.engine)
        with self.engine.begin() as c:
            for table in Base.metadata.sorted_tables:
                have = {r[1] for r in c.exec_driver_sql(f'PRAGMA table_info("{table.name}")')}
                for col in table.columns:
                    if col.name not in have:
                        c.exec_driver_sql(f'ALTER TABLE "{table.name}" ADD COLUMN {_column_ddl(col, self.engine)}')
            if int(c.exec_driver_sql("PRAGMA user_version").scalar() or 0) != self.schema:   # sinon : ne rien écrire
                c.exec_driver_sql(f"PRAGMA user_version = {int(self.schema)}")

    # ---------------------------------------------------------- sauvegardes
    def _recover(self) -> dict:
        """Remplace la base abîmée par la copie lisible la plus récente (instantanés « Annuler » et sauvegardes du
        jour), et renvoie {"damaged": fichier gardé, "when": date de la copie reprise}."""
        candidates = [p for d in (self.undo_dir, self.backups_dir) for p in d.glob("*.db")]
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        good = next((p for p in candidates if db_intact(p)), None)
        if good is None:
            raise DamagedDatabaseError(self.path)
        now = dt.datetime.now()
        damaged = self.dir / f"compta (abîmée {now:%Y-%m-%d %Hh%M}).db"
        n = 2
        while damaged.exists():
            damaged = self.dir / f"compta (abîmée {now:%Y-%m-%d %Hh%M} {n}).db"
            n += 1
        os.replace(self.path, damaged)
        for side in ("-journal", "-wal", "-shm"):    # ce qui accompagnait la base abîmée ne vaut plus rien
            Path(f"{self.path}{side}").unlink(missing_ok=True)
        self._copy(good, self.path)
        return dict(damaged=damaged, when=dt.datetime.fromtimestamp(good.stat().st_mtime))

    def recovered_notice(self) -> str | None:
        if not self.recovered:
            return None
        return (f"Le fichier de tes voyages était abîmé : Trip Compta a repris la dernière copie intacte "
                f"({human_when(self.recovered['when'])}). Ce qui a été saisi après est perdu. Le fichier abîmé est "
                f"gardé à côté : « {self.recovered['damaged'].name} ».")

    def daily_backup(self) -> Path | None:
        """Une sauvegarde par jour (les 10 dernières sont gardées), et les justificatifs qui n'y sont pas encore."""
        if not self.path.exists():
            return None
        target = self.backups_dir / f"compta-{dt.date.today().isoformat()}.db"
        if not target.exists():
            self._copy(self.path, target)
            backups = sorted(self.backups_dir.glob("compta-*.db"))
            for old in backups[:-BACKUP_KEEP]:
                old.unlink()
        self.mirror_receipts()
        return target

    def mirror_receipts(self, force: bool = False) -> int:
        """Recopie dans les sauvegardes les justificatifs qui n'y sont pas encore (jamais d'effacement : un
        justificatif retiré d'une dépense reste récupérable). Renvoie le nombre de fichiers copiés. Ne fait rien
        tant que le dossier des justificatifs n'a pas bougé depuis le dernier passage."""
        if not self.complete_backups or not self.receipts_dir.is_dir():
            return 0
        try:
            sig = self.receipts_dir.stat().st_mtime_ns
        except OSError:
            return 0
        if not force and sig == self._mirror_sig:
            return 0
        pool = self.backups_dir / RECEIPTS_POOL
        pool.mkdir(exist_ok=True)
        copied, complete = 0, True
        with os.scandir(self.receipts_dir) as entries:
            for e in entries:
                if e.name.startswith("."):
                    continue
                try:
                    if not e.is_file():
                        continue
                    dst = pool / e.name
                    if dst.exists() and dst.stat().st_size == e.stat().st_size:
                        continue
                    tmp = pool / f".{e.name}.copie"
                    shutil.copyfile(e.path, tmp)
                    os.replace(tmp, dst)
                    copied += 1
                except OSError:                  # fichier pas encore téléchargé, disque plein… : au prochain passage
                    complete = False
        self._mirror_sig = sig if complete else None
        return copied

    # ---------------------------------------------------------- revenir à une sauvegarde
    @staticmethod
    def _backup_summary(path: Path) -> dict | None:
        """Ce que contient une sauvegarde : noms des voyages, nombre de dépenses, justificatifs. None si illisible."""
        try:
            c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                trips = [r[0] for r in c.execute("SELECT name FROM trips ORDER BY id")]
                expenses = c.execute("SELECT COUNT(*) FROM expenses").fetchone()[0]
                receipts = [r[0] for r in c.execute("SELECT stored_name FROM receipts")]
            finally:
                c.close()
            return dict(trips=trips, expenses=expenses, receipts=receipts)
        except sqlite3.Error:
            return None

    def backups(self) -> list[dict]:
        """Les sauvegardes disponibles, de la plus récente à la plus ancienne :
        {name, when, timed, kind, title, trips, expenses, receipts, missing_receipts, readable}.
        `timed` : l'heure de `when` est fiable (pour une sauvegarde du jour dont le fichier a été recopié depuis,
        seul le jour, écrit dans son nom, l'est)."""
        out = []
        for p in self.backups_dir.glob("*.db"):
            try:
                when = dt.datetime.fromtimestamp(p.stat().st_mtime)
            except OSError:
                continue
            stem = p.stem
            timed = True
            if stem.startswith("compta-"):
                try:
                    day = dt.date.fromisoformat(stem[len("compta-"):])
                except ValueError:
                    day = when.date()
                if day != when.date():
                    when, timed = dt.datetime.combine(day, dt.time(12, 0)), False
                kind, title = "jour", f"Sauvegarde du {long_date(day)}"
            elif stem.startswith("avant-maj-"):
                kind, title = "maj", f"Avant la mise à jour vers Trip Compta {stem[len('avant-maj-'):]}"
            elif stem.startswith("avant-retour-"):
                kind, title = "retour", "Juste avant ton dernier retour à une sauvegarde"
            elif stem.startswith("pas-encore-enregistree-"):
                kind, title = "attente", "Modifications qui n'avaient pas pu être enregistrées dans le dossier"
            else:
                kind, title = "autre", stem
            info = self._backup_summary(p)
            names = (info or {}).get("receipts", [])
            pool = self.backups_dir / RECEIPTS_POOL
            missing = sum(1 for n in names if not (self.receipts_dir / n).exists() and not (pool / n).exists()) \
                if self.complete_backups else 0
            out.append(dict(name=p.name, when=when, timed=timed, kind=kind, title=title, readable=info is not None,
                            trips=(info or {}).get("trips", []), expenses=(info or {}).get("expenses", 0),
                            receipts=len(names), missing_receipts=missing))
        out.sort(key=lambda b: b["when"], reverse=True)
        return out

    def restore_backup(self, name: str) -> dict:
        """Revenir à une sauvegarde : l'état actuel est d'abord gardé (« Défaire » le rend, et une copie « juste
        avant ton retour » reste dans la liste), puis la base est remplacée et les justificatifs qui manquent sont
        remis. Renvoie {title, when, timed, receipts_back, receipts_missing}."""
        found = next((b for b in self.backups() if b["name"] == name), None)
        src = self.backups_dir / name if found else None
        if src is None or not src.is_file():
            raise BackupError("Cette sauvegarde est introuvable.")
        if not db_intact(src):
            raise BackupError("Cette sauvegarde est abîmée : elle ne peut pas être reprise. Choisis-en une autre.")
        if (stored_schema(src) or 0) > self.schema:
            raise BackupError("Cette sauvegarde a été faite par une version plus récente de Trip Compta : mets "
                              "l'application à jour pour la reprendre.")
        now = dt.datetime.now()
        self._copy(self.path, self.backups_dir / f"avant-retour-{now:%Y%m%d-%H%M%S-%f}.db")
        for old in sorted(self.backups_dir.glob("avant-retour-*.db"))[:-BEFORE_RETURN_KEEP]:
            old.unlink()
        when = found["when"]
        self.snapshot(f"retour à la sauvegarde du {when:%d/%m/%Y}")
        self.engine.dispose()
        self._copy(src, self.path)
        self.after_replace()
        self.mark_writer()
        back, missing = self.restore_receipts()
        return dict(title=found["title"], when=when, timed=found["timed"], receipts_back=back,
                    receipts_missing=missing)

    def restore_receipts(self) -> tuple[int, int]:
        """Remet dans le dossier des justificatifs ceux que la base connaît et qui n'y sont plus, depuis les
        sauvegardes. Renvoie (remis, toujours introuvables)."""
        info = self._backup_summary(self.path) or {}
        pool = self.backups_dir / RECEIPTS_POOL
        back = missing = 0
        for n in info.get("receipts", []):
            dst = self.receipts_dir / n
            if dst.exists():
                continue
            src = pool / n
            try:
                if not src.is_file():
                    raise OSError("absent des sauvegardes")
                self.receipts_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
                back += 1
            except OSError:
                missing += 1
        return back, missing

    @staticmethod
    def _copy(src: Path, dst: Path):
        s = sqlite3.connect(src)
        d = sqlite3.connect(dst)
        with d:
            s.backup(d)
        s.close()
        d.close()

    # ---------------------------------------------------------- annuler
    def snapshot(self, label: str):
        """À appeler AVANT une modification : permet de la défaire ensuite. Une nouvelle action efface « Rétablir »."""
        self._push(self.undo_dir, label)
        for f in self.redo_dir.glob("*"):
            f.unlink()
        self.mark_writer()

    def _push(self, folder: Path, label: str):
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        self._copy(self.path, folder / f"{stamp}.db")
        (folder / f"{stamp}.json").write_text(json.dumps({"label": label}), encoding="utf-8")
        snaps = sorted(folder.glob("*.db"))
        for old in snaps[:-UNDO_KEEP]:
            old.unlink()
            old.with_suffix(".json").unlink(missing_ok=True)

    @staticmethod
    def _label(folder: Path) -> str | None:
        snaps = sorted(folder.glob("*.db"))
        if not snaps:
            return None
        try:
            return json.loads(snaps[-1].with_suffix(".json").read_text(encoding="utf-8"))["label"]
        except Exception:
            return "dernière modification"

    def _pop_restore(self, folder: Path):
        snap = sorted(folder.glob("*.db"))[-1]
        self.engine.dispose()
        self._copy(snap, self.path)
        snap.unlink()
        snap.with_suffix(".json").unlink(missing_ok=True)
        self.after_replace()
        self.mark_writer()

    def after_replace(self):
        """La base a été remplacée sur le disque (Annuler, version reprise du dossier synchronisé) : on la rouvre,
        alignée sur la structure de cette version."""
        self.engine.dispose()
        self._open_structure()

    def drop_last_snapshot(self):
        """Supprime le dernier instantané (opération finalement annulée / en erreur)."""
        snaps = sorted(self.undo_dir.glob("*.db"))
        if snaps:
            snaps[-1].unlink()
            snaps[-1].with_suffix(".json").unlink(missing_ok=True)

    @staticmethod
    def _top(folder: Path) -> str | None:
        snaps = sorted(folder.glob("*.db"))
        return snaps[-1].stem if snaps else None

    def undo_token(self) -> str | None:
        """Identifiant de la dernière action défaisable (change à chaque nouvelle action)."""
        return self._top(self.undo_dir)

    def redo_token(self) -> str | None:
        return self._top(self.redo_dir)

    def last_action(self) -> str | None:
        return self._label(self.undo_dir)

    def last_undone(self) -> str | None:
        """Ce que « Rétablir » referait (la dernière action défaite), s'il y en a une."""
        return self._label(self.redo_dir)

    def history(self) -> list[dict]:
        """Les modifications que l'on peut encore défaire, de la plus récente à la plus ancienne :
        [{token, label, when}] (`token` : celui que donnait undo_token() juste après cette modification)."""
        out = []
        for snap in sorted(self.undo_dir.glob("*.db"), reverse=True):
            try:
                label = json.loads(snap.with_suffix(".json").read_text(encoding="utf-8"))["label"]
            except Exception:
                label = "modification"
            try:
                when = dt.datetime.strptime(snap.stem, "%Y%m%d-%H%M%S-%f")
            except ValueError:
                when = dt.datetime.fromtimestamp(snap.stat().st_mtime)
            out.append(dict(token=snap.stem, label=label, when=when))
        return out

    def undo_to(self, token: str) -> list[str]:
        """Défait la modification `token` et toutes celles faites après elle (de la plus récente à la plus
        ancienne). Renvoie leurs libellés ; [] si `token` n'est plus dans l'historique (rien n'est touché).
        Chaque étape se rétablit ensuite une à une avec « Rétablir »."""
        if token not in {h["token"] for h in self.history()}:
            return []
        done = []
        while True:
            top = self.undo_token()
            if top is None or top < token:
                break
            done.append(self.undo())
        return done

    def undo(self) -> str | None:
        label = self.last_action()
        if label is None:
            return None
        self._push(self.redo_dir, label)          # pour pouvoir « Rétablir »
        self._pop_restore(self.undo_dir)
        return label

    def redo(self) -> str | None:
        label = self.last_undone()
        if label is None:
            return None
        self._push(self.undo_dir, label)          # rétablir se défait aussi
        self._pop_restore(self.redo_dir)
        return label
