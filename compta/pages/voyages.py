"""Accueil, « Où j'en suis », créer / modifier / supprimer un voyage, clôturer, dupliquer, voyage d'exemple.

Partie de l'application web (voir compta/web.py, qui installe ces pages dans l'ordre).
"""
from __future__ import annotations

import datetime as dt

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from .. import engine as E
from .. import models as M
from .. import services as S
from .. import transfer as TR
from .. import writes as W


def install(site):
    """Installe ces pages dans l'application. `site` : ce que les pages ont en commun (voir web.py)."""
    app = site.app
    back = site.back
    db = site.db
    drafts = site.drafts
    get_s = site.get_s
    refuse = site.refuse
    render = site.render
    trip_or_404 = site.trip_or_404

    # ------------------------------------------------------------------ voyages
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, s: Session = Depends(get_s)):
        trips = s.query(M.Trip).order_by(M.Trip.is_demo, M.Trip.created.desc(), M.Trip.id.desc()).all()   # récents
        # d'abord ; le voyage d'exemple en dernier
        cards = []
        for t in trips:
            r = S.compute_trip(t)
            cards.append((t, r))
        q = request.query_params
        stored = S.asso_name(s)
        plans = [S.plan_summary(p) for p in s.query(M.Plan).order_by(M.Plan.created.desc(), M.Plan.id.desc()).all()]
        soon = S.upcoming(s, site.today())
        return render(request, "index.html", cards=cards, plans=plans, soon=soon, data_dir=db.home, open_plan=q.get("preparer") == "1",
                      asso_default="" if stored == "l'asso" else stored,
                      diving_default=bool(trips) and any(S.is_diving(t_) for t_ in trips[:1] if not t_.is_demo),
                      open_new=q.get("nouveau") == "1", open_import=q.get("importer") == "1")

    @app.post("/voyages")
    async def create_trip(request: Request, s: Session = Depends(get_s)):
        f = await request.form()
        name = (f.get("name") or "").strip()
        if not name:
            return refuse("/?nouveau=1", "Donne un nom au voyage", "name")
        holder = " ".join(str(f.get("cashier_name") or "").split())[:200]
        if f.get("caisse") == "personne" and not holder:
            return refuse("/?nouveau=1", "Écris le nom de la personne qui tient la caisse", "cashier_name")
        db.snapshot(f"création du voyage « {name} »")
        t = W.create_trip(s, name, place=f.get("place", ""), organizer=f.get("organizer", ""),
                          start=S.parse_date(f.get("start")), end=S.parse_date(f.get("end")),
                          diving=bool(f.get("plongee")))
        who = ""
        if f.get("caisse") == "personne":           # pas d'association : une personne du voyage tient la caisse
            p = W.add_participant(s, t, holder, roles=[S.DIVER] if S.is_diving(t) else [])
            t.cashier_id = p.id
            who = f" {holder} tient la caisse."
        elif "asso_name" in f and str(f.get("asso_name") or "").strip():
            S.set_trip_asso_name(t, str(f.get("asso_name")), s)
        s.commit()
        return back(f"/v/{t.id}/participants?ajout=1#ajout", f"Voyage créé.{who} Ajoute maintenant les participants.")

    @app.post("/v/{tid}/jeu-plongee")
    def add_diving_set(tid: int, s: Session = Depends(get_s)):
        """Ajoute au voyage les rôles Plongeur / Accompagnant et les catégories de plongée."""
        t = trip_or_404(s, tid)
        if S.is_diving(t) and all(E.sort_key(n) in {E.sort_key(c.name) for c in t.categories}
                                  for n, _ in S.DIVING_CATEGORIES):
            return back(f"/v/{tid}/participants/organisation#roles", "Ce voyage a déjà les réglages de plongée")
        db.snapshot("réglages de plongée ajoutés")
        added = S.add_diving_set(t)
        s.commit()
        return back(f"/v/{tid}/participants/organisation#roles",
                    "Réglages de plongée ajoutés : rôles Plongeur et Accompagnant"
                    + (f", catégories {', '.join(added)}" if added else ""))

    @app.post("/v/{tid}/modifier")
    async def edit_trip(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        db.snapshot(f"modification du voyage « {t.name} »")
        t.name = (f.get("name") or t.name).strip()
        t.place = f.get("place", "").strip()
        t.organizer = f.get("organizer", "").strip()
        if "note" in f:
            t.note = (f.get("note") or "").strip()
        t.start = S.parse_date(f.get("start"))
        t.end = S.parse_date(f.get("end"))
        s.commit()
        return back(f"/v/{tid}", "Voyage modifié")

    # ------------------------------------------------------------------ clôturer, rouvrir, dupliquer
    @app.post("/v/{tid}/cloturer")
    def trip_close(tid: int, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        db.snapshot(f"clôture du voyage « {t.name} »")
        S.close_trip(t)
        s.commit()
        return back(f"/v/{tid}", "Voyage clôturé : ses comptes sont figés. Tu peux toujours le consulter, "
                                 "l'exporter, et le rouvrir si besoin.")

    @app.post("/v/{tid}/rouvrir")
    def trip_reopen(tid: int, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        if not S.is_closed(t):
            return back(f"/v/{tid}", "Ce voyage est déjà ouvert")
        db.snapshot(f"réouverture du voyage « {t.name} »")
        S.reopen_trip(t)
        s.commit()
        return back(f"/v/{tid}", "Voyage rouvert : tu peux de nouveau le modifier")

    @app.get("/v/{tid}/dupliquer", response_class=HTMLResponse)
    def trip_duplicate_page(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        wd = {w.participant_id for w in t.withdrawals}
        return render(request, "duplicate.html", trip=t, r=S.compute_trip(t),
                      proposed=S.next_trip_name(t.name, [x.name for x in s.query(M.Trip).all()]),
                      n_people=len(t.participants), n_withdrawn=len(wd), n_expenses=len(t.expenses))

    @app.post("/v/{tid}/dupliquer")
    async def trip_duplicate(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        name = (f.get("name") or "").strip()[:200]
        if not name:
            return refuse(f"/v/{tid}/dupliquer", "Donne un nom au nouveau voyage", "name")
        if any(E.sort_key(x.name) == E.sort_key(name) for x in s.query(M.Trip).all()):
            return refuse(f"/v/{tid}/dupliquer", f"Un voyage s'appelle déjà « {name} » : choisis un autre nom", "name")
        start, end = S.parse_date(f.get("start")), S.parse_date(f.get("end"))
        if start and end and end < start:
            return refuse(f"/v/{tid}/dupliquer", "La date de fin est avant la date de début", "end")
        people, expenses = bool(f.get("participants")), bool(f.get("depenses"))
        db.snapshot(f"voyage « {name} » créé à partir de « {t.name} »")
        new = TR.duplicate_trip(s, t, name, start, end, with_people=people, with_expenses=expenses)
        s.commit()
        S.store_draws(new)
        s.commit()
        parts = []
        if people:
            n = len(new.participants)
            parts.append(f"{n} participant{'s' if n > 1 else ''}")
        if expenses:
            n = len(new.expenses)
            parts.append(f"{n} dépense{'s' if n > 1 else ''} reprise{'s' if n > 1 else ''} comme montant{'s' if n > 1 else ''} prévu{'s' if n > 1 else ''}")
        return back(f"/v/{new.id}", f"Voyage « {new.name} » créé à partir de « {t.name} »"
                    + (" : " + ", ".join(parts) if parts else "") + ". Rien n'a changé dans l'ancien voyage.")

    @app.post("/v/{tid}/supprimer")
    def delete_trip(tid: int, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        db.snapshot(f"suppression du voyage « {t.name} »")
        _delete_trip(s, t)
        s.commit()
        # ses brouillons IA sont mis de côté : un nouveau voyage peut recevoir le même numéro, il ne doit pas en hériter
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        for f_ in drafts().glob(f"voyage-{tid}-*"):
            try:
                f_.rename(drafts() / f"supprime-{stamp}-{f_.name}")
            except OSError:
                pass
        return back("/", f"Voyage « {t.name} » supprimé (tu peux encore annuler)")

    # ------------------------------------------------------------------ voyage d'exemple (Hendaye 2026 – démo)
    @app.post("/exemple/ouvrir")
    def example_open(s: Session = Depends(get_s)):
        from .. import transfer as TR
        t = s.query(M.Trip).filter(M.Trip.is_demo.is_(True)).first()
        if t is not None:
            return back(f"/v/{t.id}", "Le voyage d'exemple est déjà là")
        db.snapshot("ouverture du voyage d'exemple")
        t = TR.import_example(s, db.receipts_dir)
        s.commit()
        return back(f"/v/{t.id}", "Voici un voyage d'exemple, entièrement fictif : explore-le et modifie-le librement.")

    def _demo_or_404(s, tid):
        t = trip_or_404(s, tid)
        if not t.is_demo:
            raise HTTPException(404, "Ce n'est pas le voyage d'exemple")
        return t

    @app.post("/v/{tid}/exemple/retirer")
    def example_remove(tid: int, s: Session = Depends(get_s)):
        t = _demo_or_404(s, tid)
        db.snapshot("retrait du voyage d'exemple")
        _delete_trip(s, t)
        s.commit()
        return back("/", "Voyage d'exemple retiré. Tu pourras le rouvrir depuis ⚙️ Réglages.")

    @app.post("/v/{tid}/exemple/recommencer")
    def example_reset(tid: int, s: Session = Depends(get_s)):
        from .. import transfer as TR
        t = _demo_or_404(s, tid)
        db.snapshot("voyage d'exemple remis à neuf")
        _delete_trip(s, t)
        s.flush()
        t = TR.import_example(s, db.receipts_dir)
        s.commit()
        return back(f"/v/{t.id}", "Voyage d'exemple remis à neuf.")

    def _delete_trip(s, t):
        W.forget_trip(s, t.id)               # un projet qui l'avait lancé n'y mène plus (et peut être relancé)
        for e in t.expenses:
            for l in list(e.receipt_links):
                s.delete(l)
        s.flush()
        for a in t.adjustments:
            s.delete(a)
        s.flush()
        for sub in t.subaccounts:
            s.delete(sub)
        s.flush()
        for e in t.expenses:
            s.delete(e)
        s.flush()
        s.delete(t)

    @app.get("/v/{tid}", response_class=HTMLResponse)
    def dashboard(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        r = S.compute_trip(t)
        asso_paid = sum(c for e in t.expenses if e.actual is not None and not e.is_income
                        for pid, c in e.splits() if pid is None)
        cat_totals = {}
        for e in t.expenses:
            cn = e.category.name if e.category else "Divers"
            cat_totals[cn] = cat_totals.get(cn, 0) + r.expense_totals.get(e.id, 0)
        prep = s.query(M.Plan).filter(M.Plan.trip_id == t.id).first()
        return render(request, "dashboard.html", trip=t, r=r, prep=prep, alerts=S.alerts(t, r), msum=S.money_summary(t, r),
                      ai_notes=S.ai_notes(t), start_steps=S.getting_started(t),
                      asso_paid=asso_paid, cat_totals=cat_totals,
                      adv_total=sum(a["amount"] for a in S.advances(t) if not a["income"]),
                      withdrawn={w.participant_id for w in t.withdrawals})
