"""Dossier de données, base SQLite, sauvegardes et « Annuler »."""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from .engine import sort_key
from .models import Base

UNDO_KEEP = 30
BACKUP_KEEP = 10
VERSION_KEY = "app_version"
# Numéro de la structure de la base (tables, colonnes), gardé dans le fichier lui-même (PRAGMA user_version).
# À augmenter de 1 à chaque changement de structure : une application qui connaît un numéro plus petit refuse
# d'ouvrir la base (elle ne saurait pas la lire sans l'abîmer) ; deux versions au même numéro s'entendent.
SCHEMA = 2
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
        self.backups_dir = self.dir / "sauvegardes"
        self.undo_dir = self.dir / ".annuler"
        self.redo_dir = self.dir / ".retablir"
        for d in (self.backups_dir, self.undo_dir, self.redo_dir):
            d.mkdir(exist_ok=True)
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
            if int(c.exec_driver_sql("PRAGMA user_version").scalar() or 0) == self.schema:
                return
            for table in Base.metadata.sorted_tables:
                have = {r[1] for r in c.exec_driver_sql(f'PRAGMA table_info("{table.name}")')}
                for col in table.columns:
                    if col.name not in have:
                        c.exec_driver_sql(f'ALTER TABLE "{table.name}" ADD COLUMN {_column_ddl(col, self.engine)}')
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
        if not self.path.exists():
            return None
        target = self.backups_dir / f"compta-{dt.date.today().isoformat()}.db"
        if target.exists():
            return target
        self._copy(self.path, target)
        backups = sorted(self.backups_dir.glob("compta-*.db"))
        for old in backups[:-BACKUP_KEEP]:
            old.unlink()
        return target

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
