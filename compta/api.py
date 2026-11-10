"""API JSON (pour une IA ou un script). Documentation interactive : /api/docs

Toutes les écritures acceptent `"simulation": true` : le résultat est calculé (changements de soldes,
avertissements) mais rien n'est enregistré. Les personnes sont désignées par leur nom ou leur id,
les montants sont en euros.
"""
from __future__ import annotations

from typing import Optional, Union

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import engine as E
from .ops import Ops, OpsError

Amount = Optional[Union[float, int, str]]
Ref = Union[int, str]


class TripIn(BaseModel):
    nom: str
    debut: Optional[str] = Field(None, description="AAAA-MM-JJ ou JJ/MM/AAAA")
    fin: Optional[str] = None
    lieu: str = ""
    organisateur: str = ""


class ParticipantsIn(BaseModel):
    noms: list[str]
    inclure_dans_depenses_communes: bool = True
    simulation: bool = False
    famille: Optional[str] = Field(None, description="ex. « Lebrun » : ces personnes règlent leurs comptes ensemble")
    accompagnant: bool = Field(False, description="True : accompagnants (ne plongent pas)")


class ExpenseIn(BaseModel):
    libelle: str
    categorie: Optional[str] = Field(None, description="ex. Logement, Transport / Covoit, Plongée, Repas")
    montant_prevu: Amount = None
    montant_reel: Amount = None
    date: Optional[str] = None
    paye_par: Optional[Union[Ref, dict[str, Union[float, str]], list[dict]]] = Field(
        "asso", description="« asso », le nom de la personne qui a avancé l'argent, ou {nom|« asso »: montant}")
    pour: Optional[Union[str, list[Ref]]] = Field("tous", description="« tous », « plongeurs », « accompagnants » ou liste")
    parts: Optional[dict[str, Union[float, str]]] = Field(None, description="{nom: nombre de parts} si inégal")
    note: str = ""
    simulation: bool = False
    type: str = Field("depense", description="« depense » ou « recette » (argent reçu ; paye_par = qui l'a reçu)")
    a_venir: bool = Field(False, description="montant pas encore connu")
    sauf: Optional[Union[str, list[str]]] = Field(None, description="personnes à retirer de « pour »")
    rembourse: Amount = Field(None, description="partie remboursée par le fournisseur (annulation, avoir)")
    rembourse_a: Optional[str] = Field("asso", description="qui a reçu ce remboursement : « asso » ou un nom")
    sous_categorie: Optional[str] = Field(None, description="ex. « Péages » dans « Transport / Covoit »")


class ExpensePatch(BaseModel):
    libelle: Optional[str] = None
    categorie: Optional[str] = None
    montant_prevu: Amount = None
    montant_reel: Amount = None
    date: Optional[str] = None
    paye_par: Optional[Ref] = None
    pour: Optional[Union[str, list[Ref]]] = None
    parts: Optional[dict[str, Union[float, str]]] = None
    note: Optional[str] = Field(None, description="Note de la dépense (remplace l'actuelle ; \"\" l'efface)")
    sous_categorie: Optional[str] = None
    sauf: Optional[list[Ref]] = Field(None, description="Personnes exclues (avec « pour »)")
    rembourse: Amount = Field(None, description="Montant remboursé par le fournisseur (0 : plus de remboursement)")
    rembourse_a: Optional[Ref] = Field(None, description="Qui a reçu ce remboursement (sinon l'asso)")
    simulation: bool = False


class MovementIn(BaseModel):
    participant: Ref
    montant: Amount
    type: str = Field("versement", description="« versement » (participant → asso) ou « remboursement » (asso → participant)")
    date: Optional[str] = None
    note: str = ""
    moyen: Optional[str] = Field(None, description="virement, chèque, espèces, carte bancaire, paiement en ligne ou autre")
    simulation: bool = False


class AdjustIn(BaseModel):
    participant: Ref
    montant: Amount
    type: str = Field("reduction", description="« reduction », « supplement » ou « nouveau_montant »")
    sur: Optional[Ref] = Field("total", description="« total » ou la dépense concernée (nom ou id)")
    compense_par: str = Field("autres", description="« autres », « asso » ou « personne »")
    compensateur: Optional[Ref] = None
    motif: str = ""
    verrouiller: bool = False
    simulation: bool = False


class PayDateIn(BaseModel):
    nom: str = Field("", description="« Acompte », « Solde »…")
    date: str = Field(..., description="au plus tard ce jour-là (AAAA-MM-JJ)")
    montant: Amount = Field(None, description="par personne ; vide = « tout ce qui reste » (dernière date seulement)")
    pour: Optional[Ref] = Field(None, description="vide : pour tout le voyage ; un nom : les dates de cette personne "
                                                   "(ou de sa famille), montants pour tout son compte")


class PayDatesIn(BaseModel):
    dates: list[PayDateIn]
    simulation: bool = False


class WithdrawIn(BaseModel):
    participant: Ref
    pris_en_charge_par: str = Field("groupe", description="« groupe » ou « asso »")
    parts_conservees: list[Ref] = Field(default_factory=list, description="dépenses non remboursables qu'il garde")
    date: Optional[str] = None
    remboursement: Amount = None
    simulation: bool = False


class SubIn(BaseModel):
    nom: str
    membres: list[Ref]


class SubExpenseIn(BaseModel):
    libelle: str
    montant: Amount
    paye_par: Ref
    pour: Optional[Union[str, list[Ref]]] = "tous"
    date: Optional[str] = None


class ReceiptIn(BaseModel):
    chemin: str = Field(description="Chemin du fichier sur cet ordinateur")
    depenses: list[Ref] = Field(default_factory=list)


class RolesIn(BaseModel):
    roles: list[str] = Field(..., description="« organisateur », « co-organisateur », « plongeur », « accompagnant » ou un rôle du voyage (« DP »…) ; liste vide = aucun")
    simulation: bool = False


class ImportIn(BaseModel):
    donnees: Union[dict, str] = Field(..., description="Le JSON décrit dans /ia/guide_ia.md (objet ou texte)")
    voyage: Optional[str] = Field("nouveau", description="« nouveau » ou l'id d'un voyage existant")
    simulation: bool = False


def install_api(app: FastAPI):
    ops = Ops(app.state.db)
    r = APIRouter(prefix="/api", tags=["API"])

    @app.exception_handler(OpsError)
    async def ops_error(request: Request, exc: OpsError):
        refusal = getattr(request.app.state, "form_refusal", None)
        if refusal and not request.url.path.startswith("/api/"):     # un formulaire : retour à la page, en clair
            return await refusal(request, exc)
        body = {"erreur": str(exc)}
        if getattr(exc, "errors", None):
            body["erreurs"] = exc.errors
        return JSONResponse(body, status_code=400)

    @r.get("/voyages", summary="Lister les voyages")
    def list_trips():
        return ops.list_trips()

    @r.post("/voyages", summary="Créer un voyage")
    def create_trip(b: TripIn):
        return ops.create_trip(**b.model_dump())

    @r.get("/voyages/{voyage}", summary="Tout sur un voyage (participants, dépenses, soldes, alertes)")
    def get_trip(voyage: str, mode: str = "reel"):
        return ops.trip(voyage, E.PLANNED if mode == "prevu" else E.ACTUAL)

    @r.post("/voyages/{voyage}/participants", summary="Ajouter des participants")
    def add_participants(voyage: str, b: ParticipantsIn):
        return ops.add_participants(voyage, **b.model_dump())

    @r.post("/voyages/{voyage}/depenses", summary="Ajouter une dépense")
    def add_expense(voyage: str, b: ExpenseIn):
        return ops.add_expense(voyage, **b.model_dump())

    @r.patch("/voyages/{voyage}/depenses/{depense}", summary="Modifier une dépense")
    def update_expense(voyage: str, depense: str, b: ExpensePatch):
        return ops.update_expense(voyage, depense, **b.model_dump())

    @r.delete("/voyages/{voyage}/depenses/{depense}", summary="Supprimer une dépense")
    def delete_expense(voyage: str, depense: str):
        return ops.delete_expense(voyage, depense)

    @r.post("/voyages/{voyage}/mouvements", summary="Versement d'un participant ou remboursement par l'asso")
    def add_movement(voyage: str, b: MovementIn):
        return ops.add_movement(voyage, **b.model_dump())

    @r.post("/voyages/{voyage}/dates-paiement", summary="Fixer les dates de paiement (acompte, solde…)")
    def set_pay_dates(voyage: str, b: PayDatesIn):
        return ops.set_pay_dates(voyage, [x.model_dump() for x in b.dates], b.simulation)

    @r.post("/voyages/{voyage}/ajustements", summary="Ajuster la part de quelqu'un")
    def adjust(voyage: str, b: AdjustIn):
        return ops.adjust(voyage, **b.model_dump())

    @r.post("/voyages/{voyage}/desistements", summary="Enregistrer un désistement")
    def withdraw(voyage: str, b: WithdrawIn):
        return ops.withdraw(voyage, **b.model_dump())

    @r.post("/voyages/{voyage}/justificatifs", summary="Ajouter un justificatif depuis un fichier local")
    def add_receipt(voyage: str, b: ReceiptIn):
        return ops.add_receipt(voyage, **b.model_dump())

    @r.post("/voyages/{voyage}/entre-nous", summary="Créer un compte « Entre nous »")
    def create_sub(voyage: str, b: SubIn):
        return ops.create_subaccount(voyage, **b.model_dump())

    @r.post("/voyages/{voyage}/entre-nous/{compte}/depenses", summary="Dépense dans un compte « Entre nous »")
    def add_subexpense(voyage: str, compte: str, b: SubExpenseIn):
        return ops.add_subexpense(voyage, compte, **b.model_dump())

    @r.put("/voyages/{voyage}/participants/{participant}/roles", summary="Rôles d'un participant")
    def set_roles(voyage: str, participant: str, b: RolesIn):
        return ops.set_roles(voyage, participant, b.roles, b.simulation)

    @r.post("/import", summary="Importer un voyage complet (format du guide /ia/guide_ia.md)")
    def import_trip(b: ImportIn):
        return ops.import_trip(b.donnees, b.voyage, b.simulation)

    @r.post("/annuler", summary="Annuler la dernière action")
    def undo():
        return ops.undo()

    app.include_router(r)
