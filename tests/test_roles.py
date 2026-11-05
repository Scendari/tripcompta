"""Rôles dans le groupe : organisateur (un seul), co-organisateurs ; « DP » (directeur de plongée) n'est plus
qu'une étiquette ordinaire du voyage (« perso:DP »), sans place à part."""
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from compta import models as M
from compta import services as S
from compta.db import Database
from compta.demo import seed_demo
from compta.ops import Ops, OpsError
from compta.web import create_app


def clean(t):
    return re.sub(r"[  ]", " ", t)


@pytest.fixture()
def demo(tmp_path):
    app = create_app(tmp_path / "d")
    tid = seed_demo(app.state.db)
    with app.state.db.Session() as s:
        P = {p.name: p.id for p in s.get(M.Trip, tid).participants}
    return TestClient(app), app, tid, P


DP = "perso:DP"


def team(app, tid):
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        out = {r: sorted(p.name for p in S.with_role(t, r)) for r in S.TEAM_ROLES}
        out["dp"] = sorted(p.name for p in S.with_role(t, DP))
        return out


def test_parse_roles():
    assert S.parse_roles("Organisateur, DP") == ["organisateur", DP]
    assert S.parse_roles(["directeur de plongée", "Co-organisatrice"]) == ["co-organisateur", DP]
    assert S.parse_roles(["co_organisateur", "orga"]) == ["organisateur", "co-organisateur"]
    assert S.parse_roles("") == []
    with pytest.raises(ValueError, match="Rôle inconnu : « trésorier »"):
        S.parse_roles("trésorier")


def test_demo_team_everywhere(demo):
    c, app, tid, P = demo
    assert team(app, tid) == {"organisateur": ["Stéphane"], "co-organisateur": ["Marie"], "dp": ["Luc", "Thierry"]}
    dash = clean(c.get(f"/v/{tid}").text)
    m = re.search(r'id="team">(.*?)<h2 class="sec-sep">Les comptes', dash, re.S)
    txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).replace(" ,", ",")
    assert "Équipe Stéphane organisateur · avec Marie" in txt and "DP" not in txt   # DP : plus dans l'Équipe
    parts = c.get(f"/v/{tid}/participants").text
    assert 'class="pill role role-perso" title="">DP</span>' in parts and 'role-organisateur' in parts
    fiche = c.get(f"/v/{tid}/p/{P['Thierry']}").text                   # onglet Comptes, filtré
    assert 'role-perso" title="">DP</span>' in fiche
    edit = parts.split(f'id="pe{P["Thierry"]}"')[1].split("</tr>")[0]       # ligne « Modifier » de la liste
    assert 'name="r_perso:DP" value="1" checked' in edit
    # dans « Qui a payé ? », l'organisateur puis la co-organisatrice en tête
    form = c.get(f"/v/{tid}/depenses/nouvelle").text
    opts = re.findall(r'<option value="\d+" >([^<]+)</option>', form.split('id="payer_sel"')[1].split("</select>")[0])
    assert opts[:2] == ["Stéphane (organisateur)", "Marie (co-organisateur)"]


def test_roles_form_single_organizer_and_undo(demo):
    c, app, tid, P = demo
    data = {"organisateur": str(P["Marie"]), f"dp_{P['Hugo']}": "1", f"co_{P['Stéphane']}": "1",
            f"dp_{P['Stéphane']}": "1"}
    r = c.post(f"/v/{tid}/roles", data=data, follow_redirects=True)
    assert "Équipe enregistrée" in r.text
    # l'ancien formulaire « Équipe » : dp_ ajoute l'étiquette DP, sans retirer celle des autres (ce n'est plus l'équipe)
    assert team(app, tid) == {"organisateur": ["Marie"], "co-organisateur": ["Stéphane"],
                              "dp": ["Hugo", "Luc", "Stéphane", "Thierry"]}
    c.post("/annuler")
    assert team(app, tid)["organisateur"] == ["Stéphane"]
    # depuis la fiche : nommer Julien organisateur le retire à Stéphane (un seul organisateur)
    c.post(f"/v/{tid}/participants/{P['Julien']}", data={"name": "Julien", "roles_form": "1",
                                                          "r_organisateur": "1", "r_perso:DP": "1"})
    assert team(app, tid)["organisateur"] == ["Julien"] and "Julien" in team(app, tid)["dp"]
    # enlever tous ses rôles
    c.post(f"/v/{tid}/participants/{P['Julien']}", data={"name": "Julien", "roles_form": "1"})
    assert team(app, tid)["organisateur"] == [] and "Julien" not in team(app, tid)["dp"]
    # une modification sans le formulaire des rôles (ex. ancienne page) ne touche pas aux rôles
    c.post(f"/v/{tid}/participants/{P['Luc']}", data={"name": "Luc", "phone": "06"})
    assert "Luc" in team(app, tid)["dp"]


def test_trip_edit_form_and_pdf(demo):
    pdfium = pytest.importorskip("pypdfium2")
    c, app, tid, P = demo
    dash = c.get(f"/v/{tid}").text
    assert 'id="t_organizer"' not in dash and "Participants → Organisation" in dash
    doc = pdfium.PdfDocument(c.get(f"/v/{tid}/export.pdf").content)
    text = clean("\n".join(doc[i].get_textpage().get_text_range() for i in range(len(doc))))
    flat = re.sub(r"\s+", " ", text)
    assert "Organisé par Stéphane" in flat and "Co-organisation : Marie" in flat
    assert "plongée (DP)" not in flat and "Thierry (" not in flat     # DP : plus d'en-tête ni d'abréviation
    assert "Stéphane (org.)" in flat and "Marie (co-org.)" in flat
    assert "org. = organisateur, co-org. = co-organisateur" in flat
    # sans organisateur désigné : le nom libre du voyage est utilisé
    c.post(f"/v/{tid}/roles", data={})
    assert team(app, tid) == {"organisateur": [], "co-organisateur": [], "dp": ["Luc", "Thierry"]}
    assert 'id="t_organizer"' in c.get(f"/v/{tid}").text
    doc = pdfium.PdfDocument(c.get(f"/v/{tid}/export.pdf").content)
    assert "Organisé par Stéphane" in clean(doc[0].get_textpage().get_text_range())


def test_ops_and_import(tmp_path):
    db = Database(tmp_path / "d")
    ops = Ops(db)
    data = {"voyage": {"nom": "Sortie", "organisateur": "Bernard", "co_organisateurs": ["Anne"], "dp": ["Bob", "anne"]},
            "participants": ["Anne", {"nom": "Bob", "roles": ["co-organisateur"]}, "Chloé"]}
    r = ops.import_trip(data)
    eq = r["apercu"]["equipe"]
    assert eq == {"organisateur": None, "co_organisateurs": ["Anne", "Bob"]}
    roles = {p["nom"]: p["roles"] for p in r["apercu"]["participants"]}
    assert "DP" in roles["Anne"] and "DP" in roles["Bob"] and "DP" not in roles["Chloé"]   # l'ancien « dp » : étiquette
    assert r["apercu"]["organisateur"] == "Bernard"            # ne participe pas : reste un simple nom
    tid = r["voyage_id"]
    ops.set_roles(tid, "Chloé", ["organisateur"])
    d = ops.trip(tid)
    assert d["organisateur"] == "Chloé" and d["equipe"]["organisateur"] == "Chloé"
    assert next(p for p in d["participants"] if p["nom"] == "Chloé")["roles"] == ["organisateur"]
    ops.set_roles(tid, "Anne", "organisateur")
    assert ops.trip(tid)["equipe"]["organisateur"] == "Anne"   # un seul organisateur
    with pytest.raises(OpsError, match="Rôle inconnu"):
        ops.set_roles(tid, "Bob", ["trésorier"])
    with pytest.raises(OpsError) as e:
        ops.import_trip({"voyage": {"nom": "x", "dp": ["Zoé"]}, "participants": ["A"]}, simulation=True)
    assert "DP « Zoé »" in e.value.errors[0] and "Faut-il l'ajouter" in e.value.errors[0]
    with pytest.raises(OpsError, match="A : Rôle inconnu"):
        ops.import_trip({"voyage": {"nom": "x"}, "participants": [{"nom": "A", "roles": "chef"}]}, simulation=True)


def test_api_and_ai_texts(tmp_path):
    from compta import importer
    c = TestClient(create_app(tmp_path / "d"))
    tid = c.post("/api/import", json={"donnees": {"voyage": {"nom": "x"}, "participants": ["A", "B"]}}).json()["voyage_id"]
    r = c.put(f"/api/voyages/{tid}/participants/B/roles", json={"roles": ["dp", "organisateur"]})
    assert r.status_code == 200 and "Organisateur, DP" in r.json()["message"]
    assert "Autres rôles utiles pour ce voyage (DP" in importer.template() and "Co-organisateur(s) :" in importer.template()
    assert "DP (directeur de plongée) :" not in importer.template()          # plus de case DP à part
    assert '"co_organisateurs"' in importer.guide() and '"dp"' not in importer.guide()


def test_old_database_gets_roles_column(tmp_path):
    d = tmp_path / "old"
    d.mkdir()
    con = sqlite3.connect(d / "compta.db")
    con.executescript("""
        CREATE TABLE trips (id INTEGER PRIMARY KEY, name VARCHAR(200), place VARCHAR(200), organizer VARCHAR(200),
                            start DATE, "end" DATE, created DATETIME);
        CREATE TABLE participants (id INTEGER PRIMARY KEY, trip_id INTEGER REFERENCES trips(id), name VARCHAR(200),
                                   phone VARCHAR(50), email VARCHAR(200), note TEXT);
        INSERT INTO trips (id, name, place, organizer) VALUES (1, 'Ancien', '', 'Luc');
        INSERT INTO participants (id, trip_id, name, phone, email, note) VALUES (1, 1, 'Luc', '', '', '');
    """)
    con.commit()
    con.close()
    ops = Ops(Database(d))
    assert ops.trip(1)["participants"][0]["roles"] == [] and ops.trip(1)["organisateur"] == "Luc"
    ops.set_roles(1, "Luc", ["organisateur", "dp"])
    assert ops.trip(1)["equipe"] == {"organisateur": "Luc", "co_organisateurs": []}
    assert ops.trip(1)["participants"][0]["roles"] == ["organisateur", "DP"]


def test_browser_roles_card(tmp_path):
    import socket
    import threading
    import time
    import uvicorn
    from playwright.sync_api import sync_playwright
    app = create_app(tmp_path / "d")
    tid = seed_demo(app.state.db)
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    while not srv.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}/v/{tid}"
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(viewport={"width": 1280, "height": 900})
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(base + "/participants")
            assert pg.locator("#roles").count() == 0                      # plus de carte « Rôles » à part
            for name, role in (("Camille", "r_organisateur"), ("Hugo", '"r_perso:DP"')):
                row = pg.locator("tr", has=pg.locator(".name-btn b", has_text=name)).first
                row.locator("button", has_text="Modifier").click()
                edit = pg.locator("tr.edit-row:visible")
                edit.locator(f"input[name={role}]").check()
                edit.locator("button", has_text="Enregistrer").click()
                assert "modifié" in pg.locator(".flash").inner_text()
            row = pg.locator("tr", has=pg.locator(".name-btn b", has_text="Camille")).first
            assert row.locator(".role-organisateur").is_visible()
            assert pg.locator("tr", has=pg.locator(".name-btn b", has_text="Hugo")).first.locator(".role-perso", has_text="DP").is_visible()
            pg.goto(base)
            assert "Camille organisateur" in re.sub(r"\s+", " ", pg.locator("#team").inner_text())
            for w in (390, 768):
                m = b.new_page(viewport={"width": w, "height": 800})
                for u in ("/participants", "", f"/p/1"):
                    m.goto(base + u)
                    assert m.evaluate("document.documentElement.scrollWidth") <= w + 1, (w, u)
            assert errors == []
            b.close()
    finally:
        srv.should_exit = True
        th.join(5)
