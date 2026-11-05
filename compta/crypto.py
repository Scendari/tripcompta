"""Protection des voyages par un mot de passe (option) : la base et les justificatifs sont chiffrés.

- Une **clé des voyages** (aléatoire, 256 bits) chiffre les données en AES-256-GCM : un fichier abîmé ou modifié
  est détecté (l'étiquette d'authentification ne correspond plus), jamais lu de travers.
- Elle est enfermée deux fois dans `cle.tripcompta` : par le mot de passe et par une **clé de secours** (chacun passe
  par scrypt, avec son propre sel). Changer de mot de passe ne rechiffre donc rien.
- Chaque fichier chiffré : `TCC1` + 12 octets d'aléa + données chiffrées ; il est lié à son rôle (« base »,
  « justificatif:<nom> »…) : on ne peut pas faire passer un fichier pour un autre.
- Le mot de passe n'est jamais enregistré. « Se souvenir sur cet ordinateur » range la clé des voyages dans le
  trousseau du système (Trousseau du Mac, Gestionnaire d'identification Windows, Secret Service sous Linux).
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import uuid
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

KEY_FILE = "cle.tripcompta"
DB_FILE = "voyages.chiffre"
SUFFIX = ".chiffre"
README = "LISEZMOI.txt"
MAGIC = b"TCC1"
SCRYPT_N = 2 ** 15          # ~32 Mo de mémoire, ~0,1 s : lent pour qui essaie des millions de mots de passe
KEYRING_SERVICE = "Trip Compta"
MIN_PASSWORD = 8

README_TEXT = """Ces fichiers sont les voyages de Trip Compta, chiffrés (protégés par un mot de passe).

Pour les ouvrir : l'application Trip Compta, et le mot de passe (ou la clé de secours).
Sans l'un ni l'autre, personne ne peut les lire, même pas l'auteur de Trip Compta.

Ne renomme pas et ne modifie pas ces fichiers : Trip Compta s'en occupe.
"""


class WrongPassword(Exception):
    """Mot de passe ou clé de secours incorrect."""


class Damaged(Exception):
    """Fichier chiffré abîmé, tronqué ou modifié."""


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _unb64(s: str) -> bytes:
    return base64.b64decode(s.encode())


def _derive(secret: str, salt: bytes, n: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=8, p=1).derive(secret.encode("utf-8"))


def new_recovery_key() -> str:
    """Clé de secours lisible : 8 groupes de 4 caractères (160 bits), sans 0/O ni 1/I ambigus."""
    raw = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
    raw = raw.replace("0", "8").replace("1", "9")
    return "-".join(raw[i:i + 4] for i in range(0, 32, 4))


def normalize_recovery(key: str) -> str:
    """Tolère espaces, minuscules et oublis de tirets à la saisie."""
    s = "".join(ch for ch in (key or "").upper() if ch.isalnum())
    return "-".join(s[i:i + 4] for i in range(0, len(s), 4))


class Vault:
    """La clé des voyages, déverrouillée : chiffre et déchiffre."""

    def __init__(self, key: bytes, vault_id: str):
        self.key = key
        self.id = vault_id
        self._aes = AESGCM(key)

    def encrypt(self, data: bytes, role: str) -> bytes:
        nonce = os.urandom(12)
        return MAGIC + nonce + self._aes.encrypt(nonce, data, role.encode())

    def decrypt(self, blob: bytes, role: str) -> bytes:
        if len(blob) < 4 + 12 + 16 or blob[:4] != MAGIC:
            raise Damaged("fichier chiffré incomplet ou inconnu")
        try:
            return self._aes.decrypt(blob[4:16], blob[16:], role.encode())
        except InvalidTag:
            raise Damaged("fichier chiffré abîmé ou modifié") from None

    def encrypt_file(self, src: Path, dst: Path, role: str) -> None:
        """Chiffre `src` dans `dst`, d'un coup (fichier temporaire puis remplacement)."""
        tmp = dst.with_name(f".{dst.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            with open(tmp, "wb") as f:
                f.write(self.encrypt(Path(src).read_bytes(), role))
                f.flush()
                os.fsync(f.fileno())
            from .dossier import replace
            replace(tmp, dst)
        finally:
            tmp.unlink(missing_ok=True)

    def decrypt_file(self, src: Path, dst: Path, role: str) -> None:
        data = self.decrypt(Path(src).read_bytes(), role)
        tmp = dst.with_name(f".{dst.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_bytes(data)
            from .dossier import replace
            replace(tmp, dst)
        finally:
            tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------- le fichier cle.tripcompta
def _wrap(key: bytes, secret: str, n: int) -> dict:
    salt = os.urandom(16)
    nonce = os.urandom(12)
    ct = AESGCM(_derive(secret, salt, n)).encrypt(nonce, key, b"cle des voyages")
    return {"sel": _b64(salt), "n": n, "cle": _b64(nonce + ct)}


def _unwrap(part: dict, secret: str) -> bytes:
    blob = _unb64(part["cle"])
    try:
        return AESGCM(_derive(secret, _unb64(part["sel"]), int(part["n"]))).decrypt(
            blob[:12], blob[12:], b"cle des voyages")
    except InvalidTag:
        raise WrongPassword() from None


def create(folder: Path, password: str, recovery: str, n: int | None = None) -> tuple[Vault, dict]:
    """Nouvelle clé des voyages, enfermée par le mot de passe et par la clé de secours. Le fichier n'est PAS encore
    écrit (voir write_key_file : c'est le moment où la protection devient effective)."""
    n = n or SCRYPT_N
    key = AESGCM.generate_key(bit_length=256)
    vid = uuid.uuid4().hex
    data = {"format": "Trip Compta, voyages chiffrés", "version": 1, "id": vid,
            "mot_de_passe": _wrap(key, password, n), "secours": _wrap(key, normalize_recovery(recovery), n)}
    return Vault(key, vid), data


def write_key_file(folder: Path, data: dict) -> None:
    tmp = Path(folder) / f".{KEY_FILE}.tmp"
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    from .dossier import replace
    replace(tmp, Path(folder) / KEY_FILE)
    (Path(folder) / README).write_text(README_TEXT, encoding="utf-8")


def read_key_file(folder: Path) -> dict | None:
    try:
        return json.loads((Path(folder) / KEY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def is_protected(folder: Path) -> bool:
    return (Path(folder) / KEY_FILE).exists()


def unlock(folder: Path, password: str | None = None, recovery: str | None = None) -> Vault:
    data = read_key_file(folder)
    if data is None:
        raise Damaged("fichier de clé introuvable ou illisible")
    if password is not None:
        key = _unwrap(data["mot_de_passe"], password)
    else:
        key = _unwrap(data["secours"], normalize_recovery(recovery or ""))
    return Vault(key, data["id"])


def change_password(folder: Path, vault: Vault, new_password: str, n: int | None = None) -> None:
    data = read_key_file(folder)
    data["mot_de_passe"] = _wrap(vault.key, new_password, n or SCRYPT_N)
    write_key_file(folder, data)


def set_recovery(folder: Path, vault: Vault, recovery: str, n: int | None = None) -> None:
    """Remplace la clé de secours (l'ancienne ne marche plus)."""
    data = read_key_file(folder)
    data["secours"] = _wrap(vault.key, normalize_recovery(recovery), n or SCRYPT_N)
    write_key_file(folder, data)


def check_group(recovery: str, index: int, typed: str) -> bool:
    """La personne a-t-elle bien noté la clé de secours ? (on redemande un de ses groupes)"""
    groups = normalize_recovery(recovery).split("-")
    return 0 <= index < len(groups) and normalize_recovery(typed) == groups[index]


# ---------------------------------------------------------------------- trousseau du système
def keyring_available() -> bool:
    try:
        import keyring
        from keyring.backends import fail
        return not isinstance(keyring.get_keyring(), fail.Keyring)
    except Exception:
        return False


def remember(vault: Vault) -> bool:
    try:
        import keyring
        keyring.set_password(KEYRING_SERVICE, vault.id, _b64(vault.key))
        return True
    except Exception:
        return False


def recall(vault_id: str) -> Vault | None:
    try:
        import keyring
        v = keyring.get_password(KEYRING_SERVICE, vault_id)
    except Exception:
        return None
    if not v:
        return None
    try:
        return Vault(_unb64(v), vault_id)
    except Exception:
        return None


def forget(vault_id: str) -> None:
    try:
        import keyring
        keyring.delete_password(KEYRING_SERVICE, vault_id)
    except Exception:
        pass
