"""Import IA : montant fixé dans une dépense, remises sur tout le voyage, anciens « ajustements » convertis,
rôles propres au voyage. Montants vérifiés à la main (Gîte 400 € pour A, B, C, D → 100 chacun)."""
import tempfile
from pathlib import Path

import pytest

from compta.db import Database
from compta.ops import Ops


@pytest.fixture
def ops(tmp_path):
    return Ops(Database(tmp_path / "d"))


def start(ops, **extra):
    data = {"voyage": {"nom": "Test"}, "participants": ["A", "B", "C", "D"],
            "depenses": [{"libelle": "Gîte", "reel": "400", "pour": "tous"}]}
    data.update(extra)
    r = ops.import_trip(data)
    return r["voyage_id"], {p["nom"]: p["sa_part"] for p in r["apercu"]["participants"]}, r


def test_fixe_on_expense(ops):
    tid, sh, r = start(ops, depenses=[{"libelle": "Gîte", "reel": "400", "pour": "tous", "fixe": {"D": "60"}}])
    # (400 − 60) / 3 = 113,333… : 113,33 chacun, et le centime restant pour l'un des trois (tiré au sort)
    assert sh["D"] == 60.0 and sorted([sh["A"], sh["B"], sh["C"]]) == [113.33, 113.33, 113.34]
    assert any("montant fixé : D 60,00 €" in a.replace("\u202f", " ").replace("\xa0", " ") for a in r["actions"])
    # mise à jour : la dépense passe à 460 € ; D reste à 60, les autres 400 / 3 = 133,33 (+1 c) ; puis D libéré
    r = ops.import_trip({"voyage_id": tid, "depenses_a_modifier": [{"depense": "Gîte", "reel": "460"}]},
                        voyage=tid, verifier_id=tid)
    assert {p["nom"]: p["sa_part"] for p in r["apercu"]["participants"]}["D"] == 60.0
    r = ops.import_trip({"voyage_id": tid, "depenses_a_modifier": [{"depense": "Gîte", "fixe": {"D": None}}]},
                        voyage=tid, verifier_id=tid)
    assert {p["nom"]: p["sa_part"] for p in r["apercu"]["participants"]} == {"A": 115.0, "B": 115.0, "C": 115.0,
                                                                             "D": 115.0}


def test_fixe_errors_are_clear(ops):
    with pytest.raises(Exception) as e:
        start(ops, depenses=[{"libelle": "Gîte", "reel": "400", "pour": ["A", "B"], "fixe": {"C": "60"}}])
    assert "C n'est pas parmi les personnes de « Gîte »" in str(e.value)
    with pytest.raises(Exception) as e:
        start(ops, depenses=[{"libelle": "Gîte", "reel": "400", "pour": "tous", "fixe": {"A": "300", "B": "200"}}])
    assert "plus que la dépense" in str(e.value)


def test_remises_and_old_adjustments_converted(ops):
    tid, sh, r = start(ops, remises=[
        {"participant": "A", "type": "remise", "montant": "30", "compense_par": "asso", "motif": "encadrant"},
        {"participant": "B", "type": "supplement", "montant": "30", "compense_par": "autres"}])
    # A : 100 − 30 offerts par l'asso = 70 ; B : 100 + 30 = 130, payés en moins par les autres au prorata
    # de leurs totaux (A 70, C 100, D 100) : A paie donc un peu moins de 70
    assert sh["A"] < 70 and sh["B"] == 130.0 and r["apercu"]["equilibre"]
    acts = " | ".join(r["actions"]).replace("\u202f", " ").replace("\xa0", " ")
    assert "Remise de 30,00 € pour A (encadrant)" in acts and "Supplément de 30,00 € pour B" in acts
    # ancien format : « nouveau_montant » sur une dépense, compensé par les autres → montant fixé
    r = ops.import_trip({"voyage_id": tid, "ajustements": [
        {"participant": "C", "type": "nouveau_montant", "montant": "50", "sur": "Gîte", "compense_par": "autres",
         "verrouiller": True}]}, voyage=tid, verifier_id=tid)
    gite = next(d for d in r["apercu"]["depenses"] if d["libelle"] == "Gîte")
    assert gite["fixe"] == {"C": 50.0}
    assert all(a["sur"] == "total" for a in r["apercu"]["ajustements"])     # plus aucun ajustement sur une dépense
    # ancien format sur une dépense mais compensé par l'asso → remise sur le total (même effet sur son compte)
    r = ops.import_trip({"voyage_id": tid, "ajustements": [
        {"participant": "D", "type": "reduction", "montant": "10", "sur": "Gîte", "compense_par": "asso",
         "motif": "étudiant"}]}, voyage=tid, verifier_id=tid)
    adj = [a for a in r["apercu"]["ajustements"] if a["participant"] == "D"]
    assert adj == [dict(adj[0], sur="total", valeur=-10.0, compense_par="asso", motif="étudiant — sur « Gîte »")]


def test_trip_roles_from_ia(ops):
    tid, sh, r = start(ops, voyage={"nom": "Test", "roles_du_voyage": ["Conducteur"]},
                       participants=[{"nom": "A", "roles": ["Conducteur"]}, "B", {"nom": "C", "roles": ["conducteur"]}, "D"],
                       depenses=[{"libelle": "Essence", "reel": "90", "pour": "Conducteur"}])
    assert sh == {"A": 45.0, "B": 0.0, "C": 45.0, "D": 0.0}
    assert "Nouveau rôle du voyage : Conducteur" in r["actions"]
    # déjà là : pas de doublon, pas d'erreur
    r = ops.import_trip({"voyage_id": tid, "voyage": {"roles_du_voyage": ["conducteur"]}}, voyage=tid, verifier_id=tid)
    assert not any("rôle" in a.lower() for a in r["actions"])


def test_asso_name_refund_date_and_supplement_default(ops):
    """Retours du test « IA externe » : nom de l'asso, date du remboursement d'un désistement,
    supplément compensé par les autres par défaut."""
    tid, sh, r = start(ops, voyage={"nom": "Test", "asso": "Club Subaqua", "caisse": "Club Subaqua"},
                       depenses=[{"libelle": "Gîte", "reel": "400", "pour": "tous", "paye_par": "Club Subaqua"}],
                       versements=[{"participant": "D", "montant": "100", "date": "2026-03-01"}],
                       desistements=[{"participant": "D", "date": "2026-05-02", "pris_en_charge_par": "groupe",
                                      "remboursement": "100", "date_remboursement": "2026-05-10"}],
                       remises=[{"participant": "A", "type": "supplement", "montant": "30"}])
    assert "Nom de l'asso : Club Subaqua" in r["actions"]
    mv = [m for m in r["apercu"]["mouvements"] if m["type"] == "remboursement"]
    assert mv[0]["date"] == "2026-05-10" and mv[0]["montant"] == 100.0
    adj = r["apercu"]["ajustements"][0]
    assert (adj["valeur"], adj["compense_par"]) == (30.0, "others")
    # Gîte 400 € entre A, B, C (D désisté sans rien garder) : 133,33 chacun + 1 centime pour l'un d'eux ;
    # A +30, B et C −15 → 163,33 / 118,33 / 118,33, l'un des trois avec 1 centime de plus
    assert sh["D"] == 0.0 and round(sh["A"] + sh["B"] + sh["C"], 2) == 400.0
    assert sorted([round(sh["A"] - 163.33, 2), round(sh["B"] - 118.33, 2), round(sh["C"] - 118.33, 2)]) == [0, 0, 0.01]
    # un autre nom alors que l'asso en a déjà un : pas appliqué, signalé
    r = ops.import_trip({"voyage_id": tid, "voyage": {"asso": "Autre club"}}, voyage=tid, verifier_id=tid)
    assert any("s'appelle déjà « Club Subaqua »" in n for n in r["a_verifier"])
