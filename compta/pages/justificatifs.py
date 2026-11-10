"""Justificatifs : photos et PDF joints aux dépenses.

Partie de l'application web (voir compta/web.py, qui installe ces pages dans l'ordre).
"""
from __future__ import annotations

import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from sqlalchemy.orm import Session

from .. import models as M
from .. import fichiers
from ..web import TRANSPARENT_PNG, _thumbnail, _write_atomic


def install(site):
    """Installe ces pages dans l'application. `site` : ce que les pages ont en commun (voir web.py)."""
    app = site.app
    back = site.back
    db = site.db
    get_s = site.get_s
    refuse = site.refuse
    render = site.render
    trip_or_404 = site.trip_or_404
    wholes = site.wholes

    # ------------------------------------------------------------------ justificatifs
    async def _store_receipt(s, t, up) -> M.Receipt | None:
        """Enregistre une photo ou un PDF ; None pour tout autre fichier (reconnu à son contenu, pas à son nom)."""
        data = await up.read()
        kind = fichiers.sniff(data[:fichiers.SNIFF_BYTES])
        if kind is None:
            return None
        ctype, ext = kind
        stored = f"{t.id}_{uuid.uuid4().hex}{ext}"
        (db.receipts_dir / stored).write_bytes(data)
        number = max([r.number for r in t.receipts] or [0]) + 1
        rec = M.Receipt(trip_id=t.id, number=number, original_name=Path(up.filename).name, stored_name=stored,
                        content_type=ctype)
        t.receipts.append(rec)
        s.flush()
        return rec

    def _refused_files(names_: list) -> str:
        """« x.html » n'a pas été joint… : phrase pour les fichiers qui ne sont ni une photo ni un PDF."""
        if not names_:
            return ""
        shown = ", ".join(f"« {n} »" for n in names_[:3]) + ("…" if len(names_) > 3 else "")
        return (f"{shown} {'n’a pas été joint' if len(names_) == 1 else 'n’ont pas été joints'} : "
                f"un justificatif doit être {fichiers.WHAT}.")

    @app.get("/v/{tid}/justificatifs", response_class=HTMLResponse)
    def receipts(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        missing = [e for e in t.expenses if e.actual is not None and not e.receipt_links]
        return render(request, "receipts.html", trip=t, missing=missing)

    @app.post("/v/{tid}/justificatifs")
    async def receipts_upload(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        files = [u for u in f.getlist("files") if getattr(u, "filename", None)]
        if not files:
            return refuse(f"/v/{tid}/justificatifs", "Choisis au moins un fichier")
        db.snapshot(f"ajout de {len(files)} justificatif(s)")
        link_to = set(wholes(f.getlist("expenses")))
        added, not_joined = 0, []
        for up in files:
            rec = await _store_receipt(s, t, up)
            if rec is None:
                not_joined.append(Path(up.filename).name)
                continue
            added += 1
            for eid in link_to:
                s.add(M.ReceiptLink(receipt_id=rec.id, expense_id=eid))
        s.commit()
        if not added:
            return refuse(f"/v/{tid}/justificatifs", _refused_files(not_joined))
        done = "1 justificatif ajouté" if added == 1 else f"{added} justificatifs ajoutés"
        if not_joined:
            return refuse(f"/v/{tid}/justificatifs", f"{done}, mais " + _refused_files(not_joined))
        return back(f"/v/{tid}/justificatifs", done)

    @app.post("/v/{tid}/justificatifs/{rid}")
    async def receipt_links(tid: int, rid: int, request: Request, s: Session = Depends(get_s)):
        trip_or_404(s, tid)
        rec = s.get(M.Receipt, rid)
        if not rec or rec.trip_id != tid:
            raise HTTPException(404, "Justificatif introuvable")
        f = await request.form()
        db.snapshot(f"liens du justificatif {rec.code}")
        s.query(M.ReceiptLink).filter_by(receipt_id=rid).delete()
        for eid in set(wholes(f.getlist("expenses"))):
            s.add(M.ReceiptLink(receipt_id=rid, expense_id=eid))
        s.commit()
        return back(f"/v/{tid}/justificatifs", f"Justificatif {rec.code} mis à jour")

    @app.post("/v/{tid}/justificatifs/{rid}/supprimer")
    def receipt_delete(tid: int, rid: int, s: Session = Depends(get_s)):
        trip_or_404(s, tid)
        rec = s.get(M.Receipt, rid)
        if not rec or rec.trip_id != tid:
            raise HTTPException(404, "Justificatif introuvable")
        db.snapshot(f"suppression du justificatif {rec.code}")
        s.delete(rec)   # le fichier reste sur le disque : « Annuler » reste possible
        s.commit()
        return back(f"/v/{tid}/justificatifs", f"Justificatif {rec.code} supprimé")

    @app.post("/apercu-fichier")
    async def file_preview(request: Request):
        """Miniature d'un fichier choisi, avant l'envoi (rien n'est enregistré)."""
        up = (await request.form()).get("file")
        if not getattr(up, "filename", None):
            raise HTTPException(400, "Aucun fichier")
        data = await up.read()
        try:
            out = _thumbnail(data, (up.content_type or "").endswith("pdf") or up.filename.lower().endswith(".pdf"))
        except Exception:
            return Response(TRANSPARENT_PNG, media_type="image/png")
        return Response(out, media_type="image/jpeg")

    @app.get("/v/{tid}/justificatifs/{rid}/vignette")
    def receipt_thumb(tid: int, rid: int, s: Session = Depends(get_s)):
        """Miniature (JPEG, 240 px) : la photo réduite, ou la 1re page d'un PDF. Gardée en cache sur le disque."""
        rec = s.get(M.Receipt, rid)
        if not rec or rec.trip_id != tid:
            raise HTTPException(404, "Justificatif introuvable")
        src = db.receipts_dir / rec.stored_name
        cache = db.thumbs_dir / f"{Path(rec.stored_name).stem}.jpg"
        if not cache.exists():
            if not src.exists():
                raise HTTPException(404, "Fichier introuvable sur le disque")
            try:
                data = _thumbnail(src.read_bytes(), (rec.content_type or "").endswith("pdf") or src.suffix.lower() == ".pdf")
                cache.parent.mkdir(exist_ok=True)
                _write_atomic(cache, data)       # deux demandes simultanées : jamais une vignette à moitié écrite
            except Exception:      # fichier illisible : image transparente, la case affiche « PDF » / rien
                return Response(TRANSPARENT_PNG, media_type="image/png", headers={"Cache-Control": "no-store"})
        return FileResponse(cache, media_type="image/jpeg")

    @app.get("/v/{tid}/justificatifs/{rid}/fichier")
    def receipt_file(tid: int, rid: int, s: Session = Depends(get_s)):
        rec = s.get(M.Receipt, rid)
        if not rec or rec.trip_id != tid:
            raise HTTPException(404, "Justificatif introuvable")
        path = db.receipts_dir / rec.stored_name
        if not path.exists():
            raise HTTPException(404, "Fichier introuvable sur le disque")
        # Le type vient du contenu du fichier, jamais de ce qui a été annoncé à l'envoi ou écrit dans un voyage
        # importé. Photo ou PDF : affiché. Tout le reste : téléchargement seulement, jamais ouvert dans l'application.
        kind = fichiers.sniff_file(path)
        name = quote(rec.original_name)
        if kind is None:
            return FileResponse(path, media_type="application/octet-stream",
                                headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}",
                                         "X-Content-Type-Options": "nosniff"})
        return FileResponse(path, media_type=kind[0],
                            headers={"Content-Disposition": f"inline; filename*=UTF-8''{name}",
                                     "X-Content-Type-Options": "nosniff"})

    # utilisé par d'autres pages
    site._refused_files = _refused_files
    site._store_receipt = _store_receipt
