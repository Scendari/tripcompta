"""« Exporter un diagnostic » : un petit fichier texte à envoyer à la personne qui aide, quand quelque chose ne va pas.

Il décrit l'application, l'ordinateur et l'état des fichiers, avec des NOMBRES seulement. Il ne contient ni nom de
voyage ou de participant, ni montant, ni texte saisi ; les chemins de dossiers sont raccourcis (« ~ » à la place du
dossier personnel, donc sans le nom de la session) et le journal est nettoyé de la même façon avant d'être joint.
"""
from __future__ import annotations

import datetime as dt
import getpass
import os
import platform
import re
import shutil
import sqlite3
import sys
from pathlib import Path

from . import journal
from . import paths

CLOUDS = (("Google Drive", ("google drive", "googledrive", "mon drive", "my drive")),
          ("OneDrive", ("onedrive",)), ("iCloud", ("icloud", "mobile documents")), ("Dropbox", ("dropbox",)),
          ("kDrive", ("kdrive",)), ("Nextcloud", ("nextcloud",)), ("pCloud", ("pcloud",)))
JOURNAL_LINES = 400
# tables dont on donne le nombre de lignes (jamais le contenu)
COUNTED = (("trips", "voyages"), ("participants", "participants"), ("families", "familles"),
           ("expenses", "dépenses"), ("movements", "versements et remboursements"), ("receipts", "justificatifs"),
           ("subaccounts", "sous-comptes « Entre nous »"), ("withdrawals", "désistements"))


def _user_names() -> list[str]:
    names = set()
    try:
        names.add(getpass.getuser())
    except Exception:
        pass
    names.add(Path.home().name)
    return sorted((n for n in names if n and len(n) >= 3), key=len, reverse=True)


def anonymize(text: str, home: Path | None = None, users: list[str] | None = None) -> str:
    """Retire d'un texte ce qui désigne la personne : dossier personnel → « ~ », nom de session → « (session) »,
    valeurs jointes aux erreurs de la base (elles peuvent contenir un nom ou un montant) → retirées."""
    text = str(text)
    home = Path.home() if home is None else home
    for h in {str(home), str(home).replace("\\", "/"), os.path.realpath(home)}:
        if h and h not in ("/", "\\"):
            text = text.replace(h, "~")
    for name in (_user_names() if users is None else users):
        text = re.sub(rf"(?<![\w]){re.escape(name)}(?![\w])", "(session)", text, flags=re.IGNORECASE)
    text = re.sub(r"\[parameters: .*?\](?=\s|$)", "[valeurs retirées]", text, flags=re.S)
    text = re.sub(r"\[SQL: .*?\](?=\s|$)", "[requête retirée]", text, flags=re.S)
    return text


ERROR_LINE = re.compile(r"^([\s|+-]*(?:[\w.]+\.)?\w*(?:Error|Exception|Warning|Exit|Interrupt)\w*)(: .*)$")


def clean_journal(text: str) -> str:
    """Le journal, sans ce qu'un message d'erreur a pu recopier d'une saisie : sur les lignes « TypeErreur : … »,
    ce qui est entre guillemets devient « … » et les nombres deviennent « # » (le type de l'erreur, l'endroit du
    code et le reste du message restent lisibles)."""
    out = []
    for line in str(text).splitlines():
        m = ERROR_LINE.match(line)
        if m:
            msg = re.sub(r"'[^']*'|\"[^\"]*\"|«[^»]*»", "'…'", m.group(2))
            msg = re.sub(r"\d+[.,]\d+|\d{3,}", "#", msg)
            line = m.group(1) + msg
        out.append(line)
    return "\n".join(out)


def cloud_hint(folder: Path | str) -> str | None:
    """Le dossier a l'air d'être dans un service de synchronisation ? (d'après son chemin seulement)"""
    low = str(folder).lower()
    return next((name for name, marks in CLOUDS if any(m in low for m in marks)), None)


def _size(n: int | None) -> str:
    if n is None:
        return "inconnue"
    for unit, k in (("Go", 1024 ** 3), ("Mo", 1024 ** 2), ("ko", 1024)):
        if n >= k:
            return f"{n / k:.1f} {unit}".replace(".", ",")
    return f"{n} octets"


def _free(folder: Path) -> str:
    p = Path(folder)
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return _size(shutil.disk_usage(p).free)
    except OSError:
        return "inconnue"


def _file_state(path: Path) -> list[str]:
    """État du fichier des voyages, lu sans rien modifier (jamais de contenu : structure et nombres)."""
    out = []
    try:
        st = path.stat()
    except OSError:
        return ["fichier des voyages : absent"]
    out.append(f"fichier des voyages : présent, {_size(st.st_size)}, modifié le "
               f"{dt.datetime.fromtimestamp(st.st_mtime):%d/%m/%Y à %H:%M}")
    for side in ("-journal", "-wal", "-shm"):
        if Path(f"{path}{side}").exists():
            out.append(f"fichier d'accompagnement présent : compta.db{side}")
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error as e:
        return out + [f"ouverture impossible : {type(e).__name__}"]
    try:
        try:
            check = con.execute("PRAGMA quick_check").fetchone()[0]
            out.append("vérification du fichier : " + ("intact" if check == "ok" else "ABÎMÉ"))
        except sqlite3.Error as e:
            out.append(f"vérification du fichier : impossible ({type(e).__name__})")
            return out
        try:
            out.append(f"numéro de structure : {con.execute('PRAGMA user_version').fetchone()[0]}")
            row = con.execute("SELECT value FROM settings WHERE key='app_version'").fetchone()
            out.append(f"enregistré en dernier par Trip Compta {row[0] if row else 'inconnue'}")
        except sqlite3.Error:
            pass
        for table, label in COUNTED:
            try:
                out.append(f"{label} : {con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]}")
            except sqlite3.Error:
                out.append(f"{label} : table illisible")
        try:
            rows = con.execute("SELECT t.id, (SELECT COUNT(*) FROM participants p WHERE p.trip_id=t.id), "
                               "(SELECT COUNT(*) FROM expenses e WHERE e.trip_id=t.id), "
                               "(SELECT COUNT(*) FROM receipts r WHERE r.trip_id=t.id) FROM trips t ORDER BY t.id")
            for tid, people, expenses, receipts in rows:
                out.append(f"  voyage n° {tid} : {people} participants, {expenses} dépenses, {receipts} justificatifs")
        except sqlite3.Error:
            pass
    finally:
        con.close()
    return out


def _folder_count(folder: Path, pattern: str = "*") -> tuple[int, int]:
    n = size = 0
    try:
        for p in folder.glob(pattern):
            if p.is_file() and not p.name.startswith("."):
                n += 1
                size += p.stat().st_size
    except OSError:
        pass
    return n, size


def build(*, version: str, folder: Path | str, db=None, source: str | None = None, protected: bool = False,
          locked: bool = False, problem: str | None = None, extra: list[str] | None = None,
          now: dt.datetime | None = None) -> str:
    """Le texte du diagnostic. `db` : la base ouverte (None si elle n'a pas pu l'être : `problem` dit pourquoi)."""
    now = now or dt.datetime.now()
    folder = Path(folder)
    L = ["DIAGNOSTIC TRIP COMPTA", f"fait le {now:%d/%m/%Y à %H:%M}", "",
         "Ce fichier ne contient ni nom, ni montant, ni texte que tu as saisi : seulement des nombres et l'état",
         "de l'application. Tu peux le lire avant de l'envoyer à la personne qui t'aide.", ""]

    L += ["== Application =="]
    L.append(f"Trip Compta {version}")
    L.append("application installée" if getattr(sys, "frozen", False) else "lancée depuis le code source")
    L.append(f"système : {platform.system()} {platform.release()} ({platform.machine()})")
    if sys.platform == "darwin":
        L.append(f"macOS {platform.mac_ver()[0]}")
    L.append(f"Python {'.'.join(map(str, sys.version_info[:3]))}, SQLite {sqlite3.sqlite_version}")
    L.append(f"emplacement de l'application : {Path(sys.executable).parent if getattr(sys, 'frozen', False) else 'code source'}")
    from . import signature
    L.append("vérification de la signature des mises à jour : " + ("prête" if signature.self_test() else "INDISPONIBLE"))
    if problem:
        L += ["", "== Problème à l'ouverture ==", problem]

    L += ["", "== Dossier des voyages =="]
    L.append(f"dossier : {folder}")
    if source:
        L.append("choisi : " + {"defaut": "dossier par défaut (Documents)", "choisi": "dans les réglages",
                                "env": "imposé par l'ordinateur (variable d'environnement)",
                                "lancement": "imposé au lancement"}.get(source, source))
    cloud = cloud_hint(folder)
    L.append(f"dossier synchronisé : {'probablement ' + cloud if cloud else 'pas repéré'}")
    L.append("protégé par un mot de passe : " + ("oui" + (" (verrouillé en ce moment)" if locked else "") if protected
                                                  else "non"))
    L.append(f"place libre sur ce disque : {_free(folder)}")
    if protected:
        n, size = _folder_count(folder / "justificatifs")
        L.append(f"justificatifs chiffrés dans le dossier : {n} fichiers, {_size(size)}")
        from . import crypto
        for name, label in ((crypto.DB_FILE, "fichier des voyages chiffré"), (crypto.KEY_FILE, "fichier de clé")):
            L.append(f"{label} : " + ("présent" if (folder / name).exists() else "ABSENT"))
    if db is not None and not locked:
        L += _file_state(db.path)
        n, size = _folder_count(db.receipts_dir)
        L.append(f"fichiers de justificatifs : {n}, {_size(size)}")
        try:
            con = sqlite3.connect(f"file:{db.path}?mode=ro", uri=True, timeout=2)
            names = [r[0] for r in con.execute("SELECT stored_name FROM receipts")]
            con.close()
            L.append(f"justificatifs dont le fichier manque : {sum(1 for x in names if not (db.receipts_dir / x).exists())}")
        except sqlite3.Error:
            pass
        L += ["", "== Sauvegardes et historique =="]
        L.append("sauvegardes rangées à part, sur l'ordinateur : " + ("oui" if db.complete_backups else "non"))
        try:
            saved = db.backups()
        except Exception as e:
            saved = []
            L.append(f"liste des sauvegardes illisible : {type(e).__name__}")
        L.append(f"sauvegardes gardées : {len(saved)}")
        for b in saved[:12]:
            L.append(f"  {b['when']:%d/%m/%Y %H:%M} · {b['kind']} · "
                     + (f"{len(b['trips'])} voyages, {b['expenses']} dépenses, {b['receipts']} justificatifs"
                        if b["readable"] else "ILLISIBLE")
                     + (f", {b['missing_receipts']} justificatifs introuvables" if b["missing_receipts"] else ""))
        n, size = _folder_count(db.backups_dir / "justificatifs")
        if db.complete_backups:
            L.append(f"justificatifs dans les sauvegardes : {n} fichiers, {_size(size)}")
        L.append(f"actions que « Défaire » peut encore annuler : {len(list(db.undo_dir.glob('*.db')))}")
        if db.recovered:
            L.append("au démarrage : fichier abîmé, dernière copie intacte reprise")
    elif db is None:
        L += _file_state(folder / "compta.db")

    L += ["", "== Réglages de l'ordinateur =="]
    conf = paths.read_config()
    L.append(f"dossier des réglages : {paths.config_file().parent}")
    L.append("réglages présents : " + (", ".join(sorted(conf)) if conf else "aucun"))
    L += list(extra or [])

    L += ["", f"== Journal de l'application ({JOURNAL_LINES} dernières lignes) =="]
    tail = clean_journal(journal.tail(JOURNAL_LINES))
    L.append(tail if tail.strip() else "(vide)")
    return anonymize("\n".join(L)) + "\n"


def file_name(now: dt.datetime | None = None) -> str:
    return f"diagnostic-trip-compta-{now or dt.datetime.now():%Y%m%d-%H%M}.txt"
