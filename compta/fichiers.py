"""Justificatifs : seuls une photo ou un PDF sont acceptés, et c'est le CONTENU du fichier qui le dit (ses premiers
octets), pas son nom ni le type annoncé par celui qui l'envoie. Un fichier d'un autre genre (page web, programme…)
glissé dans un voyage importé n'est jamais affiché dans l'application : il est seulement proposé au téléchargement."""
from __future__ import annotations

from pathlib import Path

HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"avif"}
SNIFF_BYTES = 1024
WHAT = "une photo (JPEG, PNG, HEIC, WebP, GIF, TIFF, BMP) ou un PDF"


def sniff(head: bytes) -> tuple[str, str] | None:
    """(type, extension) d'une photo ou d'un PDF reconnu à ses premiers octets ; None pour tout le reste."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if head[4:8] == b"ftyp" and head[8:12] in HEIF_BRANDS:
        return ("image/avif", ".avif") if head[8:12] == b"avif" else ("image/heic", ".heic")
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff", ".tif"
    if head.startswith(b"BM") and len(head) > 14 and head[6:10] == b"\x00\x00\x00\x00":
        return "image/bmp", ".bmp"
    if b"%PDF-" in head[:SNIFF_BYTES] and head.lstrip()[:5] == b"%PDF-":
        return "application/pdf", ".pdf"
    return None


def sniff_file(path: Path) -> tuple[str, str] | None:
    try:
        with open(path, "rb") as f:
            return sniff(f.read(SNIFF_BYTES))
    except OSError:
        return None
