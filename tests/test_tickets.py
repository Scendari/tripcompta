"""Plusieurs tickets sur une même dépense (ex. deux pleins d'essence) : le total se calcule tout seul."""
import datetime as dt
import io
import re

import pypdfium2 as pdfium
import pytest
from fastapi.testclient import TestClient

from compta import models as M
from compta.ops import Ops, OpsError
from compta.web import create_app


@pytest.fixture
def env(tmp_path):
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.import_trip({"voyage": {"nom": "x"}, "participants": ["Anne", "Bob"]})["voyage_id"]
    return app, ops, tid, TestClient(app)


def _exp(app, tid, label):
    with app.state.db.Session() as s:
        e = next(e for e in s.get(M.Trip, tid).expenses if e.label == label)
        return e.actual, [(x["cents"], x["date"]) for x in e.ticket_list], e.date


def test_form_tickets_rows(env):
    app, ops, tid, c = env
    p = {x["nom"]: x["id"] for x in ops.trip(tid)["participants"]}
    r = c.post(f"/v/{tid}/depenses", data={"label": "Essence", "actual": "", "payer": "asso", "split": "egal",
                                           "ticket_amount": ["80,12", "69,10", ""],
                                           "ticket_date": ["2026-10-25", "2026-10-31", ""],
                                           **{f"inc_{i}": "1" for i in p.values()}, "date": ""})
    assert r.status_code in (200, 303)
    actual, tickets, date = _exp(app, tid, "Essence")
    assert actual == 8012 + 6910 == 14922
    assert tickets == [(8012, dt.date(2026, 10, 25)), (6910, dt.date(2026, 10, 31))]
    assert date == dt.date(2026, 10, 25)            # pas de date : celle du premier ticket
    page = c.get(f"/v/{tid}/depenses").text
    assert "2 tickets</span>" in page and "Ticket 1 : 80,12" in page.replace(" ", " ").replace("\xa0", " ")
    # le formulaire de modification réaffiche les tickets
    eid = next(d["id"] for d in ops.trip(tid)["depenses"] if d["libelle"] == "Essence")
    form = c.get(f"/v/{tid}/depenses/{eid}/modifier").text
    assert 'value="80,12"' in form and 'value="69,10"' in form and 'value="2026-10-31"' in form


def test_form_plus_expression(env):
    app, ops, tid, c = env
    p = {x["nom"]: x["id"] for x in ops.trip(tid)["participants"]}
    c.post(f"/v/{tid}/depenses", data={"label": "Péages", "actual": "12,40 + 8,60+ 4", "payer": "asso", "split": "egal",
                                       **{f"inc_{i}": "1" for i in p.values()}})
    actual, tickets, _ = _exp(app, tid, "Péages")
    assert actual == 2500 and [c_ for c_, _ in tickets] == [1240, 860, 400]


def test_single_ticket_is_a_plain_amount(env):
    app, ops, tid, c = env
    p = {x["nom"]: x["id"] for x in ops.trip(tid)["participants"]}
    c.post(f"/v/{tid}/depenses", data={"label": "Pain", "actual": "", "payer": "asso", "split": "egal",
                                       "ticket_amount": ["5,20", ""], "ticket_date": ["", ""],
                                       **{f"inc_{i}": "1" for i in p.values()}})
    assert _exp(app, tid, "Pain")[:2] == (520, [])


def test_ai_tickets_and_shares(env):
    app, ops, tid, c = env
    ops.import_trip({"voyage_id": tid, "depenses": [
        {"libelle": "Essence", "reel": [{"montant": "80,12", "date": "2026-10-25"}, "69,10"], "paye_par": "Anne",
         "pour": "tous"}]}, voyage=tid, verifier_id=tid)
    d = next(x for x in ops.trip(tid)["depenses"] if x["libelle"] == "Essence")
    assert d["reel"] == 149.22 and d["tickets"] == [{"montant": 80.12, "date": "2026-10-25"},
                                                     {"montant": 69.1, "date": None}]
    p = {x["nom"]: x for x in ops.trip(tid)["participants"]}
    assert p["Anne"]["sa_part"] == 74.61 and p["Bob"]["sa_part"] == 74.61
    # ajustement : un 3e ticket, puis retour à un montant simple
    r = ops.import_trip({"voyage_id": tid, "depenses_a_modifier": [
        {"depense": "Essence", "reel": ["80,12", "69,10", "10"]}]}, voyage=tid, verifier_id=tid)
    assert any("tickets" in a for a in r["actions"])
    assert next(x for x in ops.trip(tid)["depenses"] if x["libelle"] == "Essence")["reel"] == 159.22
    ops.import_trip({"voyage_id": tid, "depenses_a_modifier": [{"depense": "Essence", "reel": "150"}]},
                    voyage=tid, verifier_id=tid)
    d = next(x for x in ops.trip(tid)["depenses"] if x["libelle"] == "Essence")
    assert d["reel"] == 150.0 and not d.get("tickets")
    from compta.importer import ImportErrors
    with pytest.raises(ImportErrors, match="ticket 2"):
        ops.import_trip({"voyage_id": tid, "depenses": [{"libelle": "X", "reel": ["5", "-3"], "pour": "tous"}]},
                        voyage=tid, verifier_id=tid)


def test_pdf_lists_tickets(env):
    app, ops, tid, c = env
    ops.import_trip({"voyage_id": tid, "depenses": [
        {"libelle": "Essence", "categorie": "Transport / Covoit", "reel": ["80,12", "69,10"], "pour": "tous"}]},
        voyage=tid, verifier_id=tid)
    doc = pdfium.PdfDocument(io.BytesIO(c.get(f"/v/{tid}/export.pdf").content))
    text = " ".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))
    text = re.sub(r"\s+", " ", text.replace(" ", " ").replace("\xa0", " "))
    assert "tickets : 80,12 € + 69,10 €" in text


def test_browser_tickets(env, tmp_path):
    """Dans le navigateur : « + Plusieurs tickets ? », deux tickets, total affiché et recopié dans le montant réel ;
    « 12 + 8 » tapé dans le montant ouvre les tickets ; enregistrement ; pas de défilement horizontal sur mobile."""
    import socket
    import threading
    import time
    import uvicorn
    from playwright.sync_api import sync_playwright
    app, ops, tid, c = env
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
            pg = b.new_page(viewport={"width": 1280, "height": 900})
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(f"http://127.0.0.1:{port}/v/{tid}/depenses/nouvelle")
            pg.fill("#label", "Essence")
            pg.fill("#actual", "80,12")
            pg.click("#tickets-open")
            assert pg.is_visible("#tickets")
            rows = pg.locator(".ticket-row [name=ticket_amount]")
            assert rows.nth(0).input_value() == "80,12"
            rows.nth(1).fill("69,10")
            assert pg.input_value("#actual") == "149,22" and pg.locator("#actual").get_attribute("readonly") is not None
            assert "149,22" in pg.inner_text("#tickets-total")
            pg.click("#ticket-add")
            assert pg.locator(".ticket-row").count() == 3
            pg.click("form button.btn:has-text('Enregistrer')")
            assert "enregistrée" in pg.locator(".flash").inner_text()
            assert _exp(app, tid, "Essence")[0] == 14922
            # « 12 + 8 » dans le montant : les tickets s'ouvrent tout seuls
            pg.goto(f"http://127.0.0.1:{port}/v/{tid}/depenses/nouvelle")
            pg.fill("#label", "Péages")
            pg.fill("#actual", "12 + 8")
            pg.locator("#actual").blur()
            assert pg.is_visible("#tickets") and pg.input_value("#actual") == "20,00"
            pg.click("#tickets-close")
            assert not pg.is_visible("#tickets") and pg.locator("#actual").get_attribute("readonly") is None
            m = b.new_page(viewport={"width": 390, "height": 800})
            m.goto(f"http://127.0.0.1:{port}/v/{tid}/depenses/nouvelle")
            m.click("#tickets-open")
            assert m.evaluate("document.documentElement.scrollWidth") <= 391
            assert errors == []
            b.close()
    finally:
        srv.should_exit = True
        th.join(5)


def test_editing_undated_expense_keeps_no_date(env):
    app, ops, tid, c = env
    ops.import_trip({"voyage_id": tid, "depenses": [{"libelle": "Pain", "reel": "5", "pour": "tous"}]},
                    voyage=tid, verifier_id=tid)
    eid = next(d["id"] for d in ops.trip(tid)["depenses"] if d["libelle"] == "Pain")
    assert 'name="date" value=""' in c.get(f"/v/{tid}/depenses/{eid}/modifier").text


def test_one_photo_for_several_expenses(env):
    """Une photo de plusieurs tickets : importée sur une dépense, puis choisie (vignette) sur une autre."""
    app, ops, tid, c = env
    ops.import_trip({"voyage_id": tid, "depenses": [{"libelle": "Péage aller", "reel": "12", "pour": "tous"},
                                                     {"libelle": "Péage retour", "reel": "14", "pour": "tous"}]},
                    voyage=tid, verifier_id=tid)
    dep = {d["libelle"]: d["id"] for d in ops.trip(tid)["depenses"]}
    c.post(f"/v/{tid}/justificatifs", data={"expenses": [str(dep["Péage aller"])]},
           files={"files": ("peages.jpg", b"\xff\xd8\xfffake", "image/jpeg")})
    form = c.get(f"/v/{tid}/depenses/{dep['Péage retour']}/modifier").text
    assert 'class="rcard"' in form and "déjà pour : Péage aller" in form and "Voir en grand" in form
    rid = re.search(r'name="receipts" value="(\d+)"', form).group(1)
    p = {x["nom"]: x["id"] for x in ops.trip(tid)["participants"]}
    c.post(f"/v/{tid}/depenses/{dep['Péage retour']}", data={"label": "Péage retour", "actual": "14", "payer": "asso",
                                                            "split": "egal", "receipts": [rid],
                                                            **{f"inc_{i}": "1" for i in p.values()}})
    d = {x["libelle"]: x["justificatifs"] for x in ops.trip(tid)["depenses"]}
    assert d["Péage aller"] == d["Péage retour"] == ["J01"]
    # sur la dépense « aller », la vignette dit qu'elle sert aussi au « retour »
    assert "pour cette dépense et : Péage retour" in c.get(f"/v/{tid}/depenses/{dep['Péage aller']}/modifier").text


def test_thumbnails_for_pdf_and_photo(env):
    """Miniature : 1re page d'un PDF, photo réduite ; fichier illisible -> image vide (pas d'erreur)."""
    app, ops, tid, c = env
    from PIL import Image
    from fpdf import FPDF
    pdf = FPDF(); pdf.add_page(); pdf.set_font("helvetica", size=30); pdf.cell(0, 20, "FACTURE 42")
    im = Image.new("RGB", (1200, 900), "#336699"); buf = io.BytesIO(); im.save(buf, "JPEG")
    c.post(f"/v/{tid}/justificatifs", files=[("files", ("facture.pdf", bytes(pdf.output()), "application/pdf")),
                                             ("files", ("photo.jpg", buf.getvalue(), "image/jpeg")),
                                             ("files", ("casse.pdf", b"%PDF-1.4 fichier abime", "application/pdf"))])   # PDF illisible
    with app.state.db.Session() as s:
        recs = {r.original_name: r.id for r in s.get(M.Trip, tid).receipts}
    for name, size in (("facture.pdf", 240), ("photo.jpg", 240)):
        r = c.get(f"/v/{tid}/justificatifs/{recs[name]}/vignette")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
        assert max(Image.open(io.BytesIO(r.content)).size) == size
    r = c.get(f"/v/{tid}/justificatifs/{recs['casse.pdf']}/vignette")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    page = c.get(f"/v/{tid}/depenses/nouvelle").text
    assert page.count("/vignette") == 3 and "data-file-preview" in page


def test_browser_file_preview(env, tmp_path):
    """Choisir une photo et un PDF : aperçu de chacun avant l'envoi ; × en retire un ; les vignettes s'affichent."""
    import socket
    import threading
    import time
    import uvicorn
    from PIL import Image
    from fpdf import FPDF
    from playwright.sync_api import sync_playwright
    app, ops, tid, c = env
    img = tmp_path / "ticket.jpg"
    Image.new("RGB", (600, 800), "#cc8844").save(img)
    pdf = FPDF(); pdf.add_page(); pdf.set_font("helvetica", size=30); pdf.cell(0, 20, "FACTURE")
    pdfp = tmp_path / "facture.pdf"
    pdfp.write_bytes(bytes(pdf.output()))
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
            pg = b.new_page(viewport={"width": 1280, "height": 900})
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(f"http://127.0.0.1:{port}/v/{tid}/justificatifs")
            # deux choix successifs : le second s'AJOUTE au premier (et un doublon n'est pas repris)
            assert pg.inner_text(".file-pick") == "📎 Choisir une photo ou un PDF"
            pg.set_input_files("input[data-file-preview]", [str(img)])
            assert pg.inner_text(".file-pick") == "+ Ajouter un autre fichier"
            pg.set_input_files("input[data-file-preview]", [str(pdfp), str(img)])
            assert pg.locator(".file-preview .fp").count() == 2
            assert pg.evaluate("document.querySelector('input[data-file-preview]').files.length") == 2
            pg.wait_for_function("document.querySelectorAll('.file-preview img').length === 2", timeout=5000)
            pg.locator(".file-preview .fp-x").first.click()
            assert pg.locator(".file-preview .fp").count() == 1
            assert pg.evaluate("document.querySelector('input[data-file-preview]').files.length") == 1
            pg.click("form button.btn:has-text('Enregistrer')")
            pg.wait_for_selector(".rthumb img")
            pg.wait_for_function("Array.from(document.querySelectorAll('.rthumb img')).every(i => i.complete && i.naturalWidth > 1)",
                                 timeout=5000)
            assert errors == []
            b.close()
    finally:
        srv.should_exit = True
        th.join(5)


def test_preview_one_line(env):
    """Aperçu (parts égales) : une seule ligne, « 33,33 € chacun (100,00 € ÷ 3 personnes) — + 1 centime pour
    l'un d'eux (tiré au sort) »."""
    app, ops, tid, c = env
    ops.import_trip({"voyage_id": tid, "participants": ["Chloé"]}, voyage=tid, verifier_id=tid)
    p = {x["nom"]: x["id"] for x in ops.trip(tid)["participants"]}
    html = c.post(f"/v/{tid}/depenses/apercu", data={"actual": "60 + 40", "split": "egal",
                                                    **{f"inc_{i}": "1" for i in p.values()}}).text
    txt = re.sub(r"<[^>]+>", "", html).replace(" ", " ").replace("\xa0", " ")
    assert "33,33 € chacun" in txt and "100,00 € ÷ 3 personnes" in txt and "<table" not in html
    assert "+ 1 centime pour l'un d'eux (tiré au sort)" in txt.replace("&#39;", "'")
    form = c.get(f"/v/{tid}/depenses/nouvelle").text
    assert 'id="split-status"' in form                # la part de chacun s'affiche directement dans « Pour qui ? »
