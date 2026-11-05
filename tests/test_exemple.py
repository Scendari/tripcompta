"""Le voyage d'exemple « Hendaye 2026 – démo » : proposé (pas imposé), complet, entièrement fictif, à part des vrais
voyages, retirable et remis à neuf d'un clic."""
import json
import re
import zipfile

from fastapi.testclient import TestClient

from compta import models as M
from compta import services as S
from compta import transfer as TR
from compta.ops import Ops
from compta.web import create_app


def demo(app):
    with app.state.db.Session() as s:
        t = s.query(M.Trip).filter(M.Trip.is_demo.is_(True)).first()
        return None if t is None else (t.id, t.name)


def test_example_is_offered_not_imposed(tmp_path):
    app = create_app(tmp_path / "d")
    c = TestClient(app)
    home = c.get("/").text
    assert "Ouvrir l'exemple Hendaye 2026" in home and demo(app) is None          # proposé, pas ajouté d'office
    assert 'action="/exemple/ouvrir"' in c.get("/reglages").text


def test_open_example_complete_and_exact(tmp_path):
    app = create_app(tmp_path / "d")
    c = TestClient(app)
    r = c.post("/exemple/ouvrir", follow_redirects=False)
    tid, name = demo(app)
    assert name == "Hendaye 2026 – démo" and r.headers["location"].startswith(f"/v/{tid}")
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        # TEMPORAIRE (démo Gisèle, voir transfer._temp_gisele) : 17 + 1 participants, 13 + 1 dépenses, + 66 €
        assert len(t.participants) == 18 and len(t.expenses) == 14 and len(t.receipts) == 17
        assert t.asso_name == "CaisseClub"
        assert S.compute_trip(t).total_cost == 851310 + 6600                        # 8 513,10 € (site promo)
        for rec in t.receipts:
            assert (app.state.db.receipts_dir / rec.stored_name).exists()
    page = c.get(f"/v/{tid}").text
    assert "Voyage d'exemple" in page and "Recommencer l'exemple" in page and "Retirer l'exemple" in page
    # une 2e fois : pas de doublon
    c.post("/exemple/ouvrir")
    with app.state.db.Session() as s:
        assert s.query(M.Trip).filter(M.Trip.is_demo.is_(True)).count() == 1
    assert "Voir ›" in c.get("/reglages").text


def test_example_kept_apart_from_real_trips(tmp_path):
    app = create_app(tmp_path / "d")
    c = TestClient(app)
    Ops(app.state.db).create_trip("Mon vrai voyage")
    with app.state.db.Session() as s:
        t = s.query(M.Trip).first()
        t.participants = [M.Participant(name="Alice")]
        s.commit()
    assert 'data-regle="1"' in c.get("/").text                                     # le vrai voyage est réglé
    c.post("/exemple/ouvrir")
    home = c.get("/").text
    assert 'data-regle="1"' in home                                                 # l'exemple n'empêche pas la fête
    assert "1 voyage (et le voyage d&#39;exemple)" in home or "1 voyage (et le voyage d'exemple)" in home
    assert home.index("Mon vrai voyage") < home.index("Hendaye 2026 – démo")          # l'exemple en dernier
    assert "Exemple</span>" in home


def test_remove_and_reset(tmp_path):
    app = create_app(tmp_path / "d")
    c = TestClient(app)
    c.post("/exemple/ouvrir")
    tid, _ = demo(app)
    c.post(f"/v/{tid}/participants", data={"name": "Essai à moi"})                  # on bricole l'exemple
    r = c.post(f"/v/{tid}/exemple/recommencer", follow_redirects=False)
    new_id, _ = demo(app)
    with app.state.db.Session() as s:
        names = [p.name for p in s.get(M.Trip, new_id).participants]
        assert "Essai à moi" not in names and len(names) == 18                    # remis à neuf (TEMPORAIRE : + Gisèle)
    r = c.post(f"/v/{new_id}/exemple/retirer", follow_redirects=False)
    assert r.headers["location"].startswith("/?") and "&d=" in r.headers["location"]   # retiré (et « Défaire »)
    assert demo(app) is None
    assert "Ouvrir l'exemple Hendaye 2026" in c.get("/").text                     # pas revenu tout seul
    Ops(app.state.db).create_trip("Vrai")
    with app.state.db.Session() as s:
        real = s.query(M.Trip).first().id
    assert c.post(f"/v/{real}/exemple/retirer").status_code == 404                  # jamais un vrai voyage


def test_example_file_is_fictional():
    """Le fichier embarqué : rien de réel (noms génériques, téléphones et e-mails factices, caisse « CaisseClub »)."""
    with zipfile.ZipFile(TR.EXAMPLE_FILE) as z:
        data = json.loads(z.read("voyages.json"))
    v = data["voyages"][0]
    assert v["voyage"]["asso_name"] == "CaisseClub"
    for p in v["participants"]:
        assert not p.get("phone") or re.fullmatch(r"06 12 34 56 \d\d", p["phone"])
        assert not p.get("email") or p["email"].endswith("@exemple.fr")
    blob = json.dumps(data, ensure_ascii=False).lower()
    assert "apnee" not in blob and "la londe" not in blob
