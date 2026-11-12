"""Modèle de données (SQLAlchemy / SQLite)."""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, event, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def now():
    return dt.datetime.now()


class Trip(Base):
    __tablename__ = "trips"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    place: Mapped[str] = mapped_column(String(200), default="")
    organizer: Mapped[str] = mapped_column(String(200), default="")
    start: Mapped[Optional[dt.date]] = mapped_column(Date, nullable=True)
    end: Mapped[Optional[dt.date]] = mapped_column(Date, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")      # club, lieu de départ, infos utiles…
    cashier_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)   # participant qui tient la caisse (sinon l'asso)
    asso_name: Mapped[str] = mapped_column(String(100), default="")   # nom de l'asso / de la caisse pour ce voyage
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)     # voyage d'exemple (Hendaye 2026 – démo)
    groups: Mapped[str] = mapped_column(Text, default="")    # JSON : [{"name": "Minibus", "members": [ids]}]
    roles_conf: Mapped[str] = mapped_column(Text, default="")  # JSON : {"perso": ["Moniteur"], "masques": ["dp"]}
    rounding: Mapped[str] = mapped_column(String(20), default="participants")   # centimes d'arrondi : participants | asso
    pay_info: Mapped[str] = mapped_column(Text, default="")  # « Comment payer » : RIB, chèque, lien, consigne… (texte libre)
    ai_notes: Mapped[str] = mapped_column(Text, default="")  # JSON : points « à vérifier » laissés par l'IA [{"texte", "le"}]
    closed_on: Mapped[Optional[dt.date]] = mapped_column(Date, nullable=True)   # voyage clôturé ce jour-là (figé) ; None : ouvert
    # JSON : dates de paiement {"etapes": [{"nom", "date", "montant" (centimes par personne, null = tout le reste)}],
    #                          "comptes": {"<id participant>": [mêmes étapes, montants pour tout le compte]}}
    pay_dates: Mapped[str] = mapped_column(Text, default="")
    created: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

    participants: Mapped[list["Participant"]] = relationship(
        back_populates="trip", cascade="all, delete-orphan",
        order_by=lambda: func.unaccent(Participant.name))
    categories: Mapped[list["Category"]] = relationship(
        back_populates="trip", cascade="all, delete-orphan", order_by="Category.position")
    expenses: Mapped[list["Expense"]] = relationship(
        back_populates="trip", cascade="all, delete-orphan", order_by="Expense.id")
    adjustments: Mapped[list["Adjustment"]] = relationship(
        back_populates="trip", cascade="all, delete-orphan", order_by="Adjustment.id")
    withdrawals: Mapped[list["Withdrawal"]] = relationship(
        back_populates="trip", cascade="all, delete-orphan")
    movements: Mapped[list["Movement"]] = relationship(
        back_populates="trip", cascade="all, delete-orphan", order_by="Movement.date")
    receipts: Mapped[list["Receipt"]] = relationship(
        back_populates="trip", cascade="all, delete-orphan", order_by="Receipt.number")
    subaccounts: Mapped[list["SubAccount"]] = relationship(
        back_populates="trip", cascade="all, delete-orphan", order_by="SubAccount.id")
    families: Mapped[list["Family"]] = relationship(
        back_populates="trip", cascade="all, delete-orphan", order_by=lambda: func.unaccent(Family.name))


class Participant(Base):
    __tablename__ = "participants"
    id: Mapped[int] = mapped_column(primary_key=True)
    trip_id: Mapped[int] = mapped_column(ForeignKey("trips.id"))
    name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str] = mapped_column(String(50), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    family_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)   # id de Family
    companion: Mapped[bool] = mapped_column(Boolean, default=False)            # accompagnant
    roles: Mapped[str] = mapped_column(String(100), default="")   # "organisateur,co-organisateur,dp"
    trip: Mapped[Trip] = relationship(back_populates="participants")

    @property
    def role_list(self) -> list:
        return [r for r in (self.roles or "").split(",") if r]

    @property
    def family(self) -> Optional["Family"]:
        return next((f for f in self.trip.families if f.id == self.family_id), None) if self.family_id else None


class Family(Base):
    """Famille : ses membres règlent leurs comptes ensemble avec l'asso."""
    __tablename__ = "families"
    id: Mapped[int] = mapped_column(primary_key=True)
    trip_id: Mapped[int] = mapped_column(ForeignKey("trips.id"))
    name: Mapped[str] = mapped_column(String(200))        # « Lebrun »
    referent_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)   # qui paie pour la famille
    trip: Mapped[Trip] = relationship(back_populates="families")

    @property
    def members(self) -> list:
        return [p for p in self.trip.participants if p.family_id == self.id]

    @property
    def label(self) -> str:
        return f"Famille {self.name}"


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    trip_id: Mapped[int] = mapped_column(ForeignKey("trips.id"))
    name: Mapped[str] = mapped_column(String(100))
    position: Mapped[int] = mapped_column(Integer, default=0)
    # Qui est concerné par défaut par une nouvelle dépense : "tous" | "plongeurs" | "accompagnants" ("" = deviné)
    audience: Mapped[str] = mapped_column(String(20), default="")
    trip: Mapped[Trip] = relationship(back_populates="categories")


class Setting(Base):
    """Réglages communs à tous les voyages (ex. la liste de catégories par défaut)."""
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Expense(Base):
    __tablename__ = "expenses"
    id: Mapped[int] = mapped_column(primary_key=True)
    trip_id: Mapped[int] = mapped_column(ForeignKey("trips.id"))
    category_id: Mapped[Optional[int]] = mapped_column(ForeignKey("categories.id"), nullable=True)
    label: Mapped[str] = mapped_column(String(200))
    date: Mapped[Optional[dt.date]] = mapped_column(Date, nullable=True)
    planned: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    actual: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    payer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("participants.id"), nullable=True)
    tickets: Mapped[str] = mapped_column(Text, default="")   # JSON : plusieurs tickets dont le total = réel
    cents_mode: Mapped[str] = mapped_column(String(20), default="")   # centimes d'arrondi : "" (réglage du voyage) | participants | asso
    cents_who: Mapped[str] = mapped_column(Text, default="")          # tirage enregistré : ids qui paient un centime de plus
    note: Mapped[str] = mapped_column(Text, default="")
    # "depense" (argent qui sort) ou "recette" (argent reçu : remboursement d'un fournisseur, subvention…)
    kind: Mapped[str] = mapped_column(String(20), default="depense")
    subcategory: Mapped[str] = mapped_column(String(100), default="")     # ex. « Péages » dans « Transport »
    # « dont remboursé » : partie rendue par le fournisseur (annulation…) et qui l'a reçue (None = l'asso)
    refund: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    refund_to_id: Mapped[Optional[int]] = mapped_column(ForeignKey("participants.id"), nullable=True)
    # « pour tout le monde », y compris ceux qui arriveront : chaque nouveau participant y entre (voyage lancé depuis
    # une préparation, avant d'avoir ses participants). Perdu dès que « Pour qui ? » ne vaut plus tout le monde.
    for_all: Mapped[bool] = mapped_column(Boolean, default=False)

    trip: Mapped[Trip] = relationship(back_populates="expenses")
    category: Mapped[Optional[Category]] = relationship()
    payer: Mapped[Optional[Participant]] = relationship(foreign_keys=[payer_id])
    refund_to: Mapped[Optional[Participant]] = relationship(foreign_keys=[refund_to_id])
    shares: Mapped[list["ExpenseShare"]] = relationship(cascade="all, delete-orphan")
    checks: Mapped[list["ExpenseCheck"]] = relationship(cascade="all, delete-orphan")   # « à vérifier »
    payments: Mapped[list["ExpensePayment"]] = relationship(cascade="all, delete-orphan",
                                                            order_by="ExpensePayment.id")
    receipt_links: Mapped[list["ReceiptLink"]] = relationship(cascade="all, delete-orphan",
                                                               back_populates="expense")

    @property
    def ticket_list(self) -> list:
        """[{"cents": int, "date": date|None, "note": str}] : les tickets dont le total fait le montant réel."""
        import json
        try:
            raw = json.loads(self.tickets) if self.tickets else []
        except ValueError:
            return []
        out = []
        for x in raw:
            try:
                d = dt.date.fromisoformat(x["date"]) if x.get("date") else None
            except ValueError:
                d = None
            out.append(dict(cents=int(x.get("cents", 0)), date=d, note=str(x.get("note") or "")))
        return out

    def set_tickets(self, items) -> None:
        """items : [(centimes, date|None)] ; 2 tickets ou plus -> réel = leur total ; sinon, plus de détail."""
        import json
        items = [(int(c), d) for c, d in items if c]
        if len(items) >= 2:
            self.tickets = json.dumps([{"cents": c, "date": d.isoformat() if d else ""} for c, d in items])
            self.actual = sum(c for c, _ in items)
        else:
            self.tickets = ""
            if items:
                self.actual = items[0][0]

    def best_known(self) -> int:
        """Montant réel s'il est connu, sinon le prévu."""
        return (self.actual if self.actual is not None else self.planned) or 0

    def net(self) -> int:
        """Montant le mieux connu, moins ce qui a été remboursé."""
        return max(self.best_known() - (self.refund or 0), 0)

    @property
    def is_income(self) -> bool:
        return self.kind == "recette"

    @property
    def pending(self) -> bool:
        """Montant pas encore connu (ni prévu ni réel)."""
        return self.planned is None and self.actual is None

    def splits(self) -> list:
        """[(payer_id ou None pour l'asso, centimes)] : qui a payé (ou reçu, pour une recette) combien.
        Avec plusieurs payeurs, ce qui n'est attribué à personne est compté pour l'asso."""
        amount = self.best_known()
        if not self.payments:
            return [(self.payer_id, amount)] if amount else []
        merged = {}
        for p in self.payments:
            merged[p.payer_id] = merged.get(p.payer_id, 0) + p.amount
        rest = amount - sum(merged.values())
        if rest > 0:
            merged[None] = merged.get(None, 0) + rest
        return list(merged.items())

    @property
    def multi_payer(self) -> bool:
        return bool(self.payments)


class ExpensePayment(Base):
    """Une part de paiement d'une dépense payée par plusieurs (ou part reçue d'une recette)."""
    __tablename__ = "expense_payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    expense_id: Mapped[int] = mapped_column(ForeignKey("expenses.id"))
    payer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("participants.id"), nullable=True)  # None = asso
    amount: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String(200), default="")
    payer: Mapped[Optional[Participant]] = relationship()


class ExpenseShare(Base):
    __tablename__ = "expense_shares"
    expense_id: Mapped[int] = mapped_column(ForeignKey("expenses.id"), primary_key=True)
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"), primary_key=True)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    fixed: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)   # montant fixé 🔒 (centimes)


class Adjustment(Base):
    __tablename__ = "adjustments"
    id: Mapped[int] = mapped_column(primary_key=True)
    trip_id: Mapped[int] = mapped_column(ForeignKey("trips.id"))
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"))
    expense_id: Mapped[Optional[int]] = mapped_column(ForeignKey("expenses.id"), nullable=True)
    value: Mapped[int] = mapped_column(Integer)
    fixed: Mapped[bool] = mapped_column(Boolean, default=False)
    compensation: Mapped[str] = mapped_column(String(20), default="others")
    compensator_id: Mapped[Optional[int]] = mapped_column(ForeignKey("participants.id"), nullable=True)
    reason: Mapped[str] = mapped_column(String(300), default="")
    created: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

    trip: Mapped[Trip] = relationship(back_populates="adjustments")
    participant: Mapped[Participant] = relationship(foreign_keys=[participant_id])
    compensator: Mapped[Optional[Participant]] = relationship(foreign_keys=[compensator_id])
    expense: Mapped[Optional[Expense]] = relationship()


class Withdrawal(Base):
    __tablename__ = "withdrawals"
    id: Mapped[int] = mapped_column(primary_key=True)
    trip_id: Mapped[int] = mapped_column(ForeignKey("trips.id"))
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"), unique=True)
    date: Mapped[Optional[dt.date]] = mapped_column(Date, nullable=True)
    absorber: Mapped[str] = mapped_column(String(20), default="group")
    kept: Mapped[str] = mapped_column(Text, default="")   # ids séparés par des virgules
    note: Mapped[str] = mapped_column(Text, default="")

    trip: Mapped[Trip] = relationship(back_populates="withdrawals")
    participant: Mapped[Participant] = relationship()

    @property
    def kept_ids(self) -> set:
        return {int(x) for x in self.kept.split(",") if x.strip()}


class Movement(Base):
    __tablename__ = "movements"
    id: Mapped[int] = mapped_column(primary_key=True)
    trip_id: Mapped[int] = mapped_column(ForeignKey("trips.id"))
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"))
    kind: Mapped[str] = mapped_column(String(20))   # versement | remboursement
    amount: Mapped[int] = mapped_column(Integer)
    date: Mapped[Optional[dt.date]] = mapped_column(Date, nullable=True)
    note: Mapped[str] = mapped_column(String(300), default="")
    method: Mapped[str] = mapped_column(String(20), default="")       # moyen de paiement (services.PAY_METHODS) ; "" : pas précisé
    deposited: Mapped[bool] = mapped_column(Boolean, default=False)   # chèque reçu : déposé à la banque ?

    trip: Mapped[Trip] = relationship(back_populates="movements")
    participant: Mapped[Participant] = relationship()


class ExpenseCheck(Base):
    """Dépense « à vérifier » : quelqu'un a rejoint un groupe (étiquette, famille) allumé dans cette dépense, mais
    ses parts sont personnalisées (parts inégales ou montant fixé) : impossible de l'y ajouter tout seul. L'alerte
    disparaît quand la dépense est enregistrée de nouveau, ou avec « C'est bon »."""
    __tablename__ = "expense_checks"
    id: Mapped[int] = mapped_column(primary_key=True)
    expense_id: Mapped[int] = mapped_column(ForeignKey("expenses.id"))
    participant_id: Mapped[int] = mapped_column(Integer)          # pas de clé : la personne peut être retirée
    reason: Mapped[str] = mapped_column(String(200), default="")  # « groupe covoit »
    created: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class Receipt(Base):
    __tablename__ = "receipts"
    id: Mapped[int] = mapped_column(primary_key=True)
    trip_id: Mapped[int] = mapped_column(ForeignKey("trips.id"))
    number: Mapped[int] = mapped_column(Integer)
    original_name: Mapped[str] = mapped_column(String(300))
    stored_name: Mapped[str] = mapped_column(String(300))
    content_type: Mapped[str] = mapped_column(String(100), default="")
    uploaded: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

    trip: Mapped[Trip] = relationship(back_populates="receipts")
    links: Mapped[list["ReceiptLink"]] = relationship(cascade="all, delete-orphan", back_populates="receipt")

    @property
    def code(self) -> str:
        return f"J{self.number:02d}"


class ReceiptLink(Base):
    __tablename__ = "receipt_links"
    receipt_id: Mapped[int] = mapped_column(ForeignKey("receipts.id"), primary_key=True)
    expense_id: Mapped[int] = mapped_column(ForeignKey("expenses.id"), primary_key=True)
    receipt: Mapped[Receipt] = relationship(back_populates="links")
    expense: Mapped[Expense] = relationship(back_populates="receipt_links")


class SubAccount(Base):
    __tablename__ = "subaccounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    trip_id: Mapped[int] = mapped_column(ForeignKey("trips.id"))
    name: Mapped[str] = mapped_column(String(200))
    member_ids: Mapped[str] = mapped_column(Text, default="")

    trip: Mapped[Trip] = relationship(back_populates="subaccounts")
    expenses: Mapped[list["SubExpense"]] = relationship(cascade="all, delete-orphan", order_by="SubExpense.id")
    transfers: Mapped[list["SubTransfer"]] = relationship(cascade="all, delete-orphan", order_by="SubTransfer.id")

    @property
    def members(self) -> list:
        return [int(x) for x in self.member_ids.split(",") if x.strip()]


class SubExpense(Base):
    __tablename__ = "sub_expenses"
    id: Mapped[int] = mapped_column(primary_key=True)
    sub_id: Mapped[int] = mapped_column(ForeignKey("subaccounts.id"))
    label: Mapped[str] = mapped_column(String(200))
    amount: Mapped[int] = mapped_column(Integer)
    payer_id: Mapped[int] = mapped_column(ForeignKey("participants.id"))
    date: Mapped[Optional[dt.date]] = mapped_column(Date, nullable=True)
    weights: Mapped[str] = mapped_column(Text, default="")   # "id:poids,id:poids"

    payer: Mapped[Participant] = relationship()

    @property
    def weight_map(self) -> dict:
        out = {}
        for part in self.weights.split(","):
            if ":" in part:
                k, v = part.split(":")
                out[int(k)] = float(v)
        return out


class SubTransfer(Base):
    __tablename__ = "sub_transfers"
    id: Mapped[int] = mapped_column(primary_key=True)
    sub_id: Mapped[int] = mapped_column(ForeignKey("subaccounts.id"))
    sender_id: Mapped[int] = mapped_column(ForeignKey("participants.id"))
    receiver_id: Mapped[int] = mapped_column(ForeignKey("participants.id"))
    amount: Mapped[int] = mapped_column(Integer)
    date: Mapped[Optional[dt.date]] = mapped_column(Date, nullable=True)


# ---------------------------------------------------------------- Préparer un voyage (calcul : preparation.py)

class Plan(Base):
    """Voyage en préparation (« projet ») : des postes, des offres, un prix par personne. À part des voyages en
    comptabilité ; `trip_id` : le voyage créé à partir de lui (lien dans un seul sens, vide tant qu'il n'est pas
    lancé)."""
    __tablename__ = "plans"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    place: Mapped[str] = mapped_column(String(200), default="")
    start: Mapped[Optional[dt.date]] = mapped_column(Date, nullable=True)
    end: Mapped[Optional[dt.date]] = mapped_column(Date, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    people: Mapped[int] = mapped_column(Integer, default=1)                  # nombre de participants (>= 1)
    margin_kind: Mapped[str] = mapped_column(String(20), default="pourcent")  # pourcent | euros (par personne)
    margin_value: Mapped[int] = mapped_column(Integer, default=0)   # centièmes de % (500 = 5 %) ou centimes par personne
    announced: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # prix annoncé par personne (centimes)
    trip_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)    # id de Trip, une fois lancé
    launched_on: Mapped[Optional[dt.date]] = mapped_column(Date, nullable=True)   # jour du lancement
    days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)       # durée du séjour, sans les deux dates
    meal_per_day: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)   # enveloppe repas (centimes par
    #                                                                            personne et par jour) ; vide : aucune
    created: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)   # projet d'exemple (demo.seed_plan_demo)

    items: Mapped[list["PlanItem"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="(PlanItem.position, PlanItem.id)")
    offers: Mapped[list["PlanOffer"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="PlanOffer.id")
    lines: Mapped[list["PlanLine"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="(PlanLine.day, PlanLine.id)")
    day_meals: Mapped[list["PlanDay"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="PlanDay.day")

    @property
    def duration(self) -> Optional[int]:
        """Nombre de jours du séjour : calculé avec les dates de départ ET de retour, sinon celui saisi."""
        if self.start and self.end:
            return max((self.end - self.start).days + 1, 1)
        return self.days


class PlanItem(Base):
    """Poste de dépense d'un projet (Avion, Hôtel, Plongées…), en texte libre."""
    __tablename__ = "plan_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    name: Mapped[str] = mapped_column(String(200))
    position: Mapped[int] = mapped_column(Integer, default=0)
    plan: Mapped[Plan] = relationship(back_populates="items")


class PlanOffer(Base):
    """Offre d'un prestataire, pour un ou plusieurs postes (un forfait « plongée + hébergement »)."""
    __tablename__ = "plan_offers"
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    label: Mapped[str] = mapped_column(String(200))                  # prestataire / libellé
    note: Mapped[str] = mapped_column(Text, default="")
    price: Mapped[int] = mapped_column(Integer, default=0)           # centimes (0 = inclus)
    counting: Mapped[str] = mapped_column(String(20), default="groupe")   # personne | groupe | lot
    capacity: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)   # places d'un lot
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    quantity_label: Mapped[str] = mapped_column(String(50), default="")      # « nuits », « plongées »
    chosen: Mapped[bool] = mapped_column(Boolean, default=False)            # offre retenue
    split: Mapped[str] = mapped_column(String(20), default="")    # répartition : "" (un bloc) | montant | pourcent
    plan: Mapped[Plan] = relationship(back_populates="offers")
    links: Mapped[list["PlanOfferItem"]] = relationship(cascade="all, delete-orphan", back_populates="offer")
    deadlines: Mapped[list["PlanDeadline"]] = relationship(
        cascade="all, delete-orphan", back_populates="offer", order_by="(PlanDeadline.date, PlanDeadline.id)")

    def item_ids(self) -> list:
        """Les postes couverts, dans l'ordre des postes du projet."""
        order = {it.id: n for n, it in enumerate(self.plan.items)}
        return [ln.item_id for ln in sorted(self.links, key=lambda ln: (order.get(ln.item_id, 1 << 30), ln.item_id))]


class PlanOfferItem(Base):
    """Une offre couvre ce poste ; `share` : sa part quand l'offre est répartie (centimes du prix saisi, ou
    centièmes de %), vide sinon."""
    __tablename__ = "plan_offer_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("plan_offers.id"))
    item_id: Mapped[int] = mapped_column(ForeignKey("plan_items.id"))
    share: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    offer: Mapped[PlanOffer] = relationship(back_populates="links")


class PlanLine(Base):
    """Une ligne du programme du séjour : ce qu'on fait tel jour (ou du jour `day` au jour `until`)."""
    __tablename__ = "plan_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    day: Mapped[int] = mapped_column(Integer)
    until: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)       # période : jusqu'au jour…
    time: Mapped[str] = mapped_column(String(5), default="")                   # « 09:30 » ; vide : sans heure
    text: Mapped[str] = mapped_column(Text)
    note: Mapped[str] = mapped_column(Text, default="")
    plan: Mapped[Plan] = relationship(back_populates="lines")
    links: Mapped[list["PlanLineItem"]] = relationship(cascade="all, delete-orphan", back_populates="line")

    def item_ids(self) -> list:
        """Les postes liés (affichage seulement), dans l'ordre des postes du projet."""
        order = {it.id: n for n, it in enumerate(self.plan.items)}
        return [ln.item_id for ln in sorted(self.links, key=lambda ln: (order.get(ln.item_id, 1 << 30), ln.item_id))]


class PlanLineItem(Base):
    """Une ligne du programme est liée à ce poste (pour l'affichage seulement : « · Vols, Transferts »)."""
    __tablename__ = "plan_line_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    line_id: Mapped[int] = mapped_column(ForeignKey("plan_lines.id"))
    item_id: Mapped[int] = mapped_column(ForeignKey("plan_items.id"))
    line: Mapped[PlanLine] = relationship(back_populates="links")
    item: Mapped["PlanItem"] = relationship()    # pour l'ordre des suppressions : le lien part avant le poste


class PlanDay(Base):
    """Réglage de l'enveloppe repas pour un jour : pas comptée ce jour-là, ou un autre montant. Un jour sans ligne
    ici suit l'enveloppe."""
    __tablename__ = "plan_days"
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    day: Mapped[int] = mapped_column(Integer)
    meal_on: Mapped[bool] = mapped_column(Boolean, default=True)
    meal_amount: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)   # centimes par personne ; vide : l'enveloppe
    plan: Mapped[Plan] = relationship(back_populates="day_meals")


class PlanDeadline(Base):
    """Échéance d'une offre, côté prestataire : option à confirmer, acompte à verser, solde à payer…"""
    __tablename__ = "plan_deadlines"
    id: Mapped[int] = mapped_column(primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("plan_offers.id"))
    label: Mapped[str] = mapped_column(String(200))
    date: Mapped[dt.date] = mapped_column(Date)
    amount: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)      # centimes, facultatif
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    offer: Mapped[PlanOffer] = relationship(back_populates="deadlines")


ASSO_NAME_KEY = "nom_asso"   # réglage : nom proposé pour la caisse des nouveaux voyages (le dernier utilisé)


@event.listens_for(Trip, "before_insert")
def _trip_asso_name(mapper, connection, target):
    """Un nouveau voyage reprend le dernier nom de caisse utilisé ; le renommer ensuite ne touche que lui."""
    if not (target.asso_name or "").strip():
        v = connection.execute(text("SELECT value FROM settings WHERE key = :k"), {"k": ASSO_NAME_KEY}).scalar()
        target.asso_name = (v or "").strip()[:100]


@event.listens_for(Trip, "before_insert")
def _trip_roles(mapper, connection, target):
    """Un nouveau voyage reprend les rôles (étiquettes) du dernier voyage créé : ceux ajoutés (« DP », « Moniteur »…)
    et ceux retirés. Le voyage d'exemple ne compte pas. Un voyage importé garde les siens."""
    if not (target.roles_conf or "").strip():
        v = connection.execute(text("SELECT roles_conf FROM trips WHERE is_demo = 0 AND roles_conf != '' "
                                    "ORDER BY created DESC, id DESC LIMIT 1")).scalar()
        if v:
            target.roles_conf = v
