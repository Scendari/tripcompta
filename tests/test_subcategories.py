"""Sous-catégories : regrouper par ex. péages et essence dans « Transport / Covoit » ; proposées automatiquement
pour les dépenses suivantes (et dans les autres voyages), sans les retaper."""
import json
import re

import pytest
from fastapi.testclient import TestClient

from compta import models as M
from compta import services as S
from compta.ops import Ops
from compta.web import create_app


def clean(t):
    return re.sub(r"[ \xa0]", " ", t)


@pytest.fixture()
def trip(tmp_path):
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.create_trip("x")["voyage_id"]
    ops.add_participants(tid, ["Anne", "Bob"])
    with app.state.db.Session() as s:
        P = {p.name: p.id for p in s.get(M.Trip, tid).participants}
    return app, TestClient(app), tid, P, ops


def add(c, tid, P, label, amount, sub="", new_sub=""):
    data = {"label": label, "category": "Transport / Covoit", "actual": amount, "payer": "asso", "payer_kind": "asso",
            "subcategory": "__new__" if new_sub else sub, "subcategory_new": new_sub,
            **{f"inc_{i}": "1" for i in P.values()}}
    return c.post(f"/v/{tid}/depenses", data=data, follow_redirects=True)


def test_create_once_then_offered(trip):
    app, c, tid, P, ops = trip
    add(c, tid, P, "Péage aller", "40", new_sub="Péages")
    form = c.get(f"/v/{tid}/depenses/nouvelle").text
    sub = json.loads(re.search(r"var SUB = (\{.*?\}), cat", form).group(1))
    assert sub == {"Transport / Covoit": ["Péages"]}                       # proposée sans retaper
    add(c, tid, P, "Péage retour", "38", sub="Péages")
    add(c, tid, P, "Essence", "90", new_sub="Essence")
    add(c, tid, P, "Location minibus", "600")
    deps = {e["libelle"]: e["sous_categorie"] for e in ops.trip(tid)["depenses"]}
    assert deps == {"Péage aller": "Péages", "Péage retour": "Péages", "Essence": "Essence", "Location minibus": None}
    page = clean(c.get(f"/v/{tid}/depenses").text)
    # regroupement + sous-totaux : Essence, Péages, puis « Autres (sans sous-catégorie) »
    i_ess, i_pea = page.index("↳</span> Essence"), page.index("↳</span> Péages")
    i_oth, i_loc = page.index("↳</span> Autres (sans sous-catégorie)"), page.index("Location minibus")
    assert i_ess < i_pea < i_oth < i_loc and "78,00 €" in page[i_pea:i_pea + 200]
    eid = next(e["id"] for e in ops.trip(tid)["depenses"] if e["libelle"] == "Péage aller")
    edit = c.get(f"/v/{tid}/depenses/{eid}/modifier").text
    assert 'data-current="Péages"' in edit


def test_offered_in_other_trips(trip):
    app, c, tid, P, ops = trip
    add(c, tid, P, "Péage", "40", new_sub="Péages")
    t2 = ops.create_trip("Autre")["voyage_id"]
    ops.add_participants(t2, ["Zoé"])
    form = c.get(f"/v/{t2}/depenses/nouvelle").text
    assert json.loads(re.search(r"var SUB = (\{.*?\}), cat", form).group(1)) == {"Transport / Covoit": ["Péages"]}


def test_person_account_ledger_pdf_and_ai(trip):
    pdfium = pytest.importorskip("pypdfium2")
    app, c, tid, P, ops = trip
    r = ops.import_trip({"voyage_id": tid, "depenses": [
        {"libelle": "Péage A7", "categorie": "Transport / Covoit", "sous_categorie": "Péages", "reel": "40"}]},
        str(tid), verifier_id=True)
    assert ops.trip(tid)["depenses"][0]["sous_categorie"] == "Péages"
    ops.import_trip({"voyage_id": tid, "depenses_a_modifier": [{"depense": "Péage A7", "sous_categorie": "Autoroute"}]},
                    str(tid), verifier_id=True)
    assert ops.trip(tid)["depenses"][0]["sous_categorie"] == "Autoroute"
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        acc = S.person_account(t, S.compute_trip(t), P["Anne"])
        L = S.ledger(t, S.compute_trip(t))
    assert acc["groups"][0]["items"][0]["sub"] == "Autoroute"
    assert L["expenses"][0]["category"] == "Transport / Covoit › Autoroute"
    fiche = clean(c.get(f"/v/{tid}/p/{P['Anne']}").text)
    assert 'class="item sub-row"' in fiche and "Autoroute" in fiche and "(1)" in fiche
    doc = pdfium.PdfDocument(c.get(f"/v/{tid}/export.pdf").content)
    text = " ".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))
    flat = re.sub(r"\s+", " ", text)
    assert "› Autoroute" in flat and "sous-total" in flat and "Péage A7" in flat


def test_browser_subcategory_follows_category(trip):
    import socket
    import threading
    import time
    import uvicorn
    from playwright.sync_api import sync_playwright
    app, c, tid, P, ops = trip
    add(c, tid, P, "Péage", "40", new_sub="Péages")
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    while not srv.started:
        time.sleep(0.05)
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page()
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(f"http://127.0.0.1:{port}/v/{tid}/depenses/nouvelle")
            opts = lambda: pg.locator("#subcategory option").all_inner_texts()
            assert opts() == ["— Aucune —", "+ Nouvelle sous-catégorie…"]          # pas encore de catégorie
            pg.select_option("#category", "Transport / Covoit")
            assert opts() == ["— Aucune —", "Péages", "+ Nouvelle sous-catégorie…"]
            pg.select_option("#subcategory", "Péages")
            pg.select_option("#category", "Repas")
            assert opts() == ["— Aucune —", "+ Nouvelle sous-catégorie…"] and pg.input_value("#subcategory") == ""
            pg.select_option("#subcategory", "__new__")
            assert pg.locator("#subcategory_new").is_visible()
            assert errors == []
            b.close()
    finally:
        srv.should_exit = True
        th.join(5)


def test_manage_subcategories_in_categories_screen(trip):
    app, c, tid, P, ops = trip
    with app.state.db.Session() as s:
        cid = next(x.id for x in s.get(M.Trip, tid).categories if x.name == "Transport / Covoit")
    r = c.post(f"/v/{tid}/categories/{cid}/sous-categories", data={"name": "Péages"}, follow_redirects=True)
    assert "Sous-catégorie « Péages » ajoutée à « Transport / Covoit »" in r.text
    c.post(f"/v/{tid}/categories/{cid}/sous-categories", data={"name": "Essence"})
    r = c.post(f"/v/{tid}/categories/{cid}/sous-categories", data={"name": "péages"}, follow_redirects=True)
    assert "existe déjà" in r.text
    page = c.get(f"/v/{tid}/categories").text
    block = page.split(f'id="c{cid}"')[1].split("</li>")[0]
    assert re.findall(r'class="sub-chip">([^<]+?) <button', block) == ["Péages", "Essence"]
    # proposées dans le formulaire de dépense, et dans un autre voyage
    form = c.get(f"/v/{tid}/depenses/nouvelle").text
    assert json.loads(re.search(r"var SUB = (\{.*?\}), cat", form).group(1))["Transport / Covoit"] == ["Péages", "Essence"]
    t2 = ops.create_trip("Autre")["voyage_id"]
    chips2 = lambda: re.findall(r'class="sub-chip">([^<]+?) <button', c.get(f"/v/{t2}/categories").text)
    assert chips2() == ["Péages", "Essence"]
    # supprimer une sous-catégorie utilisée : ses dépenses restent dans la catégorie, sans sous-catégorie
    add(c, tid, P, "Péage A7", "40", sub="Péages")
    r = c.post(f"/v/{tid}/categories/{cid}/sous-categories/supprimer", data={"name": "Péages"}, follow_redirects=True)
    assert "ses 1 dépense(s) restent dans « Transport / Covoit »" in r.text
    assert ops.trip(tid)["depenses"][0]["sous_categorie"] is None
    block = c.get(f"/v/{tid}/categories").text.split(f'id="c{cid}"')[1].split("</li>")[0]
    assert re.findall(r'class="sub-chip">([^<]+?) <button', block) == ["Essence"]
    assert chips2() == ["Essence"]


def test_expenses_without_subcategory_get_their_own_subtotal(trip):
    pdfium = pytest.importorskip("pypdfium2")
    app, c, tid, P, ops = trip
    add(c, tid, P, "Location minibus", "603")
    add(c, tid, P, "Péage", "40", new_sub="Péages")
    add(c, tid, P, "Essence", "90", new_sub="Essence")
    page = clean(c.get(f"/v/{tid}/depenses").text)
    rows = re.findall(r'class="subcat-row"><td colspan="7">.*?</span> ([^<]+)</td>\s*<td class="num"><b>([^<]+)</b>', page)
    assert rows == [("Essence", "90,00 €"), ("Péages", "40,00 €"), ("Autres (sans sous-catégorie)", "603,00 €")]
    # une catégorie sans aucune sous-catégorie n'a pas de « Autres »
    c.post(f"/v/{tid}/depenses", data={"label": "Pizza", "category": "Repas", "actual": "30", "payer": "asso",
                                       "payer_kind": "asso", **{f"inc_{i}": "1" for i in P.values()}})
    assert clean(c.get(f"/v/{tid}/depenses").text).count("Autres (sans sous-catégorie)") == 1
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        acc = S.person_account(t, S.compute_trip(t), P["Anne"])
    tr = next(g for g in acc["groups"] if g["name"] == "Transport / Covoit")
    assert list(tr["subs"]) == ["Essence", "Péages", "Autres (sans sous-catégorie)"]
    assert sum(v["debit"] for v in tr["subs"].values()) == tr["debit"]
    doc = pdfium.PdfDocument(c.get(f"/v/{tid}/export.pdf").content)
    flat = re.sub(r"\s+", " ", " ".join(doc[i].get_textpage().get_text_range() for i in range(len(doc))))
    assert "› Autres (sans sous-catégorie)" in flat


def test_matrix_subtotals_by_subcategory(trip):
    app, c, tid, P, ops = trip
    add(c, tid, P, "Location minibus", "600")
    add(c, tid, P, "Péage aller", "40", new_sub="Péages")
    add(c, tid, P, "Péage retour", "20", sub="Péages")
    add(c, tid, P, "Essence", "90", new_sub="Essence")
    page = clean(c.get(f"/v/{tid}/repartition?vue=detail").text)
    labels = re.findall(r'<th class="num[^"]*" title="([^"]+)">', page)
    assert labels == ["Essence", "Σ Essence", "Péage aller", "Péage retour", "Σ Péages", "Location minibus",
                      "Σ Autres (sans sous-catégorie)", "Total Transport / Covoit"]
    assert page.count('cat-start" title=') == 1                    # un seul trait : début de la catégorie
    row = page.split(">Anne</a>")[1].split("</tr>")[0]
    cells = [x.strip() for x in re.findall(r'<td class="num (?:cell[^"]*|tot-(?:sub|cat)[^"]*)">\s*(?:<a[^>]*>)?([^<]+)', row)]
    # Anne (1 sur 2) : Essence 45 | Σ 45 | 20 | 10 | Σ Péages 30 | Location 300 | Σ Autres 300 | Total 375
    assert cells == ["45,00 €", "45,00 €", "20,00 €", "10,00 €", "30,00 €", "300,00 €", "300,00 €", "375,00 €"]
    # par défaut : sous-totaux seulement, avec « ▸ » pour déplier la catégorie
    compact = clean(c.get(f"/v/{tid}/repartition").text)
    labels = re.findall(r'<th class="num[^"]*" title="([^"]+)">', compact)
    assert labels == ["Σ Essence", "Σ Péages", "Σ Autres (sans sous-catégorie)", "Total Transport / Covoit"]
    assert 'class="on">Sous-totaux' in compact
    cid = int(re.search(r'class="cat-toggle" href="\?o=(\d+)"', compact).group(1))
    assert "▸ Transport / Covoit" in compact
    one = clean(c.get(f"/v/{tid}/repartition?o={cid}").text)       # seulement cette catégorie dépliée
    assert re.findall(r'<th class="num[^"]*" title="([^"]+)">', one) == re.findall(r'<th class="num[^"]*" title="([^"]+)">', page)
    assert "▾ Transport / Covoit" in one and 'class="cat-toggle" href="?"' in one   # le même clic replie
    c.post(f"/v/{tid}/depenses", data={"label": "Gîte", "category": "Logement", "actual": "100", "payer": "asso",
                                       "payer_kind": "asso", **{f"inc_{i}": "1" for i in P.values()}})
    assert ">None<" not in c.get(f"/v/{tid}/repartition").text


def test_expense_date_has_its_own_column(tmp_path):
    """La date d'une dépense est dans sa colonne « Date », pas sous le libellé (pas de ligne en plus)."""
    from compta.web import create_app
    from compta.ops import Ops
    from fastapi.testclient import TestClient
    app = create_app(tmp_path / "d")
    tid = Ops(app.state.db).import_trip({"voyage": {"nom": "x"}, "participants": ["A", "B"], "depenses": [
        {"libelle": "Péage", "reel": "10", "date": "2026-10-25", "paye_par": "asso", "pour": "tous"}]})["voyage_id"]
    page = TestClient(app).get(f"/v/{tid}/depenses").text
    assert '<th class="date-col"><span class="sr-only">Date</span></th>' in page
    assert re.search(r'<td><a [^>]+><b>Péage</b></a></td>\s*<td class="date-col small">25/10/2026</td>', page)


def test_matrix_single_subcategory_no_duplicate_total(trip):
    """Une catégorie avec une seule sous-catégorie : pas de « Σ X » identique au « Total »."""
    app, c, tid, P, ops = trip
    add(c, tid, P, "Essence aller", "40", new_sub="Essence")
    add(c, tid, P, "Essence retour", "20", sub="Essence")
    compact = clean(c.get(f"/v/{tid}/repartition").text)
    assert re.findall(r'<th class="num[^"]*" title="([^"]+)">', compact) == ["Total Transport / Covoit"]
    detail = clean(c.get(f"/v/{tid}/repartition?vue=detail").text)
    assert re.findall(r'<th class="num[^"]*" title="([^"]+)">', detail) == ["Essence aller", "Essence retour",
                                                                            "Total Transport / Covoit"]


def test_matrix_dotted_line_between_subcategories(trip):
    """Trait plein au début d'une catégorie, pointillé au début de chaque sous-catégorie et du total."""
    app, c, tid, P, ops = trip
    add(c, tid, P, "Péage aller", "40", new_sub="Péages")
    add(c, tid, P, "Péage retour", "20", sub="Péages")
    add(c, tid, P, "Essence", "90", new_sub="Essence")
    page = clean(c.get(f"/v/{tid}/repartition?vue=detail").text)
    heads = re.findall(r'<th class="num[^"]*?( cat-start)?( sub-start)?" title="([^"]+)">', page)
    marks = {h[2]: ("cat" if h[0] else "sub" if h[1] else "") for h in heads}
    assert marks == {"Essence": "cat", "Σ Essence": "", "Péage aller": "sub", "Péage retour": "",
                     "Σ Péages": "", "Total Transport / Covoit": "sub"}
