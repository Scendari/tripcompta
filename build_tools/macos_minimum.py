"""Vérifie la version minimale de macOS exigée par l'application fabriquée (dist/TripCompta.app).

Chaque binaire (exécutable, .so, .dylib, Python) annonce le macOS minimal dont il a besoin (« minos »). Si une
bibliothèque fabriquée pour un macOS plus récent se glisse dans l'app (cas typique : la machine de fabrication passe à
un macOS plus récent et pip y choisit des paquets plus exigeants), l'app ne s'ouvrirait plus sur les Mac plus anciens.
On échoue alors la fabrication. Usage : python build_tools/macos_minimum.py [--max 13.0] [app]"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def minos(path: Path) -> str | None:
    out = subprocess.run(["otool", "-l", str(path)], capture_output=True, text=True).stdout
    lines = out.splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        if s == "cmd LC_BUILD_VERSION":
            for nxt in lines[i + 1:i + 6]:
                if nxt.strip().startswith("minos "):
                    return nxt.split()[1]
        if s == "cmd LC_VERSION_MIN_MACOSX":
            for nxt in lines[i + 1:i + 4]:
                if nxt.strip().startswith("version "):
                    return nxt.split()[1]
    return None


def as_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def binaries(app: Path):
    for p in app.rglob("*"):
        if p.is_file() and not p.is_symlink() and (p.suffix in (".so", ".dylib") or p.parent.name == "MacOS"
                                                   or p.name == "Python"):
            yield p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("app", nargs="?", default="dist/TripCompta.app")
    ap.add_argument("--max", default="13.0", help="macOS minimal le plus récent toléré")
    a = ap.parse_args()
    app = Path(a.app)
    if not app.exists():
        print(f"introuvable : {app}")
        return 2
    limit, seen, bad = as_tuple(a.max), {}, []
    for p in binaries(app):
        v = minos(p)
        if v is None:
            continue
        seen[v] = seen.get(v, 0) + 1
        if as_tuple(v) > limit:
            bad.append((v, p.relative_to(app)))
    print("macOS minimal par binaire :", ", ".join(f"{v} ×{n}" for v, n in sorted(seen.items(), key=lambda x: as_tuple(x[0]))))
    if bad:
        print(f"ÉCHEC : {len(bad)} binaire(s) exigent un macOS plus récent que {a.max} :")
        for v, p in sorted(bad):
            print(f"  {v}  {p}")
        return 1
    print(f"OK : l'application s'ouvre à partir de macOS {max(seen, key=as_tuple) if seen else '?'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
