"""Préparer un voyage : créer un projet, ses postes et ses offres, retenir une offre par poste, et voir le budget,
le prix par personne, la marge et le prix annoncé. Le calcul est dans compta/preparation.py, les écritures dans
compta/writes.py.

Partie de l'application web (voir compta/web.py, qui installe ces pages dans l'ordre).
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from markupsafe import escape
from sqlalchemy.orm import Session

from .. import engine as E
from .. import models as M
from .. import preparation as P
from .. import services as S
from .. import writes as W

def install(site):
    """Installe ces pages dans l'application. `site` : ce que les pages ont en commun (voir web.py)."""
    app = site.app
    back = site.back
    db = site.db
    get_s = site.get_s
    refuse = site.refuse
    render = site.render
    FormError = site.FormError

    # ------------------------------------------------------------------ lire un formulaire
    def plan_or_404(s, pid: int) -> M.Plan:
        p = s.get(M.Plan, pid)
        if not p:
            raise HTTPException(404, "Ce voyage en préparation n'existe pas (ou plus).")
        return p

    def item_or_404(plan, iid: int) -> M.PlanItem:
        it = next((i for i in plan.items if i.id == iid), None)
        if it is None:
            raise HTTPException(404, "Ce poste n'existe pas (ou plus).")
        return it

    def offer_or_404(plan, oid: int) -> M.PlanOffer:
        o = next((x for x in plan.offers if x.id == oid), None)
        if o is None:
            raise HTTPException(404, "Cette offre n'existe pas (ou plus).")
        return o

    def number(f, key, what: str, empty=None):
        """Nombre entier tapé dans un formulaire (participants, places, nombre de fois)."""
        v = str(f.get(key) or "").strip()
        if not v:
            if empty is not None:
                return empty
            raise FormError(f"Indique {what}.", key)
        if not v.lstrip("-").isdigit():
            raise FormError(f"« {v} » : écris un nombre entier, par exemple 10.", key)
        return int(v)

    def euros(f, key):
        """Montant en euros tapé (« 1 250,50 ») -> centimes ; vide -> None. Le signe est vérifié par writes.py."""
        try:
            return E.parse_amount(f.get(key))
        except ValueError:
            raise FormError(f"Montant invalide : « {f.get(key)} ». Exemple : 210 ou 12,50", key)

    def percent(f, key):
        """Pourcentage tapé (« 5 » ou « 5,5 ») -> centièmes de % (500, 550) ; vide -> None."""
        raw = str(f.get(key) or "").replace("%", "")
        try:
            return E.parse_amount(raw)
        except ValueError:
            raise FormError(f"Pourcentage invalide : « {f.get(key)} ». Exemple : 5 ou 5,5", key)

    def write(s, label: str, fn, err_url: str, check=None):
        """Une modification : `check` (sans rien écrire) d'abord, puis la copie « Défaire », puis `fn`. Refusée :
        retour au formulaire avec le message près du champ, et rien n'est gardé (ni écrit, ni copie)."""
        try:
            if check:
                check()
        except W.WriteError as e:
            return None, refuse(err_url, str(e), e.field)
        db.snapshot(label)
        try:
            out = fn()
        except W.WriteError as e:
            s.rollback()
            db.drop_last_snapshot()
            return None, refuse(err_url, str(e), e.field)
        s.commit()
        return out, None

    def page_url(plan, anchor: str = "") -> str:
        return f"/preparer/{plan.id}" + (f"#{anchor}" if anchor else "")

    # ------------------------------------------------------------------ créer, modifier, supprimer un projet
    @app.post("/preparer")
    async def plan_create(request: Request, s: Session = Depends(get_s)):
        f = await request.form()
        values = dict(name=f.get("name"), people=number(f, "people", "le nombre de participants"),
                      place=f.get("place") or "", start=S.parse_date(f.get("start")), end=S.parse_date(f.get("end")))
        name = " ".join(str(values["name"] or "").split())
        plan, err = write(s, f"création du voyage en préparation « {name} »", lambda: W.save_plan(s, **values),
                          "/?preparer=1", check=lambda: W.check_plan(**values))
        if err:
            return err
        return back(page_url(plan), "Projet créé. Ajoute maintenant les postes de dépense (transport, hébergement…).")

    @app.post("/preparer/{pid}/modifier")
    async def plan_edit(pid: int, request: Request, s: Session = Depends(get_s)):
        plan = plan_or_404(s, pid)
        f = await request.form()
        fields = dict(name=f.get("name"), place=f.get("place") or "", start=S.parse_date(f.get("start")),
                      end=S.parse_date(f.get("end")), note=f.get("note") or "")
        _, err = write(s, f"modification du voyage en préparation « {plan.name} »",
                       lambda: W.update_plan(s, plan, **fields), page_url(plan, "modifier"),
                       check=lambda: W.check_plan(**W.plan_values(plan, **fields)))
        return err or back(page_url(plan), "Projet modifié")

    @app.post("/preparer/{pid}/supprimer")
    def plan_delete(pid: int, s: Session = Depends(get_s)):
        plan = plan_or_404(s, pid)
        name, _ = write(s, f"suppression du voyage en préparation « {plan.name} »", lambda: W.delete_plan(s, plan), "/")
        return back("/", f"Projet « {name} » supprimé (tu peux encore annuler)")

    @app.post("/preparer/{pid}/participants")
    async def plan_people(pid: int, request: Request, s: Session = Depends(get_s)):
        plan = plan_or_404(s, pid)
        n = number(await request.form(), "people", "le nombre de participants")
        _, err = write(s, f"« {plan.name} » : {n} personne{'s' if n > 1 else ''}",
                       lambda: W.update_plan(s, plan, people=n), page_url(plan, "resultat"),
                       check=lambda: W.check_plan(**W.plan_values(plan, people=n)))
        return err or back(page_url(plan, "resultat"), f"Calculé pour {n} personne{'s' if n > 1 else ''}")

    @app.post("/preparer/{pid}/marge")
    async def plan_margin(pid: int, request: Request, s: Session = Depends(get_s)):
        plan = plan_or_404(s, pid)
        f = await request.form()
        kind = P.EUROS if f.get("margin_kind") == P.EUROS else P.PERCENT
        value = (euros(f, "margin_value") if kind == P.EUROS else percent(f, "margin_value")) or 0
        shown = E.fmt(value) + " par personne" if kind == P.EUROS else S.plan_percent(value)
        _, err = write(s, f"« {plan.name} » : marge de {shown}",
                       lambda: W.update_plan(s, plan, margin_kind=kind, margin_value=value), page_url(plan, "resultat"),
                       check=lambda: W.check_plan(**W.plan_values(plan, margin_kind=kind, margin_value=value)))
        return err or back(page_url(plan, "resultat"), f"Marge : {shown}")

    @app.post("/preparer/{pid}/prix-annonce")
    async def plan_announced(pid: int, request: Request, s: Session = Depends(get_s)):
        plan = plan_or_404(s, pid)
        value = euros(await request.form(), "announced")
        label = f"« {plan.name} » : prix annoncé " + (E.fmt(value) if value is not None else "retiré")
        _, err = write(s, label, lambda: W.update_plan(s, plan, announced=value), page_url(plan, "resultat"),
                       check=lambda: W.check_plan(**W.plan_values(plan, announced=value)))
        return err or back(page_url(plan, "resultat"),
                           f"Prix annoncé : {E.fmt(value)}" if value is not None else "Prix annoncé retiré")

    # ------------------------------------------------------------------ la page d'un projet
    @app.get("/preparer/{pid}", response_class=HTMLResponse)
    def plan_page(pid: int, request: Request, s: Session = Depends(get_s)):
        plan = plan_or_404(s, pid)
        r = S.compute_plan(plan)
        names = {i.id: i.name for i in plan.items}
        labels = {o.id: o.label for o in plan.offers}
        offers = {}
        for o in plan.offers:
            w = r.what_if.get(o.id)
            offers[o.id] = dict(o=o, text=S.plan_offer_text(o, plan.people), total=r.totals[o.id],
                                each=r.offer_per_person[o.id], items=o.item_ids(), w=w,
                                replaces=[labels[x] for x in w.replaces] if w else [],
                                lost=[names[i] for i in w.lost] if w else [],
                                lost_text=S.plan_lost_text([names[i] for i in w.lost]) if w and w.lost else "")
        rows = []
        for it in plan.items:
            got = r.by_item[it.id]
            mine = [dict(offers[o.id], first_item=offers[o.id]["items"][0],
                         others=[names[i] for i in offers[o.id]["items"] if i != it.id])
                    for o in plan.offers if it.id in offers[o.id]["items"]]
            alone = [x["o"].label for x in mine if len(x["items"]) == 1]
            kept = [x["o"].label for x in mine if len(x["items"]) > 1]
            confirm = f"Supprimer le poste « {it.name} » ?"
            if alone:
                confirm += (" L'offre " if len(alone) == 1 else " Les offres ") + ", ".join(
                    f"« {a} »" for a in alone) + (" part avec lui." if len(alone) == 1 else " partent avec lui.")
            if kept:
                confirm += " " + ", ".join(f"« {k} »" for k in kept) + (" reste" if len(kept) == 1 else " restent") \
                    + " pour ses autres postes (sans sa répartition)."
            rows.append(dict(item=it, got=got, chosen=labels.get(got["offer"]), offers=mine, confirm=confirm,
                             with_names=[names[i] for i in got["with"]]))
        return render(request, "plan.html", plan=plan, r=r, rows=rows, names=names, missing=[names[i] for i in r.empty],
                      missing_ni=S.plan_ni_list([names[i] for i in r.empty]),
                      suggestions=[x for x in S.PLAN_SUGGESTIONS if E.sort_key(x) not in {E.sort_key(n) for n in names.values()}],
                      margin_shown=(E.fmt(plan.margin_value) + " par personne" if plan.margin_kind == P.EUROS
                                    else S.plan_percent(plan.margin_value)),
                      margin_input=(S.plan_amount_input(plan.margin_value) if plan.margin_kind == P.EUROS
                                    else S.plan_percent(plan.margin_value, sign=False)))

    # ------------------------------------------------------------------ postes
    @app.post("/preparer/{pid}/postes")
    async def item_add(pid: int, request: Request, s: Session = Depends(get_s)):
        plan = plan_or_404(s, pid)
        name = " ".join(str((await request.form()).get("name") or "").split())
        it, err = write(s, f"poste « {name} » ajouté à « {plan.name} »", lambda: W.add_plan_item(s, plan, name),
                        page_url(plan, "ajout-poste"))
        return err or back(page_url(plan, f"poste-{it.id}"), f"Poste « {it.name} » ajouté")

    @app.post("/preparer/{pid}/postes/{iid}/renommer")
    async def item_rename(pid: int, iid: int, request: Request, s: Session = Depends(get_s)):
        plan = plan_or_404(s, pid)
        it = item_or_404(plan, iid)
        old, name = it.name, " ".join(str((await request.form()).get("name") or "").split())
        _, err = write(s, f"poste « {old} » renommé « {name} »", lambda: W.rename_plan_item(s, plan, it, name),
                       page_url(plan, f"poste-{iid}"))
        return err or back(page_url(plan, f"poste-{iid}"), f"Poste renommé : « {it.name} »")

    @app.post("/preparer/{pid}/postes/{iid}/monter")
    @app.post("/preparer/{pid}/postes/{iid}/descendre")
    def item_move(pid: int, iid: int, request: Request, s: Session = Depends(get_s)):
        plan = plan_or_404(s, pid)
        it = item_or_404(plan, iid)
        up = request.url.path.endswith("/monter")
        where = [i.id for i in plan.items].index(iid) + (-1 if up else 1)
        if where < 0 or where >= len(plan.items):
            return back(page_url(plan, f"poste-{iid}"))
        write(s, f"poste « {it.name} » {'monté' if up else 'descendu'} dans la liste",
              lambda: W.move_plan_item(s, plan, it, where), page_url(plan))
        return back(page_url(plan, f"poste-{iid}"), f"« {it.name} » {'monté' if up else 'descendu'}")

    @app.post("/preparer/{pid}/postes/{iid}/supprimer")
    def item_delete(pid: int, iid: int, s: Session = Depends(get_s)):
        plan = plan_or_404(s, pid)
        it = item_or_404(plan, iid)
        name = it.name
        gone, _ = write(s, f"poste « {name} » supprimé", lambda: W.delete_plan_item(s, plan, it), page_url(plan))
        extra = ""
        if gone:
            extra = (" avec l'offre " if len(gone) == 1 else " avec les offres ") + ", ".join(f"« {g} »" for g in gone)
        return back(page_url(plan), f"Poste « {name} » supprimé{extra}")

    # ------------------------------------------------------------------ offres
    def offer_values(plan, f) -> dict:
        counting = {"personne": P.PER_PERSON, "groupe": P.GROUP, "nombre": P.LOT}.get(str(f.get("counting") or ""),
                                                                                     str(f.get("counting") or ""))
        items = [int(x) for x in f.getlist("items") if str(x).isdigit()]
        split = str(f.get("split") or "")
        shares = None
        if split in (P.AMOUNTS, P.PERCENT) and len(set(items)) > 1:
            read = euros if split == P.AMOUNTS else percent
            shares = {}
            for i in items:
                v = read(f, f"share_{i}")
                shares[i] = v if v is not None else 0
        else:
            split = ""
        return dict(label=f.get("label") or "", price=euros(f, "price"), counting=counting,
                    quantity=number(f, "quantity", "combien de fois", empty=1),
                    quantity_label=f.get("quantity_label") or "",
                    capacity=number(f, "capacity", "pour combien de personnes est ce prix") if counting == P.LOT else None,
                    items=items, split=split, shares=shares, note=f.get("note") or "")

    def offer_form(request, plan, offer=None, item_id=None):
        checked = set(offer.item_ids()) if offer else ({item_id} if item_id else set())
        shares = {ln.item_id: ln.share for ln in offer.links} if offer and offer.split else {}
        counting = {P.PER_PERSON: "personne", P.GROUP: "groupe", P.LOT: "nombre"}[offer.counting if offer else P.PER_PERSON]
        return render(request, "plan_offer.html", plan=plan, offer=offer, checked=checked, shares=shares,
                      counting=counting,
                      share_input={i: (S.plan_amount_input(v) if offer.split == P.AMOUNTS
                                       else S.plan_percent(v, sign=False)) for i, v in shares.items()},
                      item_id=item_id)

    @app.get("/preparer/{pid}/offres/nouvelle", response_class=HTMLResponse)
    def offer_new(pid: int, request: Request, poste: int | None = None, s: Session = Depends(get_s)):
        plan = plan_or_404(s, pid)
        return offer_form(request, plan, item_id=poste if poste in {i.id for i in plan.items} else None)

    @app.get("/preparer/{pid}/offres/{oid}/modifier", response_class=HTMLResponse)
    def offer_edit(pid: int, oid: int, request: Request, s: Session = Depends(get_s)):
        plan = plan_or_404(s, pid)
        return offer_form(request, plan, offer_or_404(plan, oid))

    @app.post("/preparer/{pid}/offres/apercu", response_class=HTMLResponse)
    async def offer_preview(pid: int, request: Request, s: Session = Depends(get_s)):
        """Aperçu en direct dans le formulaire : le total pour le groupe et ce que ça fait par personne."""
        plan = plan_or_404(s, pid)
        f = await request.form()
        try:
            v = offer_values(plan, f)
        except FormError as e:
            return HTMLResponse(f'<p class="warn-text">{escape(str(e))}</p>')
        if v["price"] is None:
            return HTMLResponse('<p class="muted">Indique un prix pour voir le total.</p>')
        if v["price"] < 0 or v["quantity"] < 1 or (v["counting"] == P.LOT and (v["capacity"] or 0) < 1):
            return HTMLResponse('<p class="muted">Complète le prix et la façon de le compter.</p>')
        o = P.Offer(id=0, price=v["price"], counting=v["counting"], quantity=v["quantity"], capacity=v["capacity"])
        total = P.offer_total(o, plan.people)
        n = plan.people
        return HTMLResponse(f'<p>Pour {n} personne{"s" if n > 1 else ""} : <b>{escape(E.fmt(total))}</b> au total, '
                            f'soit <b>{escape(E.fmt(P.ceil_div(total, n)))}</b> par personne.</p>')

    @app.post("/preparer/{pid}/offres")
    @app.post("/preparer/{pid}/offres/{oid}")
    async def offer_save(pid: int, request: Request, oid: int | None = None, s: Session = Depends(get_s)):
        plan = plan_or_404(s, pid)
        offer = offer_or_404(plan, oid) if oid is not None else None
        f = await request.form()
        first = next(iter([int(x) for x in f.getlist("items") if str(x).isdigit()]), None)
        err_url = (f"/preparer/{pid}/offres/{oid}/modifier" if offer
                   else f"/preparer/{pid}/offres/nouvelle" + (f"?poste={first}" if first else ""))
        try:
            v = offer_values(plan, f)
        except FormError as e:
            return refuse(err_url, str(e), e.field)
        if v["price"] is None:
            return refuse(err_url, "Indique le prix (0 si c'est inclus).", "price")
        check = {k: v[k] for k in ("label", "price", "counting", "quantity", "capacity", "items", "split", "shares")}
        label = " ".join(str(v["label"]).split())
        what = f"offre « {label} » modifiée" if offer else f"offre « {label} » ajoutée à « {plan.name} »"
        saved, err = write(s, what, lambda: W.save_offer(s, plan, offer, **v), err_url,
                           check=lambda: W.check_offer(plan, **check))
        if err:
            return err
        return back(page_url(plan, f"offre-{saved.id}"), f"Offre « {saved.label} » {'modifiée' if offer else 'ajoutée'}")

    @app.post("/preparer/{pid}/offres/{oid}/retenir")
    def offer_choose(pid: int, oid: int, s: Session = Depends(get_s)):
        plan = plan_or_404(s, pid)
        o = offer_or_404(plan, oid)
        gone, _ = write(s, f"offre « {o.label} » retenue", lambda: W.choose_offer(s, plan, o), page_url(plan))
        extra = f" (à la place de {', '.join(f'« {g} »' for g in gone)})" if gone else ""
        return back(page_url(plan, f"offre-{oid}"), f"Offre « {o.label} » retenue{extra}")

    @app.post("/preparer/{pid}/offres/{oid}/ne-plus-retenir")
    def offer_unchoose(pid: int, oid: int, s: Session = Depends(get_s)):
        plan = plan_or_404(s, pid)
        o = offer_or_404(plan, oid)
        write(s, f"offre « {o.label} » plus retenue", lambda: W.choose_offer(s, plan, o, chosen=False), page_url(plan))
        return back(page_url(plan, f"offre-{oid}"), f"L'offre « {o.label} » n'est plus retenue")

    @app.post("/preparer/{pid}/offres/{oid}/supprimer")
    def offer_delete(pid: int, oid: int, s: Session = Depends(get_s)):
        plan = plan_or_404(s, pid)
        o = offer_or_404(plan, oid)
        label, _ = write(s, f"offre « {o.label} » supprimée", lambda: W.delete_offer(s, plan, o), page_url(plan))
        return back(page_url(plan), f"Offre « {label} » supprimée")
