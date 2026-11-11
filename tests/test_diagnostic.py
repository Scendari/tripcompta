"""« Exporter un diagnostic » : un fichier texte utile à la personne qui aide, sans nom, ni montant, ni texte saisi."""
import datetime as dt
import html
import os
import re
from pathlib import Path

from fastapi.testclient import TestClient

from compta import db as D
from compta import diagnostic as DIAG
from compta import journal
from compta.ops import Ops
from compta.web import create_app

# ce qui ne doit jamais sortir de l'ordinateur par ce fichier
PRIVATE = ["Zanzibar", "Quetzalcoatl", "Wolfeschlegel", "Ornithorynque", "1234,56", "1234.56", "123456",
           "FR76 3000", "payer par chèque à l'ordre"]


def clean(t):
    return re.sub(r"\s+", " ", html.unescape(t).replace("\xa0", " "))


def seed(app) -> int:
    ops = Ops(app.state.db)
    tid = ops.create_trip("Zanzibar secret")["voyage_id"]
    ops.add_participants(tid, ["Quetzalcoatl Wolfeschlegel", "Bruno", "Chloé"])
    ops.add_expense(tid, "Ornithorynque", categorie="Logement", montant_reel="1234,56")
    ops.add_expense(tid, "Essence", categorie="Transport", montant_reel="90", paye_par="Bruno")
    ops.add_movement(tid, "Bruno", "50")
    return tid


def get(cl):
    r = cl.get("/reglages/diagnostic.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert re.fullmatch(r'attachment; filename="diagnostic-trip-compta-\d{8}-\d{4}\.txt"', r.headers["content-disposition"])
    return r.text


def test_diagnostic_gives_numbers_and_state(tmp_path):
    f = tmp_path / "voyages"
    app = create_app(f)
    cl = TestClient(app)
    tid = seed(app)
    cl.post(f"/v/{tid}/justificatifs", files=[("files", ("ticket Zanzibar.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf"))])
    cl.post(f"/v/{tid}/envoyer/comment-payer", data={"pay_info": "payer par chèque à l'ordre de Zanzibar, FR76 3000"})
    text = get(cl)
    assert text.startswith("DIAGNOSTIC TRIP COMPTA\nfait le ")
    assert "ni nom, ni montant" in text
    for expected in ("== Application ==", f"Trip Compta {app.state.updater.label}", "SQLite ",
                     "vérification de la signature des mises à jour : prête",
                     "== Dossier des voyages ==", "protégé par un mot de passe : non", "place libre sur ce disque : ",
                     "vérification du fichier : intact", f"numéro de structure : {D.SCHEMA}",
                     "voyages : 1", "participants : 3", "dépenses : 2", "versements et remboursements : 1",
                     "justificatifs : 1", f"  voyage n° {tid} : 3 participants, 2 dépenses, 1 justificatifs",
                     "fichiers de justificatifs : 1", "justificatifs dont le fichier manque : 0",
                     "== Sauvegardes et historique ==", "sauvegardes rangées à part, sur l'ordinateur : oui",
                     "sauvegardes gardées : 1", "justificatifs dans les sauvegardes : 1 fichiers",
                     "actions que « Défaire » peut encore annuler : ", "sauvegarde du jour : faite",
                     "== Journal de l'application"):
        assert expected in text, expected
    for secret in PRIVATE:
        assert secret not in text, secret


def test_diagnostic_hides_the_person(tmp_path, monkeypatch):
    home = tmp_path / "Users" / "marguerite.duras"
    home.mkdir(parents=True)
    folder = home / "Mon Drive" / "TripCompta"
    text = DIAG.anonymize(f"dossier : {folder}\nFile \"{home}/app/web.py\", line 3\nsession marguerite.duras ouverte",
                          home=home, users=["marguerite.duras"])
    assert "marguerite" not in text
    sep = os.sep                                                 # Windows : le chemin garde ses « \\ », c'est naturel
    assert f"dossier : ~{sep}Mon Drive{sep}TripCompta" in text and 'File "~/app/web.py"' in text
    assert "session (session) ouverte" in text
    # les valeurs jointes aux erreurs de la base peuvent contenir un nom ou un montant : retirées
    err = ("sqlalchemy.exc.IntegrityError: (sqlite3.IntegrityError) NOT NULL constraint failed: expenses.label\n"
           "[SQL: INSERT INTO expenses (label, amount) VALUES (?, ?)]\n[parameters: ('Zanzibar', 123456)]\n(Background)")
    out = DIAG.anonymize(err, home=home, users=[])
    assert "Zanzibar" not in out and "123456" not in out and "INSERT" not in out
    assert "NOT NULL constraint failed: expenses.label" in out and "[valeurs retirées]" in out and "(Background)" in out


def test_diagnostic_joins_a_cleaned_journal(tmp_path, monkeypatch):
    f = tmp_path / "voyages"
    app = create_app(f)
    cl = TestClient(app)
    seed(app)
    home = str(Path.home())
    journal.log.error("erreur imprévue : POST /v/1/depenses dans %s/dossier [parameters: ('Zanzibar', 123456)]", home)
    text = get(cl)
    assert "erreur imprévue : POST /v/1/depenses dans ~/dossier [valeurs retirées]" in text
    assert home not in text
    for secret in PRIVATE:
        assert secret not in text, secret
    assert "diagnostic exporté" in journal.tail(5)


def test_cloud_folder_is_recognised_from_its_path():
    assert DIAG.cloud_hint("/Users/x/Library/CloudStorage/GoogleDrive-a@b.fr/Mon Drive/TripCompta") == "Google Drive"
    assert DIAG.cloud_hint(r"C:\Users\x\OneDrive\Documents\TripCompta") == "OneDrive"
    assert DIAG.cloud_hint("/Users/x/Library/Mobile Documents/com~apple~CloudDocs/TripCompta") == "iCloud"
    assert DIAG.cloud_hint("/Users/x/Documents/TripCompta") is None


def test_missing_receipt_file_and_failed_backup_are_reported(tmp_path, monkeypatch):
    f = tmp_path / "voyages"
    app = create_app(f)
    cl = TestClient(app)
    tid = seed(app)
    cl.post(f"/v/{tid}/justificatifs", files=[("files", ("ticket.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf"))])
    for p in app.state.db.receipts_dir.iterdir():
        if p.is_file():
            p.unlink()
    app.state.backup_failed = "2026-11-10"
    text = get(cl)
    assert "justificatifs dont le fichier manque : 1" in text
    assert "sauvegarde du jour : ÉCHEC le 2026-11-10" in text


def test_button_on_about_page_and_error_page(tmp_path):
    f = tmp_path / "voyages"
    app = create_app(f)
    cl = TestClient(app, raise_server_exceptions=False)
    about = cl.get("/reglages/a-propos").text
    assert 'href="/reglages/diagnostic.txt"' in about and "Exporter un diagnostic" in about
    assert "ni nom, ni montant, ni texte que tu as saisi" in clean(about)
    assert "diagnostic" in clean(cl.get("/reglages").text)

    @app.get("/boum")
    def boum():
        raise RuntimeError(f"valeur {'Zanz' + 'ibar'!r} refusée pour le montant {2469.12 / 2}, code 42")
    page = cl.get("/boum")
    assert page.status_code == 500 and 'href="/reglages/diagnostic.txt"' in page.text
    text = get(cl)
    assert "erreur imprévue : GET /boum" in text
    # le type de l'erreur et son message restent, sans ce qui a pu venir d'une saisie
    assert "RuntimeError: valeur '…' refusée pour le montant #, code 42" in text
    for secret in PRIVATE:
        assert secret not in text, secret


def test_diagnostic_when_trips_cannot_be_opened(tmp_path):
    """Fichier des voyages abîmé sans sauvegarde : l'écran d'explication propose quand même le diagnostic."""
    f = tmp_path / "voyages"
    app = create_app(f)
    seed(app)
    app.state.db.engine.dispose()
    for d in (D.backups_dir_for(f), f / ".annuler"):
        for p in d.glob("*.db"):
            p.unlink()
    with open(f / "compta.db", "r+b") as fh:
        fh.seek(100)
        fh.write(os.urandom(4000))
    app2 = create_app(f)
    assert app2.state.db is None
    cl = TestClient(app2)
    page = cl.get("/")
    assert page.status_code == 503 and 'href="/reglages/diagnostic.txt"' in page.text
    text = get(cl)
    assert "== Problème à l'ouverture ==" in text and "abîmé, sans sauvegarde lisible" in text
    assert "fichier des voyages : présent" in text
    assert "vérification du fichier : ABÎMÉ" in text or "vérification du fichier : impossible" in text \
        or "ouverture impossible" in text
    for secret in PRIVATE:
        assert secret not in text, secret


def test_diagnostic_when_trips_are_newer(tmp_path):
    f = tmp_path / "voyages"
    app = create_app(f)
    seed(app)
    app.state.db.engine.dispose()
    import sqlite3
    con = sqlite3.connect(f / "compta.db")
    con.execute(f"PRAGMA user_version = {D.SCHEMA + 1}")
    con.commit()
    con.close()
    app2 = create_app(f)
    assert app2.state.db is None
    text = get(TestClient(app2))
    assert "plus récente que celle-ci" in text and f"numéro de structure : {D.SCHEMA + 1}" in text
    assert "voyages : 1" in text and "Zanzibar" not in text


def test_file_name_has_the_date():
    assert DIAG.file_name(dt.datetime(2026, 11, 10, 21, 5)) == "diagnostic-trip-compta-20261110-2105.txt"
