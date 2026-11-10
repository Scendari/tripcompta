"""Envoyer à chacun son compte (message, PDF), et les fichiers du bilan : PDF, tableur, archive complète.

Partie de l'application web (voir compta/web.py, qui installe ces pages dans l'ordre).
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from .. import engine as E
from .. import services as S
from ..web import _slug


def install(site):
    """Installe ces pages dans l'application. `site` : ce que les pages ont en commun (voir web.py)."""
    app = site.app
    back = site.back
    db = site.db
    get_s = site.get_s
    refuse = site.refuse
    render = site.render
    trip_or_404 = site.trip_or_404

    # ------------------------------------------------------------------ envoyer à chacun son compte
    def _send_rows(t, r):
        rows = []
        for a in r.accounts:
            ref = next(p for p in t.participants if p.id == a.referent)
            msg = S.account_message(t, r, a)
            num = S.whatsapp_number(ref.phone)
            subject = (f"Compte de la {a.name[:1].lower() + a.name[1:]}" if a.is_family else "Ton compte") + \
                f" — voyage « {t.name} »"
            rows.append(dict(acc=a, ref=ref, msg=msg, num=num, subject=subject, key=a.key,
                             wa=f"https://wa.me/{num}?text={quote(msg)}" if num else f"https://wa.me/?text={quote(msg)}",
                             mail=f"mailto:{quote(ref.email or '', safe='@.+-_')}?subject={quote(subject)}&body={quote(msg)}",
                             pdf=f"/v/{t.id}/compte/{a.key}.pdf"))
        return rows

    @app.get("/v/{tid}/envoyer", response_class=HTMLResponse)
    def send_page(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        r = S.compute_trip(t)
        return render(request, "send.html", trip=t, r=r, rows=_send_rows(t, r), pay_info=t.pay_info or "",
                      plans=S.pay_plans(t, r),
                      pay_max=S.PAY_INFO_MAX, owing=sum(1 for a in r.accounts if S.owes_money(a)))

    @app.post("/v/{tid}/envoyer/comment-payer")
    async def send_pay_info(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        text = str((await request.form()).get("pay_info") or "")
        if len(text.strip()) > S.PAY_INFO_MAX:
            return refuse(f"/v/{tid}/envoyer", f"Ce texte est trop long : {S.PAY_INFO_MAX} caractères au plus", "pay_info")
        if text.replace("\r", "").strip() == (t.pay_info or ""):
            return back(f"/v/{tid}/envoyer", "Rien à changer")
        db.snapshot("« Comment payer »")
        S.set_pay_info(t, text)
        s.commit()
        return back(f"/v/{tid}/envoyer", "C'est noté : « Comment payer » est ajouté au message et au PDF de ceux qui "
                    "doivent encore de l'argent" if t.pay_info else "« Comment payer » est effacé")

    @app.get("/v/{tid}/compte/{key}.pdf")
    def account_pdf_route(tid: int, key: str, request: Request, s: Session = Depends(get_s)):
        from ..export import account_pdf
        t = trip_or_404(s, tid)
        r = S.compute_trip(t)
        if key not in {a.key for a in r.accounts}:
            raise HTTPException(404, "Compte introuvable")
        data, name = account_pdf(t, key, E.ACTUAL)
        return Response(data, media_type="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    @app.get("/v/{tid}/comptes-personnels.zip")
    def accounts_zip_route(tid: int, request: Request, s: Session = Depends(get_s)):
        from ..export import accounts_zip
        t = trip_or_404(s, tid)
        return Response(accounts_zip(t, E.ACTUAL), media_type="application/zip",
                        headers={"Content-Disposition":
                                 f"attachment; filename*=UTF-8''{quote('Comptes_' + _slug(t.name))}.zip"})

    @app.get("/v/{tid}/export.pdf")
    def export_pdf(tid: int, request: Request, s: Session = Depends(get_s)):
        from ..export import trip_pdf
        t = trip_or_404(s, tid)
        data = trip_pdf(t, E.ACTUAL)
        return Response(data, media_type="application/pdf",
                        headers={"Content-Disposition":
                                 f"attachment; filename*=UTF-8''{quote('Bilan_' + _slug(t.name))}.pdf"})

    @app.get("/v/{tid}/export.xlsx")
    def export_xlsx(tid: int, s: Session = Depends(get_s)):
        """Tableur : dépenses, argent reçu et rendu, qui paie quoi (trois feuilles)."""
        from ..tableur import trip_workbook
        t = trip_or_404(s, tid)
        return Response(trip_workbook(t, s),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition":
                                 f"attachment; filename*=UTF-8''{quote('Tableur_' + _slug(t.name))}.xlsx"})

    @app.get("/v/{tid}/export.zip")
    def export_zip(tid: int, request: Request, s: Session = Depends(get_s)):
        from ..export import trip_zip
        t = trip_or_404(s, tid)
        data = trip_zip(t, E.ACTUAL, db.receipts_dir)
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition":
                                 f"attachment; filename*=UTF-8''{quote('Justificatifs_' + _slug(t.name))}.zip"})

    # utilisé par d'autres pages
    site._send_rows = _send_rows
