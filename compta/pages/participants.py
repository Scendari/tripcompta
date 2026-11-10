"""Participants : liste, organisation (rôles, familles, groupes), comptes par personne.

Partie de l'application web (voir compta/web.py, qui installe ces pages dans l'ordre).
"""
from __future__ import annotations

import datetime as dt
import re
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import engine as E
from .. import models as M
from .. import services as S
from .. import writes as W


def install(site):
    """Installe ces pages dans l'application. `site` : ce que les pages ont en commun (voir web.py)."""
    _then_join = site._then_join
    app = site.app
    back = site.back
    db = site.db
    get_s = site.get_s
    refuse = site.refuse
    render = site.render
    trip_or_404 = site.trip_or_404

    def _send_rows(*a, **k):                         # défini plus loin, dans pages/envoyer.py
        return site._send_rows(*a, **k)

    # ------------------------------------------------------------------ participants
    @app.get("/v/{tid}/participants", response_class=HTMLResponse)
    def participants(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        r = S.compute_trip(t)
        groups = S.trip_groups(t)
        known = S.known_people(t)
        return render(request, "participants.html", trip=t, r=r, groups=groups, export_cols=S.EXPORT_COLS,
                      known=known, known_trips=S.known_trips(t, known),
                      known_fill={S.person_key(x["name"]): dict(phone=x["phone"], email=x["email"],
                                                                companion=x["companion"]) for x in known},
                      open_add=request.query_params.get("ajout") == "1",
                      open_list=request.query_params.get("liste") == "1", list_text="",
                      open_edit=int(request.query_params.get("modifier") or 0) if (request.query_params.get("modifier") or "").isdigit() else 0,
                      pgroups={p.id: [g["name"] for g in groups if p.id in g["ids"]] for p in t.participants},
                      withdrawn={w.participant_id for w in t.withdrawals},
                      withdrawn_dates={w.participant_id: w.date for w in t.withdrawals},
                      has_expenses=bool(t.expenses))

    @app.get("/v/{tid}/participants/avances", response_class=HTMLResponse)
    def people_advances(tid: int, request: Request, s: Session = Depends(get_s)):
        """Ce que des participants ont payé eux-mêmes (avances) ou reçu à la place de l'asso."""
        t = trip_or_404(s, tid)
        refunds = [dict(date=m.date, label=m.note or "Remboursement", mid=m.id, pid=m.participant_id,
                        name=m.participant.name if m.participant else "?", amount=m.amount)
                   for m in t.movements if m.kind == "remboursement"]
        return render(request, "avances.html", trip=t, advances=S.advances(t), refunds=refunds)

    @app.get("/v/{tid}/participants/organisation", response_class=HTMLResponse)
    def organisation(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        return render(request, "organisation.html", trip=t, groups=S.trip_groups(t), diving=S.is_diving(t),
                      role_members={rk: S.with_role(t, rk) for rk in S.trip_roles(t)})

    @app.post("/v/{tid}/roles-du-voyage")
    async def add_role_route(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        name = str((await request.form()).get("name") or "")
        db.snapshot(f"nouveau rôle {name}")
        try:
            key = S.add_trip_role(t, name)
        except ValueError as e:
            return refuse(f"/v/{tid}/participants/organisation#roles", str(e))
        s.commit()
        return back(f"/v/{tid}/participants/organisation#roles", f"Rôle « {S.role_label(key)} » ajouté")

    @app.post("/v/{tid}/roles-du-voyage/supprimer")
    async def remove_role_route(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        key = str((await request.form()).get("role") or "")
        db.snapshot(f"suppression du rôle {S.role_label(key)}")
        try:
            S.remove_trip_role(t, key)
        except ValueError as e:
            return refuse(f"/v/{tid}/participants/organisation#roles", str(e))
        s.commit()
        return back(f"/v/{tid}/participants/organisation#roles", f"Rôle « {S.role_label(key)} » supprimé de ce voyage")

    @app.post("/v/{tid}/roles/{role}")
    async def save_role_members(tid: int, role: str, request: Request, s: Session = Depends(get_s)):
        """Onglet Organisation : les membres d'un rôle (organisateur : un seul). Ceux qui y entrent se voient proposer
        les dépenses qui concernaient exactement ce rôle."""
        t = trip_or_404(s, tid)
        if role not in S.trip_roles(t):
            raise HTTPException(404, "Rôle inconnu")
        ids = {int(x) for x in (await request.form()).getlist("members") if str(x).isdigit()}
        if role == S.ORGANIZER and len(ids) > 1:
            return refuse(f"/v/{tid}/participants/organisation#roles", "Un seul organisateur : choisis une personne")
        db.snapshot(f"rôle {S.role_label(role)}")
        before = S.label_members(t)
        for p in t.participants:
            rs = [r for r in p.role_list if r != role]
            if p.id in ids:
                rs.append(role)
            S.set_roles(t, p, rs)
        s.commit()
        n = len(ids)
        return _then_join(t, before, f"/v/{tid}/participants/organisation#roles",
                          f"{S.role_label(role)} : {n} personne{'s' if n > 1 else ''}")

    @app.post("/v/{tid}/familles")
    async def create_family(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        db.snapshot(f"famille {f.get('name')}")
        try:
            fam = S.save_family(s, t, f.get("name"), f.getlist("members"), f.get("referent"))
        except ValueError as e:
            s.rollback()
            return refuse(f"/v/{tid}/participants/organisation#familles", str(e))
        s.commit()
        return back(f"/v/{tid}/participants/organisation#familles", f"Famille {fam.name} créée")

    @app.post("/v/{tid}/familles/{fid}/supprimer")
    def remove_family(tid: int, fid: int, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        fam = next((x for x in t.families if x.id == fid), None)
        if fam is None:
            raise HTTPException(404, "Famille introuvable")
        name = fam.name
        db.snapshot(f"suppression de la famille {name}")
        S.delete_family(s, t, fam)
        s.commit()
        return back(f"/v/{tid}/participants/organisation#familles", f"Famille {name} supprimée : chacun a son compte")

    @app.post("/v/{tid}/groupes")
    async def save_group_route(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        old = f.get("old_name") or None
        db.snapshot(f"groupe « {f.get('name') or old} »")
        before = S.label_members(t)
        try:
            S.save_group(t, f.get("name") or "", f.getlist("members"), old)
        except ValueError as e:
            return refuse(f"/v/{tid}/participants/organisation#groupes", str(e))
        s.commit()
        g = S.find_group(t, f.get("name"))
        return _then_join(t, before, f"/v/{tid}/participants/organisation#groupes",
                          f"Groupe « {g['name']} » enregistré ({len(g['ids'])} personne{'s' if len(g['ids']) > 1 else ''})")

    @app.post("/v/{tid}/groupes/supprimer")
    async def delete_group_route(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        name = (await request.form()).get("name") or ""
        db.snapshot(f"suppression du groupe « {name} »")
        S.delete_group(t, name)
        s.commit()
        return back(f"/v/{tid}/participants/organisation#groupes", f"Groupe « {name} » supprimé (les dépenses ne changent pas)")

    def _roles_from_form(f) -> list:
        """Rôles cochés (pastilles r_<rôle>) ; l'ancien choix « role » = plongeur / accompagnant reste accepté.
        Sans pastilles (ancien formulaire, liste de noms) : plongeur par défaut."""
        rs = [r for r in S.ROLES if f.get(f"r_{r}")] + [r for r in f.keys() if r.startswith("r_" + S.CUSTOM) and f.get(r)]
        rs = [r[2:] if r.startswith("r_") else r for r in rs]
        for v in f.getlist("role"):
            if v in (S.DIVER, S.COMPANION) and v not in rs:
                rs.append(v)
        if "roles_form" not in f and S.DIVER not in rs and S.COMPANION not in rs:
            rs.append(S.DIVER)
        return rs

    def _family_from_form(f):
        """Famille choisie : pastilles (« Aucune », une existante, « Nouvelle… » + nom) ou ancien champ texte."""
        if "family_pick" in f:
            pick = f.get("family_pick") or ""
            return (f.get("family_new") or "").strip() if pick == "__new__" else pick
        return f.get("family") if "family" in f else None

    @app.post("/v/{tid}/participants/reprendre")
    async def take_known_people(tid: int, request: Request, s: Session = Depends(get_s)):
        """Ajoute des personnes déjà venues sur d'autres voyages : nom, téléphone, e-mail, plongeur ou accompagnant
        (pas la famille, les rôles, les groupes ni la note, propres à chaque voyage)."""
        t = trip_or_404(s, tid)
        f = await request.form()
        wanted = set(f.getlist("known"))
        people = [x for x in S.known_people(t) if x["key"] in wanted]
        if not people:
            return refuse(f"/v/{tid}/participants?ajout=1#ajout", "Coche au moins une personne")
        db.snapshot(f"reprise de {len(people)} participant(s) d'autres voyages")
        before = S.label_members(t)
        added = []
        for x in people:
            dflt = S.COMPANION if x["companion"] else S.DIVER
            added.append(W.add_participant(s, t, x["name"], roles=[dflt] if dflt in S.trip_roles(t) else [],
                                           companion=x["companion"], phone=x["phone"], email=x["email"]))
        s.commit()
        msg = f"{added[0].name} ajouté" if len(added) == 1 else f"{len(added)} participants ajoutés"
        return _then_join(t, before, f"/v/{tid}/participants", msg)

    @app.post("/v/{tid}/participants")
    async def add_participants(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        raw = f.get("names") or f.get("name") or ""
        many = "names" in f                                  # « Plusieurs personnes d'un coup » : un nom par ligne
        new, known_already = [], 0
        existing = {E.sort_key(p.name) for p in t.participants}
        for line in re.split(r"[\n;]", raw):
            n = re.sub(r"\s+", " ", line).strip().strip(",").strip()   # tabulations d'un tableur : des espaces
            if n and E.sort_key(n) not in existing:
                new.append(n)
                existing.add(E.sort_key(n))
            elif n:
                known_already += 1
        if not new and many:
            return refuse(f"/v/{tid}/participants?ajout=1&liste=1",
                          "Tous ces noms sont déjà dans la liste" if known_already else "Écris au moins un nom, un par ligne")
        if not new:
            return refuse(f"/v/{tid}/participants?ajout=1#ajout", "Ce nom est vide ou déjà dans la liste", "name")
        roles_in = _roles_from_form(f)
        companion = S.COMPANION in roles_in
        family = (_family_from_form(f) or "").strip()
        db.snapshot(f"ajout de {len(new)} participant(s)")
        before = S.label_members(t)
        kp = S.known_person(t, new[0]) if len(new) == 1 else None   # déjà venu sur un autre voyage ?
        details = {}
        if len(new) == 1 and "name" in f:        # formulaire « un participant » : toutes ses infos
            details = {k: f.get(k, "").strip() for k in ("phone", "email", "note")}
            if kp:                               # déjà venu sur un autre voyage : ses contacts s'il n'en a pas saisi
                details["phone"], details["email"] = details["phone"] or kp["phone"], details["email"] or kp["email"]
        added = [W.add_participant(s, t, n, roles=roles_in, companion=companion, family=family, **details)
                 for n in new]
        if details and f.getlist("groups"):
            S.set_person_groups(t, added[0].id, f.getlist("groups"))
        s.commit()
        if len(added) == 1:
            msg = f"{added[0].name} ajouté{' (accompagnant)' if companion else ''}"
        else:
            msg = f"{len(new)} {'accompagnants' if companion else 'participants'} ajoutés"
        if known_already and many:
            msg += f" ({known_already} déjà dans la liste)"
        if family:
            msg += f" dans la famille {S.find_family(t, family).name}"
        if "join_form" in f or f.get("join_all"):   # ancien envoi (scripts) : les dépenses à rejoindre sont données
            if "join_form" in f:
                joined = [e for e in t.expenses if f.get(f"join_{e.id}")]
                for e in joined:
                    for p in added:
                        W.join_expense(e, p.id)
            else:
                joined = W.join_group_expenses(t, added, before)
            s.commit()
            n = len({e.id for e in joined})
            if n:
                msg += f", inclus dans {n} dépense(s) de groupe"
            return back(f"/v/{tid}/participants#pe{added[0].id}" if len(added) == 1 else f"/v/{tid}/participants", msg)
        if f.get("again"):                                   # « Ajouter et continuer » : le formulaire reste ouvert
            return _then_join(t, before, f"/v/{tid}/participants?ajout=1#ajout", msg)
        return _then_join(t, before, f"/v/{tid}/participants#pe{added[0].id}" if len(added) == 1
                          else f"/v/{tid}/participants", msg)

    @app.post("/v/{tid}/participants/{pid}")
    async def edit_participant(tid: int, pid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        p = s.get(M.Participant, pid)
        if not p or p.trip_id != tid:
            raise HTTPException(404, "Participant introuvable")
        f = await request.form()
        db.snapshot(f"modification de {p.name}")
        before = S.label_members(t)
        p.name = (f.get("name") or p.name).strip()
        p.phone = f.get("phone", "").strip()
        p.email = f.get("email", "").strip()
        p.note = f.get("note", "").strip()
        fam_ = _family_from_form(f)
        if fam_ is not None:
            S.set_family(s, t, p, fam_)
        if "role" in f and "roles_form" not in f:        # ancien formulaire : plongeur / accompagnant seulement
            keep = [r for r in p.role_list if r not in (S.DIVER, S.COMPANION)]
            S.set_roles(t, p, keep + [S.COMPANION if f.get("role") == "accompagnant" else S.DIVER])
        if "roles_form" in f:
            S.set_roles(t, p, _roles_from_form(f))
        if "groups_form" in f:
            S.set_person_groups(t, p.id, f.getlist("groups"))
        s.commit()
        return _then_join(t, before, f"/v/{tid}/participants", f"{p.name} modifié")

    def _safe_next(tid: int, url: str) -> str:
        url = str(url or "")
        return url if url.startswith(f"/v/{tid}/") or url == f"/v/{tid}" else f"/v/{tid}/participants"

    @app.get("/v/{tid}/rejoindre", response_class=HTMLResponse)
    def join_page(tid: int, request: Request, s: Session = Depends(get_s)):
        """« Julie est maintenant dans Plongeurs : l'ajouter aussi à ces dépenses ? » (pré-coché)."""
        t = trip_or_404(s, tid)
        q = request.query_params
        nxt = _safe_next(tid, q.get("next"))
        whys = (q.get("w") or "").split("|")
        people = {p.id: p for p in t.participants}
        exps = {e.id: e for e in t.expenses}
        r = S.compute_trip(t)
        rows: dict = {}
        for item in (q.get("s") or "").split(","):
            parts = item.split(".")
            try:
                pid, eid, wi = (int(x) for x in parts[:3])
                ji = int(parts[3]) if len(parts) > 3 else wi
                kind = {"g": "groupes", "a": "autres", "v": "verifier"}.get(parts[4] if len(parts) > 4 else "g")
                oi = int(parts[5]) if len(parts) > 5 else -1
            except (ValueError, IndexError):
                continue
            p, e = people.get(pid), exps.get(eid)
            if p is None or e is None or kind is None or pid in {x.participant_id for x in e.shares}:
                continue
            rows.setdefault(pid, dict(p=p, whys=[], items=[], checks=[]))
            text = (lambda i: whys[i] if 0 <= i < len(whys) else "")
            if text(ji) and text(ji) not in rows[pid]["whys"]:
                rows[pid]["whys"].append(text(ji))
            it = dict(e=e, why=text(wi), others=text(oi), kind=kind, total=r.expense_totals.get(e.id, 0),
                      n=len(e.shares))
            rows[pid]["checks" if kind == "verifier" else "items"].append(it)
        if not rows:
            return RedirectResponse(nxt, status_code=303)
        n_checks = sum(len(x["checks"]) for x in rows.values())
        return render(request, "join.html", trip=t, rows=list(rows.values()), next_url=nxt, n_checks=n_checks,
                      any_items=any(x["items"] for x in rows.values()))

    @app.post("/v/{tid}/rejoindre")
    async def join_save(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        nxt = _safe_next(tid, f.get("next"))
        people = {p.id: p for p in t.participants}
        exps = {e.id: e for e in t.expenses}
        wanted = []
        for v in f.getlist("add"):
            try:
                pid, eid = (int(x) for x in str(v).split("."))
            except ValueError:
                continue
            e = exps.get(eid)
            if pid in people and e is not None and pid not in {x.participant_id for x in e.shares} \
                    and not any(x.weight != 1 or x.fixed is not None for x in e.shares):   # parts personnalisées : non
                wanted.append((pid, e))
        if not wanted:
            return back(nxt, "Aucune dépense changée")
        db.snapshot("ajout aux dépenses")
        for pid, e in wanted:
            W.join_expense(e, pid)
        s.commit()
        names = list(dict.fromkeys(people[pid].name for pid, _ in wanted))
        n = len({e.id for _, e in wanted})
        who = names[0] if len(names) == 1 else f"{len(names)} personnes"
        return back(nxt, f"{who} ajouté{'s' if len(names) > 1 else ''} à {n} dépense{'s' if n > 1 else ''}")

    @app.post("/v/{tid}/roles")
    async def save_roles(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        org = f.get("organisateur") or ""
        db.snapshot("rôles du groupe")
        for p in t.participants:
            rs = [r for r in p.role_list if r in (S.DIVER, S.COMPANION)]
            if str(p.id) == org:
                rs.append(S.ORGANIZER)
            if f.get(f"co_{p.id}"):
                rs.append(S.CO_ORGANIZER)
            keep_custom = [r for r in p.role_list if r.startswith(S.CUSTOM)]
            if f.get(f"dp_{p.id}"):                    # ancien formulaire : DP est devenu une étiquette ordinaire
                keep_custom += [r for r in S.parse_roles(["dp"], t) if r not in keep_custom]
            p.roles = ",".join([r for r in S.ROLES if r in rs] + keep_custom)
        s.commit()
        return back(f"/v/{tid}/participants/organisation#equipe", "Équipe enregistrée")

    @app.post("/v/{tid}/familles/{fid}")
    async def edit_family(tid: int, fid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        fam = next((x for x in t.families if x.id == fid), None)
        if fam is None:
            raise HTTPException(404, "Famille introuvable")
        f = await request.form()
        if "members" in f:                      # onglet Organisation : nom, membres, qui paie
            db.snapshot(f"modification de la famille {fam.name}")
            before = S.label_members(t)
            try:
                S.save_family(s, t, f.get("name"), f.getlist("members"), f.get("referent"), fam)
            except ValueError as e:
                s.rollback()
                return refuse(f"/v/{tid}/participants/organisation#familles", str(e))
            s.commit()
            return _then_join(t, before, f"/v/{tid}/participants/organisation#familles", f"Famille {fam.name} modifiée")
        name = (f.get("name") or "").strip()
        if name.lower().startswith("famille "):
            name = name[8:].strip()
        other = S.find_family(t, name) if name else None
        if other is not None and other.id != fam.id:
            return refuse(f"/v/{tid}/participants", f"Il y a déjà une famille {other.name}")
        db.snapshot(f"modification de la famille {fam.name}")
        if name:
            fam.name = name
        ref = f.get("referent")
        if ref and int(ref) in {m.id for m in fam.members}:
            fam.referent_id = int(ref)
        s.commit()
        return back(f"/v/{tid}/participants", f"Famille {fam.name} modifiée")

    @app.post("/v/{tid}/participants/{pid}/supprimer")
    def delete_participant(tid: int, pid: int, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        p = s.get(M.Participant, pid)
        if not p or p.trip_id != tid:
            raise HTTPException(404, "Participant introuvable")
        used = (any(m.participant_id == pid for m in t.movements)
                or any(e.payer_id == pid or any(x.payer_id == pid for x in e.payments) for e in t.expenses)
                or any(pid in sub.members for sub in t.subaccounts))
        if used:
            return refuse(f"/v/{tid}/participants",
                          f"{p.name} a déjà des versements ou des avances : utilise plutôt « Quelqu'un se désiste »")
        db.snapshot(f"suppression de {p.name}")
        S.set_family(s, t, p, None)
        s.query(M.ExpenseShare).filter_by(participant_id=pid).delete()
        s.query(M.Adjustment).filter((M.Adjustment.participant_id == pid) |
                                     (M.Adjustment.compensator_id == pid)).delete()
        s.query(M.Withdrawal).filter_by(participant_id=pid).delete()
        s.delete(p)
        s.commit()
        return back(f"/v/{tid}/participants", f"{p.name} retiré du voyage")

    # ------------------------------------------------------------------ comptes par personne
    @app.get("/v/{tid}/p/{pid}")
    def person(tid: int, pid: int, request: Request, s: Session = Depends(get_s)):
        """Ancienne fiche : l'onglet Comptes, filtré sur cette personne (message et « Défaire » conservés)."""
        t = trip_or_404(s, tid)
        p = s.get(M.Participant, pid)
        if not p or p.trip_id != tid:
            raise HTTPException(404, "Participant introuvable")
        extra = "".join(f"&{k}={quote(v)}" for k, v in request.query_params.items() if k in ("msg", "e", "f", "d", "r"))
        return RedirectResponse(f"/v/{tid}/participants/comptes?p={pid}{extra}#p{pid}", status_code=303)

    @app.get("/v/{tid}/participants/comptes", response_class=HTMLResponse)
    def people_accounts(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        mode = E.ACTUAL
        r = S.compute_trip(t, mode)
        rp = S.compute_trip(t, E.PLANNED)
        sends = _send_rows(t, r)
        wds = {w.participant_id: w for w in t.withdrawals}
        blocks = []
        for p in sorted(t.participants, key=lambda x: E.sort_key(x.name)):
            subs = [sub for sub in t.subaccounts if p.id in sub.members]
            blocks.append(dict(
                p=p, acc=r.account_of(p.id), me=r.people[p.id], account=S.person_account(t, r, p.id),
                adjs=[a for a in t.adjustments if a.participant_id == p.id], wd=wds.get(p.id), subs=subs,
                sub_bal={sub.id: S.sub_view(sub)["balances"].get(p.id, 0) for sub in subs},
                send=next(x for x in sends if any(m.id == p.id for m in x["acc"].members)),
                planned_share=rp.people[p.id].share, actual_share=r.people[p.id].share))
        wanted = {int(x) for x in request.query_params.getlist("p") if x.isdigit()}
        selected = wanted or {p.id for p in t.participants}
        return render(request, "comptes.html", trip=t, r=r, blocks=blocks, selected=selected,
                      from_url=bool(wanted), today=dt.date.today().isoformat())
