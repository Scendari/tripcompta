"""Comptes « Entre nous » (hors asso).

Partie de l'application web (voir compta/web.py, qui installe ces pages dans l'ordre).
"""
from __future__ import annotations

import datetime as dt
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from .. import engine as E
from .. import models as M
from .. import services as S
from ..web import _slug


def install(site):
    """Installe ces pages dans l'application. `site` : ce que les pages ont en commun (voir web.py)."""
    amount = site.amount
    app = site.app
    back = site.back
    db = site.db
    get_s = site.get_s
    names = site.names
    refuse = site.refuse
    render = site.render
    trip_or_404 = site.trip_or_404
    whole = site.whole
    wholes = site.wholes

    # ------------------------------------------------------------------ Entre nous
    @app.get("/v/{tid}/entre-nous", response_class=HTMLResponse)
    def subs(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        views = {sub.id: S.sub_view(sub) for sub in t.subaccounts}
        return render(request, "subaccounts.html", trip=t, views=views, names=names(t))

    @app.post("/v/{tid}/entre-nous")
    async def sub_create(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        name = (f.get("name") or "").strip()
        members = wholes(f.getlist("members"))
        if not name or len(members) < 2:
            return refuse(f"/v/{tid}/entre-nous", "Donne un nom et choisis au moins 2 personnes", "name")
        db.snapshot(f"création du compte « {name} »")
        sub = M.SubAccount(trip_id=tid, name=name, member_ids=",".join(map(str, members)))
        s.add(sub)
        s.commit()
        return back(f"/v/{tid}/entre-nous/{sub.id}", f"Compte « {name} » créé")

    def _sub_or_404(s, tid, sid):
        sub = s.get(M.SubAccount, sid)
        if not sub or sub.trip_id != tid:
            raise HTTPException(404, "Compte introuvable")
        return sub

    @app.get("/v/{tid}/entre-nous/{sid}", response_class=HTMLResponse)
    def sub_detail(tid: int, sid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        sub = _sub_or_404(s, tid, sid)
        return render(request, "subaccount.html", trip=t, sub=sub, v=S.sub_view(sub), names=names(t),
                      today=dt.date.today().isoformat())

    @app.post("/v/{tid}/entre-nous/{sid}/depense")
    async def sub_expense(tid: int, sid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        sub = _sub_or_404(s, tid, sid)
        f = await request.form()
        label = (f.get("label") or "").strip()
        amt = amount(f, "amount")
        payer = whole(f.get("payer"))
        bens = [x for x in wholes(f.getlist("for")) if x in sub.members]
        if not label or not amt or amt <= 0 or payer not in sub.members or not bens:
            return refuse(f"/v/{tid}/entre-nous/{sid}", "Remplis le libellé, le montant, qui a payé et pour qui")
        db.snapshot(f"dépense « {label} » dans « {sub.name} »")
        s.add(M.SubExpense(sub_id=sid, label=label, amount=amt, payer_id=payer,
                           date=S.parse_date(f.get("date")), weights=",".join(f"{b}:1" for b in bens)))
        s.commit()
        return back(f"/v/{tid}/entre-nous/{sid}", f"« {label} » ajoutée")

    @app.post("/v/{tid}/entre-nous/{sid}/regler")
    async def sub_settle(tid: int, sid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        sub = _sub_or_404(s, tid, sid)
        f = await request.form()
        a, b = whole(f.get("sender")), whole(f.get("receiver"))
        amt = amount(f, "amount")
        if not amt or amt <= 0:
            return refuse(f"/v/{tid}/entre-nous/{sid}", "Montant invalide", "amount")
        if a not in sub.members or b not in sub.members or a == b:
            return refuse(f"/v/{tid}/entre-nous/{sid}", "Choisis qui rembourse et qui reçoit, parmi les personnes de ce compte")
        n = names(t)
        db.snapshot(f"{n[a]} a remboursé {n[b]}")
        s.add(M.SubTransfer(sub_id=sid, sender_id=a, receiver_id=b, amount=amt, date=dt.date.today()))
        s.commit()
        return back(f"/v/{tid}/entre-nous/{sid}", f"{n[a]} a remboursé {E.fmt(amt)} à {n[b]}")

    @app.post("/v/{tid}/entre-nous/{sid}/supprimer-ligne")
    async def sub_delete_line(tid: int, sid: int, request: Request, s: Session = Depends(get_s)):
        trip_or_404(s, tid)
        _sub_or_404(s, tid, sid)
        f = await request.form()
        db.snapshot("suppression d'une ligne « Entre nous »")
        if f.get("expense"):
            obj = s.get(M.SubExpense, whole(f.get("expense")))
        else:
            obj = s.get(M.SubTransfer, whole(f.get("transfer")))
        if obj and obj.sub_id == sid:
            s.delete(obj)
            s.commit()
        return back(f"/v/{tid}/entre-nous/{sid}", "Ligne supprimée")

    @app.post("/v/{tid}/entre-nous/{sid}/supprimer")
    def sub_delete(tid: int, sid: int, s: Session = Depends(get_s)):
        trip_or_404(s, tid)
        sub = _sub_or_404(s, tid, sid)
        db.snapshot(f"suppression du compte « {sub.name} »")
        s.delete(sub)
        s.commit()
        return back(f"/v/{tid}/entre-nous", f"Compte « {sub.name} » supprimé")

    @app.get("/v/{tid}/entre-nous/{sid}/export.pdf")
    def sub_pdf(tid: int, sid: int, s: Session = Depends(get_s)):
        from ..export import subaccount_pdf
        t = trip_or_404(s, tid)
        sub = _sub_or_404(s, tid, sid)
        data = subaccount_pdf(t, sub)
        return Response(data, media_type="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(_slug(sub.name))}.pdf"})
