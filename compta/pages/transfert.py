"""Page « Exporter », et passer un voyage d'un ordinateur à un autre : fichier d'un voyage, de tous les voyages,
import.

Partie de l'application web (voir compta/web.py, qui installe ces pages dans l'ordre).
"""
from __future__ import annotations

import datetime as dt
import re
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from .. import models as M
from .. import services as S
from .. import transfer as TR


def install(site):
    """Installe ces pages dans l'application. `site` : ce que les pages ont en commun (voir web.py)."""
    app = site.app
    back = site.back
    db = site.db
    get_s = site.get_s
    refuse = site.refuse
    render = site.render
    trip_or_404 = site.trip_or_404

    # ------------------------------------------------------------------ export
    @app.get("/v/{tid}/export", response_class=HTMLResponse)
    def export_page(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        r = S.compute_trip(t)
        subject, body = S.treasurer_mail(t, r)
        return render(request, "export.html", trip=t, r=r, alerts=S.alerts(t, r), mail_subject=subject, mail_body=body,
                      mail_link=f"mailto:?subject={quote(subject)}&body={quote(body)}")

    # ------------------------------------------------------------------ exporter / importer des voyages
    def imports_dir() -> Path:                   # suit la base (déverrouillage d'un dossier protégé)
        return db.dir / "imports_en_attente"

    def _zip_name(label):
        return re.sub(r"[^\w-]+", "_", label).strip("_") or "export"

    @app.get("/v/{tid}/transfert", response_class=HTMLResponse)
    def transfer_page(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        return render(request, "transfer.html", trip=t, n_trips=s.query(M.Trip).count())

    @app.get("/v/{tid}/voyage.zip")
    def transfer_export(tid: int, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        data = TR.export_zip(s, [t], db.receipts_dir)
        name = _zip_name(f"voyage {t.name}") + ".zip"
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    @app.get("/tous-les-voyages.zip")
    def transfer_export_all(s: Session = Depends(get_s)):
        trips = s.query(M.Trip).order_by(M.Trip.id).all()
        data = TR.export_zip(s, trips, db.receipts_dir)
        name = f"tripcompta_{dt.date.today().isoformat()}.zip"
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    @app.post("/importer", response_class=HTMLResponse)
    async def transfer_preview(request: Request, s: Session = Depends(get_s)):
        """1re étape : on lit le fichier et on montre ce qui va arriver ; rien n'est encore enregistré."""
        f = await request.form()
        up = f.get("fichier")
        content = await up.read() if up is not None and hasattr(up, "read") else b""
        if not content:
            return refuse("/?importer=1", "Choisis le fichier .zip exporté depuis l'autre ordinateur")
        try:
            data, _ = TR.read_zip(content)
        except TR.TransferError as e:
            return refuse("/?importer=1", str(e))
        imports_dir().mkdir(exist_ok=True)
        for old in imports_dir().glob("*.zip"):            # ménage : les aperçus abandonnés depuis plus d'un jour
            if old.stat().st_mtime < dt.datetime.now().timestamp() - 86400:
                old.unlink(missing_ok=True)
        token = uuid.uuid4().hex
        (imports_dir() / f"{token}.zip").write_bytes(content)
        existing = {t.name for t in s.query(M.Trip).all()}
        return render(request, "import_preview.html", token=token, voyages=TR.summary(data), existing=existing,
                      fichier=getattr(up, "filename", "") or "", reglages=sorted((data.get("reglages") or {}).keys()))

    @app.post("/importer/{token}")
    def transfer_confirm(token: str, s: Session = Depends(get_s)):
        if not re.fullmatch(r"[0-9a-f]{32}", token) or not (imports_dir() / f"{token}.zip").exists():
            return refuse("/?importer=1", "Cet aperçu a expiré : choisis à nouveau le fichier")
        path = imports_dir() / f"{token}.zip"
        db.snapshot("import de voyage(s)")
        try:
            trips, _ = TR.import_zip(s, path.read_bytes(), db.receipts_dir)
        except (TR.TransferError, ValueError, KeyError, TypeError) as e:
            s.rollback()
            return refuse("/?importer=1", f"Import impossible : {e}")
        s.commit()
        path.unlink(missing_ok=True)
        if len(trips) == 1:
            return back(f"/v/{trips[0].id}", f"Voyage « {trips[0].name} » importé")
        return back("/", f"{len(trips)} voyages importés")
