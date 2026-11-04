"""Modifier une dépense par l'IA (MCP) ou l'API : note, sous-catégorie, personnes exclues, remboursement.
(Jusqu'ici, seul le formulaire de l'application le permettait.)

Voyage : Anne, Bruno, Chloé. Gîte 300 € payé par l'asso, pour tous → 100 € chacun.
  remboursé 60 € par le fournisseur (reçu par l'asso) → coût partagé 240 € → 80 € chacun
  puis « sauf Chloé »                                  → 240 € / 2 = 120 € pour Anne et Bruno, 0 pour Chloé
  puis plus de remboursement                           → 300 € / 2 = 150 € chacun
"""
import pytest
from fastapi.testclient import TestClient

from compta import models as M, services as S
from compta.ops import Ops
from compta.web import create_app


@pytest.fixture
def env(tmp_path):
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.create_trip("Sortie")["voyage_id"]
    ops.add_participants(tid, ["Anne", "Bruno", "Chloé"])
    ops.add_expense(tid, "Gîte", categorie="Logement", montant_reel="300", note="à vérifier")
    return app, ops, tid


def state(app, tid):
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        e = t.expenses[0]
        r = S.compute_trip(t)
        return dict(note=e.note, sub=e.subcategory, refund=e.refund,
                    parts={p.name: r.share(p.id, e.id) for p in t.participants})


def test_ops_note_subcategory_refund_and_exclusion(env):
    app, ops, tid = env
    assert state(app, tid)["parts"] == {"Anne": 10000, "Bruno": 10000, "Chloé": 10000}
    r = ops.update_expense(tid, "Gîte", note="Facture n° 12, payée à l'arrivée", sous_categorie="location",
                           rembourse="60")
    assert "note modifiée" in r["message"] and "remboursé 60,00" in r["message"].replace(" ", " ")
    st = state(app, tid)
    assert st["note"] == "Facture n° 12, payée à l'arrivée" and st["sub"] == "location" and st["refund"] == 6000
    assert st["parts"] == {"Anne": 8000, "Bruno": 8000, "Chloé": 8000}
    ops.update_expense(tid, "Gîte", sauf=["Chloé"])
    assert state(app, tid)["parts"] == {"Anne": 12000, "Bruno": 12000, "Chloé": 0}
    ops.update_expense(tid, "Gîte", rembourse="0")
    assert state(app, tid)["parts"] == {"Anne": 15000, "Bruno": 15000, "Chloé": 0}
    ops.update_expense(tid, "Gîte", note="")                     # « » efface la note
    assert state(app, tid)["note"] == ""
    # rien d'autre ne bouge quand on ne change que la note
    before = state(app, tid)["parts"]
    ops.update_expense(tid, "Gîte", note="nouvelle note")
    assert state(app, tid)["parts"] == before


def test_api_patch(env):
    app, ops, tid = env
    cl = TestClient(app)
    r = cl.patch(f"/api/voyages/{tid}/depenses/Gîte", json={"note": "Note par l'API", "rembourse": "30",
                                                                    "rembourse_a": "Anne"})
    assert r.status_code == 200, r.text
    st = state(app, tid)
    assert st["note"] == "Note par l'API" and st["refund"] == 3000
    assert sum(st["parts"].values()) == 27000                    # 300 − 30 remboursés


def test_mcp_tool_accepts_the_new_fields(env):
    import inspect
    from compta import mcp_server
    src = inspect.getsource(mcp_server.build_server)
    for f in ("note: Optional[str]", "sous_categorie", "sauf", "rembourse", "rembourse_a"):
        assert f in src.split("def modifier_depense")[1].split("def supprimer_depense")[0], f
