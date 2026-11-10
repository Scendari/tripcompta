"""« Comment payer » : le texte libre de l'organisateur (RIB, chèque, lien, consigne…) est repris dans le message et
le PDF de ceux qui doivent encore de l'argent, et seulement eux. Le message se retouche avant l'envoi."""
import html
import re
from urllib.parse import unquote

from fastapi.testclient import TestClient

from compta import models as M
from compta import services as S
from compta.ops import Ops
from compta.web import create_app
from tests.test_send import clean, pdf_text, trip  # noqa: F401  (même voyage : Anne doit 35 €, Bob 145 €, Lebrun 70 €)

RIB = "Par virement : IBAN FR76 3000 4000 0312 3456 7890 143\nOu par chèque à l'ordre du Club Subaqua."


def save(app, tid, text):
    r = TestClient(app).post(f"/v/{tid}/envoyer/comment-payer", data={"pay_info": text}, follow_redirects=False)
    assert r.status_code == 303
    return unquote(r.headers["location"])


def messages(app, tid):
    """{nom du compte: message} tel qu'il part (lu dans la page)."""
    raw = TestClient(app).get(f"/v/{tid}/envoyer").text
    return {html.unescape(m.group(1)): html.unescape(m.group(2)) for m in re.finditer(
        r'<label class="sr-only" for="msg-[^"]+">Message pour ([^<]+)</label>\s*<textarea[^>]*>(.*?)</textarea>',
        raw, re.S)}


def test_empty_by_default_and_nothing_added(trip):
    app, ops, tid = trip
    page = clean(TestClient(app).get(f"/v/{tid}/envoyer").text)
    assert "Comment payer ?" in page and "(3 en ce moment)" in page
    assert all("Comment payer" not in m for m in messages(app, tid).values())
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        r = S.compute_trip(t)
        from compta.export import account_pdf
        assert "Comment payer" not in pdf_text(account_pdf(t, r.accounts[0].key)[0])


def test_saved_text_goes_into_the_message_of_those_who_owe(trip):
    app, ops, tid = trip
    loc = save(app, tid, RIB)
    assert "C'est noté" in loc and "&e=1" not in loc
    msgs = messages(app, tid)
    assert set(msgs) == {"Anne Martin", "Bob", "Famille Lebrun"}
    for name, rest in (("Anne Martin", "35,00"), ("Bob", "145,00"), ("Famille Lebrun", "70,00")):
        m = msgs[name]
        assert f"reste {rest} € à verser à l'asso." in m.replace("\xa0", " ").replace(" ", " ")
        # juste après la somme due, sur ses propres lignes, tel qu'écrit
        assert "\n\nComment payer :\nPar virement : IBAN FR76 3000 4000 0312 3456 7890 143\n" \
               "Ou par chèque à l'ordre du Club Subaqua.\n\nLe détail par catégorie" in m
    # les liens WhatsApp et e-mail emportent le même texte
    raw = TestClient(app).get(f"/v/{tid}/envoyer").text
    wa = re.search(r'href="(https://wa\.me/33612345678\?text=[^"]+)"', raw)
    assert "IBAN FR76 3000 4000 0312 3456 7890 143" in unquote(html.unescape(wa.group(1)))
    assert raw.count("IBAN%20FR76%203000") >= 6            # 3 comptes × (WhatsApp + e-mail)


def test_not_shown_to_someone_who_owes_nothing(trip):
    """Anne verse ses 35 € : elle est à jour, les consignes ne la concernent plus. Bob verse 200 € : on lui doit 55 €."""
    app, ops, tid = trip
    save(app, tid, RIB)
    ops.add_movement(tid, "Anne Martin", "35")
    ops.add_movement(tid, "Bob", "200")
    msgs = messages(app, tid)
    assert "Tout est réglé" in msgs["Anne Martin"] and "IBAN" not in msgs["Anne Martin"]
    assert "te doit 55,00" in msgs["Bob"].replace("\xa0", " ").replace(" ", " ") and "IBAN" not in msgs["Bob"]
    assert "IBAN" in msgs["Famille Lebrun"]
    assert "(1 en ce moment)" in clean(TestClient(app).get(f"/v/{tid}/envoyer").text)
    c = TestClient(app)
    with app.state.db.Session() as s:
        r = S.compute_trip(s.get(M.Trip, tid))
        keys = {a.name: a.key for a in r.accounts}
    assert "Comment payer" not in pdf_text(c.get(f"/v/{tid}/compte/{keys['Anne Martin']}.pdf").content)
    assert "Comment payer" not in pdf_text(c.get(f"/v/{tid}/compte/{keys['Bob']}.pdf").content)
    fam = pdf_text(c.get(f"/v/{tid}/compte/{keys['Famille Lebrun']}.pdf").content)
    assert "Comment payer" in fam and "IBAN FR76 3000 4000 0312 3456 7890 143" in fam
    assert "chèque à l'ordre du Club Subaqua" in fam


def test_the_one_who_holds_the_cash_gets_no_instructions(trip):
    app, ops, tid = trip
    save(app, tid, RIB)
    with app.state.db.Session() as s:
        t = s.get(M.Trip, tid)
        t.cashier_id = next(p.id for p in t.participants if p.name == "Bob")
        s.commit()
    msgs = messages(app, tid)
    assert "Tu tiens la caisse" in msgs["Bob"] and "IBAN" not in msgs["Bob"]
    assert "IBAN" in msgs["Anne Martin"]


def test_free_text_any_process(trip):
    """Pas seulement un RIB : un lien, une consigne. Les espaces et lignes vides en trop sont retirés."""
    app, ops, tid = trip
    save(app, tid, "  Sur HelloAsso :   https://www.helloasso.com/associations/club/paiements/hendaye \r\n\r\n\r\n"
                   "Sinon, attends le message du trésorier avant de payer.  ")
    m = messages(app, tid)["Bob"]
    assert "Comment payer :\nSur HelloAsso : https://www.helloasso.com/associations/club/paiements/hendaye\n\n" \
           "Sinon, attends le message du trésorier avant de payer.\n" in m
    # le lien reste entier dans le lien WhatsApp (encodé)
    raw = TestClient(app).get(f"/v/{tid}/envoyer").text
    assert "https%3A//www.helloasso.com/associations/club/paiements/hendaye" in raw


def test_html_in_the_text_is_never_interpreted(trip):
    app, ops, tid = trip
    save(app, tid, "<script>alert(1)</script> & <b>gras</b>")
    raw = TestClient(app).get(f"/v/{tid}/envoyer").text
    assert "<script>alert(1)</script>" not in raw and "&lt;script&gt;alert(1)&lt;/script&gt;" in raw


def test_too_long_is_refused_in_red_and_nothing_changes(trip):
    app, ops, tid = trip
    save(app, tid, "Chèque au club")
    loc = save(app, tid, "x" * (S.PAY_INFO_MAX + 1))
    assert "trop long" in loc and "e=1" in loc
    with app.state.db.Session() as s:
        assert s.get(M.Trip, tid).pay_info == "Chèque au club"


def test_erase_and_undo(trip):
    app, ops, tid = trip
    save(app, tid, "Chèque au club")
    assert "Rien à changer" in save(app, tid, "Chèque au club\r\n")
    loc = save(app, tid, "")
    assert "effacé" in loc
    assert all("Comment payer" not in m for m in messages(app, tid).values())
    assert app.state.db.undo()                                   # « Défaire » remet le texte
    with app.state.db.Session() as s:
        assert s.get(M.Trip, tid).pay_info == "Chèque au club"


def test_kept_when_the_trip_moves_to_another_computer(trip, tmp_path):
    from compta import transfer as TR
    app, ops, tid = trip
    save(app, tid, RIB)
    with app.state.db.Session() as s:
        data = TR.export_zip(s, [s.get(M.Trip, tid)], app.state.db.receipts_dir)
    other = create_app(tmp_path / "autre")
    with other.state.db.Session() as s:
        trips, _ = TR.import_zip(s, data, other.state.db.receipts_dir)
        s.commit()
        assert trips[0].pay_info == RIB


def test_ai_reads_it(trip):
    app, ops, tid = trip
    save(app, tid, "Chèque au club")
    assert ops.trip(tid)["comment_payer"] == "Chèque au club"


def test_browser_edit_message_before_sending(trip):
    """Le message retouché part tel quel : WhatsApp, e-mail et « Copier » suivent ; « Remettre » revient à l'origine."""
    import socket, threading, time
    import uvicorn
    from playwright.sync_api import sync_playwright
    app, ops, tid = trip
    save(app, tid, "Chèque au club")
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
            ctx = b.new_context(permissions=["clipboard-read", "clipboard-write"])
            pg = ctx.new_page()
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(f"http://127.0.0.1:{port}/v/{tid}/envoyer")
            row = pg.locator("[data-send-row]").first
            row.locator("summary").click()
            ta = row.locator("textarea[data-msg-edit]")
            original = ta.input_value()
            assert original.startswith("Bonjour Anne") and "Chèque au club" in original
            assert not row.locator("[data-msg-reset]").is_visible()
            ta.fill("Coucou Anne, il reste 35 € & un sourire ! Chèque au club.")
            wa = row.locator("a[href^='https://wa.me/']").get_attribute("href")
            assert wa == "https://wa.me/33612345678?text=" \
                         "Coucou%20Anne%2C%20il%20reste%2035%20%E2%82%AC%20%26%20un%20sourire%20!%20Ch%C3%A8que%20au%20club."
            mail = row.locator("a[href^='mailto:']").get_attribute("href")
            assert "body=Coucou%20Anne" in mail and "subject=Ton%20compte" in mail
            row.locator(".share-btn").click()
            row.locator("[data-copy]").click()
            assert pg.evaluate("navigator.clipboard.readText()") == \
                "Coucou Anne, il reste 35 € & un sourire ! Chèque au club."
            pg.keyboard.press("Escape")
            # les autres lignes ne bougent pas
            other = pg.locator("[data-send-row]").nth(1)
            assert "Bonjour%20Bob" in other.locator("a[href^='https://wa.me/']").get_attribute("href")
            # revenir au message d'origine
            row.locator("[data-msg-reset]").click()
            assert ta.input_value() == original
            assert "Bonjour%20Anne" in row.locator("a[href^='https://wa.me/']").get_attribute("href")
            assert not row.locator("[data-msg-reset]").is_visible()
            assert errors == []
            b.close()
    finally:
        srv.should_exit = True
        th.join(5)
