"""Un seul endroit écrit dans les voyages.

Les formulaires (web.py) et l'API / l'IA (ops.py, importer.py) lisent chacun leur entrée (champs d'un formulaire,
JSON, noms ou numéros, euros tapés), puis appellent ces fonctions avec des valeurs déjà lues : des centimes, des
dates, des identifiants. Les lignes sont créées et modifiées ici, et les règles qui protègent les comptes y sont
vérifiées, pour les deux chemins : ce qui est vrai à l'écran l'est aussi pour l'IA, et inversement.

Ces fonctions n'enregistrent pas (pas de commit) et ne font pas de copie « Défaire » : c'est l'affaire de
l'appelant (une copie par action de l'utilisateur, une seule pour tout un import).
"""
from __future__ import annotations

from . import engine as E
from . import models as M
from . import services as S

MOVES = ("versement", "remboursement")
KEEP = object()             # save_expense(category=KEEP) : la catégorie ne change pas
KINDS = ("depense", "recette")
WITHDRAWAL_REFUND = "Remboursement désistement"


class WriteError(ValueError):
    """Écriture refusée, dite en clair. `field` : le champ du formulaire en cause, quand il y en a un."""

    def __init__(self, msg: str, field: str | None = None):
        super().__init__(msg)
        self.field = field


def _clean(text, limit: int | None = None) -> str:
    out = " ".join(str(text or "").split())
    return out[:limit] if limit else out


def _ids(t: M.Trip) -> set:
    return {p.id for p in t.participants}


def _member(t: M.Trip, pid, what: str, field: str | None = None) -> int:
    if pid not in _ids(t):
        raise WriteError(f"{what} ne fait pas partie de ce voyage.", field)
    return pid


# ---------------------------------------------------------------- voyage

def create_trip(s, name, place="", organizer="", start=None, end=None, diving: bool = True) -> M.Trip:
    """Un voyage tout neuf, avec ses catégories et ses rôles de départ (les étiquettes du dernier voyage sont
    reprises ; sans plongée : ni rôle Plongeur / Accompagnant, ni catégorie de plongée)."""
    name = _clean(name)
    if not name:
        raise WriteError("Donne un nom au voyage", "name")
    t = M.Trip(name=name, place=_clean(place), organizer=_clean(organizer), start=start, end=end)
    S.start_trip(s, t, diving=diving)
    s.add(t)
    s.flush()
    return t


# ---------------------------------------------------------------- participants

def add_participant(s, t: M.Trip, name, roles=(), companion: bool = False, phone="", email="", note="",
                    family=None) -> M.Participant:
    """Une personne de plus dans le voyage. Deux personnes ne portent jamais le même nom (accents et majuscules
    ignorés) : c'est par leur nom qu'on les désigne partout."""
    name = _clean(name)
    if not name:
        raise WriteError("Écris un nom", "name")
    if E.sort_key(name) in {E.sort_key(p.name) for p in t.participants}:
        raise WriteError(f"{name} est déjà dans la liste", "name")
    p = M.Participant(name=name, companion=bool(companion), phone=str(phone or "").strip(),
                      email=str(email or "").strip(), note=str(note or "").strip())
    t.participants.append(p)
    s.flush()
    S.set_roles(t, p, list(roles or []))
    if family and str(family).strip():
        S.set_family(s, t, p, str(family))
    return p


def join_group_expenses(t: M.Trip, added, before: dict) -> list:
    """Des nouveaux venus rejoignent les dépenses partagées exactement entre les membres d'une étiquette, d'un groupe
    ou d'une famille qu'ils rejoignent (et celles de tout le monde). Les dépenses à parts personnalisées sont
    seulement marquées « à vérifier ». `before` : S.label_members(t) AVANT l'ajout. Renvoie les dépenses rejointes."""
    mine = {p.id for p in added}
    exps = {e.id: e for e in t.expenses}
    sug = S.join_suggestions(t, before)
    S.flag_checks(t, sug)
    joined = []
    for x in sug:
        if x["pid"] in mine and x["kind"] == "groupes":
            join_expense(exps[x["eid"]], x["pid"]) and joined.append(exps[x["eid"]])
    return joined


def join_expense(e: M.Expense, pid: int) -> bool:
    """Ajoute une personne à une dépense, à part égale. False si elle y était déjà."""
    if pid in {x.participant_id for x in e.shares}:
        return False
    e.shares.append(M.ExpenseShare(participant_id=pid, weight=1.0))
    return True


# ---------------------------------------------------------------- catégories

def category_id(s, t: M.Trip, name) -> int:
    """La catégorie de ce nom dans le voyage (accents et majuscules ignorés) ; créée si elle manque, et retenue
    pour les prochains voyages. Sans nom : « Divers »."""
    name = _clean(name, 100) or "Divers"
    for c in t.categories:
        if E.sort_key(c.name) == E.sort_key(name):
            return c.id
    c = M.Category(name=name, audience="", position=len(t.categories))
    t.categories.append(c)
    S.add_to_library(s, name)
    s.flush()
    return c.id


# ---------------------------------------------------------------- dépenses

def check_expense(t: M.Trip, e: M.Expense | None = None, *, label, kind="depense", planned=None, actual=None,
                  payer_id=None, payments=(), weights=None, fixed=None, refund=None, refund_to_id=None,
                  pending: bool = False, **_) -> None:
    """Refuse (WriteError) une dépense qui fausserait les comptes. Ne modifie rien : à appeler avant la copie
    « Défaire » ; save_expense le refait de toute façon."""
    label = _clean(label)
    if not label:
        raise WriteError("Donne un nom à la dépense", "label")
    if kind not in KINDS:
        raise WriteError("Type : « depense » ou « recette » (argent reçu : remboursement, subvention…).")
    if (planned or 0) < 0 or (actual or 0) < 0:
        raise WriteError("Un montant doit être positif. Pour de l'argent reçu (remboursement d'un fournisseur, "
                         "subvention…), utilise « type »: « recette ».", "actual")
    if planned is None and actual is None and not pending:
        raise WriteError(f"Quel est le montant de « {label} » ? (prévu et/ou réel ; s'il n'est pas encore connu, "
                         f"mets \"a_venir\": true)", "actual")
    best = (actual if actual is not None else planned) or 0
    if payer_id is not None:
        _member(t, payer_id, "La personne qui a payé", "payer")
    if refund is not None and refund < 0:
        raise WriteError("Le montant remboursé doit être positif.", "refund")
    if refund:
        if refund > best:
            raise WriteError(f"Le remboursement ({E.fmt(refund)}) dépasse le montant de « {label} ».", "refund")
        if refund_to_id is not None:
            _member(t, refund_to_id, "La personne qui a reçu le remboursement", "refund_to")
    payments = list(payments or [])
    for pid, cents in payments:
        if pid is not None:
            _member(t, pid, "Un des payeurs")
        if not cents or cents <= 0:
            raise WriteError("Le montant payé par chaque payeur doit être positif.")
    if payments and (planned is not None or actual is not None) and sum(c for _, c in payments) > best:
        raise WriteError(f"Les payeurs de « {label} » totalisent {E.fmt(sum(c for _, c in payments))}, plus que "
                         f"son montant ({E.fmt(best)}).")
    if weights is not None:
        if not weights:
            raise WriteError("Pour qui est cette dépense ? Indique « tous » ou une liste de personnes.")
        for pid, w in weights.items():
            _member(t, pid, "Une des personnes de « Pour qui ? »")
            if w is None or w <= 0:
                raise WriteError("Un nombre de parts doit être positif.")
        fixed = {pid: c for pid, c in (fixed or {}).items() if c is not None}
        if fixed:
            if set(fixed) - set(weights):
                raise WriteError("Un montant fixé concerne une personne qui n'est pas dans « Pour qui ? ».")
            if any(c < 0 for c in fixed.values()):
                raise WriteError("Un montant fixé doit être positif.")
            if not best:
                raise WriteError("Indique le montant de la dépense avant de fixer des montants")
            total = max(0, best - (refund or 0))       # c'est le coût, remboursement déduit, qui est partagé
            got = sum(fixed.values())
            if got > total:
                what = "le coût à partager" if refund else "la dépense"
                raise WriteError(f"Les montants fixés font {E.fmt(got)}, plus que {what} ({E.fmt(total)})")
            if len(fixed) == len(weights) and got != total:
                raise WriteError(f"Il reste {E.fmt(total - got)} à répartir : tous les montants sont fixés. "
                                 f"Laisse au moins une personne sans montant fixé, ou corrige les montants.")


def save_expense(s, t: M.Trip, e: M.Expense | None = None, *, label, category=None, subcategory="",
                 kind="depense", planned=None, actual=None, tickets=None, date=None, payer_id=None, payments=(),
                 weights=None, fixed=None, refund=None, refund_to_id=None, note="", pending: bool = False,
                 reviewed: bool = False) -> M.Expense:
    """Crée (e=None) ou réécrit une dépense, avec toutes ses valeurs.

    - category : le nom de la catégorie (créée si besoin ; KEEP = celle qu'elle a déjà) ; subcategory : retenue pour les prochaines saisies ;
    - tickets : [(centimes, date ou None)] ; None = ne pas toucher à ceux qui existent ;
    - payer_id : la personne qui a payé (None = l'association) ; payments : [(personne ou None, centimes)] quand ils
      sont plusieurs — un seul payeur pour tout le montant redevient simplement « payé par » ;
    - weights : {personne: parts} ; None = ne pas toucher à « Pour qui ? » ; fixed : {personne: centimes fixés 🔒} ;
    - pending : montant pas encore connu (ni prévu ni réel) ;
    - reviewed : la dépense vient d'être revue à l'écran, les marques « à vérifier » tombent.
    """
    label = _clean(label)
    check_expense(t, e, label=label, kind=kind, planned=planned, actual=actual, payer_id=payer_id,
                  payments=payments, weights=weights, fixed=fixed, refund=refund, refund_to_id=refund_to_id,
                  pending=pending)
    if e is None:
        e = M.Expense(label=label)
        t.expenses.append(e)
    e.label = label
    if category is not KEEP or e.category_id is None:
        e.category_id = category_id(s, t, None if category is KEEP else category)
    e.subcategory = _clean(subcategory, 100)
    if e.subcategory:
        s.flush()
        cname = next((c.name for c in t.categories if c.id == e.category_id), None)
        if cname:
            S.add_subcategory(s, cname, e.subcategory)
    e.kind = kind
    e.planned, e.actual = planned, actual
    if tickets is not None:
        e.set_tickets(list(tickets))
        e.date = date or next((d for _, d in tickets if d), None)
    else:
        e.date = date
    e.refund, e.refund_to_id = (refund, refund_to_id) if refund else (None, None)
    e.note = str(note or "").strip()
    payments = list(payments or [])
    best = (actual if actual is not None else planned) or 0
    if len(payments) == 1 and payments[0][1] == best:
        payer_id, payments = payments[0][0], []
    e.payer_id = None if payments else payer_id
    s.flush()
    for x in list(e.payments):
        e.payments.remove(x)
    for pid, cents in payments:
        e.payments.append(M.ExpensePayment(payer_id=pid, amount=cents))
    if weights is not None:
        for x in list(e.shares):
            e.shares.remove(x)
        s.flush()
        for pid, w in weights.items():
            e.shares.append(M.ExpenseShare(participant_id=pid, weight=w, fixed=(fixed or {}).get(pid)))
    if reviewed:
        e.checks.clear()
    s.flush()
    s.refresh(e)
    if S.normalize_shares(e):                    # des parts : montants fixés 🔒, une fois le montant connu
        s.flush()
    return e


def delete_expense(s, t: M.Trip, e: M.Expense) -> str:
    """Supprime une dépense et tout ce qui la désigne : ses anciens ajustements, et sa mention dans les « parts
    conservées » d'un désistement (sinon une dépense créée plus tard pourrait reprendre son numéro, et la personne
    désistée se retrouver à la payer). Renvoie son nom."""
    label = e.label
    for a in [a for a in t.adjustments if a.expense_id == e.id]:
        t.adjustments.remove(a)
    for w in t.withdrawals:
        if e.id in w.kept_ids:
            w.kept = ",".join(str(x) for x in sorted(w.kept_ids) if x != e.id)
    t.expenses.remove(e)          # delete-orphan : la dépense, ses parts et ses liens justificatifs partent avec
    s.flush()
    return label


# ---------------------------------------------------------------- argent reçu / rendu

def add_movement(t: M.Trip, pid, kind, cents, date=None, note="", method="") -> M.Movement:
    """Un versement (participant → caisse) ou un remboursement (caisse → participant)."""
    if kind not in MOVES:
        raise WriteError("Le type doit être « versement » (participant → asso) ou « remboursement » "
                         "(asso → participant).")
    if not cents or cents <= 0:
        raise WriteError("Le montant doit être positif.", "amount")
    if S.pay_method(method) is None:
        raise WriteError(f"Moyen de paiement inconnu : « {method} ». Choix possibles : "
                         + ", ".join(label.lower() for _, label in S.PAY_METHODS) + " (ou rien).", "method")
    _member(t, pid, "Cette personne", "participant")
    m = M.Movement(participant_id=pid, kind=kind, amount=cents, date=date, note=str(note or "").strip(),
                   method=S.pay_method(method))
    t.movements.append(m)
    return m


def delete_movement(s, t: M.Trip, m: M.Movement) -> None:
    t.movements.remove(m)
    s.delete(m)
    s.flush()


def settle_batch(t: M.Trip, result: E.Result, kind: str, referents, date=None, method: str = "",
                 note: str = "") -> list:
    """« Cocher plusieurs lignes, puis Reçu » : un versement (ou un remboursement) par compte coché, du montant
    qu'il reste exactement à régler."""
    return [add_movement(t, acc.referent, kind, cents, date=date, note=note, method=method)
            for acc, cents in S.settle_amounts(result, kind, referents)]


# ---------------------------------------------------------------- remises et suppléments

def save_discount(s, t: M.Trip, pid, value: int, compensation=E.OTHERS, compensator_id=None, reason="",
                  fixed: bool = False, adjustment: M.Adjustment | None = None) -> M.Adjustment:
    """Remise (value négative) ou supplément (positive) sur tout le voyage d'une personne, en centimes, compensé par
    les autres, par l'association ou par une personne. `adjustment` : la ligne à modifier, sinon une nouvelle.
    fixed : value est alors le total que la personne paie, quoi qu'il arrive (ancien réglage, gardé pour l'IA)."""
    _member(t, pid, "Cette personne", "participant")
    if compensation not in (E.OTHERS, E.ASSO, E.PERSON):
        raise WriteError("Qui compense ? « autres » (les autres participants), « asso », ou « personne » + compensateur.")
    if compensation == E.PERSON:
        if compensator_id not in _ids(t):
            raise WriteError("Choisis la personne qui compense", "compensator")
        if compensator_id == pid:
            raise WriteError("La personne qui compense doit être différente.", "compensator")
    else:
        compensator_id = None
    a = adjustment
    if a is None:
        a = M.Adjustment(expense_id=None)
        t.adjustments.append(a)
    a.participant_id, a.value, a.fixed = pid, int(value), bool(fixed)
    a.compensation, a.compensator_id, a.reason = compensation, compensator_id, str(reason or "").strip()
    s.flush()
    return a


# ---------------------------------------------------------------- désistements

def save_withdrawal(s, t: M.Trip, pid, date=None, absorber=E.GROUP, kept=(), note=None, refund=None,
                    refund_date=None) -> tuple:
    """Note (ou modifie) le désistement d'une personne. kept : les dépenses qu'elle paie quand même (celles d'un
    autre voyage sont ignorées). refund : un remboursement fait à cette occasion, en centimes.
    Renvoie (désistement, True s'il vient d'être créé)."""
    _member(t, pid, "Cette personne", "participant")
    if absorber not in (E.GROUP, E.ASSO):
        raise WriteError("Qui reprend la part de la personne qui se désiste : « groupe » ou « asso » ?")
    if refund is not None and refund < 0:
        raise WriteError("Le remboursement doit être positif.", "refund")
    mine = {e.id for e in t.expenses}
    kept = sorted({int(x) for x in kept or []} & mine)
    w = next((x for x in t.withdrawals if x.participant_id == pid), None)
    new = w is None
    if new:
        w = M.Withdrawal(participant_id=pid)
        t.withdrawals.append(w)
        S.flag_withdrawal(t, pid, kept, absorber)    # montants fixés 🔒 dans ses dépenses : « à vérifier »
    w.date, w.absorber, w.kept = date, absorber, ",".join(str(x) for x in kept)
    if note is not None:
        w.note = str(note).strip()
    if refund:
        add_movement(t, pid, "remboursement", refund, date=refund_date or date, note=WITHDRAWAL_REFUND)
    s.flush()
    return w, new
