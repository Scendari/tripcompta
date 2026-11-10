"""Opérations « métier » réutilisables par l'API JSON et le serveur MCP (IA).

Principes :
- les personnes peuvent être désignées par leur nom (sans tenir compte des accents/majuscules) ou leur id ;
- les montants sont en euros (« 12,50 », 12.5, « 1 500 ») ;
- chaque écriture peut être simulée (`simulation=True`) : on calcule le résultat puis on annule tout ;
- les erreurs (`OpsError`) sont rédigées pour qu'une IA puisse poser la bonne question à l'utilisateur.
"""
from __future__ import annotations

import datetime as dt
import shutil
import uuid
from pathlib import Path
from typing import Iterable, Optional

from . import engine as E
from . import fichiers
from . import models as M
from . import services as S
from .db import Database


class OpsError(ValueError):
    pass


# ---------------------------------------------------------------- utilitaires

def euros(value, field: str = "montant", allow_none: bool = False) -> Optional[int]:
    if value is None or value == "":
        if allow_none:
            return None
        raise OpsError(f"Il manque le {field}.")
    try:
        c = E.parse_amount(value)
    except ValueError:
        raise OpsError(f"{field.capitalize()} invalide : « {value} ». Exemple attendu : 12,50")
    return c


def to_euros(c: Optional[int]) -> Optional[float]:
    return None if c is None else round(c / 100, 2)


def date_of(value, field="date") -> Optional[dt.date]:
    if not value:
        return None
    if isinstance(value, dt.date):
        return value
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise OpsError(f"{field.capitalize()} invalide : « {value} ». Formats acceptés : 2027-05-13 ou 13/05/2027")


def get_trip(s, trip_id) -> M.Trip:
    t = s.get(M.Trip, int(trip_id)) if str(trip_id).isdigit() else None
    if not t:
        trips = s.query(M.Trip).all()
        match = [x for x in trips if E.sort_key(x.name) == E.sort_key(str(trip_id))]
        if len(match) == 1:
            return match[0]
        raise OpsError(f"Voyage introuvable : {trip_id}. Voyages existants : "
                       + ", ".join(f"{x.id} = {x.name}" for x in trips) if trips else "Aucun voyage pour l'instant.")
    return t


def person(t: M.Trip, ref, field="personne") -> M.Participant:
    if isinstance(ref, M.Participant):
        return ref
    if ref is None or ref == "":
        raise OpsError(f"Il manque la {field}.")
    if isinstance(ref, int) or str(ref).isdigit():
        for p in t.participants:
            if p.id == int(ref):
                return p
    key = E.sort_key(str(ref)).strip()
    exact = [p for p in t.participants if E.sort_key(p.name) == key]
    if len(exact) == 1:
        return exact[0]
    starts = [p for p in t.participants if E.sort_key(p.name).startswith(key)]
    if len(starts) == 1:
        return starts[0]
    fam = S.find_family(t, str(ref))
    if fam is not None and fam.members:
        ref_id = fam.referent_id if fam.referent_id in {m.id for m in fam.members} else fam.members[0].id
        return next(m for m in fam.members if m.id == ref_id)
    known = ", ".join(p.name for p in t.participants) or "aucun participant"
    if len(starts) > 1:
        raise OpsError(f"« {ref} » est ambigu ({', '.join(p.name for p in starts)}). Précise la {field}.")
    raise OpsError(f"Personne inconnue : « {ref} ». Participants du voyage : {known}. "
                   f"Faut-il l'ajouter comme participant ?")


def expense_of(t: M.Trip, ref) -> M.Expense:
    if isinstance(ref, int) or str(ref).isdigit():
        for e in t.expenses:
            if e.id == int(ref):
                return e
    key = E.sort_key(str(ref))
    match = [e for e in t.expenses if E.sort_key(e.label) == key] or \
            [e for e in t.expenses if key in E.sort_key(e.label)]
    if len(match) == 1:
        return match[0]
    known = "; ".join(f"{e.id} = {e.label}" for e in t.expenses) or "aucune"
    if len(match) > 1:
        raise OpsError(f"Plusieurs dépenses correspondent à « {ref} » : "
                       + "; ".join(f"{e.id} = {e.label}" for e in match) + ". Précise laquelle.")
    raise OpsError(f"Dépense introuvable : « {ref} ». Dépenses du voyage : {known}")


def category_id(s, t: M.Trip, name) -> int:
    name = (name or "").strip() or "Divers"
    for c in t.categories:
        if E.sort_key(c.name) == E.sort_key(name):
            return c.id
    c = M.Category(name=name, audience="", position=len(t.categories))
    t.categories.append(c)
    s.flush()
    return c.id


# Groupes nommés le temps d'un import (« covoit » : [...]) : utilisables dans « pour », « sauf » et « parts ».
import contextvars
GROUPS: contextvars.ContextVar = contextvars.ContextVar("groupes", default={})


def weights_of(t: M.Trip, pour=None, parts: Optional[dict] = None, sauf=None) -> dict:
    """pour = 'tous' | 'plongeurs' | 'accompagnants' | groupe | liste de noms ; parts = {nom: nombre de parts} ;
    sauf = personnes (ou groupes) à retirer."""
    out = {}
    groups = {E.sort_key(g["name"]).strip(): [p.name for p in g["members"]] for g in S.trip_groups(t)}
    groups.update({E.sort_key(k).strip(): v for k, v in (GROUPS.get() or {}).items()})
    def expand(ref, depth=0) -> list:
        key = E.sort_key(str(ref)).strip()
        if key in groups and depth < 5:
            members = groups[key] if isinstance(groups[key], list) else [groups[key]]
            return [pid for m in members for pid in expand(m, depth + 1)]
        if key in ("tous", "tout le monde", "all"):
            return [p.id for p in t.participants]
        for role, label in S.trip_roles(t).items():   # un rôle : « plongeurs », « accompagnants », « DP », « moniteurs »…
            plural = S.ROLE_PLURALS.get(role) or E.sort_key(label) + "s"
            if key in (role, plural, "les " + plural, E.sort_key(label)):
                return [p.id for p in t.participants if role in p.role_list]
        if key.startswith("famille "):
            fam = S.find_family(t, str(ref))
            if fam is None:
                raise OpsError(f"Famille inconnue : « {ref} ». Familles : "
                               + (", ".join(f.name for f in t.families) or "aucune"))
            return [m.id for m in fam.members]
        return [person(t, ref).id]

    if parts:
        for ref, w in parts.items():
            try:
                wv = float(E.to_weight(w))
            except Exception:
                raise OpsError(f"Nombre de parts invalide pour {ref} : {w}")
            if wv < 0:
                raise OpsError(f"Nombre de parts négatif pour {ref}")
            if wv > 0:
                for pid in expand(ref):
                    out[pid] = wv

    if pour in (None, "", "tous", "tout le monde", "all") and not parts:
        out = {p.id: 1.0 for p in t.participants}
    elif isinstance(pour, (list, tuple)):
        for ref in pour:
            for pid in expand(ref):
                out.setdefault(pid, 1.0)
    elif isinstance(pour, str) and pour not in ("tous", "tout le monde", "all", ""):
        for ref in [x.strip() for x in pour.replace(";", ",").split(",") if x.strip()]:
            for pid in expand(ref):
                out.setdefault(pid, 1.0)
    if sauf:
        for ref in (sauf if isinstance(sauf, (list, tuple)) else str(sauf).replace(";", ",").split(",")):
            if str(ref).strip():
                for pid in expand(str(ref).strip()):
                    out.pop(pid, None)
    if not out:
        raise OpsError("Pour qui est cette dépense ? Indique « tous » ou une liste de personnes.")
    return out


def payers_of(t: M.Trip, paye_par) -> tuple:
    """« asso » | nom | {nom ou « asso »: montant} | [{"qui": nom, "montant": m}]
    -> (payer_id ou None, [(payer_id ou None, centimes)] ou None si un seul payeur)."""
    if isinstance(paye_par, dict) and not any(k in paye_par for k in ("qui", "participant", "montant")):
        items = list(paye_par.items())
    elif isinstance(paye_par, list):
        items = []
        for x in paye_par:
            if not isinstance(x, dict):
                raise OpsError("Plusieurs payeurs : donne une liste de {\"qui\": nom ou \"asso\", \"montant\": …}.")
            items.append((x.get("qui") or x.get("participant") or x.get("nom") or "asso", x.get("montant")))
    else:
        return (None if _is_asso(paye_par, t) else person(t, paye_par, "personne qui a payé").id), None
    out = []
    for who, amount in items:
        pid = None if _is_asso(who, t) else person(t, who, "personne qui a payé").id
        cents = euros(amount, f"montant payé par {who}")
        if cents <= 0:
            raise OpsError(f"Le montant payé par {who} doit être positif.")
        out.append((pid, cents))
    return None, out


def _set_payments(e: M.Expense, payments):
    for x in list(e.payments):
        e.payments.remove(x)
    for pid, cents in payments or []:
        e.payments.append(M.ExpensePayment(payer_id=pid, amount=cents))


def _check_payments(e: M.Expense):
    if e.payments and not e.pending:
        total = sum(x.amount for x in e.payments)
        if total > e.best_known():
            raise OpsError(f"Les payeurs de « {e.label} » totalisent {E.fmt(total)}, plus que son montant "
                           f"({E.fmt(e.best_known())}).")


# ---------------------------------------------------------------- lecture

def trip_summary(t: M.Trip, mode=E.ACTUAL) -> dict:
    r = S.compute_trip(t, mode)
    return dict(
        id=t.id, nom=t.name, lieu=t.place, organisateur=S.organizer_name(t),
        debut=t.start.isoformat() if t.start else None, fin=t.end.isoformat() if t.end else None,
        nb_participants=len(t.participants), cout_total=to_euros(r.total_cost),
        deja_recu=to_euros(r.total_paid), reste_a_recevoir=to_euros(r.to_collect),
        reste_a_rembourser=to_euros(r.to_refund),
    )


def trip_detail(t: M.Trip, mode=E.ACTUAL) -> dict:
    r = S.compute_trip(t, mode)
    names = {p.id: p.name for p in t.participants}
    wd = {w.participant_id: w for w in t.withdrawals}
    out = trip_summary(t, mode)
    out["note"] = t.note or ""
    out["comment_payer"] = S.pay_info(t)
    out["cloture_le"] = t.closed_on.isoformat() if t.closed_on else None     # voyage clôturé : plus de modification
    out["points_a_verifier"] = [n["texte"] for n in S.ai_notes(t)]
    out["asso"] = S.trip_asso_name(t)
    out["groupes"] = {g["name"]: [p.name for p in g["members"]] for g in S.trip_groups(t)}
    out["caisse"] = S.cashier(t).name if S.cashier(t) else "asso"
    out["mode_de_calcul"] = "réel" if mode == E.ACTUAL else "prévu"
    out["categories"] = [c.name for c in t.categories]
    out["participants"] = []
    for p in t.participants:
        ps = r.people[p.id]
        out["participants"].append(dict(
            id=p.id, nom=p.name, telephone=p.phone, email=p.email, note=p.note,
            famille=p.family.name if p.family else None, accompagnant=bool(p.companion),
            roles=[r if r in S.ROLES else S.role_label(r) for r in p.role_list if r not in (S.DIVER, S.COMPANION)],  # plongeur / acc. : « accompagnant »
            desiste=p.id in wd, sa_part=to_euros(ps.share), a_verse=to_euros(ps.paid),
            a_avance=to_euros(ps.advanced), rembourse_par_asso=to_euros(ps.refunded),
            reste=None if p.family else to_euros(ps.balance),
            situation=(f"compte réglé avec la famille {p.family.name} (voir « familles »)" if p.family else
                       "doit encore" if ps.balance > 0 else "l'asso lui doit" if ps.balance < 0 else "à jour"),
        ))
    out["equipe"] = dict(organisateur=next(iter(x.name for x in S.with_role(t, S.ORGANIZER)), None),
                         co_organisateurs=[x.name for x in S.with_role(t, S.CO_ORGANIZER)])
    out["familles"] = [dict(nom=a.name, membres=[m.name for m in a.members], paie=r.people[a.referent].name,
                            sa_part=to_euros(a.share), deja_donne=to_euros(a.paid + a.advanced - a.refunded),
                            reste=to_euros(a.balance)) for a in r.accounts if a.is_family]
    out["depenses"] = [dict(
        id=e.id, libelle=e.label, categorie=e.category.name if e.category else None,
        date=e.date.isoformat() if e.date else None, prevu=to_euros(e.planned), reel=to_euros(e.actual),
        type=e.kind or "depense", a_venir=e.pending, sous_categorie=e.subcategory or None,
        rembourse=to_euros(e.refund) if e.refund else None,
        tickets=[dict(montant=to_euros(x["cents"]), date=x["date"].isoformat() if x["date"] else None)
                 for x in e.ticket_list] or None,
        rembourse_a=(names.get(e.refund_to_id, "asso") if e.refund_to_id else "asso") if e.refund else None,
        paye_par=({(names.get(pid, "asso") if pid else "asso"): to_euros(c) for pid, c in e.splits()}
                  if e.multi_payer else (names.get(e.payer_id, "asso") if e.payer_id else "asso")),
        pour={names[s_.participant_id]: s_.weight for s_ in e.shares if s_.participant_id in names},
        fixe={names[s_.participant_id]: to_euros(s_.fixed) for s_ in e.shares
              if s_.fixed is not None and s_.participant_id in names} or None,
        justificatifs=[l.receipt.code for l in e.receipt_links],
    ) for e in t.expenses]
    out["mouvements"] = [dict(id=m.id, type=m.kind, participant=m.participant.name, montant=to_euros(m.amount),
                              date=m.date.isoformat() if m.date else None, note=m.note,
                              moyen=S.pay_method_label(m.method).lower() or None) for m in t.movements]
    out["ajustements"] = [dict(
        id=a.id, participant=a.participant.name, sur=a.expense.label if a.expense else "total",
        valeur=to_euros(a.value), verrouille=a.fixed, compense_par=a.compensation,
        compensateur=a.compensator.name if a.compensator else None, motif=a.reason) for a in t.adjustments]
    out["desistements"] = [dict(participant=w.participant.name, date=w.date.isoformat() if w.date else None,
                                pris_en_charge_par=w.absorber,
                                parts_conservees=[e.label for e in t.expenses if e.id in w.kept_ids])
                           for w in t.withdrawals]
    out["justificatifs"] = [dict(id=x.id, code=x.code, fichier=x.original_name,
                                 depenses=[l.expense.label for l in x.links]) for x in t.receipts]
    out["entre_nous"] = []
    for sub in t.subaccounts:
        v = S.sub_view(sub)
        out["entre_nous"].append(dict(
            id=sub.id, nom=sub.name, membres=[names.get(m) for m in sub.members], total=to_euros(v["total"]),
            a_regler=[dict(de=names.get(a), a=names.get(b), montant=to_euros(c)) for a, b, c in v["settlements"]]))
    out["pris_en_charge_par_asso"] = [dict(sur=c.label, pourquoi=c.reason, montant=to_euros(c.cents))
                                      for c in r.asso_charges]
    out["ecart_arrondi_asso"] = to_euros(r.rounding_gain)
    out["equilibre"] = r.is_balanced()
    out["alertes"] = [a["text"] for a in S.alerts(t, r)]
    return out


def balances(t: M.Trip, mode=E.ACTUAL) -> dict:
    r = S.compute_trip(t, mode)
    return {a.name: to_euros(a.balance) for a in r.accounts}


def _diff(before: dict, after: dict) -> list:
    out = []
    for k in after:
        if before.get(k) != after[k]:
            out.append(dict(personne=k, reste_avant=before.get(k), reste_apres=after[k]))
    return out


# ---------------------------------------------------------------- écritures élémentaires
# Chaque fonction do_* travaille dans une session ouverte (s) sur un voyage (t) : elles servent aux
# méthodes de la classe Ops (une action = un « Annuler ») et à l'import d'un fichier complet (tout d'un coup).

ASSO_WORDS = ("", "asso", "l'asso", "association", "caisse", "la caisse")


def _is_asso(ref, t: M.Trip | None = None) -> bool:
    """« asso », « l'asso », « caisse »… ou le nom donné à l'asso (s'il n'est pas aussi celui d'un participant)."""
    if ref is None or E.sort_key(str(ref)).strip() in ASSO_WORDS:
        return True
    if t is None:
        return False
    name = S.trip_asso_name(t)
    key = E.sort_key(str(ref)).strip()
    return name != "l'asso" and key == E.sort_key(name).strip() and \
        not any(E.sort_key(p.name).strip() == key for p in t.participants)


def do_create_trip(s, nom, debut=None, fin=None, lieu="", organisateur="") -> M.Trip:
    if not (nom or "").strip():
        raise OpsError("Quel est le nom du voyage ?")
    t = M.Trip(name=nom.strip(), place=lieu or "", organizer=organisateur or "",
               start=date_of(debut, "date de début"), end=date_of(fin, "date de fin"))
    t.categories = S.new_trip_categories(s)
    s.add(t)
    s.flush()
    return t


def _truthy(v) -> bool:
    return v is True or E.sort_key(str(v)).strip() in ("1", "true", "oui", "yes", "o", "x", "accompagnant")


def do_add_participants(s, t, noms, inclure_dans_depenses_communes=True, famille=None, accompagnant=False):
    """noms : liste de prénoms, ou de dicts {nom, telephone, email, note, famille, accompagnant}.
    Les nouveaux sont ajoutés aux dépenses existantes de tout le monde, et à celles qui concernaient exactement les
    membres d'une étiquette, d'un groupe ou d'une famille qu'ils rejoignent."""
    existing = {E.sort_key(p.name) for p in t.participants}
    before = S.label_members(t)
    added, skipped = [], []
    for n in noms:
        info = n if isinstance(n, dict) else {"nom": n}
        name = str(info.get("nom") or "").strip()
        if not name:
            continue
        if E.sort_key(name) in existing:
            skipped.append(name)
            continue
        kp = S.known_person(t, name)              # déjà venu sur un autre voyage : ses infos par défaut
        if "accompagnant" in info or accompagnant or not kp:
            comp = _truthy(info.get("accompagnant", accompagnant) or False)
        else:
            comp = kp["companion"]
        p = M.Participant(name=name, phone=str(info.get("telephone") or (kp or {}).get("phone") or ""),
                          email=str(info.get("email") or (kp or {}).get("email") or ""),
                          note=str(info.get("note") or ""), companion=comp)
        t.participants.append(p)
        s.flush()
        fam = info.get("famille", famille)
        if fam:
            S.set_family(s, t, p, str(fam))
        try:
            rs = S.parse_roles(info.get("roles") or [], t)
        except ValueError as e:
            raise OpsError(f"{name} : {e}")
        if comp and S.COMPANION not in S.trip_roles(t):   # voyage sans ce rôle : l'information n'est pas perdue,
            S.add_trip_role(t, S.ROLES[S.COMPANION])      # le rôle « Accompagnant » est remis dans ce voyage
        if S.DIVER not in rs and S.COMPANION not in rs:   # par défaut : plongeur, sauf accompagnant (s'ils existent ici)
            dflt = S.COMPANION if comp else S.DIVER
            if dflt in S.trip_roles(t):
                rs.append(dflt)
        S.set_roles(t, p, rs)
        existing.add(E.sort_key(name))
        added.append(p)
    if inclure_dans_depenses_communes and added:
        mine = {p.id for p in added}
        exps = {e.id: e for e in t.expenses}
        sug = S.join_suggestions(t, before)
        S.flag_checks(t, sug)                    # parts personnalisées : « à vérifier » dans l'application
        for x in sug:                            # seulement les dépenses partagées exactement entre des groupes
            if x["pid"] in mine and x["kind"] == "groupes":
                exps[x["eid"]].shares.append(M.ExpenseShare(participant_id=x["pid"], weight=1.0))
        s.flush()
    return added, skipped


def _refund_of(t, rembourse, rembourse_a):
    if rembourse in (None, "", 0, "0"):
        return None, None
    cents = euros(rembourse, "montant remboursé")
    if cents <= 0:
        raise OpsError("Le montant remboursé doit être positif.")
    return cents, (None if _is_asso(rembourse_a, t) else person(t, rembourse_a, "personne qui a reçu le remboursement").id)


def tickets_of(value) -> list | None:
    """Plusieurs tickets pour un montant réel : ["80,12", "69,10"], [{"montant": …, "date": …}] ou « 80,12 + 69,10 ».
    -> [(centimes, date|None)], ou None si c'est un montant simple."""
    if isinstance(value, str) and "+" in value.strip().lstrip("+"):
        value = [x for x in value.split("+") if x.strip()]
    if not isinstance(value, list):
        return None
    out = []
    for i, x in enumerate(value, 1):
        if isinstance(x, dict):
            c, d = euros(x.get("montant"), f"montant du ticket {i}"), date_of(x.get("date"))
        else:
            c, d = euros(x, f"montant du ticket {i}"), None
        if c is None or c <= 0:
            raise OpsError(f"Le ticket {i} doit avoir un montant positif.")
        out.append((c, d))
    if not out:
        raise OpsError("Liste de tickets vide.")
    return out


def do_add_expense(s, t, libelle, categorie=None, montant_prevu=None, montant_reel=None, date=None,
                   paye_par="asso", pour="tous", parts=None, note="", type="depense", a_venir=False,
                   sauf=None, rembourse=None, rembourse_a=None, sous_categorie=None) -> M.Expense:
    """type = « depense » ou « recette » (argent reçu : remboursement, subvention… ; paye_par = qui l'a reçu).
    a_venir = montant pas encore connu (ni prévu ni réel)."""
    if not (libelle or "").strip():
        raise OpsError("Quel est le libellé de la dépense ?")
    kind = {"depense": "depense", "dépense": "depense", "recette": "recette"}.get(str(type or "depense").lower())
    if kind is None:
        raise OpsError("Type : « depense » ou « recette » (argent reçu : remboursement, subvention…).")
    planned = euros(montant_prevu, "montant prévu", allow_none=True)
    tickets = tickets_of(montant_reel)
    actual = sum(c for c, _ in tickets) if tickets else euros(montant_reel, "montant réel", allow_none=True)
    if planned is None and actual is None and not a_venir:
        raise OpsError(f"Quel est le montant de « {libelle} » ? (prévu et/ou réel ; s'il n'est pas encore connu, "
                       f"mets \"a_venir\": true)")
    if (planned or 0) < 0 or (actual or 0) < 0:
        raise OpsError("Un montant doit être positif. Pour de l'argent reçu (remboursement d'un fournisseur, "
                       "subvention…), utilise « type »: « recette ».")
    payer, payments = payers_of(t, paye_par)
    w = weights_of(t, pour, parts, sauf)
    refund, refund_to = _refund_of(t, rembourse, rembourse_a)
    if refund and refund > ((actual if actual is not None else planned) or 0):
        raise OpsError(f"Le remboursement ({E.fmt(refund)}) dépasse le montant de « {libelle} ».")
    e = M.Expense(label=libelle.strip(), category_id=category_id(s, t, categorie), kind=kind,
                  planned=planned, actual=actual, date=date_of(date), payer_id=payer, note=note or "",
                  refund=refund, refund_to_id=refund_to, subcategory=str(sous_categorie or "").strip()[:100])
    if tickets:
        e.set_tickets(tickets)
        e.date = e.date or next((d for _, d in tickets if d), None)
    t.expenses.append(e)
    s.flush()
    for pid, wv in w.items():
        e.shares.append(M.ExpenseShare(participant_id=pid, weight=wv))
    s.flush()
    S.normalize_shares(e)                        # pas de « parts » : montants fixés 🔒 (voir services)
    _set_payments(e, payments)
    _check_payments(e)
    s.flush()
    return e


def do_update_expense(s, t, depense, libelle=None, categorie=None, montant_prevu=None, montant_reel=None,
                      date=None, paye_par=None, pour=None, parts=None, note=None, sauf=None, rembourse=None,
                      rembourse_a=None, sous_categorie=None) -> tuple:
    """Modifie une dépense existante ; renvoie (dépense, [changements lisibles])."""
    e = expense_of(t, depense)
    changes = []
    names = {p.id: p.name for p in t.participants}
    if libelle and libelle.strip() != e.label:
        changes.append(f"nom « {e.label} » → « {libelle.strip()} »")
        e.label = libelle.strip()
    if categorie:
        cid = category_id(s, t, categorie)
        if cid != e.category_id:
            changes.append(f"catégorie → {next(c.name for c in t.categories if c.id == cid)}")
            e.category_id = cid
    tickets = tickets_of(montant_reel)
    if montant_reel is not None:
        before_t = [c for c in (x["cents"] for x in e.ticket_list)]
        e.set_tickets(tickets or [])
        if tickets and [c for c, _ in tickets] != before_t:
            changes.append("tickets : " + " + ".join(E.fmt(c) for c, _ in tickets))
        if tickets:
            montant_reel = to_euros(sum(c for c, _ in tickets))
    for field, value, label in (("planned", montant_prevu, "prévu"), ("actual", montant_reel, "réel")):
        if value is None:
            continue
        new = euros(value, f"montant {label}", allow_none=True)
        if new is not None and new < 0:
            raise OpsError("Un montant de dépense doit être positif.")
        old = getattr(e, field)
        if new != old:
            changes.append(f"{label} {E.fmt(old) if old is not None else '—'} → {E.fmt(new) if new is not None else '—'}")
            setattr(e, field, new)
    if e.planned is None and e.actual is None and not (montant_prevu is None and montant_reel is None):
        raise OpsError(f"« {e.label} » n'aurait plus aucun montant (ni prévu ni réel).")
    if date:
        e.date = date_of(date)
    if paye_par is not None:
        before = S.payer_label(e, names)
        payer, payments = payers_of(t, paye_par)
        e.payer_id = payer
        _set_payments(e, payments)
        s.flush()
        after = S.payer_label(e, names)
        if after != before:
            changes.append(f"payé par {before} → {after}".replace("l'asso", "l’asso"))
    if pour is not None or parts is not None or sauf is not None:
        w = weights_of(t, pour if (pour is not None or parts is not None) else "tous", parts, sauf)
        old = {s_.participant_id: s_.weight for s_ in e.shares}
        if w != old:
            changes.append("pour qui : " + ", ".join(names[pid] + (f" ×{E.fmt_weight(E.to_weight(x))}" if x != 1 else "")
                                                  for pid, x in w.items()))
            for sh in list(e.shares):
                e.shares.remove(sh)
            s.flush()
            for pid, wv in w.items():
                e.shares.append(M.ExpenseShare(participant_id=pid, weight=wv))
    if note is not None and note != e.note:
        e.note = note
        changes.append("note modifiée")
    if sous_categorie is not None and str(sous_categorie).strip() != (e.subcategory or ""):
        e.subcategory = str(sous_categorie).strip()[:100]
        changes.append(f"sous-catégorie → {e.subcategory or 'aucune'}")
    if rembourse is not None:
        refund, refund_to = _refund_of(t, rembourse, rembourse_a)
        if (refund, refund_to) != (e.refund, e.refund_to_id):
            who = next((p.name for p in t.participants if p.id == refund_to), "l’asso")
            changes.append(f"remboursé {E.fmt(refund)} (reçu par {who})" if refund else "plus de remboursement")
            e.refund, e.refund_to_id = refund, refund_to
    if e.refund and e.refund > e.best_known():
        raise OpsError(f"Le remboursement ({E.fmt(e.refund)}) dépasse le montant de « {e.label} ».")
    _check_payments(e)
    s.flush()
    s.refresh(e)
    if S.normalize_shares(e):                    # des parts (IA) : montants fixés 🔒, une fois le montant connu
        s.flush()
    return e, changes


def do_delete_expense(s, t, depense) -> str:
    e = expense_of(t, depense)
    label = e.label
    for a in [a for a in t.adjustments if a.expense_id == e.id]:
        t.adjustments.remove(a)
    for w in t.withdrawals:
        if e.id in w.kept_ids:
            w.kept = ",".join(str(x) for x in w.kept_ids if x != e.id)
    t.expenses.remove(e)          # delete-orphan : la dépense, ses parts et ses liens justificatifs partent avec
    s.flush()
    return label


def do_update_participant(s, t, participant, nouveau_nom=None, telephone=None, email=None, note=None,
                          famille=None, accompagnant=None, roles=None) -> tuple:
    p = person(t, participant)
    changes = []
    if nouveau_nom and nouveau_nom.strip() != p.name:
        if any(E.sort_key(o.name) == E.sort_key(nouveau_nom) for o in t.participants if o.id != p.id):
            raise OpsError(f"Il y a déjà un participant nommé « {nouveau_nom} ».")
        changes.append(f"nom → {nouveau_nom.strip()}")
        p.name = nouveau_nom.strip()
    for attr, value, label in (("phone", telephone, "téléphone"), ("email", email, "e-mail"), ("note", note, "note")):
        if value is not None and str(value) != getattr(p, attr):
            setattr(p, attr, str(value))
            changes.append(f"{label} → {value}")
    if famille is not None:
        old = p.family.name if p.family else ""
        S.set_family(s, t, p, str(famille))
        new = p.family.name if p.family else ""
        if new != old:
            changes.append(f"famille → {new or 'aucune'}")
    if accompagnant is not None:
        comp = _truthy(accompagnant) if accompagnant is not False else False
        if comp != bool(p.companion) or (not comp and S.DIVER not in p.role_list):
            keep = [r for r in p.role_list if r not in (S.DIVER, S.COMPANION)]
            S.set_roles(t, p, keep + [S.COMPANION if comp else S.DIVER])
            changes.append("devient accompagnant" if comp else "devient plongeur")
    if roles is not None:
        try:
            rs = S.parse_roles(roles, t)
        except ValueError as e:
            raise OpsError(str(e))
        if S.DIVER not in rs and S.COMPANION not in rs:   # « roles » ne parle que de l'équipe : garder plongeur / acc.
            rs = rs + [r for r in p.role_list if r in (S.DIVER, S.COMPANION)]
        if sorted(rs) != sorted(p.role_list):
            S.set_roles(t, p, rs)
            changes.append("rôles → " + (", ".join(S.role_label(r) for r in rs) or "aucun"))
    s.flush()
    return p, changes


def do_delete_movement(s, t, participant, montant, type="versement", date=None) -> M.Movement:
    p = person(t, participant)
    amt = euros(montant)
    d = date_of(date)
    match = [m for m in t.movements if m.participant_id == p.id and m.kind == type and m.amount == amt
             and (d is None or m.date == d)]
    if not match:
        have = "; ".join(f"{m.kind} {E.fmt(m.amount)} le {m.date.strftime('%d/%m/%Y') if m.date else '?'}"
                         for m in t.movements if m.participant_id == p.id) or "aucun"
        raise OpsError(f"Je ne trouve pas de {type} de {E.fmt(amt)} pour {p.name}. Ses mouvements : {have}")
    if len(match) > 1:
        raise OpsError(f"{len(match)} {type}s de {E.fmt(amt)} pour {p.name} : précise la date.")
    m = match[0]
    t.movements.remove(m)
    s.delete(m)
    s.flush()
    return m


def do_update_trip(s, t, nom=None, lieu=None, debut=None, fin=None, organisateur=None, note=None) -> list:
    changes = []
    if nom and nom.strip() != t.name:
        t.name = nom.strip()
        changes.append(f"nom → {t.name}")
    if lieu is not None and lieu != t.place:
        t.place = lieu
        changes.append(f"lieu → {lieu}")
    if debut:
        t.start = date_of(debut, "date de début")
    if fin:
        t.end = date_of(fin, "date de fin")
    if organisateur is not None and organisateur != t.organizer:
        t.organizer = str(organisateur)
    if note is not None and str(note) != (t.note or ""):
        t.note = str(note)
        changes.append("note du voyage mise à jour")
    s.flush()
    return changes


def do_add_movement(s, t, participant, montant, type="versement", date=None, note="", moyen=None) -> M.Movement:
    if type not in ("versement", "remboursement"):
        raise OpsError("Le type doit être « versement » (participant → asso) ou « remboursement » (asso → participant).")
    amt = euros(montant)
    if amt <= 0:
        raise OpsError("Le montant doit être positif.")
    method = S.pay_method(moyen)
    if method is None:
        raise OpsError(f"Moyen de paiement inconnu : « {moyen} ». Choix possibles : "
                       + ", ".join(label.lower() for _, label in S.PAY_METHODS) + " (ou rien).")
    p = person(t, participant)
    m = M.Movement(participant_id=p.id, kind=type, amount=amt, date=date_of(date) or dt.date.today(),
                   note=note or "", method=method)
    t.movements.append(m)
    s.flush()
    return m


class FixedShare:
    """Résultat d'un « ajustement » converti en montant fixé dans une dépense (même interface pour les messages)."""
    def __init__(self, participant, expense, cents, reason=""):
        self.participant, self.expense, self.value, self.reason, self.fixed = participant, expense, cents, reason, True
        self.expense_id = expense.id


def do_fix_share(s, t, participant, depense, montant, in_cents: bool = False) -> tuple:
    """Montant fixé 🔒 pour une personne dans une dépense : le reste est réparti entre les autres personnes cochées.
    montant vide / None : la personne n'est plus fixée. Renvoie (participant, dépense, centimes ou None)."""
    e = expense_of(t, depense) if not isinstance(depense, M.Expense) else depense
    p = person(t, participant)
    sh = next((x for x in e.shares if x.participant_id == p.id), None)
    if sh is None:
        raise OpsError(f"{p.name} n'est pas parmi les personnes de « {e.label} » (« pour »). Ajoute-le d'abord.")
    val = (int(montant) if montant is not None else None) if in_cents else \
        euros(montant, "montant fixé", allow_none=True)
    if val is not None and val < 0:
        raise OpsError("Un montant fixé doit être positif.")
    sh.fixed = val
    total = max(0, (e.best_known() or 0) - (e.refund or 0))     # coût partagé : remboursement déduit
    fixed = [x.fixed for x in e.shares if x.fixed is not None]
    if fixed and not total:
        raise OpsError(f"« {e.label} » n'a pas encore de montant : impossible d'y fixer la part de {p.name}.")
    if sum(fixed) > total:
        raise OpsError(f"Les montants fixés de « {e.label} » font {E.fmt(sum(fixed))}, plus que la dépense "
                       f"({E.fmt(total)}).")
    if fixed and len(fixed) == len(e.shares) and sum(fixed) != total:
        raise OpsError(f"« {e.label} » : tous les montants sont fixés mais ils font {E.fmt(sum(fixed))} pour une "
                       f"dépense de {E.fmt(total)}. Laisse au moins une personne sans montant fixé.")
    s.flush()
    return p, e, val


def do_adjust(s, t, participant, montant, type="reduction", sur="total", compense_par="autres",
              compensateur=None, motif="", verrouiller=False) -> M.Adjustment:
    """Remise ou supplément. Sur une dépense, compensé par les autres : devient un montant fixé 🔒 dans la dépense
    (le seul mécanisme à l'écran). Sur une dépense mais compensé par l'asso ou une personne : devient une remise
    sur le total de la personne (même effet sur son compte), avec la dépense dans le motif."""
    val = euros(montant)
    comp = {"autres": E.OTHERS, "others": E.OTHERS, "groupe": E.OTHERS, "asso": E.ASSO,
            "personne": E.PERSON, "person": E.PERSON}.get(str(compense_par).lower())
    if comp is None:
        raise OpsError("Qui compense ? « autres » (les autres participants), « asso », ou « personne » + compensateur.")
    if type not in ("reduction", "supplement", "nouveau_montant"):
        raise OpsError("Type d'ajustement : « reduction », « supplement » ou « nouveau_montant ».")
    p = person(t, participant)
    e = None if sur in (None, "", "total") else expense_of(t, sur)
    r0 = S.compute_trip(t)
    current = r0.share(p.id, e.id) if e else r0.people[p.id].share
    if e is not None:
        new = val if type == "nouveau_montant" else current + (-abs(val) if type == "reduction" else abs(val))
        if comp == E.OTHERS:
            do_fix_share(s, t, p, e, new, in_cents=True)
            return FixedShare(p, e, new, motif or "")
        type, val, motif = ("reduction" if new < current else "supplement"), abs(new - current), \
            (f"{motif} — sur « {e.label} »" if motif else f"sur « {e.label} »")
        e, verrouiller = None, False
        current = r0.people[p.id].share
    if type == "nouveau_montant":
        fixed, value = (True, val) if verrouiller else (False, val - current)
    else:
        fixed, value = False, (-abs(val) if type == "reduction" else abs(val))
        if verrouiller:
            fixed, value = True, current + value
    cid = None
    if comp == E.PERSON:
        cid = person(t, compensateur, "personne qui compense").id
        if cid == p.id:
            raise OpsError("La personne qui compense doit être différente.")
    a = M.Adjustment(participant_id=p.id, expense_id=e.id if e else None, value=value,
                     fixed=fixed, compensation=comp, compensator_id=cid, reason=motif or "")
    t.adjustments.append(a)
    s.flush()
    return a


def do_withdraw(s, t, participant, pris_en_charge_par="groupe", parts_conservees=(), date=None,
                remboursement=None, date_remboursement=None) -> M.Withdrawal:
    absorber = {"groupe": E.GROUP, "group": E.GROUP, "asso": E.ASSO}.get(str(pris_en_charge_par).lower())
    if not absorber:
        raise OpsError("Qui reprend la part de la personne qui se désiste : « groupe » ou « asso » ?")
    p = person(t, participant)
    if any(w.participant_id == p.id for w in t.withdrawals):
        raise OpsError(f"{p.name} est déjà désisté(e).")
    kept = [expense_of(t, x).id for x in (parts_conservees or [])]
    d = date_of(date) or dt.date.today()
    refund = euros(remboursement, "remboursement", allow_none=True)
    if refund is not None and refund < 0:
        raise OpsError("Le remboursement doit être positif.")
    w = M.Withdrawal(participant_id=p.id, date=d, absorber=absorber, kept=",".join(map(str, kept)))
    t.withdrawals.append(w)
    S.flag_withdrawal(t, p.id, kept, absorber)   # montants fixés 🔒 dans ses dépenses : « à vérifier »
    if refund:
        t.movements.append(M.Movement(participant_id=p.id, kind="remboursement", amount=refund,
                                      date=date_of(date_remboursement) or d, note="Remboursement désistement"))
    s.flush()
    return w


def do_create_subaccount(s, t, nom, membres) -> M.SubAccount:
    if not (nom or "").strip():
        raise OpsError("Quel nom donner au compte « Entre nous » ?")
    ids = []
    for m in membres or []:
        pid = person(t, m).id
        if pid not in ids:
            ids.append(pid)
    if len(ids) < 2:
        raise OpsError("Il faut au moins 2 personnes dans un compte « Entre nous ».")
    sub = M.SubAccount(name=nom.strip(), member_ids=",".join(map(str, ids)))
    t.subaccounts.append(sub)
    s.flush()
    return sub


def do_add_subexpense(s, t, compte, libelle, montant, paye_par, pour="tous", date=None) -> M.SubExpense:
    amt = euros(montant)
    if amt <= 0:
        raise OpsError("Le montant doit être positif.")
    sub = compte if isinstance(compte, M.SubAccount) else next(
        (x for x in t.subaccounts if str(x.id) == str(compte) or E.sort_key(x.name) == E.sort_key(str(compte))), None)
    if not sub:
        raise OpsError(f"Compte « Entre nous » introuvable : {compte}. Comptes : "
                       + (", ".join(f"{x.id} = {x.name}" for x in t.subaccounts) or "aucun"))
    payer = person(t, paye_par).id
    if payer not in sub.members:
        raise OpsError("La personne qui a payé ne fait pas partie de ce compte.")
    if pour in (None, "", "tous"):
        bens = sub.members
    else:
        bens = [person(t, x).id for x in (pour if isinstance(pour, list) else str(pour).split(","))]
        if any(b not in sub.members for b in bens):
            raise OpsError(f"« {libelle} » : une des personnes ne fait pas partie du compte « {sub.name} ».")
    if not (libelle or "").strip():
        raise OpsError("Quel est le libellé de la dépense « Entre nous » ?")
    se = M.SubExpense(label=libelle.strip(), amount=amt, payer_id=payer, date=date_of(date),
                      weights=",".join(f"{b}:1" for b in bens))
    sub.expenses.append(se)
    s.flush()
    return se


# ---------------------------------------------------------------- classe Ops

class Ops:
    """Toutes les écritures passent par ici (avec instantané « Annuler »)."""

    def __init__(self, db: Database):
        self.db = db

    def _write(self, label: str, fn, trip_id=None, simulation: bool = False) -> dict:
        d = self.db.dir
        if (d / "VERROUILLE").exists() or (d.name == ".verrouille" and (d.parent / "VERROUILLE").exists()):
            raise OpsError("Trip Compta est verrouillé (voyages protégés par un mot de passe) : ouvre l'application "
                           "et saisis le mot de passe, puis réessaie.")
        s = self.db.Session()
        snapped = False
        try:
            t = get_trip(s, trip_id) if trip_id is not None else None
            if S.is_closed(t):
                raise OpsError(S.CLOSED_MSG)
            before = balances(t) if t else {}
            if not simulation:
                self.db.snapshot(label)
                snapped = True
            result = fn(s, t) or {}
            s.flush()
            tid = t.id if t is not None else result.get("voyage_id")
            if tid is not None:
                s.expire_all()
                t = s.get(M.Trip, tid)
                if S.store_draws(t):                  # centimes d'arrondi : tirage des dépenses nouvelles ou changées
                    s.flush()
                after = balances(t)
                result["changements"] = _diff(before, after)
                r = S.compute_trip(t)
                result["equilibre"] = r.is_balanced()
                result["avertissements"] = r.warnings
            if simulation:
                s.rollback()
                for k in [k for k in result if k.endswith("_id")]:
                    result.pop(k)
                result["simulation"] = True
                result["message"] = "Simulation : rien n'a été enregistré."
            else:
                s.commit()
                result.setdefault("message", f"OK : {label}")
            return result
        except Exception:
            s.rollback()
            if snapped:
                self.db.drop_last_snapshot()
            raise
        finally:
            s.close()

    # ---- lecture
    def list_trips(self) -> list:
        with self.db.Session() as s:
            return [trip_summary(t) for t in s.query(M.Trip).order_by(M.Trip.id).all()]

    def trip(self, trip_id, mode=E.ACTUAL) -> dict:
        with self.db.Session() as s:
            return trip_detail(get_trip(s, trip_id), mode)

    # ---- voyages / participants
    def create_trip(self, nom, debut=None, fin=None, lieu="", organisateur="") -> dict:
        if not (nom or "").strip():
            raise OpsError("Quel est le nom du voyage ?")

        def fn(s, _):
            t = do_create_trip(s, nom, debut, fin, lieu, organisateur)
            return dict(voyage_id=t.id, message=f"Voyage « {t.name} » créé (id {t.id}).")
        return self._write(f"création du voyage « {nom} » (IA)", fn)

    def add_participants(self, trip_id, noms: Iterable[str], inclure_dans_depenses_communes=True,
                         simulation=False, famille=None, accompagnant=False) -> dict:
        def fn(s, t):
            added, skipped = do_add_participants(s, t, noms, inclure_dans_depenses_communes, famille, accompagnant)
            return dict(ajoutes=[p.name for p in added], deja_presents=skipped,
                        ids={p.name: p.id for p in added})
        return self._write("ajout de participants (IA)", fn, trip_id, simulation)

    # ---- dépenses
    def add_expense(self, trip_id, libelle, categorie=None, montant_prevu=None, montant_reel=None, date=None,
                    paye_par="asso", pour="tous", parts=None, note="", simulation=False, type="depense",
                    a_venir=False, sauf=None, rembourse=None, rembourse_a=None, sous_categorie=None) -> dict:
        def fn(s, t):
            e = do_add_expense(s, t, libelle, categorie, montant_prevu, montant_reel, date, paye_par, pour,
                               parts, note, type, a_venir, sauf, rembourse, rembourse_a, sous_categorie)
            return dict(depense_id=e.id, message=f"Dépense « {e.label} » enregistrée.")
        return self._write(f"ajout de la dépense « {libelle} » (IA)", fn, trip_id, simulation)

    def update_expense(self, trip_id, depense, libelle=None, categorie=None, montant_prevu=None, montant_reel=None,
                       date=None, paye_par=None, pour=None, parts=None, simulation=False, note=None,
                       sous_categorie=None, sauf=None, rembourse=None, rembourse_a=None) -> dict:
        def fn(s, t):
            e, changes = do_update_expense(s, t, depense, libelle, categorie, montant_prevu, montant_reel, date,
                                           paye_par, pour, parts, note=note, sauf=sauf, rembourse=rembourse,
                                           rembourse_a=rembourse_a, sous_categorie=sous_categorie)
            return dict(depense_id=e.id, message=f"Dépense « {e.label} » modifiée"
                        + (f" : {' ; '.join(changes)}." if changes else " (rien n'a changé)."))
        return self._write(f"modification de la dépense « {depense} » (IA)", fn, trip_id, simulation)

    def delete_expense(self, trip_id, depense) -> dict:
        def fn(s, t):
            return dict(message=f"Dépense « {do_delete_expense(s, t, depense)} » supprimée.")
        return self._write(f"suppression de la dépense « {depense} » (IA)", fn, trip_id)

    # ---- argent
    def add_movement(self, trip_id, participant, montant, type="versement", date=None, note="",
                     simulation=False, moyen=None) -> dict:
        def fn(s, t):
            p_ = person(t, participant)
            same = S.duplicate_movement(t, p_.id, type, euros(montant), date_of(date) or dt.date.today()) \
                if type in ("versement", "remboursement") else None
            m = do_add_movement(s, t, participant, montant, type, date, note, moyen)
            msg = f"{type.capitalize()} de {E.fmt(m.amount)} ({m.participant.name}) enregistré."
            if same is not None:
                msg += (f" Attention : un {type} identique était déjà noté"
                        + (f" le {same.date:%d/%m/%Y}" if same.date else "") + " (peut-être un doublon).")
            return dict(message=msg)
        return self._write(f"{type} de {participant} (IA)", fn, trip_id, simulation)

    # ---- ajustements
    def adjust(self, trip_id, participant, montant, type="reduction", sur="total", compense_par="autres",
               compensateur=None, motif="", verrouiller=False, simulation=False) -> dict:
        def fn(s, t):
            a = do_adjust(s, t, participant, montant, type, sur, compense_par, compensateur, motif, verrouiller)
            if isinstance(a, FixedShare):
                return dict(message=f"{a.participant.name} : montant fixé à {E.fmt(a.value)} dans « {a.expense.label} ».")
            return dict(message=f"{'Remise' if a.value < 0 else 'Supplément'} pour {a.participant.name} enregistré.")
        return self._write(f"ajustement de {participant} (IA)", fn, trip_id, simulation)

    # ---- désistement
    def withdraw(self, trip_id, participant, pris_en_charge_par="groupe", parts_conservees=(), date=None,
                 remboursement=None, simulation=False) -> dict:
        def fn(s, t):
            w = do_withdraw(s, t, participant, pris_en_charge_par, parts_conservees, date, remboursement)
            return dict(message=f"Désistement de {w.participant.name} enregistré.")
        return self._write(f"désistement de {participant} (IA)", fn, trip_id, simulation)

    # ---- justificatifs
    def add_receipt(self, trip_id, chemin: str, depenses=()) -> dict:
        src = Path(chemin).expanduser()
        if not src.is_file():
            raise OpsError(f"Fichier introuvable : {chemin}")
        kind = fichiers.sniff_file(src)
        if kind is None:
            raise OpsError(f"« {src.name} » n'est pas accepté : un justificatif doit être {fichiers.WHAT}.")

        def fn(s, t):
            stored = f"{t.id}_{uuid.uuid4().hex}{kind[1]}"
            shutil.copyfile(src, self.db.receipts_dir / stored)
            number = max([r.number for r in t.receipts] or [0]) + 1
            rec = M.Receipt(trip_id=t.id, number=number, original_name=src.name, stored_name=stored,
                            content_type=kind[0])
            t.receipts.append(rec)
            s.flush()
            for ref in depenses or []:
                s.add(M.ReceiptLink(receipt_id=rec.id, expense_id=expense_of(t, ref).id))
            return dict(code=rec.code, message=f"Justificatif {rec.code} ajouté.")
        return self._write(f"ajout du justificatif {src.name} (IA)", fn, trip_id)

    # ---- entre nous
    def create_subaccount(self, trip_id, nom, membres) -> dict:
        def fn(s, t):
            sub = do_create_subaccount(s, t, nom, membres)
            return dict(compte_id=sub.id, message=f"Compte « {sub.name} » créé.")
        return self._write(f"création du compte « {nom} » (IA)", fn, trip_id)

    def add_subexpense(self, trip_id, compte, libelle, montant, paye_par, pour="tous", date=None) -> dict:
        def fn(s, t):
            do_add_subexpense(s, t, compte, libelle, montant, paye_par, pour, date)
            return dict(message=f"« {libelle} » ajoutée au compte « {compte} ».")
        return self._write(f"dépense « {libelle} » entre nous (IA)", fn, trip_id)

    # ---- import d'un voyage complet (fichier préparé par une IA)
    def import_trip(self, data, voyage=None, simulation=False, verifier_id=False) -> dict:
        """voyage = id d'un voyage existant (ou « nouveau »). verifier_id : le JSON doit porter le même voyage_id
        (toujours vérifié s'il est présent)."""
        from .importer import run_import
        if voyage not in (None, "", "nouveau"):
            def fn(s, t):
                return run_import(s, data, t, expected_id=t.id, require_id=verifier_id)
            return self._write("changements proposés par l'IA", fn, voyage, simulation)

        def fn_new(s, _):
            return run_import(s, data, None)
        return self._write("import d'un voyage (IA)", fn_new, None, simulation)

    # ---- rôles
    def set_roles(self, trip_id, participant, roles, simulation=False) -> dict:
        def fn(s, t):
            p = person(t, participant)
            try:
                S.set_roles(t, p, S.parse_roles(roles, t))
            except ValueError as e:
                raise OpsError(str(e))
            return dict(message=f"Rôles de {p.name} : {', '.join(S.role_label(r) for r in p.role_list) or 'aucun'}.")
        return self._write(f"rôles de {participant} (IA)", fn, trip_id, simulation)

    # ---- annuler
    def undo(self) -> dict:
        label = self.db.undo()
        return dict(message=f"Annulé : {label}" if label else "Rien à annuler.")
