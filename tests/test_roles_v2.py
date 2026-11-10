"""Plongeur et accompagnant sont des rôles comme les autres ; « Pour qui ? » se coche par rôle, famille ou groupe."""
import re
import sqlite3

from fastapi.testclient import TestClient

from compta import models as M
from compta import services as S
from compta.db import Database
from compta.ops import Ops
from compta.web import create_app


def test_roles_pills_and_who_chips(tmp_path):
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.import_trip({"voyage": {"nom": "x"}, "participants": [
        "Anne", {"nom": "Bob", "roles": ["dp"]}, {"nom": "Léa Roux", "accompagnant": True, "famille": "Roux"},
        {"nom": "Max Roux", "famille": "Roux"}]})["voyage_id"]
    c = TestClient(app)
    liste = c.get(f"/v/{tid}/participants").text
    assert liste.count("role-plongeur") >= 3 and "role-accompagnant" in liste            # affichés comme les autres
    form = c.get(f"/v/{tid}/depenses/nouvelle").text
    assert "Plongeurs seulement" not in form and "Accompagnants seulement" not in form
    chips = re.findall(r'data-only="([\d,]*)"[^>]*>([^<]+)</button>', form)
    names = {lbl: ids for ids, lbl in chips}
    assert set(names) >= {"Plongeur", "Accompagnant", "DP", "Roux"}
    P = {p["nom"]: p["id"] for p in ops.trip(tid)["participants"]}
    assert names["Roux"] == ",".join(str(i) for i in sorted([P["Léa Roux"], P["Max Roux"]]))


def test_ai_pour_by_role(tmp_path):
    ops = Ops(Database(tmp_path / "d"))
    tid = ops.import_trip({"voyage": {"nom": "x"}, "participants": [
        "Anne", {"nom": "Bob", "roles": ["dp"]}, {"nom": "Léa", "accompagnant": True}],
        "depenses": [{"libelle": "Bateau", "reel": "60", "pour": "plongeurs"},
                     {"libelle": "Cadeau DP", "reel": "20", "pour": "DP"},
                     {"libelle": "Resto", "reel": "30", "pour": ["accompagnants", "Anne"]}]})["voyage_id"]
    p = {x["nom"]: x["sa_part"] for x in ops.trip(tid)["participants"]}
    assert p == {"Anne": 30.0 + 15.0, "Bob": 30.0 + 20.0, "Léa": 15.0}


def test_expense_label_without_les_plongeurs(tmp_path):
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.import_trip({"voyage": {"nom": "x"}, "participants": ["Anne", "Bob", {"nom": "Léa", "accompagnant": True}],
                           "depenses": [{"libelle": "Bateau", "reel": "60", "pour": "plongeurs"},
                                        {"libelle": "Gîte", "reel": "90", "pour": "tous"}]})["voyage_id"]
    page = TestClient(app).get(f"/v/{tid}/depenses").text
    assert "les plongeurs" not in page and "3 pers." in page and "2 pers." in page and "<span>Tout le monde</span>" in page
