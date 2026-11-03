"""Quelle version tourne ? Et comparer deux versions.

- Application fabriquée pour une publication (étiquette vX.Y.Z) : build_tools/build.py écrit `compta/_build.py`
  avec la version exacte (« 1.2.0 », « 1.2.0-beta1 ») : c'est une version « publiée », qui peut se mettre à jour.
- Tout le reste (code source, application fabriquée chez soi) : version de développement « X.Y.Z-dev », qui ne
  se met JAMAIS à jour toute seule.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import __version__

_RX = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc|dev)\.?(\d*))?$", re.I)
_RANK = {"dev": 0, "alpha": 1, "beta": 2, "rc": 3, None: 9}


def parse(v: str | None):
    """« 1.2.0-beta1 » → (1, 2, 0, 2, 1). Une version finale passe après ses pré-versions. None si illisible."""
    m = _RX.match((v or "").strip())
    if not m:
        return None
    a, b, c, pre, n = m.groups()
    return int(a), int(b), int(c), _RANK[pre.lower() if pre else None], int(n or 0)


def is_newer(candidate: str | None, current: str | None) -> bool:
    c, cur = parse(candidate), parse(current)
    return bool(c and cur and c > cur)


def base(v: str | None):
    """Seulement X.Y.Z (sert à protéger la base : une 1.1.x a pu changer sa structure, pas une 1.0.0-beta2)."""
    p = parse(v)
    return p[:3] if p else None


def _git_commit() -> str:
    root = Path(__file__).resolve().parent.parent
    if not (root / ".git").exists():
        return ""
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True,
                              timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def build_info() -> dict:
    """{version, commit, dev} de l'application qui tourne."""
    try:
        from . import _build                      # écrit par build_tools/build.py
        info = dict(version=_build.VERSION, commit=_build.COMMIT, dev=bool(_build.DEV))
    except ImportError:
        info = dict(version=f"{__version__}-dev", commit=_git_commit(), dev=True)
    return info


def label(info: dict) -> str:
    """Pour l'affichage : « 1.2.0 » ou « 1.2.0-dev (a3f9c21) »."""
    return info["version"] + (f" ({info['commit']})" if info.get("dev") and info.get("commit") else "")
