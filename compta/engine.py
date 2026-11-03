"""Moteur de calcul — Python pur, sans base de données.

Tous les montants sont en centimes entiers. Les calculs sont faits en fractions
exactes ; le total de chaque participant est arrondi UNE fois :
- par défaut (rounding="participants") au centime supérieur : l'asso n'y perd jamais, et chacun paie ce que donne
  une calculette (à moins d'un centime près). Les centimes en trop forment « l'écart d'arrondi (au profit de
  l'asso) » ;
- au choix (rounding="asso") au centime inférieur : personne ne paie plus que sa part exacte, l'asso prend les
  centimes à sa charge (au plus 1 centime par personne).
Chaque part de dépense est affichée au centime le plus proche.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Optional

ASSO = "asso"
OTHERS = "others"
PERSON = "person"
GROUP = "group"

PLANNED = "prevu"
ACTUAL = "reel"


# --------------------------------------------------------------------------
# Formatage
# --------------------------------------------------------------------------

def fmt(cents, sign: bool = False) -> str:
    """1234567 -> '12 345,67 €' (espace fine insécable pour les milliers)."""
    if isinstance(cents, Fraction):
        cents = round(cents)
    neg = cents < 0
    c = abs(int(cents))
    euros, cts = divmod(c, 100)
    s = f"{euros:,}".replace(",", " ") + f",{cts:02d} €"
    if neg:
        return "−" + s
    if sign and cents > 0:
        return "+" + s
    return s


def sort_key(name: str) -> str:
    """Tri alphabétique sans tenir compte des accents ni des majuscules."""
    import unicodedata
    return unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()


def fmt_frac(x: Fraction) -> str:
    """Montant exact (fraction de centimes) pour les explications."""
    if x.denominator == 1:
        return fmt(int(x))
    return "≈ " + fmt(round(x))


def fmt_weight(w: Fraction) -> str:
    if w.denominator == 1:
        return str(w.numerator)
    return f"{float(w):g}".replace(".", ",")


def parse_amount(text) -> Optional[int]:
    """'1 500,50' / '1500.5' / '12' -> centimes. Vide -> None."""
    if text is None:
        return None
    s = str(text).strip().replace("€", "").replace(" ", "").replace(" ", "").replace(" ", "")
    if not s:
        return None
    s = s.replace(",", ".")
    neg = s.startswith("-") or s.startswith("−")
    s = s.lstrip("-−+")
    if s.count(".") > 1:
        raise ValueError(f"Montant invalide : {text}")
    from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
    try:
        d = Decimal(s)
    except InvalidOperation as e:
        raise ValueError(f"Montant invalide : {text}") from e
    c = int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return -c if neg else c


def to_weight(x) -> Fraction:
    if isinstance(x, Fraction):
        return x
    from decimal import Decimal
    return Fraction(Decimal(str(x).replace(",", ".")))


# --------------------------------------------------------------------------
# Données d'entrée
# --------------------------------------------------------------------------

@dataclass
class Participant:
    id: int
    name: str
    family: Optional[int] = None    # id de la famille (les comptes se font en famille)
    companion: bool = False         # accompagnant (ne fait pas l'activité principale)


@dataclass
class Family:
    id: int
    name: str                       # ex. « Lebrun » (affiché « Famille Lebrun »)
    referent: Optional[int] = None  # qui verse / reçoit l'argent pour la famille


@dataclass
class Expense:
    id: int
    label: str
    category: str = "Divers"
    planned: Optional[int] = None
    actual: Optional[int] = None
    payer: Optional[int] = None          # None = l'asso ; sinon id participant (avance)
    weights: dict = field(default_factory=dict)  # participant id -> parts
    kind: str = "depense"                # "recette" = argent reçu (remboursement, subvention) : réduit le coût
    payers: Optional[list] = None        # plusieurs payeurs : [(id participant ou None = asso, centimes)]
    refund: int = 0                      # « dont remboursé » : partie rendue par le fournisseur (réduit le coût)
    refund_to: Optional[int] = None      # qui a reçu ce remboursement (None = l'asso)
    fixed: dict = field(default_factory=dict)    # participant id -> montant fixé (centimes) ; le reste aux autres

    @property
    def sign(self) -> int:
        return -1 if self.kind == "recette" else 1

    def splits(self) -> list:
        """Qui a payé (ou reçu) combien, sur le montant le mieux connu. Le reste non attribué = l'asso."""
        amount = self.best_known()
        if not self.payers:
            return [(self.payer, amount)] if amount else []
        merged = {}
        for who, cents in self.payers:
            merged[who] = merged.get(who, 0) + cents
        rest = amount - sum(merged.values())
        if rest > 0:
            merged[None] = merged.get(None, 0) + rest
        return list(merged.items())

    def amount(self, mode: str) -> int:
        if mode == PLANNED:
            v = self.planned if self.planned is not None else self.actual
        else:
            v = self.actual if self.actual is not None else self.planned
        return v or 0

    def best_known(self) -> int:
        return self.amount(ACTUAL)


@dataclass
class Adjustment:
    id: int
    participant: int
    value: int                      # delta (si fixed=False) ou montant fixé (si fixed=True)
    expense: Optional[int] = None   # None = sur le total du voyage
    fixed: bool = False             # True = montant verrouillé
    compensation: str = OTHERS      # OTHERS | PERSON | ASSO
    compensator: Optional[int] = None
    reason: str = ""


@dataclass
class Withdrawal:
    participant: int
    absorber: str = GROUP           # GROUP | ASSO
    kept: set = field(default_factory=set)   # ids des dépenses dont il garde sa part
    date: str = ""


@dataclass
class Movement:
    participant: int
    kind: str          # 'versement' (participant -> asso) | 'remboursement' (asso -> participant)
    amount: int
    date: str = ""
    note: str = ""


# --------------------------------------------------------------------------
# Résultat
# --------------------------------------------------------------------------

@dataclass
class Component:
    text: str
    amount: Fraction


@dataclass
class Line:
    """Part d'un participant pour une dépense (ou un ajustement global)."""
    participant: int
    expense: Optional[int]
    label: str
    category: str
    components: list = field(default_factory=list)
    exact: Fraction = Fraction(0)
    cents: int = 0
    adjusted: bool = False
    locked: bool = False

    def add(self, text: str, amount: Fraction):
        self.components.append(Component(text, Fraction(amount)))
        self.exact += Fraction(amount)


@dataclass
class AssoCharge:
    label: str
    reason: str
    exact: Fraction
    cents: int = 0
    expense: Optional[int] = None     # dépense concernée (None = sur le total du voyage)


@dataclass
class PersonSummary:
    id: int
    name: str
    share: int = 0
    paid: int = 0          # versements
    advanced: int = 0      # avances
    refunded: int = 0      # remboursements reçus de l'asso
    withdrawn: bool = False
    family: Optional[int] = None
    companion: bool = False
    rounding: int = 0      # total arrondi − somme des parts affichées (quelques centimes au plus)

    @property
    def sort_name(self) -> str:
        return sort_key(self.name)

    @property
    def balance(self) -> int:
        """> 0 : doit encore ; < 0 : l'asso lui doit."""
        return self.share - self.paid - self.advanced + self.refunded

    @property
    def status(self) -> str:
        if self.balance > 0:
            return "doit"
        if self.balance < 0:
            return "du"
        return "ok"


@dataclass
class Account:
    """Ce qui se règle avec l'asso : une personne seule, ou une famille (les parts de ses membres)."""
    key: str                        # "p12" (personne) ou "f3" (famille)
    name: str                       # « Luc » ou « Famille Lebrun »
    members: list                   # PersonSummary
    referent: int                   # participant qui verse / reçoit pour ce compte
    family: Optional[int] = None
    holds_cash: bool = False        # c'est lui qui tient la caisse (voyage sans asso)

    @property
    def is_family(self) -> bool:
        return self.family is not None

    @property
    def sort_name(self) -> str:
        return sort_key(self.name.removeprefix("Famille "))

    share = property(lambda self: sum(m.share for m in self.members))
    paid = property(lambda self: sum(m.paid for m in self.members))
    advanced = property(lambda self: sum(m.advanced for m in self.members))
    refunded = property(lambda self: sum(m.refunded for m in self.members))

    @property
    def withdrawn(self) -> bool:
        return all(m.withdrawn for m in self.members)

    @property
    def balance(self) -> int:
        return self.share - self.paid - self.advanced + self.refunded

    @property
    def status(self) -> str:
        return "doit" if self.balance > 0 else "du" if self.balance < 0 else "ok"


@dataclass
class Result:
    mode: str
    total_cost: int
    lines: dict            # (pid, eid|None-key) -> Line
    people: dict           # pid -> PersonSummary
    asso_charges: list
    rounding_gain: int
    warnings: list
    expense_totals: dict   # eid -> montant retenu
    families: dict = field(default_factory=dict)   # fid -> Family
    cashier: Optional[int] = None    # participant qui tient la caisse (voyage sans asso) ; None = l'asso
    rounding_mode: str = "participants"   # qui porte les centimes d'arrondi : "participants" ou "asso"

    @property
    def accounts(self) -> list:
        """Comptes à régler avec l'asso, triés par nom : familles regroupées, les autres seuls."""
        out, fam = [], {}
        for p in self.people.values():
            f = self.families.get(p.family) if p.family is not None else None
            if f is None:
                out.append(Account(f"p{p.id}", p.name, [p], p.id))
                continue
            if f.id not in fam:
                fam[f.id] = Account(f"f{f.id}", f"Famille {f.name}", [], f.referent, f.id)
                out.append(fam[f.id])
            fam[f.id].members.append(p)
        for a in fam.values():
            a.members.sort(key=lambda m: m.sort_name)
            if a.referent not in {m.id for m in a.members}:
                a.referent = a.members[0].id
        for a in out:
            a.holds_cash = self.cashier is not None and any(m.id == self.cashier for m in a.members)
        return sorted(out, key=lambda a: a.sort_name)

    @property
    def cashier_account(self):
        """Compte de celui qui tient la caisse : il se règle avec la caisse qu'il tient (pas de virement à faire)."""
        return next((a for a in self.accounts if a.holds_cash), None)

    def account_of(self, pid: int) -> Account:
        return next(a for a in self.accounts if any(m.id == pid for m in a.members))

    def lines_for(self, pid: int) -> list:
        return [l for (p, _), l in self.lines.items() if p == pid]

    def share(self, pid: int, eid) -> int:
        l = self.lines.get((pid, eid))
        return l.cents if l else 0

    def subtotal(self, pid, eids) -> int:
        """Sous-total d'une personne (ou de tous si pid est None) sur plusieurs dépenses, calculé sur les montants
        exacts puis arrondi au centime le plus proche : comme à la calculette (1 522,29 / 11 = 138,39)."""
        eids = set(eids)
        x = sum((l.exact for (p, e), l in self.lines.items() if e in eids and (pid is None or p == pid)), Fraction(0))
        return nearest(x)

    @property
    def asso_total(self) -> int:
        return sum(c.cents for c in self.asso_charges)

    @property
    def total_shares(self) -> int:
        return sum(p.share for p in self.people.values())

    @property
    def total_paid(self) -> int:
        return sum(p.paid for p in self.people.values())

    @property
    def to_collect(self) -> int:
        """Encore à recevoir (sans celui qui tient la caisse : il ne se paie pas lui-même)."""
        return sum(a.balance for a in self.accounts if a.balance > 0 and not a.holds_cash)

    @property
    def to_refund(self) -> int:
        return -sum(a.balance for a in self.accounts if a.balance < 0 and not a.holds_cash)

    def is_balanced(self) -> bool:
        if self.total_shares + self.asso_total - self.rounding_gain != self.total_cost:
            return False
        if self.rounding_mode == ROUND_ASSO:      # au plus 1 centime par personne, à la charge de l'asso
            return -len(self.people) <= self.rounding_gain <= len(self.asso_charges)
        return self.rounding_gain >= 0

    def settlements(self) -> list:
        """Virements à faire pour tout solder : (de, vers, montant), avec des Account ou ASSO (la caisse)."""
        out = []
        for a in self.accounts:
            if a.holds_cash:
                continue
            if a.balance > 0:
                out.append((a, ASSO, a.balance))
            elif a.balance < 0:
                out.append((ASSO, a, -a.balance))
        return out


GLOBAL = "global"


def nearest(x: Fraction) -> int:
    """Centime le plus proche (0,5 vers le haut)."""
    return math.floor(x + Fraction(1, 2))


ROUND_PARTICIPANTS, ROUND_ASSO = "participants", "asso"


def compute(participants, expenses, adjustments=(), withdrawals=(), movements=(),
            mode: str = ACTUAL, families=(), rounding: str = ROUND_PARTICIPANTS) -> Result:
    names = {p.id: p.name for p in participants}
    pids = [p.id for p in participants]
    wd = {w.participant: w for w in withdrawals}
    warnings: list = []
    lines: dict = {}
    asso: list = []
    exp_by_id = {e.id: e for e in expenses}

    def line(pid, e: Optional[Expense]) -> Line:
        key = (pid, e.id if e else GLOBAL)
        if key not in lines:
            lines[key] = Line(pid, e.id if e else None,
                              e.label if e else "Ajustements sur le total",
                              e.category if e else "Ajustements")
        return lines[key]

    def name(pid):
        return names.get(pid, "?")

    total_cost = 0
    expense_totals = {}

    # ---- 1. Répartition de base, désistements, ajustements par dépense ----
    for e in expenses:
        gross = e.amount(mode)
        refund = min(e.refund or 0, gross) if gross else 0
        if e.refund and gross and e.refund > gross:
            warnings.append(f"« {e.label} » : le remboursement ({fmt(e.refund)}) dépasse le montant ({fmt(gross)}).")
        amount = (gross - refund) * e.sign
        shown = f"{fmt(gross)} − {fmt(refund)} remboursés = {fmt(amount)}" if refund else fmt(amount)
        expense_totals[e.id] = amount
        total_cost += amount
        if amount == 0:
            continue          # montant pas encore connu : rien à répartir pour l'instant
        weights = {p: to_weight(w) for p, w in e.weights.items() if p in names and to_weight(w) > 0}
        if amount == 0 and not weights:
            continue
        if not weights:
            if amount:
                warnings.append(f"« {e.label} » n'est réparti sur personne : l'asso prend tout en charge.")
                asso.append(AssoCharge(e.label, "dépense répartie sur personne", Fraction(amount), expense=e.id))
            continue

        # Désistement avec part reprise par le groupe : on retire la personne de la répartition.
        released_group = [p for p in weights if p in wd and e.id not in wd[p].kept and wd[p].absorber == GROUP]
        released_asso = [p for p in weights if p in wd and e.id not in wd[p].kept and wd[p].absorber == ASSO]
        active = {p: w for p, w in weights.items() if p not in released_group}
        if not active:
            asso.append(AssoCharge(e.label, "tous les participants se sont désistés", Fraction(amount), expense=e.id))
            continue
        # Montants fixés (🔒) : retirés d'abord ; le reste est réparti entre les autres selon leurs parts.
        fixed = {p: Fraction(c) * e.sign for p, c in (e.fixed or {}).items() if p in active}
        free = {p: w for p, w in active.items() if p not in fixed}
        rest = Fraction(amount) - sum(fixed.values())
        for p, c in fixed.items():
            l = line(p, e)
            l.add(f"{e.label} : montant fixé pour lui ({fmt(round(c))} sur {shown})", c)
            l.locked = True
        if fixed and not free:
            if rest > 0:
                warnings.append(f"« {e.label} » : les montants fixés ne font pas tout ({fmt(round(rest))} restent) : "
                                f"l'asso prend le reste en charge.")
                asso.append(AssoCharge(e.label, "reste non réparti (montants fixés)", rest, expense=e.id))
            elif rest < 0:
                warnings.append(f"« {e.label} » : les montants fixés dépassent la dépense de {fmt(round(-rest))}.")
        if fixed:
            if rest < 0 and free:
                warnings.append(f"« {e.label} » : les montants fixés dépassent la dépense de {fmt(round(-rest))}.")
            amount_free, active_free = rest, free
        else:
            amount_free, active_free = Fraction(amount), active
        total_w = sum(active_free.values())
        for p, w in active_free.items():
            l = line(p, e)
            base = Fraction(amount_free) * w / total_w
            if fixed:
                if len(fixed) == 1:
                    who_fx = name(next(iter(fixed)))
                elif len(fixed) <= 3:
                    who_fx = ", ".join(f"{name(p_)} {fmt(round(c_))}" for p_, c_ in fixed.items())
                else:
                    who_fx = f"{len(fixed)} personnes"
                rest_txt = f"{shown} − {fmt(round(sum(fixed.values())))} fixés ({who_fx}) = {fmt(round(rest))}"
                if w == total_w:
                    txt = f"{e.label} : {rest_txt} pour lui seul"
                elif len(set(active_free.values())) == 1:
                    txt = f"{e.label} : {rest_txt} ÷ {len(active_free)} personnes"
                else:
                    txt = f"{e.label} : {rest_txt}, part inégale {fmt_weight(w)} sur {fmt_weight(total_w)}"
            elif w == total_w:
                txt = f"{e.label} : {shown} pour lui seul"
            elif len(set(active.values())) == 1:
                txt = f"{e.label} : {shown} ÷ {len(active)} personnes"
            else:
                # répartition saisie en montants (en centimes depuis l'écran, ou en euros par une IA)
                if total_w in (abs(amount), gross) or total_w * 100 in (abs(amount), gross):
                    txt = f"{e.label} : montant indiqué pour lui ({fmt(round(base))} sur {shown})"
                else:
                    txt = (f"{e.label} : part inégale, {fmt_weight(w)} sur {fmt_weight(total_w)} "
                           f"({fmt(round(base))} sur {shown})")
            if released_group:
                txt += " (après désistement de " + ", ".join(name(x) for x in released_group) + ")"
            l.add(txt, base)
        for p in released_asso:
            l = lines[(p, e.id)]
            moved = l.exact
            l.add(f"Désistement : sa part est reprise par l'asso", -moved)
            asso.append(AssoCharge(e.label, f"désistement de {name(p)}", moved, expense=e.id))

        # Ajustements sur cette dépense
        e_adjs = [a for a in adjustments if a.expense == e.id]
        # Un verrou protège des ajustements faits APRÈS lui (ce que montre l'aperçu au moment de verrouiller).
        locked = set()
        for a in e_adjs:
            if a.participant not in active:
                if a.participant in wd:
                    continue  # personne désistée, ajustement sans objet
                warnings.append(f"Ajustement ignoré : {name(a.participant)} ne participe pas à « {e.label} ».")
                continue
            if a.participant in released_asso:
                continue
            l = line(a.participant, e)
            current = l.exact
            if a.fixed:
                delta = Fraction(a.value) - current
                l.locked = True
                locked.add(a.participant)
            else:
                delta = Fraction(a.value)
            if delta == 0:
                continue
            l.adjusted = True
            reason = f" ({a.reason})" if a.reason else ""
            kind = "Réduction" if delta < 0 else "Supplément"
            # Une personne désistée qui GARDE sa part de cette dépense la paie comme si elle était venue :
            # elle participe donc aussi aux compensations sur cette dépense. Celles dont la part est reprise
            # par l'asso n'y participent pas.
            _compensate(a, delta, e, active, locked | set(released_asso), lines, line, asso, name,
                        f"{kind} accordée{reason}", warnings, e.label)

    # ---- 2. Montant affiché de chaque part de dépense : au centime le plus proche ----
    # (l'arrondi qui compte se fait UNE fois, sur le total de chacun, au centime supérieur : étape 5)

    for l in lines.values():
        l.cents = nearest(l.exact)

    # ---- 3. Ajustements sur le total (calculés sur les montants exacts) ----
    global_locked = set()
    for a in [a for a in adjustments if a.expense is None]:
        if a.participant not in names:
            continue
        current = sum((l.exact for (p, _), l in lines.items() if p == a.participant), Fraction(0))
        delta = a.value - current if a.fixed else a.value
        if a.fixed:
            global_locked.add(a.participant)
        if delta == 0:
            continue
        gl = line(a.participant, None)
        gl.adjusted = True
        gl.locked = gl.locked or a.fixed
        reason = f" ({a.reason})" if a.reason else ""
        kind = "Réduction" if delta < 0 else "Supplément"
        totals = {}
        for (p, _), l in lines.items():
            totals[p] = totals.get(p, 0) + l.exact
        eligible = {p: t for p, t in totals.items()
                    if p != a.participant and p not in global_locked and p not in wd and t > 0}
        _compensate(a, Fraction(delta), None, eligible, set(), lines, line, asso, name,
                    f"{kind} accordée{reason}", warnings, "le total")
        # Les écarts « sur le total » vont tous dans la ligne globale de chaque personne.
        for (p, k), l in lines.items():
            if k == GLOBAL:
                l.cents = nearest(l.exact)

    # ---- 4. Prises en charge par l'asso (arrondies au centime supérieur) ----
    for c in asso:
        c.cents = math.ceil(c.exact)

    # ---- 5. Synthèse par personne ----
    people = {pid: PersonSummary(pid, names[pid], withdrawn=pid in wd) for pid in pids}
    exact_share = {pid: Fraction(0) for pid in pids}
    shown = {pid: 0 for pid in pids}
    for (p, _), l in lines.items():
        exact_share[p] += l.exact
        shown[p] += l.cents
    for pid in pids:
        # Une seule fois, sur le total de chacun : au centime supérieur (l'asso n'y perd jamais),
        # ou inférieur si l'asso prend les centimes à sa charge (personne ne paie plus que sa part exacte)
        people[pid].share = (math.floor if rounding == ROUND_ASSO else math.ceil)(exact_share[pid])
        people[pid].rounding = people[pid].share - shown[pid]      # écart avec la somme des parts affichées
    for e in expenses:
        splits = e.splits()
        if e.payers and sum(c for _, c in e.payers) > e.best_known():
            warnings.append(f"« {e.label} » : les payeurs totalisent {fmt(sum(c for _, c in e.payers))}, "
                            f"plus que son montant ({fmt(e.best_known())}).")
        for payer, cents in splits:
            if payer is not None and payer in people:
                people[payer].advanced += cents * e.sign
        if e.refund and e.refund_to is not None and e.refund_to in people and e.best_known():
            people[e.refund_to].advanced -= min(e.refund, e.best_known())   # il a reçu le remboursement : il le doit
    for m in movements:
        if m.participant not in people:
            continue
        if m.kind == "versement":
            people[m.participant].paid += m.amount
        elif m.kind == "remboursement":
            people[m.participant].refunded += m.amount

    shares_total = sum(p.share for p in people.values())
    asso_total = sum(c.cents for c in asso)
    gain = shares_total + asso_total - total_cost
    if gain < 0 and rounding != ROUND_ASSO:
        warnings.append("Contrôle : le total ne tombe pas juste (écart négatif). Merci de le signaler.")

    # Nettoyage : lignes vides
    lines = {k: l for k, l in lines.items() if l.components}
    for (p, k), l in lines.items():
        if l.cents < 0 and k != GLOBAL and exp_by_id.get(k) is not None and exp_by_id[k].kind != "recette":
            warnings.append(f"La part de {names[p]} sur « {l.label} » devient négative ({fmt(l.cents)}) : "
                            f"la réduction est plus grande que sa part.")
    for p in people.values():
        if p.share < 0:
            warnings.append(f"La part totale de {p.name} est négative ({fmt(p.share)}).")

    fams = {f.id: f for f in families}
    for p in participants:
        people[p.id].family = p.family if p.family in fams else None
        people[p.id].companion = p.companion
    return Result(mode, total_cost, lines, people, asso, gain, warnings, expense_totals, fams,
                  rounding_mode=ROUND_ASSO if rounding == ROUND_ASSO else ROUND_PARTICIPANTS)


def _compensate(a: Adjustment, delta: Fraction, e, pool: dict, excluded: set, lines, line,
                asso, name, label, warnings, where):
    """Applique un écart `delta` à a.participant et le fait compenser selon a.compensation."""
    target = line(a.participant, e)
    target.add(f"{label}", delta)
    who = name(a.participant)
    if a.compensation == PERSON and a.compensator is not None and a.compensator != a.participant:
        line(a.compensator, e).add(f"Compense l'ajustement de {who}", -delta)
        target.components[-1].text += f" — compensé par {name(a.compensator)}"
        return
    if a.compensation == OTHERS:
        eligible = {p: w for p, w in pool.items() if p != a.participant and p not in excluded}
        if eligible:
            tw = sum(Fraction(w) for w in eligible.values())
            for p, w in eligible.items():
                line(p, e).add(f"Compense l'ajustement de {who}", -delta * Fraction(w) / tw)
            target.components[-1].text += " — compensé par les autres"
            return
        warnings.append(f"Personne ne peut compenser l'ajustement de {who} sur {where} "
                        f"(tous verrouillés) : l'asso le prend en charge.")
    # ASSO (ou repli)
    target.components[-1].text += " — pris en charge par l'asso"
    asso.append(AssoCharge(e.label if e else "Total du voyage",
                           f"ajustement de {who}" + (f" : {a.reason}" if a.reason else ""), -delta,
                           expense=e.id if e else None))


# --------------------------------------------------------------------------
# Comptes « Entre nous » (type Tricount, hors asso)
# --------------------------------------------------------------------------

@dataclass
class SubExpense:
    id: int
    label: str
    amount: int
    payer: int
    weights: dict


@dataclass
class SubTransfer:
    sender: int
    receiver: int
    amount: int


def split_exact(amount: int, weights: dict) -> dict:
    """Répartit `amount` centimes selon les poids, somme exacte (plus forts restes)."""
    weights = {p: to_weight(w) for p, w in weights.items() if to_weight(w) > 0}
    if not weights:
        return {}
    tw = sum(weights.values())
    raw = {p: Fraction(amount) * w / tw for p, w in weights.items()}
    base = {p: math.floor(v) for p, v in raw.items()}
    rest = amount - sum(base.values())
    order = sorted(raw, key=lambda p: (-(raw[p] - base[p]), list(weights).index(p)))
    for p in order[:rest]:
        base[p] += 1
    return base


def sub_balances(members, expenses, transfers) -> dict:
    """> 0 : on lui doit de l'argent ; < 0 : il doit de l'argent."""
    bal = {m: 0 for m in members}
    for e in expenses:
        bal[e.payer] = bal.get(e.payer, 0) + e.amount
        for p, c in split_exact(e.amount, e.weights).items():
            bal[p] = bal.get(p, 0) - c
    for t in transfers:
        bal[t.sender] = bal.get(t.sender, 0) + t.amount
        bal[t.receiver] = bal.get(t.receiver, 0) - t.amount
    return bal


def minimal_settlements(balances: dict) -> list:
    """Liste (débiteur, créancier, montant) minimisant le nombre de virements (glouton)."""
    debtors = sorted([[p, -b] for p, b in balances.items() if b < 0], key=lambda x: -x[1])
    creditors = sorted([[p, b] for p, b in balances.items() if b > 0], key=lambda x: -x[1])
    out = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        d, c = debtors[i], creditors[j]
        x = min(d[1], c[1])
        out.append((d[0], c[0], x))
        d[1] -= x
        c[1] -= x
        if d[1] == 0:
            i += 1
        if c[1] == 0:
            j += 1
    return out
