"""Mises à jour : savoir s'il y a une nouvelle version, puis l'installer en un clic.

Les versions publiées sont les « Releases » GitHub du dépôt public. Chacune contient un fichier par système
(TripCompta-mac-arm64.zip, TripCompta-mac-intel.zip, TripCompta-windows-setup.exe, TripCompta-linux.tar.gz) et
SHA256SUMS.txt (l'empreinte de chaque fichier, pour vérifier que le téléchargement est intact).

- La vérification se fait au plus une fois par jour, en arrière-plan ; hors ligne ou en cas d'erreur : rien.
- Une version de développement ne se met jamais à jour toute seule.
- L'installation : sauvegarde de la base, téléchargement, contrôle de l'empreinte, puis
  - Mac, Linux : un petit script (remplacer.sh) attend que l'application soit fermée, la remplace, la relance et
    vérifie qu'elle répond ; sinon il remet l'ancienne ;
  - Windows : l'installeur, en silencieux (build_tools/windows/TripCompta.iss), attend que l'application soit
    fermée, installe, relance ; s'il échoue, il remet tout en place et relance l'ancienne.
  Les données (dossier TripCompta) ne sont jamais touchées.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from . import paths as PATHS
from . import version as V

REPO = "Scendari/tripcompta"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"     # ignore les pré-versions
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
INSTALL_HELP = f"https://github.com/{REPO}/blob/main/INSTALLER.md"
CHECK_EVERY = 24 * 3600
SUMS = "SHA256SUMS.txt"
PENDING = "en-cours.txt"
APP_NAME = "TripCompta"                  # nom des fichiers : TripCompta.app, TripCompta.exe
OLD_APP_NAME = "Trip Compta"             # jusqu'à la 1.1 (installations faites avant le renommage)
SCRIPTS = Path(__file__).parent / "scripts"


class UpdateError(Exception):
    """Message lisible par l'utilisateur."""


# ---------------------------------------------------------------- quel fichier pour cet ordinateur ?

def asset_name(system: str | None = None, machine: str | None = None) -> str | None:
    system = system or sys.platform
    machine = (machine or platform.machine() or "").lower()
    if system == "darwin":
        return "TripCompta-mac-arm64.zip" if machine in ("arm64", "aarch64") else "TripCompta-mac-intel.zip"
    if system == "win32":
        return "TripCompta-windows-setup.exe" if machine in ("amd64", "x86_64", "") else None
    if system.startswith("linux"):
        return "TripCompta-linux.tar.gz" if machine in ("x86_64", "amd64") else None
    return None


def release_info(data: dict) -> dict:
    """Réponse de l'API GitHub → ce qui nous sert."""
    return dict(version=(data.get("tag_name") or "").lstrip("v"), notes=(data.get("body") or "").strip(),
                page=data.get("html_url") or RELEASES_PAGE, date=(data.get("published_at") or "")[:10],
                assets={a["name"]: a["browser_download_url"] for a in data.get("assets") or []
                        if a.get("name") and a.get("browser_download_url")})


def _get(url: str, timeout: float = 10):
    req = urllib.request.Request(url, headers={"User-Agent": "TripCompta-maj", "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_latest() -> dict:
    # TRIPCOMPTA_MAJ_API : autre adresse, pour les essais de bout en bout (faux GitHub local)
    with _get(os.environ.get("TRIPCOMPTA_MAJ_API") or API_LATEST, timeout=8) as r:
        return release_info(json.load(r))


# ---------------------------------------------------------------- vérification

class Updater:
    """État des mises à jour, mémorisé dans reglages.json (propre à l'ordinateur) :
    maj_auto (vérifier tout seul, oui par défaut), maj_verifiee (heure de la dernière vérification),
    maj_derniere (la dernière version publiée vue), maj_plus_tard (version dont on a masqué le bandeau)."""

    def __init__(self, info: dict | None = None, fetch=None, download=None, now=time.time):
        info = info or V.build_info()
        self.version, self.commit, self.dev = info["version"], info.get("commit") or "", bool(info.get("dev"))
        self._fetch = fetch or fetch_latest
        self._download = download or download_file
        self._now = now
        self._lock = threading.Lock()
        self.job: dict | None = None             # installation en cours : {step, percent, error}

    # ----- réglages
    @staticmethod
    def _conf() -> dict:
        return PATHS.read_config()

    @property
    def auto(self) -> bool:
        return self._conf().get("maj_auto", True) is not False

    def set_auto(self, on: bool):
        PATHS.write_config(maj_auto=bool(on))

    @property
    def label(self) -> str:
        return V.label(dict(version=self.version, commit=self.commit, dev=self.dev))

    def latest(self) -> dict | None:
        v = self._conf().get("maj_derniere")
        return v if isinstance(v, dict) and v.get("version") else None

    def checked_at(self) -> float | None:
        v = self._conf().get("maj_verifiee")
        return float(v) if isinstance(v, (int, float)) else None

    # ----- vérifier
    def due(self) -> bool:
        if self.dev or not self.auto or os.environ.get("TRIPCOMPTA_NO_UPDATE_CHECK"):
            return False
        last = self.checked_at()
        return last is None or self._now() - last >= CHECK_EVERY or last > self._now()

    def check(self, force: bool = False) -> dict:
        """Renvoie {ok, latest, error}. Sans « force », ne fait rien si ce n'est pas le moment ; ne lève jamais."""
        if not force and not self.due():
            return dict(ok=True, latest=self.latest(), error=None, skipped=True)
        with self._lock:
            try:
                info = self._fetch()
                if not V.parse(info.get("version")):
                    raise ValueError("version illisible")
            except Exception:
                return dict(ok=False, latest=self.latest(), error="Impossible de joindre le site des mises à jour "
                                                                   "(pas d'internet ?). Réessaie plus tard.")
            PATHS.write_config(maj_derniere=info, maj_verifiee=self._now())
            return dict(ok=True, latest=info, error=None)

    def available(self) -> dict | None:
        """La version publiée la plus récente, si elle est plus récente que celle-ci (jamais pour une version
        de développement)."""
        latest = self.latest()
        if self.dev or not latest or not V.is_newer(latest["version"], self.version):
            return None
        return latest

    def banner(self) -> dict | None:
        a = self.available()
        if a and self._conf().get("maj_plus_tard") != a["version"]:
            return a
        return None

    def later(self, version: str):
        PATHS.write_config(maj_plus_tard=version)

    def start_background(self, interval: float = 3600):
        """Vérifie au démarrage puis toutes les heures s'il s'est passé un jour (l'appli peut rester ouverte)."""
        if self.dev or os.environ.get("TRIPCOMPTA_NO_UPDATE_CHECK"):
            return None

        def loop():
            time.sleep(5)                         # laisser l'application démarrer
            while True:
                self.check()
                time.sleep(interval)
        th = threading.Thread(target=loop, daemon=True, name="maj-verif")
        th.start()
        return th

    # ----- installer
    def installable(self) -> tuple[bool, str]:
        """(possible ?, raison sinon)."""
        if self.dev:
            return False, "C'est une version de développement : elle se met à jour avec le code source."
        if asset_name() is None:
            return False, "Pas de version toute prête pour cet ordinateur."
        loc = install_location()
        if loc is None:
            return False, "L'application ne tourne pas depuis une version installée."
        target, _ = loc
        if (asset_name() or "").endswith(".exe"):
            return True, ""                      # Windows : l'installeur installe pour l'utilisateur (sans droits admin)
        if "/AppTranslocation/" in target.as_posix():  # Mac : app ouverte depuis Téléchargements, isolée par macOS
            return False, ("Trip Compta tourne depuis le dossier où tu l'as téléchargée : macOS l'empêche de se "
                           "remplacer. Glisse-la dans le dossier Applications, puis relance-la.")
        if not os.access(target.parent, os.W_OK):
            return False, f"Trip Compta n'a pas le droit de se remplacer dans « {target.parent} »."
        return True, ""

    def run_install(self, db, port: int, app_args: list[str], quit_app, workdir: Path | None = None,
                    location=None, launch=None):
        """Fait toute l'installation (dans un fil à part). self.job suit l'avancement pour la page d'attente."""
        info = self.available()
        self.job = dict(step="sauvegarde", percent=0, error=None, version=info["version"] if info else "")
        try:
            if not info:
                raise UpdateError("Pas de nouvelle version à installer.")
            name = asset_name()
            if not name or name not in info["assets"] or SUMS not in info["assets"]:
                raise UpdateError("Cette version n'a pas de fichier pour cet ordinateur.")
            loc = location or install_location()
            if loc is None:
                raise UpdateError("L'application ne tourne pas depuis une version installée.")
            target, exe_rel = loc
            work = workdir or work_dir()
            if work.exists():
                shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True, exist_ok=True)
            backup_database(db, info["version"])
            self.job["step"] = "telechargement"
            archive = work / name
            sums = parse_sums(self._read_text(info["assets"][SUMS]))
            got = self._download(info["assets"][name], archive,
                                 progress=lambda p: self.job.__setitem__("percent", p))
            self.job.update(step="verification", percent=100)
            if sums.get(name) != got:
                raise UpdateError("Le fichier téléchargé est abîmé (son empreinte ne correspond pas). "
                                  "Rien n'a été changé : réessaie plus tard.")
            if name.endswith(".exe"):            # Windows : l'installeur fait le reste (et relance)
                self.job["step"] = "redemarrage"
                (work / PENDING).write_text(info["version"], encoding="utf-8")
                write_relaunch(work, port, app_args)
                (launch or launch_installer)(installer=archive, work=work, current=target / exe_rel)
                quit_app()
                return
            self.job["step"] = "preparation"
            out = work / "nouvelle"
            extract(archive, out)
            new = find_app(out, target)
            exe_rel = new_exe_rel(new, exe_rel)
            archive.unlink(missing_ok=True)
            self.job["step"] = "redemarrage"
            (work / PENDING).write_text(info["version"], encoding="utf-8")   # la nouvelle version s'y reconnaîtra
            (launch or launch_script)(target=target, new=new, exe_rel=exe_rel, port=port,
                                      version=info["version"], work=work, app_args=app_args)
        except UpdateError as e:
            self.job.update(step="erreur", error=str(e))
            return
        except Exception as e:                    # réseau, disque plein… : l'ancienne version reste en place
            self.job.update(step="erreur", error=f"La mise à jour n'a pas pu se faire ({e}). Rien n'a été changé.")
            return
        quit_app()

    def _read_text(self, url: str) -> str:
        try:
            with _get(url, timeout=15) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            raise UpdateError(f"Impossible de télécharger la liste des empreintes ({e}). Rien n'a été changé.")

    # ----- résultat d'une installation précédente
    def startup_notice(self, work: Path | None = None) -> str | None:
        """Au démarrage : si on est la version qu'une mise à jour vient d'installer, on le dit (une fois)."""
        f = (work or work_dir()) / PENDING
        try:
            if f.exists() and f.read_text(encoding="utf-8").strip() == self.version:
                f.unlink()
                return f"✓ Trip Compta est à jour : version {self.version}."
        except OSError:
            pass
        return None

    def failure_notice(self, work: Path | None = None) -> str | None:
        """Le script a dû remettre l'ancienne version (il l'écrit après l'avoir relancée) : message, une fois."""
        work = work or work_dir()
        f = work / "echec.txt"
        if not f.exists():
            return None
        try:
            msg = f.read_text(encoding="utf-8-sig", errors="replace").strip()
            f.unlink()
            (work / PENDING).unlink(missing_ok=True)
        except OSError:
            return None
        return f"⚠️ {msg}" if msg else None


# ---------------------------------------------------------------- les étapes

def work_dir() -> Path:
    """Dossier de travail des mises à jour, à côté des réglages de l'ordinateur (jamais dans les données)."""
    return PATHS.config_file().parent / "maj"


def backup_database(db, new_version: str) -> Path | None:
    if db is None or not db.path.exists():       # None : données refusées (trop récentes), on n'y a pas touché
        return None
    safe = "".join(ch for ch in new_version if ch.isalnum() or ch in ".-")
    dst = db.backups_dir / f"avant-maj-{safe}.db"
    db._copy(db.path, dst)
    return dst


def download_file(url: str, dest: Path, progress=None) -> str:
    """Télécharge et renvoie l'empreinte SHA-256."""
    h = hashlib.sha256()
    with _get(url, timeout=30) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if progress and total:
                progress(min(99, done * 100 // total))
    return h.hexdigest()


def parse_sums(text: str) -> dict:
    """Format de sha256sum : « <empreinte>  <nom> » (un « * » devant le nom en mode binaire)."""
    out = {}
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and len(parts[0]) == 64:
            out[parts[1].lstrip("*").strip()] = parts[0].lower()
    return out


def extract(archive: Path, dest: Path):
    dest.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if name.endswith(".zip") and sys.platform == "darwin":
        # ditto garde ce qu'une app Mac exige (liens symboliques, droits, signature)
        subprocess.run(["ditto", "-x", "-k", str(archive), str(dest)], check=True, capture_output=True)
    elif name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for m in z.infolist():
                p = (dest / m.filename).resolve()
                if not str(p).startswith(str(dest.resolve())):
                    raise UpdateError("Fichier de mise à jour invalide.")
            z.extractall(dest)
    elif name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive) as t:
            t.extractall(dest, filter="data")
    else:
        raise UpdateError("Format de fichier inconnu.")


def find_app(folder: Path, target: Path) -> Path:
    """La nouvelle application dans l'archive dépliée : « TripCompta.app » ou le dossier « TripCompta » (ou l'ancien
    nom « Trip Compta », avant la 1.2)."""
    for cand in (folder / target.name, folder / (APP_NAME + ".app"), folder / APP_NAME,
                 folder / (OLD_APP_NAME + ".app"), folder / OLD_APP_NAME):
        if cand.exists():
            return cand
    raise UpdateError("Le fichier téléchargé ne contient pas l'application.")


def new_exe_rel(new: Path, exe_rel: str) -> str:
    """Le programme à relancer, dans la NOUVELLE application (son nom a pu changer : « Trip Compta » → « TripCompta »)."""
    if (new / exe_rel).exists():
        return exe_rel
    plist = new / "Contents" / "Info.plist"
    if plist.exists():
        import plistlib
        try:
            exe = plistlib.loads(plist.read_bytes()).get("CFBundleExecutable")
        except Exception:
            exe = None
        if exe and (new / "Contents" / "MacOS" / exe).exists():
            return str(Path("Contents") / "MacOS" / exe)
    suffix = Path(exe_rel).suffix
    for name in (APP_NAME + suffix, OLD_APP_NAME + suffix):
        if (new / name).exists():
            return name
    return exe_rel


def install_location(executable: str | None = None, system: str | None = None, frozen: bool | None = None):
    """(dossier ou .app à remplacer, chemin du programme dedans) ; None si on tourne depuis le code source."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if not frozen:
        return None
    exe = Path(executable or sys.executable).resolve()
    system = system or sys.platform
    if system == "darwin":
        app = exe.parents[2] if len(exe.parents) > 2 else None
        if app is None or app.suffix != ".app":
            return None
        return app, str(exe.relative_to(app))
    return exe.parent, exe.name


def clean_args(argv: list[str]) -> list[str]:
    """Les options de lancement à garder pour la relance (le port et --no-browser sont redonnés par le script)."""
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
            continue
        if a == "--port":
            skip = True
            continue
        if a.startswith("--port=") or a == "--no-browser" or a.startswith("-psn_"):
            continue
        out.append(a)
    return out


RELAUNCH = "relance.json"


def write_relaunch(work: Path, port: int, app_args: list[str]) -> None:
    """Pour l'installeur Windows : comment relancer (lu par « TripCompta.exe --apres-maj », voir relaunch_args)."""
    (work / RELAUNCH).write_text(json.dumps({"port": port, "args": app_args}), encoding="utf-8")


def relaunch_args(work: Path | None = None) -> list[str]:
    """« --apres-maj » (relancé par l'installeur) : les options d'avant la mise à jour, à la même adresse, sans
    rouvrir d'onglet (la page ouverte se recharge seule)."""
    f = (work or work_dir()) / RELAUNCH
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        f.unlink()
    except (OSError, ValueError):
        return ["--no-browser"]
    args = [str(a) for a in data.get("args") or []]
    if data.get("port"):
        args += ["--port", str(int(data["port"]))]
    return args + ["--no-browser"]


def installer_command(installer: Path, work: Path, current: Path, pid: int) -> list[str]:
    """L'installeur reprend de lui-même le dossier de l'installation précédente."""
    return [str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-",
            f"/LOG={work / 'installation.log'}", f"/ATTENDRE={pid}", f"/ECHEC={work / 'echec.txt'}",
            f"/ANCIEN={current}"]


def launch_installer(installer: Path, work: Path, current: Path, pid: int | None = None):
    """Windows : lance l'installeur en silencieux, détaché (il attend que Trip Compta soit fermé)."""
    flags = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
    return subprocess.Popen(installer_command(installer, work, current, pid or os.getpid()),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=flags, close_fds=True)


def launch_script(target: Path, new: Path, exe_rel: str, port: int, version: str, work: Path, app_args: list[str],
                  pid: int | None = None):
    """Mac, Linux : lance le script de remplacement, détaché : il survit à la fermeture de l'application."""
    pid = pid or os.getpid()
    log = open(work / "script.log", "ab")
    script = work / "remplacer.sh"
    shutil.copy2(SCRIPTS / "remplacer.sh", script)
    cmd = ["/bin/sh", str(script), str(pid), str(target), str(new), exe_rel, str(port), version, str(work)] + app_args
    return subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True,
                            close_fds=True)
