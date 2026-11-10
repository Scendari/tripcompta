"""Empreintes signées d'une version (voir compta/signature.py et docs/PUBLIER.md).

    python build_tools/signer_empreintes.py signer DOSSIER 1.6.0
        Calcule l'empreinte de chaque « TripCompta-* » du dossier, écrit SHA256SUMS.txt (avec la version) et
        SHA256SUMS.txt.sig. La clé privée est lue dans la variable TRIPCOMPTA_CLE_SIGNATURE (secret de la
        publication). Refuse si cette clé n'est pas une de celles que l'application accepte : une version signée
        avec une autre clé serait refusée par toutes les applications installées.

    python build_tools/signer_empreintes.py verifier DOSSIER [1.6.0]
        Vérifie la signature et l'empreinte de chaque fichier (ce que fait l'application avant d'installer).

    python build_tools/signer_empreintes.py cle
        Vérifie seulement que la clé de TRIPCOMPTA_CLE_SIGNATURE est bien acceptée par l'application.

    python build_tools/signer_empreintes.py nouvelle-cle FICHIER
        Crée une paire de clés : la clé privée est écrite dans FICHIER (hors du dépôt !), la clé publique est
        affichée, à ajouter dans compta/signature.py. La clé privée n'est jamais affichée.
"""
import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from compta import signature as SIG          # noqa: E402

SUMS, SUMS_SIG, ENV = "SHA256SUMS.txt", "SHA256SUMS.txt.sig", "TRIPCOMPTA_CLE_SIGNATURE"


def stop(msg: str):
    sys.exit(f"ERREUR : {msg}")


def private_key() -> str:
    key = (os.environ.get(ENV) or "").strip()
    if not key:
        stop(f"la clé de signature manque (secret {ENV}). Voir docs/PUBLIER.md.")
    try:
        public = SIG.public_of(key)
    except ValueError as e:
        stop(str(e))
    if public not in SIG.PUBLIC_KEYS:
        stop("cette clé de signature n'est pas celle que l'application accepte (compta/signature.py) : les "
             "applications installées refuseraient la mise à jour.")
    return key


def files(folder: Path) -> list[Path]:
    return sorted(p for p in folder.glob("TripCompta-*") if p.is_file())


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sums_text(folder: Path, version: str) -> str:
    """Format de sha256sum (« <empreinte>  <nom> »), précédé de la version : la signature vaut pour elle seule."""
    lines = [f"{SIG.VERSION_LINE}{version}"] + [f"{digest(p)}  {p.name}" for p in files(folder)]
    return "\n".join(lines) + "\n"


def cmd_sign(folder: Path, version: str):
    key = private_key()
    if not files(folder):
        stop(f"aucun fichier « TripCompta-* » dans {folder}")
    text = sums_text(folder, version)
    (folder / SUMS).write_bytes(text.encode("utf-8"))
    (folder / SUMS_SIG).write_text(SIG.sign(text.encode("utf-8"), key) + "\n", encoding="ascii")
    cmd_verify(folder, version)


def cmd_verify(folder: Path, version: str | None = None):
    try:
        data = (folder / SUMS).read_bytes()
        sig = (folder / SUMS_SIG).read_text(encoding="ascii")
    except OSError as e:
        stop(f"fichier d'empreintes ou signature illisible ({e})")
    if not SIG.verify(data, sig):
        stop("la signature ne correspond pas aux clés de l'application")
    text = data.decode("utf-8")
    signed = SIG.signed_version(text)
    if version and signed != version:
        stop(f"le fichier d'empreintes est signé pour la version {signed}, pas pour {version}")
    from compta.updater import parse_sums
    sums = parse_sums(text)
    for p in files(folder):
        if sums.get(p.name) != digest(p):
            stop(f"l'empreinte de {p.name} ne correspond pas")
    missing = sorted(set(sums) - {p.name for p in files(folder)})
    if missing:
        stop("fichiers annoncés mais absents : " + ", ".join(missing))
    print(f"Empreintes signées : version {signed}, {len(sums)} fichiers, signature valide.")


def cmd_new_key(dest: Path):
    if dest.exists():
        stop(f"{dest} existe déjà : je ne l'écrase pas")
    try:
        dest.resolve().relative_to(ROOT)
        stop("la clé privée ne doit pas être écrite dans le dépôt : choisis un fichier ailleurs")
    except ValueError:
        pass
    private, public = SIG.new_key()
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(private + "\n")
    print(f"Clé privée écrite dans {dest} (à garder secrète, avec une copie en lieu sûr).")
    print("Clé publique, à mettre dans PUBLIC_KEYS (compta/signature.py) :")
    print(public)


def main(argv: list[str]):
    if len(argv) >= 3 and argv[0] == "signer":
        cmd_sign(Path(argv[1]), argv[2])
    elif len(argv) >= 2 and argv[0] == "verifier":
        cmd_verify(Path(argv[1]), argv[2] if len(argv) > 2 else None)
    elif argv[:1] == ["cle"]:
        private_key()
        print("La clé de signature est bien celle que l'application accepte.")
    elif len(argv) == 2 and argv[0] == "nouvelle-cle":
        cmd_new_key(Path(argv[1]).expanduser())
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
