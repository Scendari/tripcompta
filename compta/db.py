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


class NewerDatabaseError(Exception):
    """La base a été ouverte par une version plus récente de Trip Compta : une version plus ancienne ne doit pas
    y toucher (sa structure a pu changer)."""

    def __init__(self, stored: str, current: str, path: Path):
        self.stored, self.current, self.path = stored, current, path
        super().__init__(f"Ces données ont été utilisées par Trip Compta {stored}, plus récente que cette version "
                         f"({current}).")


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


def default_data_dir() -> Path:
    from .paths import default_data_dir as _d
    return _d()


class Database:
    """`data_dir` : où l'app travaille. `home` (dossier synchronisé, voir cloud.py) : là où sont rangés la version
    publiée de la base et les justificatifs ; la base, l'historique « Annuler », les sauvegardes et les vignettes
    restent alors dans `data_dir`, sur l'ordinateur. Sans `home`, tout est dans `data_dir` (comme avant)."""

    def __init__(self, data_dir: Path | str | None = None, app_version: str | None = None,
                 home: Path | str | None = None, receipts_in_home: bool = True):
        """receipts_in_home=False (dossier protégé par un mot de passe) : les justificatifs en clair restent dans
        `data_dir`, sur l'ordinateur ; le dossier ne contient que leurs versions chiffrées (voir cloud.py)."""
        from . import version as V
        self._args = (data_dir, app_version, home, receipts_in_home)
        self.dir = Path(data_dir) if data_dir else default_data_dir()
        self.home = Path(home) if home else self.dir
        self.app_version = app_version or V.build_info()["version"]
        # AVANT toute écriture : une version plus ancienne refuse une base passée par une version plus récente
        stored = stored_version(self.dir / "compta.db")
        if stored and V.base(stored) and V.base(self.app_version) and V.base(stored) > V.base(self.app_version):
            raise NewerDatabaseError(stored, self.app_version, self.dir)
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
        self.engine = create_engine(f"sqlite:///{self.path}", connect_args={"check_same_thread": False})

        @event.listens_for(self.engine, "connect")
        def _fk(conn, _):
            conn.execute("PRAGMA foreign_keys=ON")
            conn.create_function("unaccent", 1, lambda v: sort_key(v or ""), deterministic=True)

        Base.metadata.create_all(self.engine)
        self._migrate()
        self._remember_version()
        self.Session = sessionmaker(self.engine, expire_on_commit=False)

    def reopen(self, data_dir=None, receipts_in_home=None):
        """Rouvre la base ailleurs (déverrouillage / verrouillage d'un dossier protégé) : tout le code qui tient
        `db` suit, puisqu'il passe par db.Session, db.receipts_dir… à chaque appel."""
        try:
            self.engine.dispose()
        except Exception:
            pass
        d, v, h, r = self._args
        self.__init__(data_dir if data_dir is not None else d, v, h,
                      receipts_in_home if receipts_in_home is not None else r)

    def _remember_version(self):
        with self.engine.begin() as c:              # écrire seulement si elle change (sinon la base « bouge » pour rien)
            c.exec_driver_sql("INSERT INTO settings (key, value) VALUES (?, ?) "
                              "ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE value != excluded.value",
                              (VERSION_KEY, self.app_version))

    # Colonnes ajoutées après la première version : on les crée dans les bases existantes.
    MIGRATIONS = {
        "participants": {"family_id": "INTEGER", "companion": "BOOLEAN NOT NULL DEFAULT 0",
                         "roles": "VARCHAR(100) NOT NULL DEFAULT ''"},
        "categories": {"audience": "VARCHAR(20) NOT NULL DEFAULT ''"},
        "expenses": {"kind": "VARCHAR(20) NOT NULL DEFAULT 'depense'", "refund": "INTEGER",
                     "refund_to_id": "INTEGER", "subcategory": "VARCHAR(100) NOT NULL DEFAULT ''",
                     "tickets": "TEXT NOT NULL DEFAULT ''", "cents_mode": "VARCHAR(20) NOT NULL DEFAULT ''",
                     "cents_who": "TEXT NOT NULL DEFAULT ''"},
        "expense_shares": {"fixed": "INTEGER"},
        "trips": {"note": "TEXT NOT NULL DEFAULT ''", "cashier_id": "INTEGER", "asso_name": "VARCHAR(100) NOT NULL DEFAULT ''",
                  "is_demo": "BOOLEAN NOT NULL DEFAULT 0", "groups": "TEXT NOT NULL DEFAULT ''",
                  "roles_conf": "TEXT NOT NULL DEFAULT ''",
                  "rounding": "VARCHAR(20) NOT NULL DEFAULT 'participants'"},
    }

    def _migrate(self):
        with self.engine.begin() as c:
            for table, cols in self.MIGRATIONS.items():
                have = {r[1] for r in c.exec_driver_sql(f"PRAGMA table_info({table})")}
                for col, ddl in cols.items():
                    if col not in have:
                        c.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                        if (table, col) == ("trips", "asso_name"):   # chaque voyage garde le nom commun d'avant
                            c.exec_driver_sql("UPDATE trips SET asso_name = COALESCE((SELECT substr(trim(value), 1, 100) "
                                              "FROM settings WHERE key = 'nom_asso'), '')")
            self._migrate_roles(c)
            self._migrate_fixed_adjustments(c)

    @staticmethod
    def _migrate_roles(c):
        """Une seule fois : plongeur et accompagnant deviennent des rôles (« roles »), à partir de « companion »."""
        tables = {r[0] for r in c.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
        if "settings" not in tables or "participants" not in tables:
            return
        if c.exec_driver_sql("SELECT 1 FROM settings WHERE key='roles_plongeur'").first():
            return
        for pid, roles, comp in c.exec_driver_sql("SELECT id, roles, companion FROM participants").fetchall():
            rs = [r for r in (roles or "").split(",") if r]
            if "plongeur" not in rs and "accompagnant" not in rs:
                rs.insert(0, "accompagnant" if comp else "plongeur")
                c.exec_driver_sql("UPDATE participants SET roles=? WHERE id=?", (",".join(rs), pid))
        c.exec_driver_sql("INSERT INTO settings (key, value) VALUES ('roles_plongeur', '1')")

    @staticmethod
    def _migrate_fixed_adjustments(c):
        """Une seule fois : un ajustement « montant fixé sur une dépense, compensé par les autres » devient
        un montant fixé 🔒 dans la dépense elle-même (même calcul : le reste est réparti entre les autres)."""
        tables = {r[0] for r in c.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"settings", "adjustments", "expense_shares"} <= tables:
            return
        if c.exec_driver_sql("SELECT 1 FROM settings WHERE key='fixed_shares'").first():
            return
        rows = c.exec_driver_sql("SELECT id, participant_id, expense_id, value FROM adjustments "
                                 "WHERE expense_id IS NOT NULL AND fixed = 1 AND compensation = 'others' "
                                 "ORDER BY id").fetchall()
        for aid, pid, eid, value in rows:
            hit = c.exec_driver_sql("UPDATE expense_shares SET fixed=? WHERE expense_id=? AND participant_id=?",
                                    (value, eid, pid))
            if hit.rowcount:
                c.exec_driver_sql("DELETE FROM adjustments WHERE id=?", (aid,))
        # Anciennes dépenses en « montants différents » (le poids de chacun = son montant en centimes) :
        # chaque montant devient un montant fixé 🔒 (mêmes parts, et la somme tombe juste).
        exps = c.exec_driver_sql("SELECT id, COALESCE(actual, planned), COALESCE(refund, 0) FROM expenses").fetchall()
        for eid, total, refund in exps:
            shares = c.exec_driver_sql("SELECT participant_id, weight, fixed FROM expense_shares WHERE expense_id=?",
                                       (eid,)).fetchall()
            ws = [w for _, w, fx in shares if fx is None]
            if (not total or refund or len(ws) != len(shares) or len(set(ws)) < 2
                    or round(sum(ws)) != total or any(w != int(w) for w in ws)):
                continue
            for pid, w, _ in shares:
                c.exec_driver_sql("UPDATE expense_shares SET fixed=?, weight=1 WHERE expense_id=? AND participant_id=?",
                                  (int(w), eid, pid))
        c.exec_driver_sql("INSERT INTO settings (key, value) VALUES ('fixed_shares', '1')")

    # ---------------------------------------------------------- sauvegardes
    def daily_backup(self) -> Path | None:
        if not self.path.exists():
            return None
        target = self.backups_dir / f"compta-{dt.date.today().isoformat()}.db"
        if not target.exists():
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

    def after_replace(self):
        """La base a été remplacée sur le disque (Annuler, version reprise du dossier synchronisé) : on la rouvre
        et on la met à niveau si elle vient d'une version plus ancienne."""
        self.engine.dispose()
        Base.metadata.create_all(self.engine)
        self._migrate()

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
