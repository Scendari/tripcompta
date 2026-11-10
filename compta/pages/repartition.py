"""« Qui paie quoi » et les comptes : soldes, remises et suppléments, l'asso (nom, caisse, arrondi), le voyage
en images, anciens ajustements.

Partie de l'application web (voir compta/web.py, qui installe ces pages dans l'ordre).
"""
from __future__ import annotations

import re
from urllib.parse import quote, urlparse

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from .. import engine as E
from .. import models as M
from .. import services as S
from .. import writes as W


def install(site):
    """Installe ces pages dans l'application. `site` : ce que les pages ont en commun (voir web.py)."""
    app = site.app
    back = site.back
    db = site.db
    get_s = site.get_s
    names = site.names
    refuse = site.refuse
    render = site.render
    trip_or_404 = site.trip_or_404
    whole = site.whole

    # ------------------------------------------------------------------ répartition
    @app.get("/v/{tid}/comptes")
    def ledger_moved(tid: int):
        # l'ancien « Tableau des comptes » : son tableau est devenu l'onglet Participants › Comptes
        return RedirectResponse(f"/v/{tid}/participants/soldes", status_code=303)

    @app.get("/v/{tid}/participants/export")
    def participants_export(tid: int, request: Request, s: Session = Depends(get_s)):
        """Liste des participants : format=csv (fichier pour Excel), tsv (à coller dans un tableur),
        txt (une personne par ligne, pour un message). cols=nom,telephone,… ; desistes=0 pour les exclure."""
        t = trip_or_404(s, tid)
        q = request.query_params
        fmt_ = q.get("format", "csv")
        cols = [c for c in (q.get("cols") or "").split(",") if c] or [k for k, _, on in S.EXPORT_COLS if on]
        r = S.compute_trip(t)
        heads, rows = S.participants_export(t, r, cols, q.get("desistes", "1") != "0")
        money_cols = {i for i, c in enumerate(cols) if c in ("part", "verse", "solde")}

        def cell(i, v):
            if i not in money_cols or isinstance(v, str):
                return str(v)
            if fmt_ == "txt":
                if cols[i] == "solde":
                    return f"doit {E.fmt(v)}" if v > 0 else f"on lui doit {E.fmt(-v)}" if v < 0 else "à jour"
                return E.fmt(v)
            return f"{v / 100:.2f}".replace(".", ",")          # nombre, lisible par Excel en français

        if fmt_ == "txt":
            body = "\n".join(" · ".join(x for x in (cell(i, v) for i, v in enumerate(row)) if x) for row in rows)
            return Response(body, media_type="text/plain; charset=utf-8")
        if "solde" in cols:
            heads = [h + " (négatif = à lui rendre)" if c == "solde" else h for h, c in zip(heads, cols)]
        sep = "\t" if fmt_ == "tsv" else ";"

        def esc(x):
            x = x.replace("\t", " ").replace("\r", " ").replace("\n", " ")
            if sep == ";" and any(ch in x for ch in ';"'):
                x = '"' + x.replace('"', '""') + '"'
            return x
        lines = [sep.join(esc(h) for h in heads)] + [sep.join(esc(cell(i, v)) for i, v in enumerate(row)) for row in rows]
        body = "\r\n".join(lines) + "\r\n"
        if fmt_ == "tsv":
            return Response(body, media_type="text/plain; charset=utf-8")
        name = re.sub(r"[^\w-]+", "_", f"participants {t.name}").strip("_")
        return Response("\ufeff" + body, media_type="text/csv; charset=utf-8",          # BOM : accents corrects dans Excel
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}.csv"})

    @app.get("/v/{tid}/participants/soldes", response_class=HTMLResponse)
    def ledger(tid: int, request: Request, remise: int | None = None, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        if remise is not None:              # anciens liens : l'onglet Remises / suppléments
            return RedirectResponse(f"/v/{tid}/participants/remises?remise={remise}", status_code=303)
        r = S.compute_trip(t)
        tags = {}
        for a in t.adjustments:
            if a.expense_id is None:
                tags[a.participant_id] = tags.get(a.participant_id, 0) + a.value
        return render(request, "ledger.html", trip=t, r=r, L=S.ledger(t, r), remise_tags=tags)

    @app.get("/v/{tid}/participants/remises", response_class=HTMLResponse)
    def remises_page(tid: int, request: Request, remise: int | None = None, rm: int | None = None,
                     s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        remises = sorted([a for a in t.adjustments if a.expense_id is None], key=lambda a: E.sort_key(a.participant.name))
        old = [a for a in t.adjustments if a.expense_id is not None]
        edit = next((a for a in remises if a.id == rm), None) if rm else None
        return render(request, "remises.html", trip=t, remises=remises, old_adjs=old, remise_edit=edit,
                      remise_for=remise, open_remise=bool(remise or edit or not remises))

    @app.post("/v/{tid}/remises")
    async def remise_save(tid: int, request: Request, s: Session = Depends(get_s)):
        """Remise (−) ou supplément (+) sur tout le voyage d'une personne, compensé par l'asso ou par les autres."""
        t = trip_or_404(s, tid)
        f = await request.form()
        here = f"/v/{tid}/participants/remises"
        pid = int(f.get("participant") or 0) if (f.get("participant") or "").isdigit() else 0
        if pid not in names(t):
            return refuse(here + "?remise=0", "Choisis la personne", "participant")
        try:
            val = E.parse_amount(f.get("value"))
        except ValueError:
            return refuse(here + f"?remise={pid}", f"Montant invalide : « {f.get('value')} ». Exemple : 50", "value")
        if not val or val <= 0:
            return refuse(here + f"?remise={pid}", "Indique un montant (ex. 50)", "value")
        by = f.get("by") if f.get("by") in ("asso", "person") else "others"
        comp = whole(f.get("compensator")) if by == "person" else 0
        if by == "person" and comp not in names(t):
            return refuse(here + f"?remise={pid}", "Choisis la personne qui compense", "compensator")
        if by == "person" and comp == pid:
            return refuse(here + f"?remise={pid}", "La personne qui compense ne peut pas être celle qui a la remise",
                          "compensator")
        aid = f.get("aid") or ""
        a = next((x for x in t.adjustments if str(x.id) == aid and x.expense_id is None), None) if aid else None
        who = names(t)[pid]
        kind = "Supplément" if f.get("kind") == "increase" else "Remise"
        db.snapshot(f"{kind.lower()} pour {who}")
        W.save_discount(s, t, pid, (1 if kind == "Supplément" else -1) * abs(val),
                        {"asso": E.ASSO, "person": E.PERSON}.get(by, E.OTHERS), comp if by == "person" else None,
                        f.get("reason"), adjustment=a)
        s.commit()
        return back(here, f"{kind} de {E.fmt(abs(val))} pour {who} enregistré" + ("e" if kind == "Remise" else ""))

    @app.post("/v/{tid}/remises/{aid}/supprimer")
    def remise_delete(tid: int, aid: int, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        a = next((x for x in t.adjustments if x.id == aid), None)
        if a is None:
            raise HTTPException(404, "Remise introuvable")
        db.snapshot(f"suppression d'une remise de {a.participant.name}")
        s.delete(a)
        s.commit()
        return back(f"/v/{tid}/participants/remises", "Supprimé")

    @app.get("/v/{tid}/en-images", response_class=HTMLResponse)
    def visuals(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        r = S.compute_trip(t)
        return render(request, "viz.html", trip=t, r=r, V=S.trip_visuals(t, r))

    @app.get("/v/{tid}/asso", response_class=HTMLResponse)
    def asso_page(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        r = S.compute_trip(t)
        return render(request, "asso.html", trip=t, r=r, A=S.asso_account(t, r), cashier=S.cashier(t),
                      asso_custom=S.trip_asso_name(t) != "l'asso", asso_real=S.trip_asso_name(t))

    @app.post("/v/{tid}/asso/nom")
    async def asso_rename(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        name = str((await request.form()).get("name") or "")
        S.set_trip_asso_name(t, name, s)
        s.commit()
        return back(f"/v/{tid}/asso", f"Dans ce voyage, l'asso s'appelle maintenant « {S.trip_asso_name(t)} »"
                    if name.strip() else "Nom de l'asso effacé pour ce voyage")

    @app.post("/v/{tid}/asso/caisse")
    async def asso_cashier(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        if f.get("cashier") == "p":
            pid = whole(f.get("participant"))
            if pid not in {p.id for p in t.participants}:
                return refuse(f"/v/{tid}/asso", "Choisis le participant qui tient la caisse")
            t.cashier_id = pid
        else:
            t.cashier_id = None
        s.commit()
        c = S.cashier(t)
        return back(f"/v/{tid}/asso", f"C'est {c.name} qui tient la caisse" if c else
                    f"C'est {S.trip_asso_name(t)} qui tient la caisse")

    @app.post("/v/{tid}/asso/arrondi")
    async def asso_rounding(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        v = (await request.form()).get("rounding")
        if v not in (E.ROUND_PARTICIPANTS, E.ROUND_ASSO):
            return refuse(f"/v/{tid}/asso", "Choix d'arrondi inconnu")
        if v != (t.rounding or E.ROUND_PARTICIPANTS):
            db.snapshot("changement de qui paie les centimes d'arrondi")
            t.rounding = v
            s.commit()
        caisse = S.caisse_label(t)
        return back(f"/v/{tid}/asso", "Les centimes d'arrondi sont tirés au sort parmi les participants" if v == E.ROUND_PARTICIPANTS
                    else f"Les centimes d'arrondi sont pris en charge par {caisse}")

    @app.get("/v/{tid}/repartition", response_class=HTMLResponse)
    def matrix(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        r = S.compute_trip(t)
        has_global = any(l.expense is None for l in r.lines.values())
        # par défaut : sous-totaux ; « ▸ catégorie » déplie une catégorie (?o=id), « Tout déplier » = ?vue=detail
        all_open = request.query_params.get("vue") == "detail"
        opened = {int(x) for x in request.query_params.getlist("o") if x.isdigit()}
        compact = not all_open
        OTHER = "Autres (sans sous-catégorie)"
        # colonnes : dépenses (détail), « Σ sous-catégorie », « Total catégorie »
        cols = []
        for c in t.categories:
            es = sorted([e for e in t.expenses if e.category_id == c.id],
                        key=lambda e: (not e.subcategory, E.sort_key(e.subcategory or ""), e.id))
            if not es:
                continue
            grouped = any(e.subcategory for e in es)
            compact = not (all_open or c.id in opened)
            if grouped:
                subs = []
                for e in es:
                    k = e.subcategory or OTHER
                    if not subs or subs[-1][0] != k:
                        subs.append((k, []))
                    subs[-1][1].append(e)
                for k, ges in subs:
                    if not compact:
                        cols += [dict(kind="exp", cat=c.name, sub=k, e=x, eids=[x.id], label=x.label) for x in ges]
                    if len(subs) > 1:                     # une seule sous-catégorie : Σ = total, on ne répète pas
                        cols.append(dict(kind="sub", cat=c.name, sub=k, eids=[x.id for x in ges], label=f"Σ {k}"))
                cols.append(dict(kind="cat", cat=c.name, sub=None, eids=[x.id for x in es], label=f"Total {c.name}"))
            else:
                if not compact:
                    cols += [dict(kind="exp", cat=c.name, sub=None, e=x, eids=[x.id], label=x.label) for x in es]
                if compact or len(es) > 1:
                    cols.append(dict(kind="cat", cat=c.name, sub=None, eids=[x.id for x in es],
                                     label=f"Total {c.name}"))
        cat_ids = {c.name: c.id for c in t.categories}
        for col in cols:
            col["total"] = sum(r.expense_totals.get(i, 0) for i in col["eids"])
        # en-têtes regroupés : catégories, puis sous-catégories
        head1, head2 = [], []
        prev = None
        for col in cols:
            col["start"] = col["cat"] != prev          # 1re colonne d'une catégorie : trait de séparation
            prev = col["cat"]
            key = (col["cat"], col["sub"] if col["kind"] != "cat" else "__total__")
            # 1re colonne d'une sous-catégorie (ou du total) dans la catégorie : trait en pointillé
            col["sub_start"] = not col["start"] and bool(head2) and head2[-1]["key"] != key
            if head1 and head1[-1]["cat"] == col["cat"]:
                head1[-1]["span"] += 1
            else:
                cid = cat_ids.get(col["cat"])
                is_open = all_open or cid in opened
                other = sorted(opened ^ {cid})
                ndetail = sum(1 for e in t.expenses if e.category_id == cid)
                head1.append(dict(cat=col["cat"], span=1, open=is_open, band=len(head1) % 2,
                                  toggle=None if all_open or ndetail < 2 else
                                  "?" + "&".join(f"o={i}" for i in other)))
            if head2 and head2[-1]["key"] == key:
                head2[-1]["span"] += 1
            else:
                head2.append(dict(key=key, label=(col["sub"] or "") if col["kind"] != "cat" else "", span=1,
                                  start=col["start"], sub_start=col["sub_start"]))
        return render(request, "matrix.html", trip=t, r=r, cols=cols, head1=head1, head2=head2,
                      all_open=all_open, any_open=bool(opened),
                      has_global=has_global, has_subs=any(c["kind"] == "sub" for c in cols),
                      withdrawn={w.participant_id for w in t.withdrawals})

    # ------------------------------------------------------------------ ajustements
    @app.get("/v/{tid}/ajuster")
    def adjust_moved(tid: int, p: int | None = None, e: str | None = None):
        """L'ancienne page « Ajuster une part » : un montant précis se fixe dans la dépense,
        une remise sur tout le voyage dans Participants › Remises / suppléments."""
        if e and e.isdigit():
            return RedirectResponse(f"/v/{tid}/depenses/{e}/modifier#who", status_code=303)
        if p:
            return RedirectResponse(f"/v/{tid}/participants/remises?remise={p}", status_code=303)
        return RedirectResponse(f"/v/{tid}/participants", status_code=303)

    @app.post("/v/{tid}/ajuster/{aid}/supprimer")
    def adjust_delete(tid: int, aid: int, request: Request, s: Session = Depends(get_s)):
        trip_or_404(s, tid)
        a = s.get(M.Adjustment, aid)
        if not a or a.trip_id != tid:
            raise HTTPException(404, "Ajustement introuvable")
        db.snapshot(f"suppression d'un ajustement de {a.participant.name}")
        s.delete(a)
        s.commit()
        ref = urlparse(request.headers.get("referer") or f"/v/{tid}/participants").path
        return back(ref, "Ajustement supprimé")
