"""Exporter un voyage (.zip) sur une installation et l'importer sur une autre : mêmes comptes, au centime près."""
import io
import json
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

from compta import models as M, services as S
from compta.demo import seed_demo, seed_family_demo
from compta.ops import Ops, trip_detail
from compta.web import create_app


def snapshot(app, tid):
    """Tout ce qui compte pour l'utilisateur, sans les identifiants (qui changent à l'import)."""
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        d = trip_detail(t)
        for k in ("voyage_id", "id"):
            d.pop(k, None)
        txt = json.dumps(d, ensure_ascii=False, sort_keys=True, default=str)
        txt = re.sub(r'"id": \d+', '"id": 0', txt)
        r = S.compute_trip(t)
        shares = {p.name: (p.share, p.balance) for p in r.people.values()}
        extra = dict(groups=[(g["name"], sorted(p.name for p in g["members"])) for g in S.trip_groups(t)],
                     cashier=next((p.name for p in t.participants if p.id == t.cashier_id), None),
                     roles={p.name: p.roles for p in t.participants},
                     families={f.name: next((p.name for p in t.participants if p.id == f.referent_id), None)
                               for f in t.families},
                     fixed={e.label: {sh.participant_id and next(p.name for p in t.participants
                                                                  if p.id == sh.participant_id): sh.fixed
                                      for sh in e.shares if sh.fixed is not None} for e in t.expenses},
                     withdrawals={w.participant.name: sorted(e.label for e in t.expenses if e.id in w.kept_ids)
                                  for w in t.withdrawals},
                     subs={a.name: sorted(p.name for p in t.participants if p.id in a.members) for a in t.subaccounts},
                     receipts=sorted((r_.code, r_.original_name, sorted(l.expense.label for l in r_.links))
                                     for r_ in t.receipts))
        return txt, shares, extra, r.is_balanced()


@pytest.mark.parametrize("seed", [seed_demo, seed_family_demo])
def test_round_trip_to_another_installation(tmp_path, seed):
    a = create_app(tmp_path / "a")
    tid = seed(a.state.db)
    ops = Ops(a.state.db)
    # de quoi tout couvrir : groupe, montant fixé, remise, caisse tenue par un participant, nom de l'asso
    with a.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        p1, p2 = t.participants[0], t.participants[1]
        S.save_group(t, "Voiture", [p1.id, p2.id])
        e = next(e for e in t.expenses if e.best_known() and len(e.shares) > 2)
        sh = next(x for x in e.shares if x.participant_id != p1.id)
        sh.fixed = 1000
        t.adjustments.append(M.Adjustment(participant_id=p1.id, expense_id=None, value=-500, compensation="asso",
                                          reason="encadrant"))
        S.set_asso_name(s, "Club Subaqua")
        s.commit()
    ca = TestClient(a)
    ca.post(f"/v/{tid}/justificatifs", files={"files": ("ticket.jpg", b"\xff\xd8\xffJPEGDATA", "image/jpeg")},
            data={"expenses": [str(e.id)]})
    before = snapshot(a, tid)
    r = ca.get(f"/v/{tid}/voyage.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert "voyages.json" in z.namelist() and any(n.startswith("justificatifs/") for n in z.namelist())

    b = create_app(tmp_path / "b")                    # l'autre ordinateur
    cb = TestClient(b)
    pv = cb.post("/importer", files={"fichier": ("voyage.zip", r.content, "application/zip")})
    assert pv.status_code == 200 and "Rien n'est encore enregistré" in pv.text
    token = re.search(r'action="/importer/([0-9a-f]{32})"', pv.text).group(1)
    with b.state.db.Session() as s:
        assert s.query(M.Trip).count() == 0                  # l'aperçu n'enregistre rien
    done = cb.post(f"/importer/{token}", follow_redirects=False)
    new_id = int(re.search(r"/v/(\d+)", done.headers["location"]).group(1))
    after = snapshot(b, new_id)
    assert after[1] == before[1]                             # parts et soldes, au centime
    assert after[2] == before[2] and after[3]                # groupes, caisse, rôles, familles, montants fixés…
    assert after[0] == before[0]                             # tout le détail du voyage
    with b.state.db.Session() as s:
        assert s.get(M.Trip, new_id).asso_name == "Club Subaqua"   # nom de l'asso du voyage
        assert S.asso_name(s) == "Club Subaqua"              # devient le nom proposé (il manquait ici)
        rec = next(x for x in s.get(M.Trip, new_id).receipts if x.original_name == "ticket.jpg")
        assert (b.state.db.receipts_dir / rec.stored_name).read_bytes() == b"\xff\xd8\xffJPEGDATA"
    assert cb.post(f"/importer/{token}", follow_redirects=True).text.count("expiré") >= 1   # usage unique


def test_import_never_replaces(tmp_path):
    a = create_app(tmp_path / "a")
    tid = seed_demo(a.state.db)
    c = TestClient(a)
    with a.state.db.Session() as s:
        S.set_asso_name(s, "Mon club")
        s.commit()
    data = c.get(f"/v/{tid}/voyage.zip").content
    token = re.search(r'/importer/([0-9a-f]{32})', c.post("/importer", files={"fichier": ("v.zip", data)}).text).group(1)
    c.post(f"/importer/{token}")
    with a.state.db.Session() as s:
        names = sorted(t.name for t in s.query(M.Trip).all())
        assert len(names) == 2 and names[1] == names[0] + " (importé)"
        assert S.asso_name(s) == "Mon club"                  # pas remplacé


def test_bad_files_and_export_all(tmp_path):
    a = create_app(tmp_path / "a")
    t1 = seed_demo(a.state.db)
    seed_family_demo(a.state.db)
    c = TestClient(a)
    for content in (b"pas un zip", _zip({"autre.txt": "x"}), _zip({"voyages.json": '{"format": "autre"}'})):
        r = c.post("/importer", files={"fichier": ("x.zip", content)}, follow_redirects=True)
        assert "pas un export de Trip Compta" in r.text
    r = c.post("/importer", files={"fichier": ("x.zip", _zip({"voyages.json": json.dumps(
        {"format": "tripcompta", "version": 99, "voyages": []})}))}, follow_redirects=True)
    assert "version plus récente" in r.text
    allz = zipfile.ZipFile(io.BytesIO(c.get("/tous-les-voyages.zip").content))
    assert len(json.loads(allz.read("voyages.json"))["voyages"]) == 2
    page = c.get(f"/v/{t1}/transfert").text
    assert "Télécharger le fichier du voyage" in page and "Exporter tous les voyages" in page
    assert 'href="/v/%d/transfert" class="on"' % t1 in page
    assert 'id="importer"' in c.get("/").text


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, v in files.items():
            z.writestr(n, v)
    return buf.getvalue()


def test_asso_name_is_per_trip(tmp_path):
    """Chaque voyage a son nom d'asso : renommer l'un ne touche pas les autres ; un nouveau voyage reprend le
    dernier nom utilisé ; l'export d'un voyage n'emporte que son nom, jamais celui des autres voyages."""
    a = create_app(tmp_path / "a")
    c = TestClient(a)
    with a.state.db.Session() as s:
        S.set_asso_name(s, "Club Réel")
        s.commit()
    t1 = seed_demo(a.state.db)
    with a.state.db.Session() as s:
        t = M.Trip(name="Autre voyage")
        s.add(t)
        s.commit()
        t2 = t.id
        assert s.get(M.Trip, t1).asso_name == s.get(M.Trip, t2).asso_name == "Club Réel"
    r = c.post(f"/v/{t1}/asso/nom", data={"name": "CaisseClub"}, follow_redirects=True)
    assert "appelle maintenant « CaisseClub »" in r.text
    assert "CaisseClub" in c.get(f"/v/{t1}/asso").text
    page2 = c.get(f"/v/{t2}/asso").text
    assert "Club Réel" in page2 and "CaisseClub" not in page2
    with a.state.db.Session() as s:
        assert s.get(M.Trip, t2).asso_name == "Club Réel"
        assert S.asso_name(s) == "CaisseClub"                # proposé aux prochains voyages
        t3 = M.Trip(name="Nouveau")
        s.add(t3)
        s.commit()
        assert t3.asso_name == "CaisseClub"
    z = zipfile.ZipFile(io.BytesIO(c.get(f"/v/{t1}/voyage.zip").content))
    raw = z.read("voyages.json").decode("utf-8")
    assert "Club Réel" not in raw and '"asso_name": "CaisseClub"' in raw
    # une autre installation : le voyage importé garde son nom
    b = create_app(tmp_path / "b")
    cb = TestClient(b)
    tok = re.search(r'/importer/([0-9a-f]{32})', cb.post("/importer", files={"fichier": ("v.zip", c.get(f"/v/{t1}/voyage.zip").content)}).text).group(1)
    cb.post(f"/importer/{tok}")
    with b.state.db.Session() as s:
        assert [t.asso_name for t in s.query(M.Trip).all()] == ["CaisseClub"]


def test_old_export_name_goes_to_trip(tmp_path):
    """Export d'une version 1.1 : le nom commun (« reglages ») devient celui du voyage importé, sans écraser
    le nom proposé déjà présent ici."""
    a = create_app(tmp_path / "a")
    tid = seed_demo(a.state.db)
    data = TestClient(a).get(f"/v/{tid}/voyage.zip").content
    zin = zipfile.ZipFile(io.BytesIO(data))
    j = json.loads(zin.read("voyages.json"))
    j["voyages"][0]["voyage"].pop("asso_name", None)
    j["reglages"]["nom_asso"] = "Ancien club"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zout:
        for n in zin.namelist():
            zout.writestr(n, json.dumps(j) if n == "voyages.json" else zin.read(n))
    b = create_app(tmp_path / "b")
    with b.state.db.Session() as s:
        S.set_asso_name(s, "Club d'ici")
        s.commit()
    cb = TestClient(b)
    tok = re.search(r'/importer/([0-9a-f]{32})', cb.post("/importer", files={"fichier": ("v.zip", buf.getvalue())}).text).group(1)
    cb.post(f"/importer/{tok}")
    with b.state.db.Session() as s:
        assert [t.asso_name for t in s.query(M.Trip).all()] == ["Ancien club"]
        assert S.asso_name(s) == "Club d'ici"


def test_migration_gives_each_trip_the_old_name(tmp_path):
    """Base d'avant : pas de colonne asso_name ; à l'ouverture, chaque voyage reçoit le nom commun."""
    import sqlite3
    a = create_app(tmp_path / "a")
    tid = seed_demo(a.state.db)
    path = a.state.db.path
    with a.state.db.Session() as s:
        S.set_asso_name(s, "Club Ancien")
        s.commit()
    a.state.db.engine.dispose()
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE trips DROP COLUMN asso_name")
    con.commit()
    con.close()
    b = create_app(tmp_path / "a")
    with b.state.db.Session() as s:
        assert s.get(M.Trip, tid).asso_name == "Club Ancien"
