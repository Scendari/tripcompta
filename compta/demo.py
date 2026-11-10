"""Jeu de données de démonstration : un voyage plongée de 12 personnes.

    python -m compta.demo            # ajoute le voyage de démo dans le dossier de données
    python -m compta.demo --dir X    # dans un autre dossier
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import uuid

from . import models as M
from .db import Database
from .services import RECOMMENDED_CATEGORIES

DEMO_NAME = "Plongée Port-Cros — mai 2027 (démo)"

PEOPLE = ["Stéphane", "Thierry", "Luc", "Marie", "Sophie", "Julien",
          "Camille", "Nicolas", "Élodie", "Antoine", "Claire", "Hugo"]


def _receipt_pdf(title: str, lines: list[tuple[str, str]], total: str) -> bytes:
    from fpdf import FPDF
    from .export import FONTS
    pdf = FPDF(format=(148, 210))
    pdf.add_font("DejaVu", "", str(FONTS / "DejaVuSans.ttf"))
    pdf.add_font("DejaVu", "B", str(FONTS / "DejaVuSans-Bold.ttf"))
    pdf.add_page()
    pdf.set_font("DejaVu", "B", 15)
    pdf.cell(0, 9, title, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("DejaVu", "", 9)
    pdf.cell(0, 6, "FACTURE — exemple de démonstration", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font("DejaVu", "", 10)
    for a, b in lines:
        pdf.cell(95, 7, a)
        pdf.cell(0, 7, b, align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("DejaVu", "B", 11)
    pdf.cell(95, 8, "TOTAL TTC")
    pdf.cell(0, 8, total, align="R", new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def _receipt_jpg(title: str, total: str, color=(240, 236, 226)) -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    from .export import FONTS
    img = Image.new("RGB", (420, 600), color)
    d = ImageDraw.Draw(img)
    big = ImageFont.truetype(str(FONTS / "DejaVuSans-Bold.ttf"), 26)
    mid = ImageFont.truetype(str(FONTS / "DejaVuSans.ttf"), 18)
    d.text((30, 40), title, fill=(34, 32, 27), font=big)
    d.text((30, 90), "Ticket — démo", fill=(111, 102, 87), font=mid)
    for i in range(8):
        d.line((30, 150 + i * 40, 390, 150 + i * 40), fill=(200, 190, 170), width=1)
    d.text((30, 500), "TOTAL", fill=(34, 32, 27), font=big)
    d.text((240, 500), total, fill=(31, 138, 122), font=big)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82)
    return buf.getvalue()


def seed_demo(db: Database, replace: bool = True) -> int:
    s = db.Session()
    try:
        old = s.query(M.Trip).filter_by(name=DEMO_NAME).first()
        if old:
            if not replace:
                return old.id
            for e in old.expenses:
                for l in list(e.receipt_links):
                    s.delete(l)
            s.flush()
            for a in old.adjustments:
                s.delete(a)
            s.flush()
            s.delete(old)
            s.commit()

        t = M.Trip(name=DEMO_NAME, place="Hyères / Port-Cros", organizer="Stéphane",
                   start=dt.date(2027, 5, 13), end=dt.date(2027, 5, 16))
        t.categories = [M.Category(name=c, audience=a, position=i)
                        for i, (c, a) in enumerate(RECOMMENDED_CATEGORIES)]
        t.participants = [M.Participant(name=n) for n in PEOPLE]
        s.add(t)
        s.flush()
        P = {p.name: p.id for p in t.participants}
        C = {c.name: c.id for c in t.categories}
        roles = {"Stéphane": "organisateur", "Marie": "co-organisateur", "Thierry": "perso:DP", "Luc": "perso:DP"}
        t.roles_conf = '{"perso": ["DP"], "masques": []}'          # « DP » : une étiquette ordinaire de ce voyage
        for p in t.participants:
            p.roles = ",".join(x for x in ("plongeur", roles.get(p.name, "")) if x)
        everyone = {n: 1 for n in PEOPLE}

        def expense(label, cat, planned, actual, payer, weights, date):
            e = M.Expense(trip_id=t.id, label=label, category_id=C[cat], planned=planned, actual=actual,
                          payer_id=P[payer] if payer else None, date=date)
            s.add(e)
            s.flush()
            for n, w in weights.items():
                s.add(M.ExpenseShare(expense_id=e.id, participant_id=P[n], weight=w))
            return e

        d = dt.date
        gite = expense("Gîte « Les Pins » — 3 nuits", "Logement", 180000, 174000, None, everyone, d(2027, 3, 20))
        dives = {n: 6 for n in PEOPLE if n not in ("Claire", "Hugo")}
        dives["Julien"] = 4
        dives["Camille"] = 3
        club = expense("Forfait plongées club (6 plongées)", "Plongée", 240000, 231000, None, dives, d(2027, 5, 16))
        matos = expense("Location matériel", "Plongée", 30000, 28500, None,
                        {"Camille": 1, "Nicolas": 1, "Élodie": 1}, d(2027, 5, 13))
        snork = expense("Sortie palmes-masque-tuba", "Plongée", None, 9000, "Claire",
                        {"Claire": 1, "Hugo": 1}, d(2027, 5, 14))
        car1 = {"Luc": 1, "Marie": 1, "Sophie": 1, "Julien": 1}
        car2 = {"Thierry": 1, "Stéphane": 1, "Nicolas": 1, "Élodie": 1}
        fuel1 = expense("Carburant — voiture de Luc", "Transport / Covoit", 18000, 18640, "Luc", car1, d(2027, 5, 13))
        toll1 = expense("Péage A7 aller-retour — voiture de Luc", "Transport / Covoit", 6500, 6480, "Luc", car1,
                        d(2027, 5, 13))
        fuel2 = expense("Carburant — voiture de Thierry", "Transport / Covoit", 18000, 17210, "Thierry", car2,
                        d(2027, 5, 13))
        toll2 = expense("Péage A7 aller-retour — voiture de Thierry", "Transport / Covoit", 6500, 6480, "Stéphane",
                        car2, d(2027, 5, 13))
        boat = expense("Navette bateau Hyères ↔ Port-Cros", "Transport / Covoit", 24000, None, None, everyone,
                       d(2027, 5, 14))
        food = expense("Courses communes", "Repas", 40000, 41235, "Stéphane", everyone, d(2027, 5, 13))
        resto = expense("Restaurant de clôture", "Repas", 42000, None, None, everyone, d(2027, 5, 15))
        s.flush()

        # Justificatifs
        def receipt(number, name, data, ctype, exps):
            stored = f"{t.id}_{uuid.uuid4().hex}{name[name.rfind('.'):]}"
            (db.receipts_dir / stored).write_bytes(data)
            r = M.Receipt(trip_id=t.id, number=number, original_name=name, stored_name=stored, content_type=ctype)
            s.add(r)
            s.flush()
            for e in exps:
                s.add(M.ReceiptLink(receipt_id=r.id, expense_id=e.id))

        receipt(1, "facture_gite_les_pins.pdf",
                _receipt_pdf("Gîte « Les Pins »", [("3 nuits × 12 personnes", "1 740,00 €")], "1 740,00 €"),
                "application/pdf", [gite])
        receipt(2, "facture_club_plongee.pdf",
                _receipt_pdf("Club de plongée de Port-Cros",
                             [("Forfait plongées", "2 025,00 €"), ("Location matériel", "285,00 €"),
                              ("Remise groupe", "0,00 €")], "2 310,00 €"),
                "application/pdf", [club, matos])
        receipt(3, "ticket_carburant_luc.jpg", _receipt_jpg("Station TotalÉnergies", "186,40 €"), "image/jpeg",
                [fuel1])
        receipt(4, "ticket_peage_luc.jpg", _receipt_jpg("Autoroute A7", "64,80 €"), "image/jpeg", [toll1])
        receipt(5, "ticket_courses.jpg", _receipt_jpg("Supermarché Hyères", "412,35 €"), "image/jpeg", [food])
        receipt(6, "ticket_carburant_thierry.jpg", _receipt_jpg("Station Esso", "172,10 €"), "image/jpeg", [fuel2])
        # Le péage de la voiture de Thierry et le snorkeling n'ont pas de justificatif (alerte de démo).

        # Versements (acompte de 200 € puis dernier versement pour certains)
        acompte = d(2027, 3, 1)
        for n in PEOPLE:
            if n not in ("Stéphane", "Hugo"):
                s.add(M.Movement(trip_id=t.id, participant_id=P[n], kind="versement", amount=20000, date=acompte,
                                 note="Acompte"))
        s.add(M.Movement(trip_id=t.id, participant_id=P["Hugo"], kind="versement", amount=10000,
                         date=d(2027, 3, 10), note="Acompte (étudiant)"))
        for n, amount in [("Marie", 23000), ("Sophie", 21000), ("Luc", 15000), ("Nicolas", 26000),
                          ("Élodie", 26000), ("Thierry", 18000)]:
            s.add(M.Movement(trip_id=t.id, participant_id=P[n], kind="versement", amount=amount,
                             date=d(2027, 5, 20), note="Dernier versement"))

        # Ajustements
        s.add(M.Adjustment(trip_id=t.id, participant_id=P["Hugo"], expense_id=gite.id, value=-4000,
                           compensation="asso", reason="étudiant"))
        s.add(M.Adjustment(trip_id=t.id, participant_id=P["Thierry"], expense_id=None, value=-2500,
                           compensation="others", reason="encadrant de la sortie"))
        s.add(M.Adjustment(trip_id=t.id, participant_id=P["Camille"], expense_id=club.id, value=12000, fixed=True,
                           compensation="others", reason="tarif baptême"))

        # Désistement : Antoine garde sa part du gîte (non remboursable), le groupe reprend le reste
        s.add(M.Withdrawal(trip_id=t.id, participant_id=P["Antoine"], date=d(2027, 4, 28), absorber="group",
                           kept=str(gite.id), note="Raison familiale"))

        # Entre nous
        sub = M.SubAccount(trip_id=t.id, name="Resto du vendredi soir",
                           member_ids=",".join(str(P[n]) for n in ["Luc", "Marie", "Sophie", "Julien", "Camille"]))
        s.add(sub)
        s.flush()
        s.add(M.SubExpense(sub_id=sub.id, label="Pizzas", amount=9650, payer_id=P["Marie"], date=d(2027, 5, 14),
                           weights=",".join(f"{P[n]}:1" for n in ["Luc", "Marie", "Sophie", "Julien", "Camille"])))
        s.add(M.SubExpense(sub_id=sub.id, label="Glaces", amount=2200, payer_id=P["Julien"], date=d(2027, 5, 14),
                           weights=",".join(f"{P[n]}:1" for n in ["Luc", "Marie", "Sophie", "Julien"])))
        s.add(M.SubTransfer(sub_id=sub.id, sender_id=P["Sophie"], receiver_id=P["Marie"], amount=1930,
                            date=d(2027, 5, 15)))
        s.commit()
        return t.id
    finally:
        s.close()


FAMILY_DEMO_NAME = "Week-end Banyuls — juin 2027 (démo familles)"

# Montants choisis pour se vérifier de tête :
#   plongeur     = gîte 125 + plongées 120 + courses 30 = 275 €
#   accompagnant = gîte 125 + balade 20 + courses 30    = 175 €
FAMILY_DEMO = {
    "voyage": {"nom": FAMILY_DEMO_NAME, "lieu": "Banyuls-sur-Mer", "debut": "2027-06-12", "fin": "2027-06-13",
               "organisateur": "Karim", "dp": ["Paul Lebrun"]},
    "participants": [
        {"nom": "Paul Lebrun", "famille": "Lebrun"},
        {"nom": "Julie Lebrun", "famille": "Lebrun"},
        {"nom": "Léa Lebrun", "famille": "Lebrun", "accompagnant": True, "note": "12 ans"},
        {"nom": "Tom Lebrun", "famille": "Lebrun", "accompagnant": True, "note": "9 ans"},
        {"nom": "Bruno Roux", "famille": "Roux"},
        {"nom": "Nadia Roux", "famille": "Roux", "accompagnant": True},
        "Karim", "Inès",
    ],
    "familles": [{"nom": "Lebrun", "paie": "Paul Lebrun"}, {"nom": "Roux", "paie": "Bruno Roux"}],
    "depenses": [
        {"libelle": "Gîte 2 nuits", "categorie": "Logement", "prevu": "1000", "reel": "1000", "paye_par": "asso",
         "pour": "tous", "date": "2027-06-12"},
        {"libelle": "Plongées (4 par plongeur)", "categorie": "Plongée", "prevu": "600", "reel": "600",
         "paye_par": "asso", "pour": "plongeurs", "date": "2027-06-12"},
        {"libelle": "Balade en kayak", "categorie": "Divers", "reel": "60", "paye_par": "asso",
         "pour": "accompagnants", "date": "2027-06-12"},
        {"libelle": "Courses", "categorie": "Repas", "reel": "240", "paye_par": "Julie Lebrun", "pour": "tous",
         "date": "2027-06-11"},
    ],
    "versements": [
        {"participant": "Famille Lebrun", "montant": "500", "date": "2027-05-02", "note": "acompte"},
        {"participant": "Famille Roux", "montant": "450", "date": "2027-05-03"},
        {"participant": "Karim", "montant": "300", "date": "2027-05-03"},
    ],
}


def seed_family_demo(db: Database, replace: bool = True) -> int:
    from .importer import run_import
    s = db.Session()
    try:
        old = s.query(M.Trip).filter_by(name=FAMILY_DEMO_NAME).first()
        if old:
            if not replace:
                return old.id
            s.delete(old)
            s.flush()
        res = run_import(s, FAMILY_DEMO, None)
        s.commit()
        return res["voyage_id"]
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser(description="Ajoute le voyage de démonstration.")
    ap.add_argument("--dir", help="Dossier de données (par défaut : Documents/TripCompta)")
    a = ap.parse_args()
    db = Database(a.dir)
    tid = seed_demo(db)
    fid = seed_family_demo(db)
    print(f"Voyages de démo créés (n°{tid} et n°{fid}) dans {db.dir}")


if __name__ == "__main__":
    main()
