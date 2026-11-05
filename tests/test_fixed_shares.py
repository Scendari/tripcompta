"""Montants fixés 🔒 dans la dépense, remise sur la fiche, conversion des anciennes données.
Montants vérifiés à la main."""
import sqlite3

from fastapi.testclient import TestClient

from compta import models as M, services as S
from compta.db import Database
from compta.ops import Ops
from compta.web import create_app


def _trip(path):
    ops = Ops(Database(path))
    tid = ops.create_trip("x")["voyage_id"]
    ops.add_participants(tid, ["A", "B", "C", "D"])
    return ops, tid


def _shares(path, tid):
    with Database(path).Session() as s:
        t = s.get(M.Trip, tid)
        r = S.compute_trip(t)
        return {p.name: p.share for p in r.people.values()}, r


def test_old_data_converted_once_with_identical_shares(tmp_path):
    """Avant : un ajustement « fixé à 60 € sur le Gîte, compensé par les autres » et une dépense en
    « montants différents » (poids = centimes). Après conversion : mêmes parts au centime près."""
    d = tmp_path / "d"
    ops, tid = _trip(d)
    gite = ops.add_expense(tid, "Gîte", montant_reel="400")["depense_id"]
    plong = ops.add_expense(tid, "Plongées", montant_reel="1196")["depense_id"]
    db = d / "compta.db"
    con = sqlite3.connect(db)
    ids = {n: i for i, n in con.execute("SELECT id, name FROM participants")}
    con.execute("INSERT INTO adjustments (trip_id, participant_id, expense_id, value, fixed, compensation, reason, "
                "created) VALUES (?, ?, ?, 6000, 1, 'others', 'enfant', '2026-01-01')", (tid, ids["A"], gite))
    for n, w in (("A", 40000), ("B", 40000), ("C", 39600), ("D", 0)):
        if w:
            con.execute("UPDATE expense_shares SET weight=? WHERE expense_id=? AND participant_id=?", (w, plong, ids[n]))
        else:
            con.execute("DELETE FROM expense_shares WHERE expense_id=? AND participant_id=?", (plong, ids[n]))
    con.execute("DELETE FROM settings WHERE key='fixed_shares'")      # comme une base d'avant
    con.commit()
    con.close()
    # Gîte : A 60, les 3 autres (400 − 60) / 3 = 113,333… ; Plongées : 400 / 400 / 396
    # A : 60 + 400 = 460 ; B, C, D : 113,33 chacun sur le gîte, + 1 centime pour l'un d'eux (tiré au sort)
    shares, r = _shares(d, tid)                        # ouvrir la base lance la conversion
    assert shares["A"] == 46000 and r.is_balanced()
    assert sorted([shares["B"] - 40000, shares["C"] - 39600, shares["D"]]) == [11333, 11333, 11334]
    assert sum(shares.values()) == 40000 + 119600       # chaque dépense tombe juste
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM adjustments").fetchone()[0] == 0
    fx = dict(con.execute("SELECT participant_id, fixed FROM expense_shares WHERE expense_id=? AND fixed IS NOT NULL",
                          (gite,)).fetchall())
    assert fx == {ids["A"]: 6000}
    pl = dict(con.execute("SELECT participant_id, fixed FROM expense_shares WHERE expense_id=?", (plong,)).fetchall())
    assert pl == {ids["A"]: 40000, ids["B"]: 40000, ids["C"]: 39600}
    assert {w for (w,) in con.execute("SELECT weight FROM expense_shares WHERE expense_id=?", (plong,))} == {1.0}
    con.close()
    # une seule fois : un nouvel ajustement de ce type n'est plus converti ensuite
    assert _shares(d, tid)[0] == shares


def test_ia_parts_become_fixed_amounts(tmp_path):
    """Plus de « parts » : des parts (2, 1, 1) données par l'IA : A paie double, 60 € fixés 🔒 ; B et C (une part)
    restent libres et se partagent le reste, 30 € chacun."""
    d = tmp_path / "d"
    ops, tid = _trip(d)
    e = ops.add_expense(tid, "Nuits", montant_reel="120", parts={"A": 2, "B": 1, "C": 1})["depense_id"]
    con = sqlite3.connect(d / "compta.db")
    con.execute("DELETE FROM settings WHERE key='fixed_shares'")
    con.commit()
    con.close()
    shares, _ = _shares(d, tid)
    assert shares == {"A": 6000, "B": 3000, "C": 3000, "D": 0}
    con = sqlite3.connect(d / "compta.db")
    assert sorted(f or 0 for (f,) in con.execute("SELECT fixed FROM expense_shares WHERE expense_id=?", (e,))) == [0, 0, 6000]
    assert {w for (w,) in con.execute("SELECT weight FROM expense_shares WHERE expense_id=?", (e,))} == {1.0}


def test_old_adjust_page_redirects(tmp_path):
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.create_trip("x")["voyage_id"]
    ops.add_participants(tid, ["A", "B"])
    eid = ops.add_expense(tid, "Gîte", montant_reel="100")["depense_id"]
    pid = ops.trip(tid)["participants"][0]["id"]
    c = TestClient(app)
    for url, to in ((f"/v/{tid}/ajuster", f"/v/{tid}/participants"),
                    (f"/v/{tid}/ajuster?p={pid}&e={eid}", f"/v/{tid}/depenses/{eid}/modifier#who"),
                    (f"/v/{tid}/ajuster?p={pid}", f"/v/{tid}/participants/remises?remise={pid}")):
        r = c.get(url, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == to, url
    page = c.get(f"/v/{tid}/participants").text
    assert "Ajuster une part" not in page and "/ajuster\"" not in page
    # « Remise ou supplément » : onglet dédié, formulaire ouvert, la personne déjà choisie
    opened = c.get(f"/v/{tid}/participants/remises?remise={pid}").text
    assert 'id="remise-form" class="remise-form">' in opened and f'<option value="{pid}" selected>' in opened
    assert 'aria-current=page>Remises / suppléments' in opened
    # aucune remise : le formulaire est déjà ouvert ; l'ancien lien vers Comptes redirige ici
    assert 'id="remise-form" class="remise-form">' in c.get(f"/v/{tid}/participants/remises").text
    r = c.get(f"/v/{tid}/participants/soldes?remise={pid}", follow_redirects=False)
    assert r.headers["location"] == f"/v/{tid}/participants/remises?remise={pid}"
    assert "remise_" not in c.get(f"/v/{tid}/participants").text            # plus rien sur la fiche


def test_remise_in_accounts_tab(tmp_path):
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.create_trip("x")["voyage_id"]
    ops.add_participants(tid, ["A", "B", "C"])
    ops.add_expense(tid, "Gîte", montant_reel="300")
    P = {p["nom"]: p["id"] for p in ops.trip(tid)["participants"]}
    c = TestClient(app)
    norm = lambda t: t.replace("\u202f", " ").replace("\xa0", " ")
    r = c.post(f"/v/{tid}/remises", data={"participant": P["A"], "kind": "reduce", "value": "30",
                                          "reason": "encadrant", "by": "others"}, follow_redirects=True)
    assert "Remise de 30,00 € pour A enregistrée" in norm(r.text)
    # A : 100 − 30 = 70 ; B et C compensent : 115 chacun
    assert {p["nom"]: p["sa_part"] for p in ops.trip(tid)["participants"]} == {"A": 70.0, "B": 115.0, "C": 115.0}
    page = norm(c.get(f"/v/{tid}/participants/soldes").text)
    assert f'class="pill grey rtag" href="/v/{tid}/participants/remises"' in page and "−30,00 €" in page  # étiquette
    page = norm(c.get(f"/v/{tid}/participants/remises").text)
    assert ">Remises / suppléments <span class=\"n\">1</span>" in page                        # compteur dans l'onglet
    assert 'id="remise-form" class="remise-form" hidden>' in page                                # liste d'abord
    assert "Remise <b>30,00 €</b>" in page and "encadrant" in page and "compensée par les autres" in page
    with app.state.db.Session() as s:
        aid = s.get(M.Trip, tid).adjustments[0].id
    # modifier : pré-rempli, puis remplacé (pas de doublon) → supplément de 15 € payé en moins par les autres
    form = c.get(f"/v/{tid}/participants/remises?rm={aid}").text
    assert f'name="aid" value="{aid}"' in form and 'name="value" value="30"' in form and 'value="encadrant"' in form
    assert 'value="others" checked' in form and "Enregistrer la modification" in form
    c.post(f"/v/{tid}/remises", data={"aid": aid, "participant": P["A"], "kind": "increase", "value": "15",
                                      "by": "others"})
    assert {p["nom"]: p["sa_part"] for p in ops.trip(tid)["participants"]} == {"A": 115.0, "B": 92.5, "C": 92.5}
    with app.state.db.Session() as s:
        assert len(s.get(M.Trip, tid).adjustments) == 1
    # erreurs claires
    assert "Choisis la personne" in c.post(f"/v/{tid}/remises", data={"value": "5"}, follow_redirects=True).text
    assert "Indique un montant" in c.post(f"/v/{tid}/remises", data={"participant": P["B"], "value": ""},
                                          follow_redirects=True).text
    # supprimer
    c.post(f"/v/{tid}/remises/{aid}/supprimer")
    assert {p["nom"]: p["sa_part"] for p in ops.trip(tid)["participants"]} == {"A": 100.0, "B": 100.0, "C": 100.0}
    # le bouton du « Détail des comptes » ouvre ce formulaire
    assert f'href="/v/{tid}/participants/remises?remise={P["A"]}">Remise ou supplément' in \
        c.get(f"/v/{tid}/participants/comptes").text


def test_amount_equal_to_the_equal_share_has_no_lock(tmp_path):
    """Des montants donnés par l'IA égaux à la part égale (30, 30, 30 sur 90 €) : pas de cadenas ; 400, 400, 396 :
    400 (le plus courant) est la part normale, 396 est fixé 🔒."""
    d = tmp_path / "d"
    ops, tid = _trip(d)
    e = ops.add_expense(tid, "Repas", montant_reel="90", parts={"A": 30, "B": 30, "C": 30})["depense_id"]
    e2 = ops.add_expense(tid, "Plongées", montant_reel="1196", parts={"A": 400, "B": 400, "C": 396})["depense_id"]
    con = sqlite3.connect(d / "compta.db")
    assert [f for (f,) in con.execute("SELECT fixed FROM expense_shares WHERE expense_id=?", (e,))] == [None] * 3
    assert sorted(f or 0 for (f,) in con.execute("SELECT fixed FROM expense_shares WHERE expense_id=?", (e2,))) == \
        [0, 0, 39600]
    con.close()


def test_old_parts_shown_as_fixed_and_converted_on_save(tmp_path):
    """Une dépense d'avant avec des parts (Antoine compte double) : rien ne change tant qu'on n'y touche pas ; le
    formulaire montre 11,00 € 🔒 ; l'enregistrer la convertit (mêmes montants)."""
    from fastapi.testclient import TestClient
    from compta.web import create_app
    import re
    d = tmp_path / "d"
    app = create_app(d)
    ops = Ops(app.state.db)
    tid = ops.import_trip({"voyage": {"nom": "T"}, "participants": [f"P{i}" for i in range(11)]})["voyage_id"]
    e = ops.add_expense(tid, "Pique-nique", montant_reel="66")["depense_id"]
    con = sqlite3.connect(d / "compta.db")                        # « d'avant » : une double part en base
    pid0 = con.execute("SELECT participant_id FROM expense_shares WHERE expense_id=? ORDER BY participant_id",
                       (e,)).fetchone()[0]
    con.execute("UPDATE expense_shares SET weight=2 WHERE expense_id=? AND participant_id=?", (e, pid0))
    con.commit()
    con.close()
    c = TestClient(app)
    form = c.get(f"/v/{tid}/depenses/{e}/modifier").text
    assert f'name="fx_{pid0}" value="1"' in form and f'name="m_{pid0}" value="11"' in form
    assert f'name="w_{pid0}" value="1.0"' in form
    data = {"label": "Pique-nique", "actual": "66", f"fx_{pid0}": "1", f"m_{pid0}": "11"}
    data.update({k: v for k, v in re.findall(r'name="(inc_\d+)" value="(1)" checked', form)})
    c.post(f"/v/{tid}/depenses/{e}", data=data)
    con = sqlite3.connect(d / "compta.db")
    rows = dict(con.execute("SELECT participant_id, fixed FROM expense_shares WHERE expense_id=?", (e,)).fetchall())
    assert rows[pid0] == 1100 and sum(1 for v in rows.values() if v is None) == 10
    assert {w for (w,) in con.execute("SELECT weight FROM expense_shares WHERE expense_id=?", (e,))} == {1.0}
    con.close()
