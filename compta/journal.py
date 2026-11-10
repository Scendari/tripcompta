"""Journal de l'application : son carnet de bord (démarrage, erreurs imprévues, sauvegardes), sur Mac comme sur
Windows, dans un fichier à côté des réglages de l'ordinateur. Aucun nom ni montant n'y est écrit : seulement ce que
l'application a fait et ce qui a échoué. Le fichier tourne tout seul (3 fichiers de 300 Ko au plus)."""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from . import paths

NAME = "journal.log"
log = logging.getLogger("tripcompta")


def file() -> Path:
    return paths.config_file().parent / NAME


def setup() -> Path | None:
    """Branche le journal sur son fichier (une seule fois par emplacement). Sans droit d'écriture : pas de journal,
    l'application marche quand même."""
    target = file()
    for h in list(log.handlers):
        if isinstance(h, logging.handlers.RotatingFileHandler):
            if Path(h.baseFilename) == target:
                return target
            log.removeHandler(h)
            h.close()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(target, maxBytes=300_000, backupCount=2, encoding="utf-8", delay=True)
    except OSError:
        return None
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(h)
    log.setLevel(logging.INFO)
    log.propagate = False
    return target


def tail(lines: int = 400) -> str:
    """Les dernières lignes du journal (pour le diagnostic)."""
    try:
        return "\n".join(file().read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""
