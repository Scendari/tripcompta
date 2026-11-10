"""Export tableur (.xlsx) : dépenses, argent reçu et rendu, qui paie quoi.

Voyage de test (montants vérifiés à la main) : Anne, Bruno, Chloé ; la caisse est tenue par l'asso.
  Gîte 300 € (prévu 320 €) payé par l'asso, pour tous            → 100 € chacun
  Essence 90 € avancée par Anne, pour Anne et Bruno               → 45 € chacun
  Bruno a versé 50 € le 02/03/2027 ; Chloé a versé 120 € puis l'asso lui a rendu 20 €.
  Parts : Anne 145, Bruno 145, Chloé 100 (total 390 = 300 + 90).
  Déjà réglé : Anne 90 (son avance), Bruno 50, Chloé 100 (120 − 20).
  Restes : Anne 55, Bruno 95, Chloé 0 (total 150).
"""
import datetime as dt
import io
import re
import zipfile
from xml.etree import ElementTree as ET

from fastapi.testclient import TestClient

from compta import tableur as T
from compta.ops import Ops
from compta.web import create_app

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def read_xlsx(data: bytes) -> dict:
    """Petit lecteur indépendant de l'écriture : {nom de feuille: lignes}, cellules = texte, nombre (float), date."""
    z = zipfile.ZipFile(io.BytesIO(data))
    assert z.testzip() is None
    for name in z.namelist():
        if name.endswith(".xml") or name.endswith(".rels"):
            ET.fromstring(z.read(name))                       # chaque partie est du XML bien formé
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    styles = ET.fromstring(z.read("xl/styles.xml"))
    fmts = [int(x.get("numFmtId")) for x in styles.find("m:cellXfs", NS)]
    out = {}
    for sh in wb.find("m:sheets", NS):
        root = ET.fromstring(z.read("xl/" + rels[sh.get(REL)]))
        rows = []
        for row in root.find("m:sheetData", NS):
            cells = {}
            for c in row:
                col = re.match(r"[A-Z]+", c.get("r")).group(0)
                idx = 0
                for ch in col:
                    idx = idx * 26 + ord(ch) - 64
                if c.get("t") == "inlineStr":
                    v = "".join(t.text or "" for t in c.iter("{%s}t" % NS["m"]))
                else:
                    raw = c.find("m:v", NS)
                    if raw is None:
                        continue
                    v = float(raw.text)
                    if fmts[int(c.get("s", 0))] == 14:
                        v = dt.date(1899, 12, 30) + dt.timedelta(days=int(v))
                cells[idx - 1] = v
            rows.append([cells.get(i) for i in range(max(cells) + 1)] if cells else [])
        out[sh.get("name")] = dict(rows=rows, pane=root.find("m:sheetViews/m:sheetView/m:pane", NS))
    return out


def seed(app):
    ops = Ops(app.state.db)
    tid = ops.create_trip("Sortie test")["voyage_id"]
    ops.add_participants(tid, ["Anne", "Bruno", "Chloé"])
    ops.add_expense(tid, "Gîte", categorie="Logement", montant_prevu="320", montant_reel="300", date="2027-03-20")
    ops.add_expense(tid, "Essence", categorie="Transport", montant_reel="90", paye_par="Anne", pour=["Anne", "Bruno"],
                    date="2027-03-21")
    return tid


def workbook(tmp_path):
    app = create_app(tmp_path / "voyages")
    cl = TestClient(app)
    tid = seed(app)
    cl.post(f"/v/{tid}/argent", data={"participant": pid(app, "Bruno"), "kind": "versement", "amount": "50",
                                       "date": "2027-03-02", "note": "Acompte"}, follow_redirects=False)
    cl.post(f"/v/{tid}/argent", data={"participant": pid(app, "Chloé"), "kind": "versement", "amount": "120",
                                       "date": "2027-03-05"}, follow_redirects=False)
    cl.post(f"/v/{tid}/argent", data={"participant": pid(app, "Chloé"), "kind": "remboursement", "amount": "20",
                                       "date": "2027-04-01", "note": "Trop versé"}, follow_redirects=False)
    r = cl.get(f"/v/{tid}/export.xlsx")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert r.headers["content-disposition"] == "attachment; filename*=UTF-8''Tableur_Sortie_test.xlsx"
    return app, cl, tid, read_xlsx(r.content)


def pid(app, name):
    from compta import models as M
    with app.state.db.Session() as s:
        return str(s.query(M.Participant).filter_by(name=name).one().id)


def test_three_sheets_with_plain_names(tmp_path):
    app, cl, tid, wb = workbook(tmp_path)
    assert list(wb) == ["Dépenses", "Argent reçu et rendu", "Qui paie quoi"]
    assert wb["Dépenses"]["pane"].get("ySplit") == "1"                       # titres figés
    pane = wb["Qui paie quoi"]["pane"]
    assert (pane.get("xSplit"), pane.get("ySplit"), pane.get("topLeftCell")) == ("1", "2", "B3")


def test_expenses_sheet_amounts(tmp_path):
    app, cl, tid, wb = workbook(tmp_path)
    rows = wb["Dépenses"]["rows"]
    assert rows[0] == ["Catégorie", "Sous-catégorie", "Dépense", "Date", "Montant prévu", "Montant payé",
                       "Compté dans les comptes", "Payé par", "Partagé entre (personnes)", "Justificatifs", "Note"]
    gite = next(r for r in rows if len(r) > 2 and r[2] == "Gîte")
    assert gite[0] == "Logement" and gite[3] == dt.date(2027, 3, 20)
    assert gite[4:9] == [320.0, 300.0, 300.0, "l'asso", 3.0]                # de vrais nombres, une vraie date
    fuel = next(r for r in rows if len(r) > 2 and r[2] == "Essence")
    assert fuel[4:9] == [None, 90.0, 90.0, "Anne", 2.0]
    total = rows[-1]
    assert total[0] == "Total" and total[4:7] == [320.0, 390.0, 390.0]


def test_money_sheet_amounts(tmp_path):
    app, cl, tid, wb = workbook(tmp_path)
    rows = wb["Argent reçu et rendu"]["rows"]
    assert rows[0] == ["Date", "Personne", "Reçu par l'asso", "Rendu par l'asso", "Note"]
    assert rows[1] == [dt.date(2027, 3, 2), "Bruno", 50.0, None, "Acompte"]
    assert rows[2] == [dt.date(2027, 3, 5), "Chloé", 120.0]
    assert rows[3] == [dt.date(2027, 4, 1), "Chloé", None, 20.0, "Trop versé"]
    assert rows[4] == ["Total", None, 170.0, 20.0]
    assert rows[5] == ["Reçu moins rendu", None, 150.0]


def test_who_pays_what_sheet_amounts(tmp_path):
    app, cl, tid, wb = workbook(tmp_path)
    rows = wb["Qui paie quoi"]["rows"]
    assert rows[0][:3] == [None, "Logement", "Transport / Covoit"] or rows[0][1] == "Logement"
    assert rows[1] == ["Participant", "Gîte", "Essence", "Part totale", "Déjà réglé",
                       "Reste à régler (négatif = à lui rendre)"]
    assert rows[2] == ["Anne", 100.0, 45.0, 145.0, 90.0, 55.0]
    assert rows[3] == ["Bruno", 100.0, 45.0, 145.0, 50.0, 95.0]
    assert rows[4] == ["Chloé", 100.0, None, 100.0, 100.0, 0.0]            # pas concernée par l'essence : case vide
    assert rows[5] == ["Total des participants", 300.0, 90.0, 390.0, 240.0, 150.0]
    assert rows[6] == ["Montant de la dépense", 300.0, 90.0, 390.0]


def test_columns_add_up_on_the_example_trip(tmp_path):
    """Sur le voyage d'exemple (remise, désistement, prise en charge par l'asso) : chaque colonne retombe juste."""
    from compta.demo import seed_demo
    from compta import models as M
    from compta import services as S
    app = create_app(tmp_path / "voyages")
    tid = seed_demo(app.state.db)
    wb = read_xlsx(TestClient(app).get(f"/v/{tid}/export.xlsx").content)
    rows = wb["Qui paie quoi"]["rows"]
    head = rows[1]
    people = rows[2:rows.index(next(r for r in rows if r[0] == "Total des participants"))]
    total = next(r for r in rows if r[0] == "Total des participants")
    caisse = next(r for r in rows if str(r[0]).startswith("Pris en charge par"))
    amount = next(r for r in rows if r[0] == "Montant de la dépense")
    cents = lambda v: round((v or 0) * 100)
    n_exp = head.index("Remise ou supplément") - 1
    for c in range(1, len(head)):
        assert sum(cents(r[c]) for r in people if c < len(r)) == cents(total[c]), head[c]
    for c in range(1, n_exp + 1):                       # participants + prise en charge = montant de la dépense
        assert cents(total[c]) + cents(caisse[c] if c < len(caisse) else 0) == cents(amount[c]), head[c]
    i_share, i_paid, i_left = (head.index(x) for x in ("Part totale", "Déjà réglé",
                                                        "Reste à régler (négatif = à lui rendre)"))
    with app.state.db.Session() as s:
        r = S.compute_trip(s.get(M.Trip, tid))
        by_name = {ps.name + (" (désisté)" if ps.withdrawn else ""): ps for ps in r.people.values()}
        for row in people:
            ps = by_name[row[0]]
            assert cents(row[i_share]) == ps.share and cents(row[i_left]) == ps.balance
            assert cents(row[i_share]) - cents(row[i_paid]) == cents(row[i_left])
            assert sum(cents(v) for v in row[1:i_share]) == ps.share        # la ligne retombe sur la part
        assert cents(amount[i_share]) == r.total_cost
    exp = wb["Dépenses"]["rows"]
    assert cents(exp[-1][exp[0].index("Compté dans les comptes")]) == r.total_cost


def test_special_characters_and_empty_trip(tmp_path):
    app = create_app(tmp_path / "voyages")
    ops = Ops(app.state.db)
    tid = ops.create_trip("Été & <mer> \"2027\" / test")["voyage_id"]
    cl = TestClient(app)
    wb = read_xlsx(cl.get(f"/v/{tid}/export.xlsx").content)               # voyage vide : classeur valide
    assert wb["Dépenses"]["rows"][-1][0] == "Total" and wb["Qui paie quoi"]["rows"][1][0] == "Participant"
    ops.add_participants(tid, ["Zoé <b>&amp;</b> O'Neil"])
    ops.add_expense(tid, "Péage \"A7\" & <retour>\x0b", categorie="Transport", montant_reel="12,34")
    r = cl.get(f"/v/{tid}/export.xlsx")
    wb = read_xlsx(r.content)
    assert wb["Qui paie quoi"]["rows"][2][0] == "Zoé <b>&amp;</b> O'Neil"
    assert wb["Qui paie quoi"]["rows"][1][1] == "Péage \"A7\" & <retour>"   # caractère de contrôle retiré
    assert wb["Qui paie quoi"]["rows"][2][1] == 12.34
    assert "Tableur_Ete_mer_2027_test.xlsx" in r.headers["content-disposition"]


def test_helpers():
    assert [T.col_letter(i) for i in (0, 25, 26, 27, 701, 702)] == ["A", "Z", "AA", "AB", "ZZ", "AAA"]
    assert [T.euros(c) for c in (0, 5, 1234, -5, -123456, 100000001)] == ["0.00", "0.05", "12.34", "-0.05",
                                                                          "-1234.56", "1000000.01"]
    used = set()
    assert T._sheet_name("Qui paie quoi ? [détail] / tout : le très long nom de feuille", used) == \
        "Qui paie quoi - -détail- - tout"
    assert T._sheet_name("Dépenses", used) == "Dépenses" and T._sheet_name("dépenses", used) == "dépenses 2"


def test_export_page_offers_the_spreadsheet(tmp_path):
    app = create_app(tmp_path / "voyages")
    tid = seed(app)
    cl = TestClient(app)
    page = cl.get(f"/v/{tid}/export").text
    assert f'href="/v/{tid}/export.xlsx"' in page and "Télécharger le tableur" in page
    assert "trois feuilles" in page
    assert f'href="/v/{tid}/export.xlsx"' in cl.get(f"/v/{tid}/repartition").text
    assert cl.get("/v/999/export.xlsx").status_code == 404
