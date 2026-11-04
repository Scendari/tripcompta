"""Où sont les données de Trip Compta ?

Ordre de priorité :
  1. l'option `--data-dir` au lancement (développement, tests) ;
  2. la variable d'environnement TRIPCOMPTA_DATA_DIR ;
  3. le dossier choisi dans l'application (« Où sont mes voyages », voir cloud.py), mémorisé dans un petit fichier de réglages
     propre à l'ordinateur (il ne peut pas être rangé dans le dossier des données lui-même) ;
  4. par défaut : <Documents>/TripCompta, où <Documents> est le VRAI dossier Documents (sous Windows avec OneDrive,
     c'est souvent C:\\Users\\<nom>\\OneDrive\\Documents, et pas C:\\Users\\<nom>\\Documents).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP_NAME = "Trip Compta"
FOLDER = "TripCompta"
CLOUD_HINTS = ("dropbox", "onedrive", "icloud", "mobile documents", "google drive", "googledrive", "pcloud", "box sync")


def documents_dir() -> Path:
    """Le dossier Documents de l'utilisateur, tel que le système le connaît (TRIPCOMPTA_DOCUMENTS : tests)."""
    if os.environ.get("TRIPCOMPTA_DOCUMENTS"):
        return Path(os.environ["TRIPCOMPTA_DOCUMENTS"])
    if sys.platform == "win32":
        try:                                   # FOLDERID_Documents : suit la redirection OneDrive
            import ctypes
            from ctypes import wintypes
            from uuid import UUID

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                            ("Data4", ctypes.c_ubyte * 8)]
            u = UUID("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}")
            g = GUID(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8)(*u.bytes[8:]))
            p = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(p)) == 0:
                path = Path(p.value)
                ctypes.windll.ole32.CoTaskMemFree(p)
                if path.exists():
                    return path
        except Exception:
            pass
    docs = Path.home() / "Documents"
    return docs if docs.exists() else Path.home()


def config_file() -> Path:
    """Petit fichier de réglages propre à cet ordinateur (hors du dossier des données)."""
    if os.environ.get("TRIPCOMPTA_CONFIG"):
        return Path(os.environ["TRIPCOMPTA_CONFIG"])
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / FOLDER / "reglages.json"


def read_config() -> dict:
    try:
        return json.loads(config_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_config(**values) -> None:
    f = config_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    data = read_config()
    data.update(values)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, f)


def chosen_dir() -> Path | None:
    """Dossier choisi dans l'application (ou par variable d'environnement), s'il y en a un."""
    env = os.environ.get("TRIPCOMPTA_DATA_DIR")
    if env:
        return Path(env).expanduser()
    v = read_config().get("data_dir")
    return Path(v).expanduser() if v else None


def default_data_dir() -> Path:
    chosen = chosen_dir()
    if chosen:
        return chosen
    return documents_dir() / FOLDER


def looks_synced(path: Path) -> bool:
    """Dossier synchronisé dans le cloud ? (la base peut s'abîmer si deux ordinateurs l'ouvrent en même temps)"""
    low = str(path).lower()
    return any(h in low for h in CLOUD_HINTS)
