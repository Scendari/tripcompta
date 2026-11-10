"""Signature des mises à jour : le fichier d'empreintes d'une version (SHA256SUMS.txt) est signé au moment de la
publication, et Trip Compta vérifie cette signature avant d'installer quoi que ce soit.

Pourquoi : l'empreinte seule protège d'un téléchargement abîmé, pas d'une publication falsifiée (quelqu'un qui
remplacerait à la fois le fichier et son empreinte). La signature, elle, ne peut être faite qu'avec la clé privée,
qui n'est ni dans le dépôt ni dans l'application : c'est le secret « TRIPCOMPTA_CLE_SIGNATURE » de la publication
(voir docs/PUBLIER.md). L'application n'embarque que les clés PUBLIQUES ci-dessous, qui ne servent qu'à vérifier.

Technique : Ed25519 (bibliothèque « cryptography », déjà utilisée pour la protection par mot de passe). Clés et
signatures sont écrites en base64. Rien à voir avec la signature des applications par Apple ou Microsoft.
"""
from __future__ import annotations

import base64
import binascii
import re

# Clés publiques acceptées (base64 de 32 octets). Plusieurs clés sont possibles : pour en changer, on ajoute la
# nouvelle ici, on publie une version signée par l'ancienne, puis on retire l'ancienne à la version suivante.
PUBLIC_KEYS: tuple[str, ...] = (
    "F0jKqH+yDeO24CZD3gRYSe/4F0YXP+JMaDcTAS3RAIU=",
)

VERSION_LINE = "# Trip Compta "           # 1re ligne du fichier d'empreintes : « # Trip Compta 1.6.0 »


def _b64(text: str, size: int) -> bytes | None:
    try:
        raw = base64.b64decode("".join(str(text).split()), validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw if len(raw) == size else None


def verify(data: bytes, signature: str, keys=None) -> bool:
    """La signature (base64) de ces octets vient-elle d'une des clés acceptées ? Ne lève jamais."""
    sig = _b64(signature, 64)
    if sig is None:
        return False
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    for key in (PUBLIC_KEYS if keys is None else keys):
        raw = _b64(key, 32)
        if raw is None:
            continue
        try:
            Ed25519PublicKey.from_public_bytes(raw).verify(sig, data)
            return True
        except (InvalidSignature, ValueError):
            continue
    return False


def signed_version(text: str) -> str | None:
    """La version pour laquelle ce fichier d'empreintes a été signé (sa ligne « # Trip Compta 1.6.0 »)."""
    m = re.search(r"^" + re.escape(VERSION_LINE) + r"(\S+)\s*$", text, re.M)
    return m.group(1) if m else None


def self_test() -> bool:
    """La vérification marche-t-elle dans CETTE application (bibliothèque présente et utilisable) ? Avec une clé
    jetable : sert au diagnostic et au contrôle de l'application fabriquée."""
    try:
        private, public = new_key()
        sig = sign(b"essai", private)
        return verify(b"essai", sig, keys=(public,)) and not verify(b"essai modifie", sig, keys=(public,))
    except Exception:
        return False


# ---------------------------------------------------------------- côté publication (jamais appelé par l'application)

def new_key() -> tuple[str, str]:
    """(clé privée, clé publique), en base64. La privée ne doit jamais être écrite dans le dépôt."""
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(ser.Encoding.Raw, ser.PrivateFormat.Raw, ser.NoEncryption())
    public = key.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
    return base64.b64encode(private).decode(), base64.b64encode(public).decode()


def _private(private: str):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    raw = _b64(private, 32)
    if raw is None:
        raise ValueError("Clé de signature illisible (attendu : 32 octets en base64).")
    return Ed25519PrivateKey.from_private_bytes(raw)


def public_of(private: str) -> str:
    from cryptography.hazmat.primitives import serialization as ser
    return base64.b64encode(_private(private).public_key().public_bytes(ser.Encoding.Raw,
                                                                         ser.PublicFormat.Raw)).decode()


def sign(data: bytes, private: str) -> str:
    return base64.b64encode(_private(private).sign(data)).decode()
