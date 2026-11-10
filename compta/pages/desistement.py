"""Désistement d'un participant.

Partie de l'application web (voir compta/web.py, qui installe ces pages dans l'ordre).
"""
from __future__ import annotations

import datetime as dt

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from .. import engine as E
from .. import models as M
from .. import services as S
from .. import writes as W


def install(site):
    """Installe ces pages dans l'application. `site` : ce que les pages ont en commun (voir web.py)."""
    amount = site.amount
    app = site.app
    back = site.back
    db = site.db
    get_s = site.get_s
    refuse = site.refuse
    render = site.render
    trip_or_404 = site.trip_or_404
    whole = site.whole
    wholes = site.wholes

    # ------------------------------------------------------------------ désistement
    @app.get("/v/{tid}/desistement", response_class=HTMLResponse)
    def withdraw_page(tid: int, request: Request, p: int | None = None, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        r = S.compute_trip(t)
        withdrawn = {w.participant_id for w in t.withdrawals}
        person_ = s.get(M.Participant, p) if p else None
        if person_ and person_.trip_id != tid:
            person_ = None
        exps = [e for e in t.expenses if any(sh.participant_id == p for sh in e.shares)] if person_ else []
        cur = next((w for w in t.withdrawals if person_ and w.participant_id == person_.id), None)
        # parts « si elle restait » : calcul sans son désistement (pour montrer ce qu'elle garde ou non)
        inputs = S.engine_inputs(t)
        r_full = E.compute(**{**inputs, "withdrawals": [w for w in inputs["withdrawals"]
                                                         if not person_ or w.participant != person_.id]},
                           mode=E.ACTUAL) if person_ else r
        return render(request, "withdraw.html", trip=t, r=r, r_full=r_full, sel=person_, exps=exps,
                      withdrawn=withdrawn, wcur=cur, today=dt.date.today().isoformat())

    def _withdraw_from_form(t, f):
        pid = whole(f.get("participant"))
        kept = set(wholes(f.getlist("kept")))
        return E.Withdrawal(pid, f.get("absorber", E.GROUP), kept, f.get("date", ""))

    @app.post("/v/{tid}/desistement/apercu", response_class=HTMLResponse)
    async def withdraw_preview(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        w = _withdraw_from_form(t, f)
        if not w.participant:
            return HTMLResponse('<p class="muted">Choisis une personne.</p>')
        mode = E.ACTUAL
        inputs = S.engine_inputs(t)
        r0 = E.compute(**inputs, mode=mode)                     # aujourd'hui (avec un éventuel désistement déjà noté)
        others = [x for x in inputs["withdrawals"] if x.participant != w.participant]
        r1 = E.compute(**{**inputs, "withdrawals": others + [w]}, mode=mode)
        return render(request, "_diff.html", trip=t, r0=r0, r1=r1, eid=None, wpid=w.participant)

    @app.post("/v/{tid}/desistement")
    async def withdraw_save(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        w = _withdraw_from_form(t, f)
        p = s.get(M.Participant, w.participant)
        if not p or p.trip_id != tid:
            return refuse(f"/v/{tid}/desistement", "Choisis une personne")
        cur = next((x for x in t.withdrawals if x.participant_id == p.id), None)
        refund = amount(f, "refund")
        db.snapshot(f"{'modification du ' if cur else ''}désistement de {p.name}")
        _, new = W.save_withdrawal(s, t, p.id, date=S.parse_date(w.date), absorber=w.absorber, kept=w.kept,
                                   note=f.get("note") or "", refund=refund or None)
        s.commit()
        return back(f"/v/{tid}/p/{p.id}", f"Désistement de {p.name} {'enregistré' if new else 'modifié'}")

    @app.post("/v/{tid}/desistement/{pid}/annuler")
    async def withdraw_cancel(tid: int, pid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        nxt = str((await request.form()).get("next") or "")
        w = next((x for x in t.withdrawals if x.participant_id == pid), None)
        if not w:
            return refuse(f"/v/{tid}/p/{pid}", "Pas de désistement à annuler")
        db.snapshot(f"retour de {w.participant.name} dans le voyage")
        s.delete(w)
        s.commit()
        return back(nxt if nxt.startswith(f"/v/{tid}/") else f"/v/{tid}/p/{pid}",
                    "Désistement annulé : la personne est de nouveau dans le voyage")
