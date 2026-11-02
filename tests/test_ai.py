"""Tests de l'accès IA : couche d'opérations, API JSON et serveur MCP (vrai sous-processus stdio)."""
import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from compta.db import Database
from compta.ops import Ops, OpsError
from compta.web import create_app

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- couche d'opérations

@pytest.fixture()
def ops(tmp_path):
    return Ops(Database(tmp_path / "d"))


def test_ops_scenario_like_an_ai(ops, tmp_path):
    tid = ops.create_trip("Plongée Groix", "12/06/2027", "15/06/2027", "Groix", "Stéphane")["voyage_id"]
    ops.add_participants(tid, ["Anne", "Bob", "Chloé", "Élodie"])
    # nom sans accent / minuscules accepté
    ops.add_expense(tid, "Gîte", "Logement", montant_prevu="800", paye_par="asso", pour="tous")
    ops.add_expense(tid, "Plongées", "Plongée", montant_reel=480, pour=["anne", "BOB", "elodie"])
    ops.add_expense(tid, "Carburant", "Transport / Covoit", montant_reel="96,40", paye_par="Chloé",
                    parts={"Chloé": 1, "Anne": 1, "Bob": "0,5"})
    ops.add_movement(tid, "Anne", "200")
    res = ops.adjust(tid, "Bob", 20, "reduction", sur="Gîte", compense_par="asso", motif="étudiant")
    assert res["equilibre"] and any(c["personne"] == "Bob" for c in res["changements"])
    ops.withdraw(tid, "Élodie", "groupe", parts_conservees=["Gîte"])
    d = ops.trip(tid)
    assert d["equilibre"]
    assert {p["nom"] for p in d["participants"]} == {"Anne", "Bob", "Chloé", "Élodie"}
    assert d["pris_en_charge_par_asso"][0]["montant"] == 20.0
    bob = next(p for p in d["participants"] if p["nom"] == "Bob")
    assert bob["sa_part"] == 180.0 + 240.0 + 19.28
    # justificatif depuis un fichier local
    f = tmp_path / "ticket.jpg"
    f.write_bytes(b"\xff\xd8x")
    assert ops.add_receipt(tid, str(f), ["Carburant"])["code"] == "J01"
    # entre nous
    ops.create_subaccount(tid, "Resto", ["Anne", "Bob"])
    ops.add_subexpense(tid, "Resto", "Pizzas", "30", "Anne")
    assert ops.trip(tid)["entre_nous"][0]["a_regler"] == [{"de": "Bob", "a": "Anne", "montant": 15.0}]


def test_ops_simulation_writes_nothing(ops):
    tid = ops.create_trip("T")["voyage_id"]
    ops.add_participants(tid, ["Anne", "Bob"])
    r = ops.add_expense(tid, "Gîte", montant_reel=100, paye_par="Anne", simulation=True)
    assert r["simulation"] and "depense_id" not in r
    assert {c["personne"]: c["reste_apres"] for c in r["changements"]} == {"Anne": -50.0, "Bob": 50.0}
    assert ops.trip(tid)["depenses"] == []


def test_ops_errors_are_questions(ops):
    tid = ops.create_trip("T")["voyage_id"]
    ops.add_participants(tid, ["Anne", "Annabelle", "Bob"])
    with pytest.raises(OpsError, match="Faut-il l'ajouter"):
        ops.add_expense(tid, "Gîte", montant_reel=100, paye_par="Zoé")
    with pytest.raises(OpsError, match="ambigu"):
        ops.add_movement(tid, "Ann", 10)
    with pytest.raises(OpsError, match="montant"):
        ops.add_expense(tid, "Gîte")
    with pytest.raises(OpsError, match="Voyage introuvable"):
        ops.trip(99)
    # une erreur ne laisse pas d'action « Annuler » fantôme
    assert ops.db.last_action() == "ajout de participants (IA)"


def test_ops_undo(ops):
    tid = ops.create_trip("T")["voyage_id"]
    ops.add_participants(tid, ["Anne"])
    ops.add_movement(tid, "Anne", 10)
    assert "Annulé" in ops.undo()["message"]
    assert ops.trip(tid)["mouvements"] == []


# ---------------------------------------------------------------- API JSON

def test_api(tmp_path):
    c = TestClient(create_app(tmp_path / "d"))
    tid = c.post("/api/voyages", json={"nom": "API", "debut": "2027-05-01"}).json()["voyage_id"]
    c.post(f"/api/voyages/{tid}/participants", json={"noms": ["Anne", "Bob"]})
    r = c.post(f"/api/voyages/{tid}/depenses", json={"libelle": "Gîte", "montant_reel": 100, "paye_par": "Anne"})
    assert r.status_code == 200
    eid = r.json()["depense_id"]
    r = c.patch(f"/api/voyages/{tid}/depenses/{eid}", json={"montant_reel": "120"})
    assert r.status_code == 200
    c.post(f"/api/voyages/{tid}/mouvements", json={"participant": "Bob", "montant": "60"})
    d = c.get(f"/api/voyages/{tid}").json()
    assert {p["nom"]: p["reste"] for p in d["participants"]} == {"Anne": -60.0, "Bob": 0.0}
    err = c.post(f"/api/voyages/{tid}/mouvements", json={"participant": "Zoé", "montant": 5})
    assert err.status_code == 400 and "Zoé" in err.json()["erreur"]
    assert c.get("/api/openapi.json").status_code == 200
    assert c.get("/api/docs").status_code == 200
    assert c.post("/api/annuler").json()["message"].startswith("Annulé")
    # la page web voit les données saisies par l'API
    assert "Gîte" in c.get(f"/v/{tid}/depenses").text


# ---------------------------------------------------------------- MCP (stdio, vrai processus)

def test_mcp_stdio_end_to_end(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        params = StdioServerParameters(command=sys.executable,
                                       args=["-m", "compta.mcp_server", "--data-dir", str(tmp_path / "d")],
                                       cwd=str(ROOT))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                assert "POSE LA QUESTION" in (init.instructions or "")
                tools = {t.name for t in (await session.list_tools()).tools}
                assert {"ajouter_depense", "ajuster_part", "desister", "voir_voyage"} <= tools
                prompts = {p.name for p in (await session.list_prompts()).prompts}
                assert "importer_un_fichier" in prompts

                async def call(name, **args):
                    res = await session.call_tool(name, args)
                    return json.loads(res.content[0].text)

                tid = (await call("creer_voyage", nom="MCP"))["voyage_id"]
                await call("ajouter_participants", voyage=tid, noms=["Anne", "Bob", "Chloé"])
                r = await call("ajouter_depense", voyage=tid, libelle="Gîte", montant_reel="300", paye_par="Bob")
                assert r["equilibre"]
                err = await call("enregistrer_mouvement", voyage=tid, participant="Zoé", montant=10)
                assert "Faut-il l'ajouter" in err["erreur"]
                await call("ajuster_part", voyage=tid, participant="Anne", montant=30, sur="Gîte",
                           compense_par="asso", motif="étudiante")
                d = await call("voir_voyage", voyage=tid)
                assert {p["nom"]: p["reste"] for p in d["participants"]} == {"Anne": 70.0, "Bob": -200.0,
                                                                             "Chloé": 100.0}
                assert d["equilibre"]

    asyncio.run(run())
