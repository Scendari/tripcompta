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
from . import preparation as P
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
    join_for_all = for_all_expenses(t)
    t.participants.append(p)
    s.flush()
    for e in join_for_all:                     # « pour tout le monde » : le nouveau venu y entre
        join_expense(e, p.id)
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


def for_all_expenses(t: M.Trip) -> list:
    """Les dépenses « pour tout le monde » (voyage lancé depuis sa préparation) qu'un nouveau venu rejoint tout seul :
    celles dont les parts sont encore simples (tous les participants, parts égales, aucun montant fixé). Une dépense
    dont les parts ont été personnalisées depuis (montant fixé, parts changées…) perd ce caractère : la règle
    habituelle s'applique alors (proposée à l'écran, « à vérifier » si les parts sont personnalisées)."""
    ids = _ids(t)
    out = []
    for e in t.expenses:
        if not e.for_all:
            continue
        if {x.participant_id for x in e.shares} == ids and all(x.weight == 1 and x.fixed is None for x in e.shares):
            out.append(e)
        else:
            e.for_all = False
    return out


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
                  pending: bool = False, for_all: bool = False, **_) -> None:
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
        # personne : refusé, sauf une dépense « pour tout le monde » d'un voyage qui n'a encore aucun participant
        # (voyage lancé depuis sa préparation)
        if not weights and (t.participants or not (for_all or (e is not None and e.for_all))):
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
                 reviewed: bool = False, for_all: bool = False) -> M.Expense:
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
                  pending=pending, for_all=for_all)
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
        # « pour tout le monde » : tant que personne n'est encore là, ou tant que tout le monde y est à parts égales
        everyone = set(weights) == _ids(t) and all(w == 1 for w in weights.values()) and not any(
            c is not None for c in (fixed or {}).values())
        e.for_all = bool(e.for_all or for_all) and (not t.participants or everyone)
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


# ---------------------------------------------------------------- dates de paiement

def set_pay_dates(t: M.Trip, steps, pid=None) -> list:
    """Fixe les dates de paiement : pour tout le voyage (pid=None, montants par personne) ou pour le compte d'une
    personne (montants pour tout son compte). steps : [(nom, date, centimes ou None = « tout ce qui reste »)] ;
    une liste vide retire ces dates. Renvoie les étapes retenues, dans l'ordre des dates."""
    if pid is not None:
        _member(t, pid, "Cette personne", "pour")
    steps = list(steps or [])
    if len(steps) > S.PAY_STEPS_MAX:
        raise WriteError(f"{S.PAY_STEPS_MAX} dates de paiement au plus.")
    clean = []
    for i, (name, date, cents) in enumerate(steps):
        if date is None:
            raise WriteError(f"Indique la date de « {_clean(name) or 'ce paiement'} »", f"date_{i}")
        if cents is not None and cents <= 0:
            raise WriteError("Un montant doit être positif (ou vide : « tout ce qui reste »).", f"montant_{i}")
        clean.append(dict(nom=_clean(name, S.PAY_STEP_NAME_MAX), date=date, montant=cents))
    clean.sort(key=lambda x: x["date"])
    rest = [i for i, x in enumerate(clean) if x["montant"] is None]
    if len(rest) > 1 or (rest and rest[0] != len(clean) - 1):
        raise WriteError("« Tout ce qui reste » (montant vide) ne peut être demandé qu'une fois, à la dernière date.")
    fixed = 0
    for x in clean:                              # un nom par défaut : Acompte, 2e paiement…, Solde
        fixed += x["montant"] is not None
        if not x["nom"]:
            x["nom"] = "Solde" if x["montant"] is None else "Acompte" if fixed == 1 else f"{fixed}e paiement"
    data = S.pay_dates(t)
    if pid is None:
        data["etapes"] = clean
    else:
        data["comptes"] = {k: v for k, v in data["comptes"].items() if k != pid}
        if clean:
            data["comptes"][pid] = clean
    t.pay_dates = S.dump_pay_dates(data)
    return clean


def set_account_pay_dates(t: M.Trip, acc, steps) -> list:
    """Des dates rien que pour un compte (une personne, ou toute une famille) : rangées chez celui qui paie pour
    le compte. Une liste vide : le compte reprend les dates du voyage."""
    for m in acc.members:
        if m.id != acc.referent:
            set_pay_dates(t, [], pid=m.id)
    return set_pay_dates(t, steps, pid=acc.referent)


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


# ---------------------------------------------------------------- préparer un voyage (projets, postes, offres)
# Calcul : compta/preparation.py. Une seule offre retenue par poste : la règle est tenue ici, pas à l'écran.

def _plan_of(plan: M.Plan, obj, what: str):
    if obj is None or obj.plan_id != plan.id:
        raise WriteError(f"{what} ne fait pas partie de ce projet.")
    return obj


def _people(people) -> int:
    if not isinstance(people, int) or isinstance(people, bool) or people < 1:
        raise WriteError("Il faut au moins une personne pour calculer un prix par personne.", "people")
    return people


def check_plan(*, name, people=1, place="", start=None, end=None, note="", margin_kind=P.PERCENT, margin_value=0,
               announced=None) -> None:
    """Refuse (WriteError) un projet impossible. Ne modifie rien : à appeler avant la copie « Défaire »."""
    if not _clean(name, 200):
        raise WriteError("Donne un nom au projet", "name")
    _people(people)
    if margin_kind not in P.MARGIN_KINDS:
        raise WriteError("Marge : en pourcentage ou en euros par personne.", "margin_kind")
    if margin_value is None or margin_value < 0:
        raise WriteError("La marge ne peut pas être négative.", "margin_value")
    if announced is not None and announced < 0:
        raise WriteError("Le prix annoncé ne peut pas être négatif.", "announced")
    if start and end and end < start:
        raise WriteError("La date de retour est avant la date de départ.", "end")


def save_plan(s, plan: M.Plan | None = None, *, name, people=1, place="", start=None, end=None, note="",
              margin_kind=P.PERCENT, margin_value=0, announced=None) -> M.Plan:
    """Crée (plan=None) ou réécrit un voyage en préparation, avec toutes ses valeurs (update_plan : seulement
    certaines). margin_value : centièmes de % (500 = 5 %) pour « pourcent », centimes par personne pour « euros » ;
    announced : prix annoncé par personne (centimes) ou None."""
    check_plan(name=name, people=people, place=place, start=start, end=end, note=note, margin_kind=margin_kind,
               margin_value=margin_value, announced=announced)
    name = _clean(name, 200)
    if plan is None:
        plan = M.Plan(name=name)
        s.add(plan)
    plan.name, plan.people, plan.place, plan.start, plan.end = name, people, _clean(place, 200), start, end
    plan.note = str(note or "").strip()
    plan.margin_kind, plan.margin_value, plan.announced = margin_kind, int(margin_value), announced
    s.flush()
    return plan


PLAN_FIELDS = ("name", "people", "place", "start", "end", "note", "margin_kind", "margin_value", "announced")


def update_plan(s, plan: M.Plan, **fields) -> M.Plan:
    """Change seulement les champs donnés (« et si on était 9 ? ») ; les autres gardent leur valeur. Mêmes refus que
    save_plan, qui fait l'écriture."""
    return save_plan(s, plan, **plan_values(plan, **fields))


def plan_values(plan: M.Plan, **fields) -> dict:
    """Les valeurs du projet, avec ces champs changés (pour check_plan avant d'écrire)."""
    unknown = set(fields) - set(PLAN_FIELDS)
    if unknown:
        raise TypeError(f"champ inconnu {sorted(unknown)}")
    values = {k: getattr(plan, k) for k in PLAN_FIELDS}
    values.update(fields)
    return values


def delete_plan(s, plan: M.Plan) -> str:
    """Supprime un voyage en préparation, ses postes et ses offres (pas le voyage créé à partir de lui)."""
    name = plan.name
    plan.offers.clear()          # les offres (et leurs liens aux postes) d'abord, puis les postes
    s.flush()
    s.delete(plan)
    s.flush()
    return name


def _item_name(plan: M.Plan, name, skip: M.PlanItem | None = None) -> str:
    name = _clean(name, 200)
    if not name:
        raise WriteError("Donne un nom au poste (Avion, Hôtel…)", "name")
    if E.sort_key(name) in {E.sort_key(i.name) for i in plan.items if i is not skip}:
        raise WriteError(f"Le poste « {name} » existe déjà dans ce projet", "name")
    return name


def add_plan_item(s, plan: M.Plan, name) -> M.PlanItem:
    """Un poste de plus, à la fin de la liste. Deux postes d'un projet ne portent pas le même nom."""
    name = _item_name(plan, name)
    it = M.PlanItem(name=name, position=max((i.position for i in plan.items), default=-1) + 1)
    plan.items.append(it)
    s.flush()
    return it


def rename_plan_item(s, plan: M.Plan, item: M.PlanItem, name) -> M.PlanItem:
    _plan_of(plan, item, "Ce poste")
    item.name = _item_name(plan, name, skip=item)
    s.flush()
    return item


def order_plan_items(s, plan: M.Plan, ids) -> list:
    """Range les postes dans cet ordre (`ids` : tous les postes du projet, chacun une fois). L'ordre compte aussi
    pour la répartition d'un forfait : le dernier de ses postes reçoit le reste. Renvoie les postes rangés."""
    ids = list(ids)
    by_id = {i.id: i for i in plan.items}
    if sorted(ids) != sorted(by_id) or len(set(ids)) != len(ids):
        raise WriteError("Pour ranger les postes, donne chacun des postes de ce projet une fois.", "items")
    for n, x in enumerate(ids):
        by_id[x].position = n
    s.flush()
    s.expire(plan, ["items"])                    # relue dans le nouvel ordre
    return list(plan.items)


def move_plan_item(s, plan: M.Plan, item: M.PlanItem, index: int) -> list:
    """Déplace un poste à la place `index` (0 = en premier ; au-delà de la fin : en dernier)."""
    _plan_of(plan, item, "Ce poste")
    ids = [i.id for i in plan.items if i.id != item.id]
    ids.insert(max(0, min(int(index), len(ids))), item.id)
    return order_plan_items(s, plan, ids)


def delete_plan_item(s, plan: M.Plan, item: M.PlanItem) -> list:
    """Supprime un poste. Ses offres qui ne couvrent que lui partent avec lui ; un forfait qui couvre aussi d'autres
    postes les garde et perd sa répartition (elle ne tomberait plus juste). Renvoie les noms des offres supprimées."""
    _plan_of(plan, item, "Ce poste")
    gone = []
    for o in list(plan.offers):
        mine = [ln for ln in o.links if ln.item_id == item.id]
        if not mine:
            continue
        if len(o.links) == len(mine):
            gone.append(o.label)
            plan.offers.remove(o)
            continue
        for ln in mine:
            o.links.remove(ln)
        o.split = ""
        for ln in o.links:
            ln.share = None
    s.flush()
    plan.items.remove(item)
    s.flush()
    return gone


def _unchoose_overlaps(plan: M.Plan, offer: M.PlanOffer) -> list:
    """Retire « retenue » aux autres offres qui partagent au moins un poste avec `offer`. Renvoie leurs noms."""
    mine = {ln.item_id for ln in offer.links}
    out = []
    for o in plan.offers:
        if o is not offer and o.chosen and mine & {ln.item_id for ln in o.links}:
            o.chosen = False
            out.append(o.label)
    return out


def check_offer(plan: M.Plan, *, label, price, counting=P.GROUP, quantity=1, capacity=None, items=(), split="",
                shares=None) -> tuple:
    """Refuse (WriteError) une offre impossible. Ne modifie rien. Renvoie (postes sans doublon, parts ou None)."""
    if not _clean(label):
        raise WriteError("Donne un nom à l'offre (le prestataire, par exemple)", "label")
    if price is None or price < 0:
        raise WriteError("Le prix ne peut pas être négatif (0 si c'est inclus).", "price")
    if counting not in P.COUNTINGS:
        raise WriteError("Compte le prix par personne, pour tout le groupe ou pour un nombre de personnes.", "counting")
    if not isinstance(quantity, int) or quantity < 1:
        raise WriteError("La quantité doit être d'au moins 1.", "quantity")
    if counting == P.LOT and (not isinstance(capacity, int) or capacity < 1):
        raise WriteError("Ce prix vaut pour combien de personnes ? Au moins 1 (un taxi de 4 places, une chambre de 2…).",
                         "capacity")
    ids, seen = [], set()
    for x in items or ():
        if x not in seen:
            seen.add(x)
            ids.append(x)
    if not ids:
        raise WriteError("Choisis au moins un poste pour cette offre.", "items")
    known = {i.id for i in plan.items}
    if any(x not in known for x in ids):
        raise WriteError("Ce poste ne fait pas partie de ce projet.", "items")
    if split not in P.SPLITS:
        raise WriteError("Répartition : en montants ou en pourcentages.", "split")
    if not split or len(ids) < 2:
        return ids, None
    shares = dict(shares or {})
    if set(shares) != set(ids) or any(v is None or v < 0 for v in shares.values()):
        raise WriteError("Donne une part (0 ou plus) pour chaque poste de l'offre.", "shares")
    total = sum(shares.values())
    if split == P.AMOUNTS and total != price:
        raise WriteError(f"Les montants par poste font {E.fmt(total)}, mais le prix de l'offre est "
                         f"{E.fmt(price)} : il faut que ça tombe juste.", "shares")
    if split == P.PERCENT and total != P.FULL:
        raise WriteError(f"Les pourcentages par poste font {_pct(total)}, il faut 100 %.", "shares")
    return ids, shares


def _pct(v: int) -> str:
    whole, rest = divmod(v, 100)
    return f"{whole} %" if not rest else f"{whole},{rest:02d}".rstrip("0") + " %"


def save_offer(s, plan: M.Plan, offer: M.PlanOffer | None = None, *, label, price, counting=P.GROUP, quantity=1,
               quantity_label="", capacity=None, items=(), split="", shares=None, note="",
               chosen: bool | None = None) -> M.PlanOffer:
    """Crée (offer=None) ou réécrit une offre.

    - price : centimes (0 = inclus) ; counting : « personne », « groupe » ou « lot » (capacity = places d'un lot) ;
    - items : les postes couverts (un ou plusieurs) ;
    - split : "" (un seul bloc), « montant » (shares = {poste: centimes}, dont la somme fait le prix) ou « pourcent »
      (shares = {poste: centièmes de %}, somme 10000) ; ignoré pour une offre d'un seul poste ;
    - chosen : None = ne pas changer. Une offre retenue (ou qui le reste en changeant de postes) retire « retenue »
      aux autres offres qui partagent un poste avec elle.
    """
    if offer is not None:
        _plan_of(plan, offer, "Cette offre")
    ids, shares = check_offer(plan, label=label, price=price, counting=counting, quantity=quantity,
                              capacity=capacity, items=items, split=split, shares=shares)
    if offer is None:
        offer = M.PlanOffer(label=_clean(label, 200))
        plan.offers.append(offer)
    offer.label, offer.price, offer.counting, offer.quantity = _clean(label, 200), price, counting, quantity
    offer.capacity = capacity if counting == P.LOT else None
    offer.quantity_label = _clean(quantity_label, 50)
    offer.note = str(note or "").strip()
    offer.split = split if shares else ""
    for ln in list(offer.links):
        offer.links.remove(ln)
    s.flush()
    for x in ids:
        offer.links.append(M.PlanOfferItem(item_id=x, share=shares[x] if shares else None))
    if chosen is not None:
        offer.chosen = bool(chosen)
    if offer.chosen:
        _unchoose_overlaps(plan, offer)
    s.flush()
    return offer


def choose_offer(s, plan: M.Plan, offer: M.PlanOffer, chosen: bool = True) -> list:
    """Retient (ou ne retient plus) une offre. La retenir retire « retenue » à toute offre qui partage au moins un
    poste avec elle ; rien n'est re-retenu tout seul. Renvoie les noms des offres qui ne sont plus retenues."""
    _plan_of(plan, offer, "Cette offre")
    offer.chosen = bool(chosen)
    out = _unchoose_overlaps(plan, offer) if chosen else []
    s.flush()
    return out


def delete_offer(s, plan: M.Plan, offer: M.PlanOffer) -> str:
    _plan_of(plan, offer, "Cette offre")
    label = offer.label
    plan.offers.remove(offer)
    s.flush()
    return label


# ---------------------------------------------------------------- échéances d'une offre (côté prestataires)

def check_deadline(plan: M.Plan, offer: M.PlanOffer, *, label, date, amount=None) -> None:
    _plan_of(plan, offer, "Cette offre")
    if not _clean(label):
        raise WriteError("Dis ce qu'il faut faire (par exemple « Acompte à verser »).", "label")
    if date is None:
        raise WriteError("Indique la date de l'échéance.", "date")
    if amount is not None and amount < 0:
        raise WriteError("Le montant ne peut pas être négatif.", "amount")


def save_deadline(s, plan: M.Plan, offer: M.PlanOffer, d: M.PlanDeadline | None = None, *, label, date,
                  amount=None, done: bool = False) -> M.PlanDeadline:
    """Crée (d=None) ou réécrit une échéance de cette offre : libellé, date (obligatoire), montant (facultatif)."""
    check_deadline(plan, offer, label=label, date=date, amount=amount)
    if d is not None and d.offer_id != offer.id:
        raise WriteError("Cette échéance ne fait pas partie de cette offre.")
    if d is None:
        d = M.PlanDeadline(label=_clean(label, 200), date=date)
        offer.deadlines.append(d)
    d.label, d.date, d.amount, d.done = _clean(label, 200), date, amount, bool(done)
    s.flush()
    return d


def _deadline_of(plan: M.Plan, deadline: M.PlanDeadline | None) -> M.PlanOffer:
    offer = next((o for o in plan.offers if deadline is not None and o.id == deadline.offer_id), None)
    if offer is None:
        raise WriteError("Cette échéance ne fait pas partie de ce projet.")
    return offer


def set_deadline_done(s, plan: M.Plan, deadline: M.PlanDeadline, done: bool = True) -> M.PlanDeadline:
    _deadline_of(plan, deadline)
    deadline.done = bool(done)
    s.flush()
    return deadline


def delete_deadline(s, plan: M.Plan, deadline: M.PlanDeadline) -> str:
    offer = _deadline_of(plan, deadline)
    label = deadline.label
    offer.deadlines.remove(deadline)
    s.flush()
    return label


# ---------------------------------------------------------------- lancer le voyage

def launch_plan(s, plan: M.Plan, *, cashier: str = "asso", asso_name: str = "", holder: str = "",
                diving: bool = False, today=None) -> M.Trip:
    """Crée le voyage (ses comptes) à partir du projet : une dépense PRÉVUE par offre retenue (un forfait réparti :
    une par poste), « pour tout le monde », y compris les participants qui seront ajoutés ensuite. Le projet garde
    le lien (sens unique : modifier le projet ne change plus les comptes du voyage). cashier : « asso » ou « personne »
    (holder : son nom, ajouté aux participants)."""
    import datetime as _dt
    if plan_trip(s, plan) is not None:
        raise WriteError("Ce projet a déjà été lancé : son voyage existe.")
    lines = S.plan_launch_lines(plan)
    if not [x for x in lines if not x["included"]]:
        raise WriteError("Retiens au moins une offre avant de lancer le voyage.")
    holder = _clean(holder, 200)
    if cashier == "personne" and not holder:
        raise WriteError("Écris le nom de la personne qui tient la caisse", "cashier_name")
    t = create_trip(s, plan.name, place=plan.place, start=plan.start, end=plan.end, diving=diving)
    note = [f"Préparé pour {plan.people} personne{'s' if plan.people > 1 else ''}."]
    if plan.announced is not None:
        note.append(f"Prix annoncé : {E.fmt(plan.announced)} par personne.")
    t.note = " ".join(note)
    for x in lines:
        if x["included"]:                    # inclus (0,00 €) : rien à payer, pas de dépense
            continue
        save_expense(s, t, label=x["label"], category=x["category"], planned=x["planned"], weights={},
                     note=x["note"], for_all=True)
    if cashier == "personne":
        p = add_participant(s, t, holder, roles=[S.DIVER] if S.is_diving(t) else [])
        t.cashier_id = p.id
    elif _clean(asso_name):
        S.set_trip_asso_name(t, str(asso_name), s)
    plan.trip_id, plan.launched_on = t.id, today or _dt.date.today()
    s.flush()
    return t


def plan_trip(s, plan: M.Plan) -> M.Trip | None:
    """Le voyage créé à partir de ce projet, s'il existe encore."""
    return s.get(M.Trip, plan.trip_id) if plan.trip_id else None


def forget_trip(s, trip_id: int) -> None:
    """Un voyage supprimé : les projets qui l'avaient lancé n'y mènent plus (et peuvent être relancés). Sans cela,
    un voyage créé ensuite pourrait reprendre le même numéro et passer pour le leur."""
    for p in s.query(M.Plan).filter(M.Plan.trip_id == trip_id).all():
        p.trip_id, p.launched_on = None, None
