"""Justificatifs : seuls une photo ou un PDF sont acceptés, reconnus à leur CONTENU. Un autre fichier (page web…)
arrivé par un voyage importé n'est jamais affiché dans l'application : téléchargement seulement."""
import html
import io
import json
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

from compta import fichiers
from compta import models as M
from compta.ops import Ops, OpsError
from compta.web import create_app

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"0" * 50
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 50
PDF = b"%PDF-1.4\n% ticket\n%%EOF\n"
HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"0" * 20
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"0" * 20
EVIL = b"<!doctype html><html><script>fetch('/api/voyages')</script></html>"
SVG = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"


def clean(t):
    return re.sub(r"\s+", " ", html.unescape(t).replace("\xa0", " "))


@pytest.fixture
def env(tmp_path):
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.import_trip({"voyage": {"nom": "x"}, "participants": ["Anne", "Bob"],
                           "depenses": [{"libelle": "Gîte", "reel": "300", "paye_par": "asso", "pour": "tous"}]})["voyage_id"]
    return app, ops, tid, TestClient(app)


def receipts(app, tid):
    with app.state.db.Session() as s:
        return [(r.id, r.original_name, r.content_type, r.stored_name) for r in s.get(M.Trip, tid).receipts]


# ---------------------------------------------------------------- reconnaissance par le contenu
@pytest.mark.parametrize("data,expected", [
    (JPEG, ("image/jpeg", ".jpg")), (PNG, ("image/png", ".png")), (PDF, ("application/pdf", ".pdf")),
    (HEIC, ("image/heic", ".heic")), (WEBP, ("image/webp", ".webp")), (b"GIF89a" + b"0" * 9, ("image/gif", ".gif")),
    (b"II*\x00" + b"0" * 9, ("image/tiff", ".tif")),
    (EVIL, None), (SVG, None), (b"", None), (b"MZ\x90\x00", None), (b"PK\x03\x04", None),
    (b"<html>%PDF-1.4", None),                                  # « %PDF » plus loin dans une page web : refusé
    (b"\xff\xd8fake", None),
])
def test_sniff(data, expected):
    assert fichiers.sniff(data) == expected


# ---------------------------------------------------------------- envoi
def test_photo_and_pdf_are_accepted_with_type_from_content(env):
    app, ops, tid, c = env
    # nom et type annoncés trompeurs : c'est le contenu qui décide
    c.post(f"/v/{tid}/justificatifs", files=[("files", ("ticket.txt", JPEG, "text/html")),
                                             ("files", ("facture", PDF, "application/x-msdownload"))])
    recs = receipts(app, tid)
    assert [(n, t) for _, n, t, _ in recs] == [("ticket.txt", "image/jpeg"), ("facture", "application/pdf")]
    assert recs[0][3].endswith(".jpg") and recs[1][3].endswith(".pdf")


def test_other_files_are_refused_in_red(env):
    app, ops, tid, c = env
    r = c.post(f"/v/{tid}/justificatifs", files=[("files", ("page.html", EVIL, "image/jpeg"))])
    page = clean(r.text)
    assert 'class="flash err"' in r.text
    assert "« page.html » n’a pas été joint" in page and "une photo" in page and "PDF" in page
    assert receipts(app, tid) == []
    assert list(app.state.db.receipts_dir.glob("*")) == []      # rien n'est écrit sur le disque


def test_mixed_upload_keeps_the_good_files_and_names_the_others(env):
    app, ops, tid, c = env
    r = c.post(f"/v/{tid}/justificatifs", files=[("files", ("ok.jpg", JPEG, "image/jpeg")),
                                             ("files", ("dessin.svg", SVG, "image/svg+xml")),
                                             ("files", ("ok.pdf", PDF, "application/pdf"))])
    page = clean(r.text)
    assert "2 justificatifs ajoutés, mais « dessin.svg » n’a pas été joint" in page
    assert [n for _, n, _, _ in receipts(app, tid)] == ["ok.jpg", "ok.pdf"]


def test_expense_is_saved_even_if_its_file_is_refused(env):
    app, ops, tid, c = env
    ids = [p["id"] for p in ops.trip(tid)["participants"]]
    r = c.post(f"/v/{tid}/depenses", data={"label": "Courses", "actual": "42,50", "category": "Divers", "payer": "asso",
                                          **{f"inc_{i}": "1" for i in ids}, **{f"w_{i}": "1" for i in ids}},
               files=[("files", ("liste.html", EVIL, "text/html"))])
    page = clean(r.text)
    assert "« Courses » enregistrée, mais « liste.html » n’a pas été joint" in page and 'class="flash err"' in r.text
    dep = [d for d in ops.trip(tid)["depenses"] if d["libelle"] == "Courses"]
    assert len(dep) == 1 and receipts(app, tid) == []
    assert "/modifier" in str(r.url)                            # on revient sur la dépense, pour joindre le bon fichier


def test_ai_cannot_add_another_kind_of_file(env, tmp_path):
    app, ops, tid, c = env
    bad = tmp_path / "ticket.jpg"
    bad.write_bytes(EVIL)
    with pytest.raises(OpsError, match="n'est pas accepté"):
        ops.add_receipt(tid, str(bad))
    good = tmp_path / "vrai.dat"
    good.write_bytes(PDF)
    ops.add_receipt(tid, str(good))
    assert [(n, t) for _, n, t, _ in receipts(app, tid)] == [("vrai.dat", "application/pdf")]


# ---------------------------------------------------------------- lecture
def test_served_inline_with_the_real_type_and_nosniff(env):
    app, ops, tid, c = env
    c.post(f"/v/{tid}/justificatifs", files=[("files", ("a.jpg", JPEG, "text/html")), ("files", ("b.pdf", PDF, "text/html"))])
    for (rid, _, _, _), ctype in zip(receipts(app, tid), ("image/jpeg", "application/pdf")):
        r = c.get(f"/v/{tid}/justificatifs/{rid}/fichier")
        assert r.headers["content-type"].startswith(ctype)
        assert r.headers["content-disposition"].startswith("inline")
        assert r.headers["x-content-type-options"] == "nosniff"


def _zip_with(tmp_path, receipt_name, data, content_type):
    """Un voyage exporté par quelqu'un d'autre, puis trafiqué : le « justificatif » contient autre chose, et l'archive
    annonce le type qu'elle veut."""
    other = create_app(tmp_path / "ailleurs")
    t2 = Ops(other.state.db).import_trip({"voyage": {"nom": "Voyage reçu"}, "participants": ["Zoé"]})["voyage_id"]
    co = TestClient(other)
    co.post(f"/v/{t2}/justificatifs", files=[("files", (receipt_name, JPEG, "image/jpeg"))])
    zin = zipfile.ZipFile(io.BytesIO(co.get(f"/v/{t2}/voyage.zip").content))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zo:
        for n in zin.namelist():
            content = zin.read(n)
            if n == "voyages.json":
                d = json.loads(content)
                for v in d["voyages"]:
                    for r in v["justificatifs"]:
                        r["content_type"] = content_type
                content = json.dumps(d).encode()
            elif n.startswith("justificatifs/"):
                content = data
            zo.writestr(n, content)
    return out.getvalue()


def _import(c, content):
    r = c.post("/importer", files={"fichier": ("voyage.zip", content, "application/zip")})
    token = re.search(r"/importer/([0-9a-f]{32})", r.text).group(1)
    assert c.post(f"/importer/{token}").status_code == 200


def test_imported_web_page_is_never_displayed_only_downloaded(env, tmp_path):
    """Le scénario du rapport : un fichier HTML caché dans un voyage importé, annoncé comme une image."""
    app, ops, tid, c = env
    _import(c, _zip_with(tmp_path, "ticket.jpg", EVIL, "text/html"))
    with app.state.db.Session() as s:
        rec = next(r for t in s.query(M.Trip).all() for r in t.receipts if r.original_name == "ticket.jpg")
        rid, t2, ctype, stored = rec.id, rec.trip_id, rec.content_type, rec.stored_name
    assert ctype == "application/octet-stream" and stored.endswith(".bin")
    r = c.get(f"/v/{t2}/justificatifs/{rid}/fichier")
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "text/html" not in r.headers["content-type"]


def test_imported_real_photo_is_displayed(env, tmp_path):
    app, ops, tid, c = env
    _import(c, _zip_with(tmp_path, "photo.bin", JPEG, "text/html"))
    with app.state.db.Session() as s:
        rec = next(r for t in s.query(M.Trip).all() for r in t.receipts if r.original_name == "photo.bin")
        rid, t2, ctype = rec.id, rec.trip_id, rec.content_type
    assert ctype == "image/jpeg"
    r = c.get(f"/v/{t2}/justificatifs/{rid}/fichier")
    assert r.headers["content-type"] == "image/jpeg" and r.headers["content-disposition"].startswith("inline")


def test_file_swapped_on_disk_is_not_displayed(env):
    """Même si le fichier est remplacé sur le disque après coup, le type est relu dans son contenu."""
    app, ops, tid, c = env
    c.post(f"/v/{tid}/justificatifs", files=[("files", ("a.jpg", JPEG, "image/jpeg"))])
    rid, _, _, stored = receipts(app, tid)[0]
    (app.state.db.receipts_dir / stored).write_bytes(EVIL)
    r = c.get(f"/v/{tid}/justificatifs/{rid}/fichier")
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"].startswith("attachment")
