"""Le dossier des voyages : fichiers et outils sans état (pas de synchronisation : Trip Compta lit et écrit
directement son dossier). Si ce dossier est dans Google Drive, Dropbox, OneDrive ou iCloud, c'est « à tes risques » :
un seul ordinateur à la fois. Garde-fous : copies en conflit laissées par le service de cloud (détectées, exportables),
fichier pas encore téléchargé (jamais pris pour une base abîmée), « Justificatifs à trier » déposés depuis un téléphone.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

DB = "compta.db"
RECEIPTS = "justificatifs"
INBOX = "Justificatifs à trier"
INBOX_DONE = "Rangés"
INBOX_TYPES = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".pdf"}

# copies en conflit créées par les services : « compta (conflicted copy 2026-…).db », « compta (copie en
# conflit de …).db », « compta-PC-DE-PAUL.db » (OneDrive), « compta (1).db » (Google Drive), « compta 2.db » (iCloud)…
CONFLICT_RE = re.compile(r"^compta(?!\.db$).*\.db$", re.I)
CONFLICT_RE_PROTECTED = re.compile(r"^voyages(?!\.chiffre$).*\.chiffre$", re.I)   # dossier protégé (crypto.py)

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
    """Nom lisible de cet ordinateur (« MacBook Pro de Paul », « PC-BUREAU »). TRIPCOMPTA_ORDINATEUR : essais à deux
    « ordinateurs » sur une même machine."""
    if os.environ.get("TRIPCOMPTA_ORDINATEUR"):
        return os.environ["TRIPCOMPTA_ORDINATEUR"]
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


def same_content(a: Path, b: Path) -> bool:
    """Les deux bases contiennent-elles les mêmes voyages ? (contenu, pas octets : une base réécrite à l'identique,
    ou seulement marquée par la version de l'application qui l'a ouverte, n'est pas une « modification »)"""
    def rows(p):
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            return {line for line in con.iterdump() if "'app_version'" not in line}
        finally:
            con.close()
    try:
        return rows(a) == rows(b)
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


def inbox_file(shared: Path, name: str) -> Path | None:
    """Un fichier de « Justificatifs à trier » (nom contrôlé : pas de sous-dossier ni de « .. »)."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    p = Path(shared) / INBOX / name
    return p if p.is_file() else None


def _unlink(p: Path, tries: int = 8) -> None:
    """Supprime un fichier, en réessayant un peu si Windows le bloque un instant (antivirus, indexation…)."""
    for i in range(tries):
        try:
            p.unlink(missing_ok=True)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.25 * (i + 1))


def conflicts(folder: Path, protected: bool = False, readable=None) -> list[dict]:
    """Copies en conflit laissées par le service de cloud, à côté de la base (« compta (1).db »…).
    readable(chemin) → bool : la copie est-elle lisible (par défaut : base SQLite intacte)."""
    folder = Path(folder)
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    pattern = CONFLICT_RE_PROTECTED if protected else CONFLICT_RE
    main = "voyages.chiffre" if protected else DB
    for n in sorted(names):
        if n.startswith(".") or n == main or not pattern.match(n):
            continue
        p = folder / n
        try:
            st = p.stat()
        except OSError:
            continue
        out.append(dict(name=n, size=st.st_size, date=dt.datetime.fromtimestamp(st.st_mtime),
                        valid=(readable or is_valid_db)(p)))
    return out


def inbox(folder: Path) -> list[dict]:
    """Photos et PDF déposés dans « Justificatifs à trier » (depuis un téléphone, par exemple)."""
    box = Path(folder) / INBOX
    out = []
    try:
        entries = sorted(box.iterdir(), key=lambda p: p.name.lower())
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
    if platform == "win32":                    # Google Drive pour ordinateur : lecteur G: par défaut, mais E:, F:… si pris
        for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
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
                        or (target / f".{DB}.icloud").exists() or (target / "cle.tripcompta").exists()))
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
                               text=True, encoding="utf-8", timeout=600,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))   # pas de console bleue
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
