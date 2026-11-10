"""Dépenses : liste, formulaire, catégories et sous-catégories.

Partie de l'application web (voir compta/web.py, qui installe ces pages dans l'ordre).
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from .. import engine as E
from .. import models as M
from .. import services as S
from .. import writes as W


def install(site):
    """Installe ces pages dans l'application. `site` : ce que les pages ont en commun (voir web.py)."""
    FormError = site.FormError
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

    def _refused_files(*a, **k):                         # défini plus loin, dans pages/justificatifs.py
        return site._refused_files(*a, **k)

    def _store_receipt(*a, **k):                         # défini plus loin, dans pages/justificatifs.py
        return site._store_receipt(*a, **k)

    # ------------------------------------------------------------------ dépenses
    @app.get("/v/{tid}/depenses", response_class=HTMLResponse)
    def expenses(tid: int, request: Request, s: Session = Depends(get_s), tri: str = ""):
        t = trip_or_404(s, tid)
        r = S.compute_trip(t)
        groups = []
        order = lambda e: (not e.subcategory, E.sort_key(e.subcategory or ""), e.id)    # « Autres » en dernier
        for c in t.categories:
            es = sorted([e for e in t.expenses if e.category_id == c.id], key=order)
            groups.append((c, es))
        orphans = sorted([e for e in t.expenses if e.category_id is None], key=order)
        if orphans:
            groups.append((None, orphans))
        # sous-totaux par sous-catégorie ; dans une catégorie qui en a, les dépenses sans sous-catégorie forment « Autres »
        with_subs = {e.category_id for e in t.expenses if e.subcategory}
        subtotals = {}
        for e in t.expenses:
            if e.subcategory or e.category_id in with_subs:
                k = (e.category_id, e.subcategory or "")
                subtotals[k] = subtotals.get(k, 0) + r.expense_totals.get(e.id, 0)
        # contrôle : une dépense dont la répartition ne tombe pas juste (rare) est signalée sur sa ligne
        bad = {x["id"] for x in S.ledger(t, r)["expenses"] if not x["ok"] and not x["pending"]}
        # tri : par catégorie (comme d'habitude), ou toutes les dépenses en une seule liste
        flat = {"date": ("Toutes les dépenses, les plus récentes en haut",
                         lambda e: (e.date is None, -(e.date.toordinal() if e.date else 0), E.sort_key(e.label), e.id)),
                "montant": ("Toutes les dépenses, les plus grosses en haut",
                            lambda e: (-abs(r.expense_totals.get(e.id, 0)), E.sort_key(e.label), e.id)),
                "nom": ("Toutes les dépenses, de A à Z", lambda e: (E.sort_key(e.label), e.id))}.get(tri)
        if flat:
            groups, with_subs = [(None, sorted(t.expenses, key=flat[1]))], set()
        paid_by = {pid for e in t.expenses for pid, _ in e.splits()}
        return render(request, "expenses.html", trip=t, r=r, groups=groups, nb=len(t.participants), bad=bad,
                      who_label=S.who_label, who_detail=S.who_detail, payer_label=S.payer_label, names=names(t),
                      subtotals=subtotals, sort=tri if flat else "", flat_title=flat[0] if flat else None,
                      payers=[p for p in t.participants if p.id in paid_by], asso_pays=None in paid_by,
                      with_subs=with_subs)

    def _expense_form_ctx(t, e=None):
        weights = {s_.participant_id: s_.weight for s_ in e.shares} if e else {p.id: 1.0 for p in t.participants}
        fixed = {s_.participant_id: s_.fixed for s_ in e.shares if s_.fixed is not None} if e else {}
        split_mode = "egal"
        total = e.best_known() if e else 0
        free = {p: w for p, w in weights.items() if p not in fixed}
        rest = total - sum(fixed.values())
        amounts = dict(fixed)
        if rest > 0 and free:
            amounts.update(E.split_exact(rest, free))
        linked = {l.receipt_id for l in e.receipt_links} if e else set()
        rank = {S.ORGANIZER: 0, S.CO_ORGANIZER: 1}
        payers = sorted(t.participants, key=lambda p: min([rank.get(r, 9) for r in p.role_list] or [9]))
        from sqlalchemy.orm import object_session
        subcats = S.subcategories_for(object_session(t), t)
        def chip(label, members):
            return dict(label=label, ids=[p.id for p in members], names=", ".join(p.name for p in members))
        who_roles = [chip(v, S.with_role(t, rk)) for rk, v in S.trip_roles(t).items() if S.with_role(t, rk)]
        who_families = [chip(f_.name, f_.members) for f_ in t.families if f_.members]
        who_groups = [chip(g["name"], g["members"]) for g in S.trip_groups(t) if g["members"]]
        # centimes d'arrondi : le tirage enregistré, et combien chacun en a déjà payé ailleurs (tirage équitable)
        import json as _json
        r_ = S.compute_trip(t)
        inf = r_.cent_info.get(e.id) if e else None
        cents = dict(mode=(e.cents_mode or "") if e else "", who=inf["winners"] if inf and inf["drawn"] else [],
                     counts=_json.dumps({str(k): v for k, v in S.cent_counts(r_, skip=e.id if e else None).items()}))
        if inf and inf["drawn"] and e:
            amounts = {p: r_.share(p, e.id) for p in weights}          # exactement ce que donne le calcul
        # plus de « parts » : une part ≠ 1 (dépense d'avant, ou IA sans montant connu) s'affiche comme un montant
        # fixé 🔒 ; enregistrer la dépense la convertit pour de bon
        equal = E.split_exact(total, {p: 1 for p in weights}) if total else {}
        for p, w in list(weights.items()):
            if w != 1 and p not in fixed and p in amounts and abs(amounts[p] - equal.get(p, -10)) > 1:
                fixed[p] = amounts[p]
            weights[p] = 1.0
        return dict(trip=t, e=e, weights=weights, linked=linked, groups=S.trip_groups(t),
                    who_roles=who_roles, who_families=who_families, who_groups=who_groups, payers=payers, subcats=subcats,
                    split_mode=split_mode, amounts=amounts, fixed=fixed, cents=cents)

    @app.get("/v/{tid}/depenses/nouvelle", response_class=HTMLResponse)
    def new_expense(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        if not t.participants:
            return refuse(f"/v/{tid}/participants", "Ajoute d'abord les participants")
        return render(request, "expense_form.html", **_expense_form_ctx(t),
                      today=dt.date.today().isoformat())

    @app.get("/v/{tid}/depenses/{eid}/modifier", response_class=HTMLResponse)
    def edit_expense_form(tid: int, eid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        e = s.get(M.Expense, eid)
        if not e or e.trip_id != tid:
            raise HTTPException(404, "Dépense introuvable")
        return render(request, "expense_form.html", **_expense_form_ctx(t, e),
                      today=dt.date.today().isoformat())

    def _parse_shares(t, f) -> tuple:
        """Personnes cochées -> (parts, montants fixés 🔒 en centimes).
        Une case « fx_<id> » cochée = le montant « m_<id> » est fixé ; les autres se partagent le reste.
        (« split=montants » : ancien formulaire, tous les montants sont fixés.)"""
        weights, fixed = {}, {}
        all_fixed = f.get("split") == "montants"
        for p in t.participants:
            if not f.get(f"inc_{p.id}"):
                continue
            try:   # nombre de parts (1 par défaut ; 2 = double part…)
                w = float(E.to_weight(f.get(f"w_{p.id}") or "1"))
            except Exception:
                w = 1.0
            if all_fixed or f.get(f"fx_{p.id}"):
                try:
                    c_ = E.parse_amount(f.get(f"m_{p.id}"))
                except ValueError:
                    raise FormError(f"Montant invalide pour {p.name} : « {f.get(f'm_{p.id}')} ». Exemple : 12,50",
                                    f"m_{p.id}")
                if c_ is not None and c_ >= 0:
                    fixed[p.id] = c_
                    w = w if w > 0 else 1.0
            if w > 0:
                weights[p.id] = w
        return weights, fixed

    def _parse_weights(t, f) -> dict:
        return _parse_shares(t, f)[0]

    def weights_ok(t, f):
        return bool(_parse_weights(t, f))

    # ------------------------------------------------------------------ catégories
    def cat_or_404(t, cid):
        c = next((c for c in t.categories if c.id == cid), None)
        if c is None:
            raise HTTPException(404, "Catégorie introuvable")
        return c

    def clean_cat_name(name):
        return re.sub(r"\s+", " ", (name or "").strip())[:100]

    def add_cat_to_trip(t, name):
        t.categories.append(M.Category(name=name, audience="", position=len(t.categories)))

    @app.get("/v/{tid}/categories", response_class=HTMLResponse)
    def categories(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        have = {E.sort_key(c.name) for c in t.categories}
        diving = S.is_diving(t)                      # les catégories de plongée : seulement pour un voyage de plongée
        available = [n for n in S.category_library(s) if E.sort_key(n) not in have
                     and (diving or not S.is_diving_category(n))]
        custom = {E.sort_key(n) for n in S.category_library(s) if S.is_custom_category(s, n)}
        return render(request, "categories.html", trip=t, available=available, custom=custom, sort_key=E.sort_key,
                      subcats=S.subcategories_for(s, t))

    @app.post("/v/{tid}/categories/{cid}/sous-categories")
    async def add_subcat(tid: int, cid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        c = cat_or_404(t, cid)
        name = clean_cat_name((await request.form()).get("name"))
        if not name:
            return refuse(f"/v/{tid}/categories#c{cid}", "Donne un nom à la sous-catégorie")
        if E.sort_key(name) in {E.sort_key(x) for x in S.subcategories_for(s, t).get(c.name, [])}:
            return refuse(f"/v/{tid}/categories#c{cid}", f"« {name} » existe déjà dans « {c.name} »")
        db.snapshot(f"sous-catégorie « {name} » ajoutée à « {c.name} »")
        S.add_subcategory(s, c.name, name)
        s.commit()
        return back(f"/v/{tid}/categories#c{cid}", f"Sous-catégorie « {name} » ajoutée à « {c.name} »")

    @app.post("/v/{tid}/categories/{cid}/sous-categories/supprimer")
    async def del_subcat(tid: int, cid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        c = cat_or_404(t, cid)
        name = clean_cat_name((await request.form()).get("name"))
        db.snapshot(f"sous-catégorie « {name} » supprimée")
        S.remove_subcategory(s, c.name, name)
        used = [e for e in t.expenses if e.category_id == c.id and E.sort_key(e.subcategory or "") == E.sort_key(name)]
        for e in used:
            e.subcategory = ""
        s.commit()
        msg = f"Sous-catégorie « {name} » supprimée"
        if used:
            msg += f" ; ses {len(used)} dépense(s) restent dans « {c.name} »"
        return back(f"/v/{tid}/categories#c{cid}", msg)

    @app.post("/v/{tid}/categories")
    async def new_category(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        name = clean_cat_name((await request.form()).get("name"))
        if not name:
            return refuse(f"/v/{tid}/categories", "Donne un nom à la catégorie", "name")
        if any(E.sort_key(c.name) == E.sort_key(name) for c in t.categories):
            return refuse(f"/v/{tid}/categories", f"« {name} » est déjà dans ce voyage")
        if E.sort_key(name) in {E.sort_key(n) for n in S.category_library(s)}:
            return refuse(f"/v/{tid}/categories", f"« {name} » est déjà dans les catégories disponibles")
        db.snapshot(f"nouvelle catégorie « {name} »")
        S.add_to_library(s, name)          # dans les disponibles seulement : on l'ajoute au voyage d'un clic
        s.commit()
        return back(f"/v/{tid}/categories",
                    f"« {name} » créée dans les catégories disponibles : clique dessus pour l'ajouter à ce voyage")

    @app.post("/v/{tid}/categories/ajouter")
    async def take_category(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        name = clean_cat_name((await request.form()).get("name"))
        if not name or any(E.sort_key(c.name) == E.sort_key(name) for c in t.categories):
            return back(f"/v/{tid}/categories")
        db.snapshot(f"catégorie « {name} » ajoutée au voyage")
        add_cat_to_trip(t, name)
        s.commit()
        return back(f"/v/{tid}/categories", f"« {name} » ajoutée à ce voyage")

    @app.post("/v/{tid}/categories/{cid}/retirer")
    def drop_category(tid: int, cid: int, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        c = cat_or_404(t, cid)
        used = [e for e in t.expenses if e.category_id == c.id]
        target = None
        if used:
            target = next((o for o in t.categories if E.sort_key(o.name) == "divers" and o.id != c.id), None)
            if target is None:
                if E.sort_key(c.name) == "divers":
                    return refuse(f"/v/{tid}/categories", "« Divers » est utilisée par des dépenses : change d'abord "
                                                          "leur catégorie")
                db.snapshot(f"catégorie « {c.name} » retirée du voyage")
                target = M.Category(name="Divers", audience="", position=len(t.categories))
                t.categories.append(target)
                s.flush()
            else:
                db.snapshot(f"catégorie « {c.name} » retirée du voyage")
        else:
            db.snapshot(f"catégorie « {c.name} » retirée du voyage")
        for e in used:
            e.category_id = target.id
        t.categories.remove(c)
        s.delete(c)
        for i, o in enumerate(sorted(t.categories, key=lambda x: (x.position, x.id))):
            o.position = i
        s.commit()
        msg = f"« {c.name} » retirée de ce voyage"
        if used:
            msg += f" ; ses {len(used)} dépense(s) sont maintenant dans « Divers »"
        return back(f"/v/{tid}/categories", msg)

    @app.post("/v/{tid}/categories/oublier")
    async def forget_category(tid: int, request: Request, s: Session = Depends(get_s)):
        trip_or_404(s, tid)
        name = clean_cat_name((await request.form()).get("name"))
        if not S.is_custom_category(s, name):
            return refuse(f"/v/{tid}/categories", "Seules les catégories que tu as créées peuvent être supprimées")
        db.snapshot(f"catégorie « {name} » supprimée de la liste")
        S.remove_from_library(s, name)
        s.commit()
        return back(f"/v/{tid}/categories", f"« {name} » n'est plus proposée (les voyages qui l'utilisent la gardent)")

    @app.post("/v/{tid}/depenses/apercu", response_class=HTMLResponse)
    async def expense_preview(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        weights, fixed = _parse_shares(t, f)
        def total_of(v):          # « 80,12 + 69,10 » accepté (plusieurs tickets)
            parts = [p_ for p_ in str(v or "").split("+") if p_.strip()]
            return sum(E.parse_amount(p_) or 0 for p_ in parts) if parts else None
        try:
            planned = total_of(f.get("planned"))
            actual = total_of(f.get("actual"))
        except ValueError:
            return HTMLResponse('<p class="muted">Montant invalide.</p>')
        amt = actual if actual is not None else planned
        if not amt:
            return HTMLResponse('<p class="muted">Indique un montant pour voir la part de chacun.</p>')
        if not weights:
            return HTMLResponse('<p class="warn-text">Coche au moins une personne.</p>')
        kind = "recette" if f.get("kind") == "recette" else "depense"
        r = E.compute([E.Participant(p.id, p.name, p.family_id, bool(p.companion)) for p in t.participants],
                      [E.Expense(0, f.get("label") or "Cette dépense", "", planned, actual, None, weights, kind,
                                 fixed=fixed)], rounding=t.rounding or E.ROUND_PARTICIPANTS)
        return render(request, "_expense_preview.html", trip=t, r=r, amount=amt, weights=weights)

    @app.post("/v/{tid}/depenses")
    @app.post("/v/{tid}/depenses/{eid}")
    async def save_expense(tid: int, request: Request, eid: int | None = None, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        label = (f.get("label") or "").strip()
        if not label:
            return refuse(request.headers.get("referer") or f"/v/{tid}/depenses/nouvelle",
                          "Donne un nom à la dépense", "label")
        # Plusieurs tickets : lignes « ticket_amount / ticket_date », ou « 80,12 + 69,10 » tapé dans le montant
        tickets = []
        for a_, d_ in zip(f.getlist("ticket_amount"), f.getlist("ticket_date")):
            c_ = amount({"x": a_}, "x")
            if c_:
                tickets.append((c_, S.parse_date(d_)))
        raw_actual = str(f.get("actual") or "")
        if not tickets and "+" in raw_actual.lstrip("+"):
            tickets = [(c_, None) for c_ in (amount({"x": p_}, "x", "actual") for p_ in raw_actual.split("+")) if c_]
        raw_planned = str(f.get("planned") or "")
        planned = (sum(amount({"x": p_}, "x", "planned") or 0 for p_ in raw_planned.split("+")) or None) \
            if "+" in raw_planned.lstrip("+") else amount(f, "planned")
        actual = sum(c_ for c_, _ in tickets) if tickets else amount(f, "actual")
        ref = request.headers.get("referer", f"/v/{tid}/depenses/nouvelle")
        if not weights_ok(t, f):
            return refuse(ref, "Coche au moins une personne pour « Pour qui ? »")
        if planned is None and actual is None and not f.get("pending"):
            return refuse(ref, "Indique au moins un montant (prévu ou réel), ou coche « Montant pas encore connu »",
                          "actual")
        weights, fixed = _parse_shares(t, f)
        if fixed:
            total_ = (actual if actual is not None else planned) or 0
            if not total_:
                return refuse(ref, "Indique le montant de la dépense avant de fixer des montants")
            refund_ = (amount(f, "refund") or 0) if f.get("has_refund") else 0
            total_ = max(0, total_ - refund_)          # c'est le coût, remboursement déduit, qui est partagé
            got = sum(fixed.values())
            if got > total_:
                what = "le coût à partager" if refund_ else "la dépense"
                return refuse(ref, f"Les montants fixés font {E.fmt(got)}, plus que {what} ({E.fmt(total_)})")
            if len(fixed) == len(weights) and got != total_:
                return refuse(ref, f"Il reste {E.fmt(total_ - got)} à répartir : tous les montants sont fixés. "
                                   f"Laisse au moins une personne sans montant fixé, ou corrige les montants.")
        payments = []
        if f.get("payer_kind") == "multi":
            for i in range(20):
                a_ = amount(f, f"ma_{i}")
                if a_:
                    who = f.get(f"mp_{i}") or "asso"
                    pid_ = None if who == "asso" else int(who)
                    if pid_ is not None and pid_ not in names(t):
                        raise FormError("Payeur inconnu")
                    payments.append((pid_, a_))
            if not payments:
                return refuse(ref, "Indique combien chaque payeur a payé")
            total_ = (actual if actual is not None else planned) or 0
            if total_ and sum(c for _, c in payments) > total_:
                return refuse(ref, f"Les payeurs totalisent {E.fmt(sum(c for _, c in payments))}, "
                                   f"plus que le montant ({E.fmt(total_)})")
        cat = f.get("category")
        if cat == "__new__":
            cat = f.get("category_new")
        sub = f.get("subcategory") or ""
        if sub == "__new__":
            sub = f.get("subcategory_new") or ""
        refund = (amount(f, "refund") if f.get("has_refund") else None) or None
        if refund and refund > ((actual if actual is not None else planned) or 0):
            raise FormError("Le montant remboursé dépasse le montant de la dépense", "refund")
        who_ = f.get("refund_to") or "asso"
        payer = f.get("payer") or "asso"
        values = dict(label=label, category=cat, subcategory=sub,
                      kind="recette" if f.get("kind") == "recette" else "depense", planned=planned, actual=actual,
                      tickets=tickets, date=S.parse_date(f.get("date")),
                      payer_id=None if (payer == "asso" or payments) else whole(payer, -1), payments=payments,
                      weights=weights, fixed=fixed, refund=refund,
                      refund_to_id=None if (who_ == "asso" or not refund) else whole(who_, -1),
                      note=f.get("note", ""), pending=bool(f.get("pending")), reviewed=True)
        W.check_expense(t, **values)             # avant la copie « Défaire » : refusé, rien n'est écrit
        same_exp, e = None, None
        if eid:
            e = s.get(M.Expense, eid)
            if not e or e.trip_id != tid:
                raise HTTPException(404, "Dépense introuvable")
            db.snapshot(f"modification de la dépense « {e.label} »")
        else:
            same_exp = S.duplicate_expense(t, label, actual if actual is not None else planned)
            db.snapshot(f"ajout de la dépense « {label} »")
        e = W.save_expense(s, t, e, **values)     # le même chemin que l'IA : mêmes règles, mêmes lignes
        if "cents_mode" in f:          # centimes d'arrondi : le choix et le tirage vus à l'écran
            e.cents_mode = f.get("cents_mode") if f.get("cents_mode") in (E.ROUND_PARTICIPANTS, E.ROUND_ASSO) else ""
            e.cents_who = ",".join(str(x) for x in S.cents_ids(f.get("cents_who")))
        # justificatifs existants
        wanted = set(wholes(f.getlist("receipts")))
        s.query(M.ReceiptLink).filter_by(expense_id=e.id).delete()
        for rid in wanted:
            s.add(M.ReceiptLink(receipt_id=rid, expense_id=e.id))
        # nouveaux fichiers
        not_joined = []
        for up in f.getlist("files"):
            if getattr(up, "filename", None):
                rec = await _store_receipt(s, t, up)
                if rec is None:                  # ni photo ni PDF : la dépense est enregistrée, sans ce fichier
                    not_joined.append(Path(up.filename).name)
                    continue
                s.add(M.ReceiptLink(receipt_id=rec.id, expense_id=e.id))
        s.commit()
        if not_joined:
            return back(f"/v/{tid}/depenses/{e.id}/modifier", f"« {label} » enregistrée, mais " + _refused_files(not_joined),
                        err=True)
        twice = ""
        if same_exp is not None:                # même nom, même montant : sans doute saisie deux fois
            twice = (f". ⚠️ Attention : une dépense « {same_exp.label} » de {E.fmt(same_exp.best_known())} existait "
                     "déjà. Si c'est la même, clique sur « Défaire »")
        if f.get("again"):
            return back(f"/v/{tid}/depenses/nouvelle", f"« {label} » enregistrée{twice}. Dépense suivante :")
        return back(f"/v/{tid}/depenses#e{e.id}", f"« {label} » enregistrée{twice}")

    @app.post("/v/{tid}/depenses/{eid}/verifiee")
    async def expense_checked(tid: int, eid: int, s: Session = Depends(get_s)):
        """« C'est bon » : la dépense à vérifier ne concerne pas la (les) personne(s) signalée(s)."""
        trip_or_404(s, tid)
        e = s.get(M.Expense, eid)
        if not e or e.trip_id != tid:
            raise HTTPException(404, "Dépense introuvable")
        e.checks.clear()
        s.commit()
        return back(f"/v/{tid}/depenses#e{eid}", f"« {e.label} » vérifiée")

    @app.post("/v/{tid}/depenses/{eid}/supprimer")
    async def delete_expense(tid: int, eid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        e = s.get(M.Expense, eid)
        if not e or e.trip_id != tid:
            raise HTTPException(404, "Dépense introuvable")
        db.snapshot(f"suppression de la dépense « {e.label} »")
        label = W.delete_expense(s, t, e)
        s.commit()
        nxt = (await request.form()).get("next") or ""
        return back(nxt if nxt.startswith(f"/v/{tid}/") else f"/v/{tid}/depenses", f"« {label} » supprimée")
