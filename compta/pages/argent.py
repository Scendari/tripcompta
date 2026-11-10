"""Argent reçu et rendu : reste à régler, mouvements, chèques, dates de paiement et relances.

Partie de l'application web (voir compta/web.py, qui installe ces pages dans l'ordre).
"""
from __future__ import annotations

import datetime as dt
from urllib.parse import quote, urlparse

from fastapi import Depends, HTTPException, Request
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
    names = site.names
    refuse = site.refuse
    render = site.render
    trip_or_404 = site.trip_or_404
    whole = site.whole

    # ------------------------------------------------------------------ argent reçu / rendu
    @app.get("/v/{tid}/argent/mouvements", response_class=HTMLResponse)
    def money_moves(tid: int, request: Request, s: Session = Depends(get_s)):
        return money(tid, request, s, tab="mouvements")

    @app.get("/v/{tid}/argent", response_class=HTMLResponse)
    def money(tid: int, request: Request, s: Session = Depends(get_s), tab: str = "regler"):
        t = trip_or_404(s, tid)
        if request.query_params.get("k") or request.query_params.get("p"):   # lien « Enregistrer un versement de … »
            tab = "mouvements"
        r = S.compute_trip(t)
        rows = []
        for m in t.movements:
            if m.kind == "versement":
                rows.append(dict(date=m.date, label=f"Versement de {m.participant.name}", inc=m.amount, out=None,
                                 note=m.note, mid=m.id, m=m, method=S.pay_method_label(m.method)))
            else:
                rows.append(dict(date=m.date, label=f"Remboursement à {m.participant.name}", inc=None,
                                 out=m.amount, note=m.note, mid=m.id, m=m, method=S.pay_method_label(m.method)))
        for e in t.expenses:
            if e.actual is None:
                continue
            asso_part = sum(c for pid, c in e.splits() if pid is None)
            if not asso_part:
                continue
            part = " (part payée par l'asso)" if e.multi_payer and not e.is_income else ""
            if e.is_income:
                rows.append(dict(date=e.date, label=f"Reçu : {e.label}", inc=asso_part, out=None,
                                 note=e.category.name if e.category else "", eid=e.id))
            else:
                rows.append(dict(date=e.date, label=f"Paiement : {e.label}{part}", inc=None, out=asso_part,
                                 note=e.category.name if e.category else "", eid=e.id))
        # sans date d'abord (à compléter), puis la plus récente en haut
        rows.sort(key=lambda x: (x["date"] is not None, -(x["date"].toordinal() if x["date"] else 0)))
        total_in = sum(x["inc"] or 0 for x in rows)
        total_out = sum(x["out"] or 0 for x in rows)
        advances = S.advances(t)
        pending = [e for e in t.expenses if e.pending]
        to_pay = [e for e in t.expenses if e.actual is None and not e.pending and not e.is_income
                  and any(pid is None for pid, _ in e.splits())]
        plans = S.pay_plans(t, r)
        dates = S.pay_dates(t)
        people = {p.id: p for p in t.participants}
        own_dates = [dict(acc=a, lines=S.pay_dates_summary(S.account_steps(t, a, dates)[0], per_person=False))
                     for a in r.accounts if S.account_steps(t, a, dates)[1]]
        return render(request, "money.html", trip=t, r=r, rows=rows, total_in=total_in, total_out=total_out,
                      plans=plans, reminders=_reminder_rows(t, r, people), pay_dates=dates, own_dates=own_dates,
                      dates_lines=S.pay_dates_summary(dates["etapes"]),
                      advances=advances, to_pay=to_pay, pending=pending, payer_label=S.payer_label, names=names(t), today=dt.date.today().isoformat(),
                      sel=request.query_params.get("p"), kind=request.query_params.get("k", "versement"),
                      prefill=request.query_params.get("a", ""), tab=tab, methods=S.PAY_METHODS,
                      cheques=sorted(S.cheques_to_deposit(t), key=lambda m: (m.date is None, m.date or dt.date.min, m.id)),
                      open_form=tab == "mouvements" and bool(request.query_params.get("k") or request.query_params.get("p")))

    @app.post("/v/{tid}/argent")
    async def money_add(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        pid = whole(f.get("participant"))
        amt = amount(f, "amount")
        kind = f.get("kind", "versement")
        if pid not in names(t) or not amt or amt <= 0 or kind not in ("versement", "remboursement"):
            ref = urlparse(request.headers.get("referer") or "")      # retour là où le formulaire a été rempli
            here = (ref.path + ("?" + ref.query if ref.query else "")) if ref.path.startswith(f"/v/{tid}/argent") \
                else f"/v/{tid}/argent/mouvements?k={kind}"
            return refuse(here, "Choisis une personne et un montant positif", "amount")
        who = names(t)[pid]
        date = S.parse_date(f.get("date"))
        same = S.duplicate_movement(t, pid, kind, amt, date)
        db.snapshot(f"{kind} de {E.fmt(amt)} ({who})")
        W.add_movement(t, pid, kind, amt, date=date, note=f.get("note"),     # pas de date d'office
                       method=S.pay_method(f.get("method")) or "")
        s.commit()
        dest = f.get("next") or f"/v/{tid}/argent"
        if not dest.startswith("/"):
            dest = f"/v/{tid}/argent"
        txt = f"Versement de {E.fmt(amt)} par {who} enregistré" if kind == "versement" else \
            f"Remboursement de {E.fmt(amt)} à {who} enregistré"
        if same is not None:                    # sans doute saisi deux fois : on prévient, « Défaire » est juste à côté
            txt += (f". ⚠️ Attention : un {kind} de {E.fmt(amt)} était déjà noté pour {who}"
                    + (f" (le {same.date:%d/%m/%Y})" if same.date else "")
                    + ". Si c'est le même, clique sur « Défaire »")
        return back(dest, txt)

    @app.post("/v/{tid}/argent/lot")
    async def money_batch(tid: int, request: Request, s: Session = Depends(get_s)):
        """Plusieurs lignes cochées dans « Reste à régler » : un versement (ou un remboursement) par ligne, du
        montant qu'il reste exactement à régler. Les montants sont recalculés ici, jamais lus dans le formulaire."""
        t = trip_or_404(s, tid)
        f = await request.form()
        kind = f.get("kind", "versement")
        if kind not in ("versement", "remboursement"):
            raise HTTPException(400, "Type de mouvement inconnu")
        r = S.compute_trip(t)
        todo = S.settle_amounts(r, kind, [whole(x) for x in f.getlist("qui")])
        if not todo:
            return refuse(f"/v/{tid}/argent", "Coche d'abord au moins une ligne" if not f.getlist("qui") else
                          "Ces lignes sont déjà réglées : rien n'a été enregistré")
        total = sum(c for _, c in todo)
        received = kind == "versement"
        db.snapshot(f"{len(todo)} {kind}s notés d'un coup ({E.fmt(total)})" if len(todo) > 1 else
                    f"{kind} de {E.fmt(total)} ({todo[0][0].name})")
        W.settle_batch(t, r, kind, [acc.referent for acc, _ in todo], date=S.parse_date(f.get("date")),
                       method=S.pay_method(f.get("method")) or "", note=(f.get("note") or "").strip())
        s.commit()
        who = ", ".join(f"{acc.name} {E.fmt(c)}" for acc, c in todo[:6]) + ("…" if len(todo) > 6 else "")
        if len(todo) == 1:
            return back(f"/v/{tid}/argent", f"{'Versement' if received else 'Remboursement'} de {E.fmt(total)} "
                                             f"{'par' if received else 'à'} {todo[0][0].name} enregistré")
        return back(f"/v/{tid}/argent", f"{len(todo)} {'versements reçus' if received else 'remboursements faits'} "
                                         f"enregistrés, {E.fmt(total)} en tout : {who}")

    # ------------------------------------------------------------------ dates de paiement, relances
    def _reminder_rows(t, r, people):
        """Les comptes en retard, avec le message de relance prêt à partir (WhatsApp, e-mail, copier)."""
        rows = []
        for a, plan in S.late_accounts(t, r):
            ref = people[a.referent]
            msg = S.reminder_message(t, r, a, plan)
            num = S.whatsapp_number(ref.phone)
            subject = f"Rappel de paiement — voyage « {t.name} »"
            rows.append(dict(acc=a, ref=ref, plan=plan, msg=msg, num=num, subject=subject, key=a.key, pdf=None,
                             wa=f"https://wa.me/{num}?text={quote(msg)}" if num else f"https://wa.me/?text={quote(msg)}",
                             mail=f"mailto:{quote(ref.email or '', safe='@.+-_')}?subject={quote(subject)}&body={quote(msg)}"))
        return rows

    def _dates_account(r, pid):
        """Le compte (personne ou famille) auquel appartient cette personne, ou None."""
        return next((a for a in r.accounts if pid in {m.id for m in a.members}), None)

    @app.get("/v/{tid}/argent/dates", response_class=HTMLResponse)
    def pay_dates_page(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        r = S.compute_trip(t)
        dates = S.pay_dates(t)
        acc = _dates_account(r, whole(request.query_params.get("pour"))) if request.query_params.get("pour") else None
        if request.query_params.get("pour") and (acc is None or acc.holds_cash):
            return refuse(f"/v/{tid}/argent/dates", "Choisis une personne ou une famille")
        if acc is not None:
            steps, own = S.account_steps(t, acc, dates)
            steps = steps if own else []
        else:
            steps = dates["etapes"]
        others = [dict(acc=a, lines=S.pay_dates_summary(S.account_steps(t, a, dates)[0], per_person=False))
                  for a in r.accounts if S.account_steps(t, a, dates)[1]]
        return render(request, "pay_dates.html", trip=t, r=r, acc=acc, steps=steps, others=others,
                      rows=min(S.PAY_STEPS_MAX, max(3, len(steps) + 2)), trip_lines=S.pay_dates_summary(dates["etapes"]),
                      choices=[a for a in r.accounts if not a.holds_cash and not S.account_steps(t, a, dates)[1]])

    @app.post("/v/{tid}/argent/dates")
    async def pay_dates_save(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        r = S.compute_trip(t)
        acc = _dates_account(r, whole(f.get("pour"))) if f.get("pour") else None
        here = f"/v/{tid}/argent/dates" + (f"?pour={acc.referent}" if acc else "")
        if f.get("pour") and (acc is None or acc.holds_cash):
            return refuse(f"/v/{tid}/argent/dates", "Choisis une personne ou une famille")
        steps = []
        for i in range(S.PAY_STEPS_MAX):
            name, raw_date, raw_amount = (str(f.get(f"{k}_{i}") or "").strip() for k in ("nom", "date", "montant"))
            if not (name or raw_date or raw_amount):
                continue                                             # ligne vide : pas de date à cet endroit
            date = S.parse_date(raw_date)
            if date is None:
                return refuse(here, f"Indique la date de « {name or 'ce paiement'} »", f"date_{i}")
            steps.append((name, date, amount(f, f"montant_{i}") or None))
        if not steps:
            return refuse(here, "Indique au moins une date (ou clique sur « Ne plus utiliser de dates »)", "date_0")
        who = f"de {acc.name}" if acc else "du voyage"
        db.snapshot(f"dates de paiement {who}")
        kept = W.set_account_pay_dates(t, acc, steps) if acc else W.set_pay_dates(t, steps)
        s.commit()
        late = S.late_accounts(t, S.compute_trip(t))
        msg = f"Dates de paiement {who} enregistrées : " + " ; ".join(S.pay_dates_summary(kept, per_person=acc is None))
        if late:
            msg += f". {len(late)} paiement{'s' if len(late) > 1 else ''} en retard : le message de relance est prêt"
        return back(f"/v/{tid}/argent" + ("#relances" if late else "#dates"), msg)

    @app.post("/v/{tid}/argent/dates/retirer")
    async def pay_dates_clear(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        r = S.compute_trip(t)
        acc = _dates_account(r, whole(f.get("pour"))) if f.get("pour") else None
        if f.get("pour") and acc is None:
            return refuse(f"/v/{tid}/argent/dates", "Choisis une personne ou une famille")
        if not (S.account_steps(t, acc)[1] if acc else S.pay_dates(t)["etapes"]):
            return back(f"/v/{tid}/argent#dates", "Rien à changer")
        db.snapshot(f"dates de paiement {'de ' + acc.name if acc else 'du voyage'} retirées")
        if acc:
            W.set_account_pay_dates(t, acc, [])
        else:
            W.set_pay_dates(t, [])
        s.commit()
        if acc:
            return back(f"/v/{tid}/argent/dates", f"{acc.name} suit de nouveau les dates du voyage")
        return back(f"/v/{tid}/argent#dates", "Dates de paiement du voyage retirées : plus de retard signalé"
                    + (" (sauf pour ceux qui ont leurs propres dates)" if S.has_pay_dates(t) else ""))

    @app.post("/v/{tid}/argent/cheques-deposes")
    def cheques_all_deposited(tid: int, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        todo = S.cheques_to_deposit(t)
        if not todo:
            return back(f"/v/{tid}/argent/mouvements", "Aucun chèque à déposer")
        db.snapshot(f"{len(todo)} chèque{'s' if len(todo) > 1 else ''} déposé{'s' if len(todo) > 1 else ''} à la banque")
        for m in todo:
            m.deposited = True
        s.commit()
        return back(f"/v/{tid}/argent/mouvements",
                    f"{len(todo)} chèques notés comme déposés" if len(todo) > 1 else "Chèque noté comme déposé")

    @app.post("/v/{tid}/argent/{mid}/depose")
    async def cheque_deposited(tid: int, mid: int, request: Request, s: Session = Depends(get_s)):
        trip_or_404(s, tid)
        m = s.get(M.Movement, mid)
        if not m or m.trip_id != tid:
            raise HTTPException(404, "Mouvement introuvable")
        f = await request.form()
        done = f.get("etat", "1") != "0"
        if m.method != "cheque" or m.kind != "versement":
            return refuse(f"/v/{tid}/argent/mouvements", "Ce mouvement n'est pas un chèque reçu")
        db.snapshot(f"chèque de {m.participant.name} ({E.fmt(m.amount)}) "
                    + ("déposé à la banque" if done else "plus noté comme déposé"))
        m.deposited = done
        s.commit()
        return back(f"/v/{tid}/argent/mouvements", f"Chèque de {m.participant.name} ({E.fmt(m.amount)}) "
                    + ("noté comme déposé" if done else "remis dans les chèques à déposer"))

    @app.post("/v/{tid}/argent/{mid}")
    async def money_edit(tid: int, mid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        m = s.get(M.Movement, mid)
        if not m or m.trip_id != tid:
            raise HTTPException(404, "Mouvement introuvable")
        f = await request.form()
        pid = whole(f.get("participant"), m.participant_id)
        amt = amount(f, "amount")
        kind = f.get("kind", m.kind)
        if pid not in names(t) or not amt or amt <= 0 or kind not in ("versement", "remboursement"):
            return refuse(f"/v/{tid}/argent#m{mid}", "Choisis une personne et un montant positif", "amount")
        db.snapshot(f"modification d'un {m.kind} de {m.participant.name}")
        m.participant_id, m.amount, m.kind = pid, amt, kind
        m.date = S.parse_date(f.get("date")) or m.date
        m.note = (f.get("note") or "").strip()
        if "method" in f:
            m.method = S.pay_method(f.get("method")) or ""
        if "deposited_shown" in f:                       # la case « déposé » était affichée (chèque reçu)
            m.deposited = bool(f.get("deposited"))
        if m.method != "cheque" or m.kind != "versement":
            m.deposited = False
        s.commit()
        return back(f"/v/{tid}/argent", f"{'Versement' if kind == 'versement' else 'Remboursement'} de "
                                         f"{E.fmt(amt)} ({names(t)[pid]}) modifié")

    @app.post("/v/{tid}/argent/{mid}/supprimer")
    def money_delete(tid: int, mid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        m = s.get(M.Movement, mid)
        if not m or m.trip_id != tid:
            raise HTTPException(404, "Mouvement introuvable")
        db.snapshot(f"suppression d'un {m.kind} de {m.participant.name}")
        W.delete_movement(s, t, m)
        s.commit()
        ref = urlparse(request.headers.get("referer") or f"/v/{tid}/argent").path
        return back(ref, "Mouvement supprimé")
