"""Serveur MCP : permet à une IA (Claude Desktop, Claude Code…) de remplir Trip Compta.

Lancement (transport stdio) :
    python -m compta.mcp_server [--data-dir DOSSIER]

L'IA travaille directement sur la même base que l'application web (Documents/TripCompta) :
il suffit de rafraîchir la page du navigateur pour voir ce qu'elle a saisi.
Chaque écriture de l'IA peut être annulée avec le bouton « Annuler » de l'application.
"""
from __future__ import annotations

import argparse
import functools
import os
from typing import Any, Optional, Union

from .db import Database
from .ops import Ops, OpsError

try:  # SDK MCP 2.x
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # SDK MCP 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore

Ref = Union[int, str]

INSTRUCTIONS = """\
Tu aides l'organisateur d'un voyage associatif (souvent non comptable) à remplir son outil de comptabilité.
L'utilisateur te donne en général un fichier (tableur, notes, relevé, photos de tickets) : lis-le, puis
saisis les informations avec les outils ci-dessous.

Méthode :
1. L'utilisateur te dit en général sur quel voyage travailler (« voyage n°2 »). Sinon, `lister_voyages` et
   demande-lui lequel. Puis `voir_voyage` pour connaître l'existant (ne recrée pas ce qui existe déjà).
2. Ajoute les participants manquants (`ajouter_participants`).
3. Pour chaque dépense, détermine : libellé, catégorie, montant prévu et/ou réel, date, QUI A PAYÉ
   (« asso » si payé depuis la caisse de l'asso, sinon le nom du participant qui a avancé l'argent),
   et POUR QUI (« tous », une liste de noms, ou des parts inégales : nuits, demi-part…).
4. Versements des participants à l'asso et remboursements : `enregistrer_mouvement`.
5. Réductions / suppléments : `ajuster_part` ; désistements : `desister`.

Règles importantes :
- Dès qu'une information est ambiguë ou manquante (qui a payé ? pour qui ? prévu ou réel ? un nom inconnu ?
  un doublon possible ?), POSE LA QUESTION à l'utilisateur au lieu de deviner. Regroupe tes questions.
- Avant d'enregistrer un lot, utilise `simulation=true` et montre un récapitulatif clair à l'utilisateur
  (dépenses, montants, qui a payé, pour qui, soldes qui changent) ; enregistre seulement après son accord.
- Les montants sont en euros (« 12,50 »). Les personnes se désignent par leur prénom.
- Familles : des participants peuvent former une famille (paramètre `famille`) ; ils règlent leurs comptes
  ensemble. Un versement « Famille Lebrun » est enregistré au nom de la personne qui paie pour la famille.
- Accompagnants : personnes qui ne plongent pas (`accompagnant=true`). Dans `pour`, on peut écrire
  « plongeurs », « accompagnants » ou « famille Lebrun ».
- Quand un outil renvoie une `erreur`, elle est rédigée pour t'aider : reformule-la en question si besoin.
- À la fin, appelle `voir_voyage` et résume : coût total, qui doit encore combien, alertes éventuelles.
- En cas d'erreur de saisie, `annuler_derniere_action` revient en arrière (une action à la fois).
"""


def _safe(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except OpsError as e:
            return {"erreur": str(e)}
    return wrapper


def build_server(data_dir: Optional[str] = None):
    db = Database(data_dir)
    ops = Ops(db)
    mcp = _Server(name="tripcompta", instructions=INSTRUCTIONS)


    @mcp.tool()
    @_safe
    def lister_voyages() -> list[dict]:
        """Liste les voyages existants avec leurs chiffres clés."""
        return ops.list_trips()

    @mcp.tool()
    @_safe
    def voir_voyage(voyage: Ref, mode: str = "reel") -> dict:
        """Tout sur un voyage : participants et leur solde, dépenses, versements, ajustements, désistements,
        justificatifs, comptes « Entre nous », alertes. mode = « reel » ou « prevu »."""
        from . import engine as E
        return ops.trip(voyage, E.PLANNED if mode == "prevu" else E.ACTUAL)

    @mcp.tool()
    @_safe
    def creer_voyage(nom: str, debut: Optional[str] = None, fin: Optional[str] = None, lieu: str = "",
                     organisateur: str = "") -> dict:
        """Crée un voyage. Dates au format 2027-05-13 ou 13/05/2027."""
        return ops.create_trip(nom, debut, fin, lieu, organisateur)

    @mcp.tool()
    @_safe
    def ajouter_participants(voyage: Ref, noms: list[str], inclure_dans_depenses_communes: bool = True,
                             simulation: bool = False, famille: Optional[str] = None,
                             accompagnant: bool = False) -> dict:
        """Ajoute des participants (les noms déjà présents sont ignorés). Si inclure_dans_depenses_communes,
        ils sont aussi ajoutés aux dépenses partagées par tout le monde (sauf la plongée pour les accompagnants).
        famille = nom de famille commun (« Lebrun ») : ses membres règlent leurs comptes ensemble avec l'asso.
        accompagnant = True pour des personnes qui ne plongent pas."""
        return ops.add_participants(voyage, noms, inclure_dans_depenses_communes, simulation, famille, accompagnant)

    @mcp.tool()
    @_safe
    def ajouter_depense(voyage: Ref, libelle: str, categorie: Optional[str] = None,
                        montant_prevu: Optional[Union[float, str]] = None,
                        montant_reel: Optional[Union[float, str]] = None, date: Optional[str] = None,
                        paye_par: Union[str, dict[str, Union[float, str]]] = "asso",
                        pour: Union[str, list[str]] = "tous",
                        parts: Optional[dict[str, float]] = None, note: str = "",
                        simulation: bool = False, type: str = "depense", a_venir: bool = False,
                        sauf: Optional[list[str]] = None) -> dict:
        """Ajoute une dépense (ou une recette).
        - paye_par : « asso », le nom du participant qui a avancé l'argent, ou plusieurs payeurs
          {"Stéphane": "3116", "asso": "1200"}. Pour une recette : qui l'a reçue.
        - pour : « tous », « plongeurs », « accompagnants » ou une liste de noms ; sauf : personnes à retirer.
        - parts : {nom: nombre de parts} pour une répartition inégale (ex. nuits, demi-part) ; remplace `pour`.
        - type : « depense » ou « recette » (remboursement d'un fournisseur, subvention…).
        - a_venir=true : montant pas encore connu.
        - simulation=true : calcule sans enregistrer."""
        return ops.add_expense(voyage, libelle, categorie, montant_prevu, montant_reel, date, paye_par, pour,
                               parts, note, simulation, type, a_venir, sauf)

    @mcp.tool()
    @_safe
    def modifier_depense(voyage: Ref, depense: Ref, libelle: Optional[str] = None, categorie: Optional[str] = None,
                         montant_prevu: Optional[Union[float, str]] = None,
                         montant_reel: Optional[Union[float, str]] = None, date: Optional[str] = None,
                         paye_par: Optional[Ref] = None, pour: Optional[Union[str, list[str]]] = None,
                         parts: Optional[dict[str, float]] = None, simulation: bool = False) -> dict:
        """Modifie une dépense existante (désignée par son id ou son libellé). Seuls les champs donnés changent."""
        return ops.update_expense(voyage, depense, libelle, categorie, montant_prevu, montant_reel, date,
                                  paye_par, pour, parts, simulation)

    @mcp.tool()
    @_safe
    def supprimer_depense(voyage: Ref, depense: Ref) -> dict:
        """Supprime une dépense (id ou libellé)."""
        return ops.delete_expense(voyage, depense)

    @mcp.tool()
    @_safe
    def enregistrer_mouvement(voyage: Ref, participant: Ref, montant: Union[float, str], type: str = "versement",
                              date: Optional[str] = None, note: str = "", simulation: bool = False) -> dict:
        """type = « versement » (le participant a donné de l'argent à l'asso) ou « remboursement »
        (l'asso a rendu de l'argent au participant)."""
        return ops.add_movement(voyage, participant, montant, type, date, note, simulation)

    @mcp.tool()
    @_safe
    def ajuster_part(voyage: Ref, participant: Ref, montant: Union[float, str], type: str = "reduction",
                     sur: Ref = "total", compense_par: str = "autres", compensateur: Optional[Ref] = None,
                     motif: str = "", verrouiller: bool = False, simulation: bool = False) -> dict:
        """Change la part de quelqu'un sans casser l'équilibre.
        - type : « reduction », « supplement » ou « nouveau_montant ».
        - sur : « total » (tout son voyage) ou une dépense (id ou libellé).
        - compense_par : « autres » (les autres participants au prorata), « asso », ou « personne » (+ compensateur).
        - verrouiller : le montant ne bougera plus lors des recalculs."""
        return ops.adjust(voyage, participant, montant, type, sur, compense_par, compensateur, motif, verrouiller,
                          simulation)

    @mcp.tool()
    @_safe
    def desister(voyage: Ref, participant: Ref, pris_en_charge_par: str = "groupe",
                 parts_conservees: Optional[list[Ref]] = None, date: Optional[str] = None,
                 remboursement: Optional[Union[float, str]] = None, simulation: bool = False) -> dict:
        """Désistement d'un participant. parts_conservees = dépenses non remboursables qu'il paie quand même ;
        le reste de sa part est repris par « groupe » ou « asso ». remboursement = montant rendu maintenant."""
        return ops.withdraw(voyage, participant, pris_en_charge_par, parts_conservees or [], date, remboursement,
                            simulation)

    @mcp.tool()
    @_safe
    def ajouter_justificatif(voyage: Ref, chemin: str, depenses: Optional[list[Ref]] = None) -> dict:
        """Joint un fichier (photo ou PDF présent sur cet ordinateur) comme justificatif d'une ou plusieurs dépenses."""
        return ops.add_receipt(voyage, chemin, depenses or [])

    @mcp.tool()
    @_safe
    def creer_compte_entre_nous(voyage: Ref, nom: str, membres: list[str]) -> dict:
        """Crée un petit compte entre participants, hors asso (ex. un resto partagé)."""
        return ops.create_subaccount(voyage, nom, membres)

    @mcp.tool()
    @_safe
    def ajouter_depense_entre_nous(voyage: Ref, compte: Ref, libelle: str, montant: Union[float, str],
                                   paye_par: str, pour: Union[str, list[str]] = "tous",
                                   date: Optional[str] = None) -> dict:
        """Ajoute une dépense dans un compte « Entre nous » (hors asso)."""
        return ops.add_subexpense(voyage, compte, libelle, montant, paye_par, pour, date)

    @mcp.tool()
    @_safe
    def importer_voyage(donnees: dict, voyage: str = "nouveau", simulation: bool = True) -> dict:
        """Applique d'un coup un lot de changements au format du guide (outil `lire_guide`) : ajouts, et pour un
        voyage existant modifications / suppressions (depenses_a_modifier, depenses_a_supprimer…).
        voyage = l'id du voyage (ou « nouveau »). Fais d'abord simulation=true et montre la liste « actions »
        et les « changements » (avant / après) à l'utilisateur."""
        return ops.import_trip(donnees, voyage, simulation)

    @mcp.tool()
    def lire_guide() -> str:
        """Mode d'emploi de l'application pour l'IA, avec le format JSON de `importer_voyage`."""
        from .importer import guide
        return guide()

    @mcp.tool()
    @_safe
    def definir_roles(voyage: Ref, participant: Ref, roles: list[str], simulation: bool = False) -> dict:
        """Rôles d'un participant dans le groupe : « organisateur » (un seul par voyage), « co-organisateur »,
        « dp » (directeur de plongée). Liste vide = aucun rôle. Remplace ses rôles actuels."""
        return ops.set_roles(voyage, participant, roles, simulation)

    @mcp.tool()
    @_safe
    def annuler_derniere_action() -> dict:
        """Annule la dernière modification (comme le bouton « Annuler » de l'application)."""
        return ops.undo()

    @mcp.prompt()
    def importer_un_fichier(voyage: str = "") -> str:
        """Importer les informations d'un fichier dans un voyage, en posant des questions si besoin."""
        cible = f"le voyage « {voyage} »" if voyage else "le bon voyage (demande-moi lequel, ou crée-le)"
        return (f"Je vais te donner un fichier avec les informations de mon voyage. Remplis {cible} dans "
                "Trip Compta. Pose-moi des questions pour tout ce qui n'est pas clair (qui a payé, pour qui, "
                "prévu ou réel, noms inconnus). Fais une simulation et montre-moi un récapitulatif avant "
                "d'enregistrer.")

    return mcp


def main():
    ap = argparse.ArgumentParser(description="Serveur MCP de Trip Compta (stdio)")
    ap.add_argument("--data-dir", default=os.environ.get("COMPTA_DATA_DIR"))
    a = ap.parse_args()
    build_server(a.data_dir).run("stdio")


if __name__ == "__main__":
    main()
