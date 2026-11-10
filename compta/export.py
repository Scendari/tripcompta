"""Exports : bilan PDF pour le trésorier, ZIP des justificatifs, PDF « Entre nous »."""
from __future__ import annotations

import datetime as dt
import io
import re
import unicodedata
import zipfile
from pathlib import Path

from fpdf import FPDF
from fpdf.fonts import FontFace

from . import engine as E
from . import models as M
from . import services as S

FONTS = Path(__file__).parent / "static" / "fonts"
TEAL = (31, 138, 122)
INK = (34, 32, 27)
MUTED = (111, 102, 87)
SAND_BG = (241, 237, 227)
CORAL = (214, 69, 69)
OCEAN = (40, 120, 181)
GREEN = (31, 138, 76)
AMBER = (150, 98, 20)


def _d(d):
    return d.strftime("%d/%m/%Y") if d else ""


def _m(c):
    return E.fmt(c).replace(" ", " ").replace(" ", " ")


class Doc(FPDF):
    def __init__(self, title: str, subtitle: str = ""):
        super().__init__(format="A4")
        self.add_font("DejaVu", "", str(FONTS / "DejaVuSans.ttf"))
        self.add_font("DejaVu", "B", str(FONTS / "DejaVuSans-Bold.ttf"))
        self.doc_title = title
        self.subtitle = subtitle
        self.asso = "l'asso"            # nom de la caisse (asso nommée, ou « la caisse (Paul) »)
        self.set_auto_page_break(True, margin=18)
        self.set_margins(15, 15, 15)
        self.alias_nb_pages()

    def header(self):
        self.set_font("DejaVu", "B", 9)
        self.set_text_color(*TEAL)
        self.cell(0, 5, self.doc_title, new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(*TEAL)
        self.line(15, self.get_y() + 1, 195, self.get_y() + 1)
        self.ln(5)
        self.set_text_color(*INK)

    def footer(self):
        self.set_y(-12)
        self.set_font("DejaVu", "", 7.5)
        self.set_text_color(*MUTED)
        self.cell(0, 5, f"{self.subtitle}   ·   Édité le {dt.date.today().strftime('%d/%m/%Y')}   ·   "
                        f"Page {self.page_no()}/{{nb}}", align="C")

    def _t(self, text):
        """Remplace « l'asso » par le nom de la caisse de ce voyage."""
        if self.asso == "l'asso" or not isinstance(text, str):
            return text
        cap = self.asso[:1].upper() + self.asso[1:]
        if self.asso.startswith("la caisse"):
            for a in ("Caisse de l'asso", "caisse de l'asso", "Caisse de l’asso", "caisse de l’asso"):
                text = text.replace(a, cap if a[0] == "C" else self.asso)
        for a in ("l'asso", "l’asso"):
            text = text.replace(a, self.asso)
        for a in ("L'asso", "L’asso"):
            text = text.replace(a, cap)
        return text

    def h1(self, text):
        text = self._t(text)
        self.set_font("DejaVu", "B", 16)
        self.set_text_color(*INK)
        self.multi_cell(0, 8, text, new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

    def h2(self, text, explain: str = ""):
        text, explain = self._t(text), self._t(explain)
        self.ln(3)
        self.set_font("DejaVu", "B", 12.5)
        self.set_text_color(*TEAL)
        self.multi_cell(0, 7, text, new_x="LMARGIN", new_y="NEXT")
        if explain:
            self.set_font("DejaVu", "", 8.5)
            self.set_text_color(*MUTED)
            self.multi_cell(0, 4.5, explain, new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(*INK)
        self.ln(2)

    def para(self, text, size=10, bold=False, color=INK):
        text = self._t(text)
        self.set_font("DejaVu", "B" if bold else "", size)
        self.set_text_color(*color)
        self.multi_cell(0, size * 0.55, text, new_x="LMARGIN", new_y="NEXT", markdown=True)
        self.set_text_color(*INK)

    def table_(self, headers, rows, widths, align=None, total=None, size=8.5, head_color=TEAL):
        self.set_font("DejaVu", "", size)
        head = FontFace(emphasis="BOLD", color=(255, 255, 255), fill_color=head_color)
        align = align or ["LEFT"] * len(headers)
        with self.table(col_widths=widths, text_align=align, headings_style=head, line_height=size * 0.6,
                        cell_fill_color=(248, 246, 240), cell_fill_mode="ROWS", borders_layout="HORIZONTAL_LINES",
                        width=sum(widths)) as t:
            r = t.row()
            for h in headers:
                r.cell(self._t(h))
            for row in rows:
                r = t.row()
                for c in row:
                    r.cell(self._t(str(c)))
            if total:
                r = t.row(style=FontFace(emphasis="BOLD", fill_color=SAND_BG))
                for c in total:
                    r.cell(self._t(str(c)))
        self.ln(2)


def _solde(v: int) -> str:
    """Débit − crédit, en mots : « doit 12,85 € » / « on lui doit 690,54 € »."""
    return f"doit {_m(v)}" if v > 0 else f"on lui doit {_m(-v)}" if v < 0 else "0"


def _status(p) -> str:
    if getattr(p, "holds_cash", False):
        return "tient la caisse"
    if p.balance > 0:
        return f"doit {_m(p.balance)}"
    if p.balance < 0:
        return f"on lui doit {_m(-p.balance)}"
    return "à jour"


def _receipt_codes(e: M.Expense) -> str:
    return ", ".join(l.receipt.code for l in sorted(e.receipt_links, key=lambda l: l.receipt.number))


def trip_pdf(trip: M.Trip, mode: str = E.ACTUAL) -> bytes:
    r = S.compute_trip(trip, mode)
    names = {p.id: p.name for p in trip.participants}
    dates = ""
    if trip.start:
        dates = f"du {_d(trip.start)}" + (f" au {_d(trip.end)}" if trip.end else "")
    mode_txt = "montants réels (le prévu est utilisé tant que le réel n'est pas connu)" \
        if mode == E.ACTUAL else "montants prévus"
    pdf = Doc(f"Bilan du voyage — {trip.name}", trip.name)
    pdf.asso = S.caisse_label(trip)

    # ---------------------------------------------------------------- page 1
    pdf.add_page()
    pdf.h1(f"Bilan du voyage\n{trip.name}")
    org = S.organizer_name(trip)
    info = [x for x in [trip.place, dates,
                        f"{len(trip.participants)} participants",
                        f"Organisé par {org}" if org else ""] if x]
    pdf.para("  ·  ".join(info), 10, color=MUTED)
    co = [p.name for p in S.with_role(trip, S.CO_ORGANIZER)]
    team = [f"Co-organisation : {', '.join(co)}"] if co else []
    if team:
        pdf.para("  ·  ".join(team), 10, color=MUTED)
    if trip.note:
        pdf.para(trip.note, 9.5, color=MUTED)
    pdf.para(f"Calculé sur les {mode_txt}.", 8.5, color=MUTED)
    pdf.ln(4)

    # Synthèse en trois blocs, la même que le mail au trésorier (mêmes chiffres, même ordre)
    m = S.money_summary(trip, r)
    cash_in, cash_out = S.asso_cash(trip)
    asso = pdf.asso
    pdf.h2("Le coût du voyage")
    rows = []
    if m["cost_split"]:
        rows = [[f"Payé par {asso}", _m(m["paid_out"])], ["Avancé directement par des participants", _m(m["advanced"])]]
    pdf.table_(["", "Montant"], rows, [130, 50], ["LEFT", "RIGHT"], total=["Coût total du voyage", _m(m["cost"])],
               size=9.5)
    caisse_title = f"La caisse de {asso}" if not asso.startswith("la caisse") else asso[:1].upper() + asso[1:]
    pdf.h2(caisse_title, "L'argent réellement entré et sorti pour ce voyage.")
    rows = [["+ Reçu des participants", _m(m["received"])]]
    if m["other_in"]:
        rows.append(["+ Autres recettes (subvention, remboursement d'un fournisseur…)", _m(m["other_in"])])
    rows.append(["− Payé aux prestataires (hébergement, club…)", _m(m["paid_out"])])
    if m["refunded"]:
        rows.append(["− Déjà remboursé aux participants", _m(m["refunded"])])
    total = (["= Dans la caisse aujourd'hui", _m(m["cash"])] if m["cash"] >= 0
             else [f"= {asso[:1].upper() + asso[1:]} a avancé de sa trésorerie", _m(-m["cash"])])
    pdf.table_(["", "Montant"], rows, [130, 50], ["LEFT", "RIGHT"], total=total, size=9.5)
    if m["asso_total"]:
        pdf.para(f"Et {asso} prend à sa charge {_m(m['asso_total'])} sur ce voyage (détail plus loin).", 9, color=MUTED)

    if r.to_collect or r.to_refund:
        # comme l'écran « Argent reçu et rendu › Reste à régler » : deux tableaux, du plus gros au plus petit
        def who(acc):
            if getattr(acc, "is_family", False) and len(acc.members) > 1:
                return f"{acc.name} (à {names.get(acc.referent, '?')})"
            return acc.name
        owe = sorted([(who(a), c) for a, b, c in r.settlements() if a != E.ASSO], key=lambda x: (-x[1], x[0]))
        due = sorted([(who(b), c) for a, b, c in r.settlements() if a == E.ASSO], key=lambda x: (-x[1], x[0]))
        need = (len(owe) + len(due) + 5) * 6.2 + 25 + 40   # + « Une fois tout réglé »          # mm : ne pas couper la section entre deux pages
        if pdf.get_y() + need > pdf.h - 20:
            pdf.add_page()
        pdf.h2("Ce qu'il reste à régler", "Pour que tout soit réglé : ce que l'asso doit encore recevoir des "
               "participants, et ce qu'elle doit leur rendre.")

        def block(title, rows_, total_, color):
            n = len(rows_)
            pdf.table_([f"{title} — {n} personne{'s' if n > 1 else ''} ou famille{'s' if n > 1 else ''}", "Montant"],
                       [[w, _m(c)] for w, c in rows_], [130, 50], ["LEFT", "RIGHT"],
                       total=["Total", _m(total_)], size=9.5, head_color=color)
        if owe:
            block("À recevoir", owe, r.to_collect, AMBER)
        if due:
            pdf.ln(2)
            block("À rendre", due, r.to_refund, OCEAN)

    pdf.h2("Une fois tout réglé")
    rows = [["Dans la caisse aujourd'hui" if m["cash"] >= 0 else f"Avancé par {asso}",
             _m(m["cash"]) if m["cash"] >= 0 else "− " + _m(-m["cash"])]]
    if m["to_collect"]:
        rows.append(["+ Reste à recevoir des participants", _m(m["to_collect"])])
    if m["to_refund"]:
        rows.append(["− À rembourser aux participants", _m(m["to_refund"])])
    res = m["final"]
    label = ("Le voyage rapporte à " if res > 0 else "Le voyage coûte à " if res < 0 else "Le voyage ne coûte rien à ") + asso
    pdf.table_(["", "Montant"], rows, [130, 50], ["LEFT", "RIGHT"],
               total=[label + (" (arrondis au centime)" if m["rounding_only"] else ""), _m(abs(res))], size=9.5)

    # ---------------------------------------------------------------- page 2 participants
    pdf.add_page()
    pdf.h2("Les participants",
           "Sa part = ce que la personne doit pour le voyage. A versé = argent donné à l'asso. "
           "A avancé = dépenses qu'elle a payées elle-même pour le groupe. Reste = ce qu'il reste à régler.")
    rows = []

    rmap = S.roles_map(trip)

    people_by_id = {p.id: p for p in trip.participants}

    def pname(p):
        tags = [S.ROLE_SHORT[x] for x in rmap.get(p.id, []) if x in S.TEAM_ROLES] + (["acc."] if p.companion else [])
        name = p.name + (f" ({', '.join(tags)})" if tags else "") + (" (désisté)" if p.withdrawn else "")
        q = people_by_id.get(p.id)
        lines = [x.strip() for x in ((q.phone, q.email) if q else ()) if x and x.strip()]
        return "\n".join([name] + lines)                        # ses contacts sous son nom, un par ligne
    for a in r.accounts:
        if a.is_family:
            rows.append([a.name, _m(a.share), _m(a.paid), _m(a.advanced), _m(a.refunded), _status(a)])
            for p in a.members:
                rows.append(["\n".join("    " + x for x in pname(p).split("\n")), _m(p.share), _m(p.paid), _m(p.advanced), _m(p.refunded), ""])
        else:
            p = a.members[0]
            rows.append([pname(p), _m(p.share), _m(p.paid), _m(p.advanced), _m(p.refunded), _status(p)])
    legend = []
    if any(x in S.TEAM_ROLES for rs in rmap.values() for x in rs):
        legend.append("org. = organisateur, co-org. = co-organisateur")
    if any(p.companion for p in r.people.values()):
        legend.append("acc. = accompagnant")
    if legend:
        pdf.para(" ; ".join(legend) + ".", 8.5, color=MUTED)
    if r.families:
        pdf.para("Les familles règlent leurs comptes ensemble : leur « Reste » est donné sur la ligne de la famille, "
                 "le détail de chaque membre en dessous.", 8.5, color=MUTED)
    tot = r.people.values()
    pdf.table_(["Nom et contact", "Sa part", "A versé", "A avancé", "Remboursé", "Reste"], rows,
               [56, 21, 21, 21, 23, 38], ["LEFT", "RIGHT", "RIGHT", "RIGHT", "RIGHT", "RIGHT"],
               total=["Total", _m(sum(p.share for p in tot)), _m(sum(p.paid for p in tot)),
                      _m(sum(p.advanced for p in tot)), _m(sum(p.refunded for p in tot)), ""])

    # ---------------------------------------------------------------- page 3 prises en charge
    pdf.h2("Ce que l'asso a pris en charge",
           "Réductions accordées, désistements… que l'asso a décidé de payer à la place des participants.")
    if r.asso_charges:
        pdf.table_(["Sur", "Pourquoi", "Montant"],
                   [[c.label, c.reason, _m(c.cents)] for c in r.asso_charges], [55, 95, 30],
                   ["LEFT", "LEFT", "RIGHT"], total=["Total", "", _m(r.asso_total)])
    else:
        pdf.para("Rien : les participants paient la totalité du voyage.", 9.5, color=MUTED)
    if trip.adjustments:
        pdf.h2("Ajustements de parts", "Modifications de la part de certains participants, et qui a compensé.")
        rows = []
        for a in trip.adjustments:
            on = a.expense.label if a.expense else "total du voyage"
            if a.fixed:
                what = f"montant fixé à {_m(a.value)}"
            else:
                what = ("réduction de " if a.value < 0 else "supplément de ") + _m(abs(a.value))
            comp = {"others": "les autres participants", "asso": "l'asso",
                    "person": a.compensator.name if a.compensator else "?"}[a.compensation]
            rows.append([a.participant.name, on, what, comp, a.reason])
        pdf.table_(["Qui", "Sur", "Quoi", "Compensé par", "Motif"], rows, [30, 38, 42, 34, 36])
    if trip.withdrawals:
        pdf.h2("Désistements")
        rows = []
        for w in trip.withdrawals:
            kept = [e.label for e in trip.expenses if e.id in w.kept_ids]
            rows.append([w.participant.name, _d(w.date), ", ".join(kept) or "aucune",
                         "le groupe" if w.absorber == E.GROUP else "l'asso"])
        pdf.table_(["Qui", "Date", "Parts conservées (non remboursables)", "Le reste est payé par"], rows,
                   [35, 22, 78, 45])

    # ---------------------------------------------------------------- page 4 dépenses
    pdf.add_page()
    pdf.h2("Les dépenses", "Payé par « asso » = payé depuis le compte de l'asso ; sinon avancé par le participant. "
                           "Les numéros J01, J02… renvoient aux justificatifs du fichier ZIP.")
    for c in trip.categories:
        es = [e for e in trip.expenses if e.category_id == c.id]
        if not es:
            continue
        pdf.set_font("DejaVu", "B", 10)
        pdf.cell(0, 6, c.name, new_x="LMARGIN", new_y="NEXT")
        def sm(v, e):
            return "—" if v is None else ("− " if e.is_income else "") + _m(v)
        es = sorted(es, key=lambda e: (not e.subcategory, E.sort_key(e.subcategory or ""), e.id))
        grouped = any(e.subcategory for e in es)
        rows = [[("    " if grouped else "") + e.label + (" (recette)" if e.is_income else "")
                 + (" (montant à venir)" if e.pending else "")
                 + ((" — tickets : " + " + ".join(_m(x["cents"]) + (f" ({_d(x['date'])})" if x["date"] else "")
                                                  for x in e.ticket_list)) if e.ticket_list else ""),
                 _d(e.date), sm(e.planned, e), sm(e.actual, e),
                 ("reçu par " if e.is_income else "") + S.payer_label(e, names).replace("l'asso", "asso"),
                 _receipt_codes(e) or "—"]
                for e in es]
        # sous-catégories : une ligne de titre avec son sous-total avant ses dépenses
        with_subs, cur = [], None
        for e, row in zip(es, rows):
            if grouped and (e.subcategory or "") != cur:
                cur = e.subcategory or ""
                grp = [x for x in es if (x.subcategory or "") == cur]
                with_subs.append([f"› {cur or 'Autres (sans sous-catégorie)'}", "", _m(sum((x.planned or 0) * (-1 if x.is_income else 1) for x in grp)),
                                  _m(sum((x.actual or 0) * (-1 if x.is_income else 1) for x in grp)), "sous-total", ""])
            with_subs.append(row)
        rows = with_subs
        pdf.table_(["Dépense", "Date", "Prévu", "Réel", "Payé par", "Justif."], rows,
                   [52, 20, 25, 25, 33, 25], ["LEFT", "LEFT", "RIGHT", "RIGHT", "LEFT", "LEFT"],
                   total=["Sous-total", "", _m(sum((e.planned or 0) * (-1 if e.is_income else 1) for e in es)),
                          _m(sum((e.actual or 0) * (-1 if e.is_income else 1) for e in es)), "", ""])
    pdf.para(f"**Total retenu pour le voyage : {_m(r.total_cost)}**", 10.5)

    # ---------------------------------------------------------------- page 5 mouvements
    pdf.add_page()
    pdf.h2("Les mouvements d'argent de l'asso", "Liste chronologique de ce qui est entré et sorti de la caisse.")
    rows = []
    for m in trip.movements:
        if m.kind == "versement":
            rows.append((m.date, f"Versement de {m.participant.name}", m.amount, None, m.note))
        else:
            rows.append((m.date, f"Remboursement à {m.participant.name}", None, m.amount, m.note))
    for e in trip.expenses:
        if e.actual is None:
            continue
        asso_part = sum(c for pid, c in e.splits() if pid is None)
        if not asso_part:
            continue
        codes = _receipt_codes(e)
        if e.is_income:
            rows.append((e.date, f"Reçu : {e.label}", asso_part, None, codes))
        else:
            part = " (part de l'asso)" if e.multi_payer else ""
            rows.append((e.date, f"Paiement : {e.label}{part}", None, asso_part, codes))
    rows.sort(key=lambda x: x[0] or dt.date.min)
    pdf.table_(["Date", "Opération", "Entrée", "Sortie", "Note / justif."],
               [[_d(d), l, _m(i) if i else "", _m(o) if o else "", n] for d, l, i, o, n in rows],
               [22, 70, 28, 28, 32], ["LEFT", "LEFT", "RIGHT", "RIGHT", "LEFT"],
               total=["", "Total", _m(cash_in), _m(cash_out), ""])
    adv = [a for a in S.advances(trip) if not a["income"]]
    exp_by_id = {e.id: e for e in trip.expenses}
    if adv:
        pdf.h2("Dépenses payées directement par des participants (avances)",
               "Ces sommes ne sont pas passées par la caisse de l'asso : elles sont déduites de ce que doit la personne.")
        pdf.table_(["Date", "Dépense", "Payé par", "Montant", "Justif."],
                   [[_d(a["date"]), a["label"] + (" (sa part)" if a["partial"] else ""), a["name"], _m(a["amount"]),
                     _receipt_codes(exp_by_id[a["eid"]]) or "—"]
                    for a in adv], [22, 70, 38, 28, 22], ["LEFT", "LEFT", "LEFT", "RIGHT", "LEFT"],
                   total=["", "Total", "", _m(sum(a["amount"] for a in adv)), ""])
    inc = [a for a in S.advances(trip) if a["income"]]
    if inc:
        pdf.h2("Argent reçu par des participants pour le compte de l'asso",
               "Remboursements ou recettes encaissés par un participant : ils s'ajoutent à ce qu'il doit à l'asso.")
        pdf.table_(["Date", "Recette", "Reçu par", "Montant", ""],
                   [[_d(a["date"]), a["label"], a["name"], _m(a["amount"]), ""] for a in inc],
                   [22, 70, 38, 28, 22], ["LEFT", "LEFT", "LEFT", "RIGHT", "LEFT"])

    # ---------------------------------------------------------------- compte de l'asso
    aname = S.caisse_label(trip)
    A = S.asso_account(trip, r)
    pdf.add_page()
    pdf.h1(f"Compte de {aname}")
    pdf.para("Argent réellement reçu (entrées) et payé (sorties) par l'asso pour ce voyage.", 9.5, color=MUTED)
    rows = []
    for g in A["groups"]:
        rows.append([g["name"].upper(), "", _m(g["cash_in"]) if g["cash_in"] else "", _m(g["cash_out"]) if g["cash_out"] else ""])
        for it in g["items"]:
            rows.append(["    " + it["label"] + (f" — {it['note']}" if it["note"] else ""), _d(it["date"]),
                         _m(it["cash_in"]) if it["cash_in"] else "", _m(it["cash_out"]) if it["cash_out"] else ""])
    if rows:
        pdf.table_(["Quoi", "Date", "Entrées", "Sorties"], rows, [104, 22, 27, 27], ["LEFT", "LEFT", "RIGHT", "RIGHT"],
                   total=["Total", "", _m(A["cash_in"]), _m(A["cash_out"])], size=7.5)
    fin = [["Dans la caisse aujourd'hui", _m(A["cash"])],
           ["+ Reste à recevoir des participants", _m(A["to_collect"])],
           ["− Reste à rendre aux participants", _m(A["to_refund"])]]
    if A["unpaid"]:
        fin.append(["− Dépenses de l'asso pas encore payées (prévu)", _m(A["unpaid_total"])])
    pdf.h2("Pour terminer le voyage")
    pdf.table_(["", "Montant"], fin, [130, 50], ["LEFT", "RIGHT"],
               total=["Une fois tout réglé, pour l'asso", ("+ " if A["final"] > 0 else "") + _m(A["final"])])
    if A["charges"]:
        pdf.h2("Ce que l'asso prend à sa charge")
        pdf.table_(["Quoi", "Pourquoi", "Montant"], [[c.label, c.reason, _m(c.cents)] for c in A["charges"]],
                   [50, 100, 30], ["LEFT", "LEFT", "RIGHT"], total=["Total", "", _m(A["charges_total"])], size=8)

    # ---------------------------------------------------------------- annexe fiches
    pdf.add_page()
    pdf.h1("Annexe — détail par participant")
    for p in sorted(trip.participants, key=lambda x: E.sort_key(x.name)):
        ps = r.people[p.id]
        if pdf.get_y() > 220:
            pdf.add_page()
        pdf.h2(p.name + (" (désisté)" if ps.withdrawn else ""))
        rows = []
        for l in sorted(r.lines_for(p.id), key=lambda l: (l.expense is None, l.category, l.label)):
            detail = " ; ".join(c.text + (f" ({_m(round(c.amount))})" if len(l.components) > 1 else "")
                                for c in l.components)
            rows.append([l.label, detail, _m(l.cents)])
        if rows and ps.rounding:
            rows.append(["Arrondi au centime", S.rounding_explain(r, p.id), ("+ " if ps.rounding > 0 else "− ") + _m(abs(ps.rounding))])
        if rows:
            pdf.table_(["Ce qu'il doit", "Calcul", "Montant"], rows, [40, 112, 28], ["LEFT", "LEFT", "RIGHT"],
                       total=["Total de sa part", "", _m(ps.share)], size=7.5)
        given = [[_d(m.date), "Versement", _m(m.amount)] for m in trip.movements
                 if m.participant_id == p.id and m.kind == "versement"]
        given += [[_d(a["date"]), f"Avance : {a['label']}", _m(a["amount"])] for a in S.advances(trip, p.id)
                  if not a["income"]]
        given += [[_d(a["date"]), f"Reçu pour l'asso : {a['label']}", "− " + _m(a["amount"])]
                  for a in S.advances(trip, p.id) if a["income"]]
        given += [[_d(m.date), "Remboursement reçu de l'asso", "− " + _m(m.amount)] for m in trip.movements
                  if m.participant_id == p.id and m.kind == "remboursement"]
        if given:
            pdf.table_(["Date", "Ce qu'il a déjà donné", "Montant"], given, [25, 127, 28],
                       ["LEFT", "LEFT", "RIGHT"], size=7.5)
        acc = r.account_of(p.id)
        if acc.is_family:
            pdf.para(f"**➜ {p.name} règle ses comptes avec la {acc.name} : {_status(acc)} pour toute la famille**", 9.5)
        else:
            pdf.para(f"**➜ {p.name} : {_status(ps)}**", 9.5)

    return bytes(pdf.output())


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:40] or "fichier"


def trip_zip(trip: M.Trip, mode: str, receipts_dir: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"Bilan_{_slug(trip.name)}.pdf", trip_pdf(trip, mode))
        index = ["Justificatifs du voyage : " + trip.name, ""]
        for rec in trip.receipts:
            src = receipts_dir / rec.stored_name
            labels = [l.expense.label for l in rec.links]
            ext = Path(rec.original_name).suffix.lower() or Path(rec.stored_name).suffix
            name = f"{rec.code}_{_slug(labels[0] if labels else Path(rec.original_name).stem)}{ext}"
            if src.exists():
                z.write(src, f"justificatifs/{name}")
            index.append(f"{rec.code}  {name}  →  " + (", ".join(labels) or "(non rattaché)"))
        z.writestr("justificatifs/LISEZ-MOI.txt", "\n".join(index) + "\n")
    return buf.getvalue()


def subaccount_pdf(trip: M.Trip, sub: M.SubAccount) -> bytes:
    v = S.sub_view(sub)
    names = {p.id: p.name for p in trip.participants}
    pdf = Doc(f"Entre nous — {sub.name}", f"{trip.name} · compte entre participants (hors asso)")
    pdf.add_page()
    pdf.h1(sub.name)
    pdf.para(f"Voyage : {trip.name}. Compte entre participants, sans lien avec la caisse de l'asso.", 9.5,
             color=MUTED)
    pdf.h2("Dépenses")
    pdf.table_(["Date", "Quoi", "Payé par", "Pour", "Montant"],
               [[_d(e.date), e.label, names.get(e.payer_id, "?"),
                 ", ".join(names.get(p, "?") for p in e.weight_map), _m(e.amount)] for e in sub.expenses],
               [20, 45, 30, 60, 25], ["LEFT", "LEFT", "LEFT", "LEFT", "RIGHT"],
               total=["", "Total", "", "", _m(v["total"])])
    pdf.h2("Où en est chacun")
    pdf.table_(["Nom", "A payé", "Sa part", "Situation"],
               [[names.get(m, "?"), _m(v["paid"].get(m, 0)), _m(v["shares"].get(m, 0)),
                 "à jour" if v["balances"].get(m, 0) == 0 else
                 (f"on lui doit {_m(v['balances'][m])}" if v["balances"][m] > 0
                  else f"doit {_m(-v['balances'][m])}")] for m in sub.members],
               [50, 40, 40, 50], ["LEFT", "RIGHT", "RIGHT", "LEFT"])
    pdf.h2("Qui doit quoi à qui")
    if v["settlements"]:
        for a, b, c in v["settlements"]:
            pdf.para(f"•  **{names.get(a)}** doit **{_m(c)}** à **{names.get(b)}**", 10.5)
    else:
        pdf.para("Tout le monde est quitte.", 10.5)
    return bytes(pdf.output())


# ---------------------------------------------------------------- compte d'une personne (ou d'une famille)
def account_pdf(trip: M.Trip, acc_key: str, mode: str = E.ACTUAL) -> tuple:
    """PDF « Son compte » d'une personne ou d'une famille, à envoyer à l'intéressé.
    -> (octets, nom du fichier). Détail par catégorie, sous-catégorie et dépense."""
    r = S.compute_trip(trip, mode)
    acc = next(a for a in r.accounts if a.key == acc_key)
    caisse = S.caisse_label(trip)
    pdf = Doc(f"{acc.name} — compte du voyage {trip.name}", trip.name)
    pdf.asso = caisse
    pdf.add_page()
    pdf.h1(f"Compte de {acc.name}")
    when = " → ".join(_d(x) for x in (trip.start, trip.end) if x)
    pdf.para(" · ".join(x for x in (trip.name, trip.place, when) if x), 10, color=MUTED)
    by_id = {p.id: p for p in trip.participants}
    for m in acc.members:                                  # ses contacts (ceux de chaque membre pour une famille)
        c = S.contact_text(by_id[m.id]) if m.id in by_id else ""
        if c:
            pdf.para(f"{m.name} : {c}" if acc.is_family else c, 9, color=MUTED)
    pdf.para(f"Calculé sur les {'montants réels' if mode == E.ACTUAL else 'montants prévus'}, le "
             f"{dt.date.today().strftime('%d/%m/%Y')}.", 8.5, color=MUTED)

    # ---- en bref
    pdf.h2("En bref")
    rows = [["Sa part du voyage" if not acc.is_family else "Part de la famille", _m(acc.share)]]
    if acc.paid:
        rows.append(["− Déjà versé à l'asso", _m(acc.paid)])
    if acc.advanced > 0:
        rows.append(["− Dépenses payées pour le groupe", _m(acc.advanced)])
    elif acc.advanced < 0:
        rows.append(["+ Argent reçu pour l'asso", _m(-acc.advanced)])
    if acc.refunded:
        rows.append(["+ Déjà rendu par l'asso", _m(acc.refunded)])
    b = acc.balance
    if acc.holds_cash:
        verdict = "Tient la caisse : son compte se règle avec elle"
    elif b > 0:
        verdict = f"Reste à verser à l'asso : {_m(b)}"
    elif b < 0:
        verdict = f"L'asso lui doit : {_m(-b)}"
    else:
        verdict = "Tout est réglé ✓"
    pdf.table_(["", "Montant"], rows, [130, 50], ["LEFT", "RIGHT"], total=[verdict, _m(abs(b))])
    how = S.pay_info(trip)
    if how and S.owes_money(acc):                          # consignes de l'organisateur : RIB, chèque, lien…
        pdf.h2("Comment payer")
        for line in how.split("\n"):
            pdf.para(line or " ", 10)

    # ---- détail par membre, par catégorie et sous-catégorie
    for m in acc.members:
        A = S.person_account(trip, r, m.id)
        if pdf.get_y() > 230:
            pdf.add_page()
        pdf.h2(f"Détail : {m.name}" if acc.is_family else "Le détail",
               "« Il doit » : sa part, l'argent reçu pour l'asso ou rendu par l'asso. "
               "« Il a donné » : ses versements, les dépenses qu'il a payées. « Reste » : la différence.")
        rows = []
        for g in A["groups"]:
            if g["kind"] == "arrondi":
                rows.append(["Arrondi au centime", g["explain"], ("+ " if g["debit"] > 0 else "− ") + _m(abs(g["debit"])), "", ""])
                continue
            pct = f" ({g['pct']} % de sa part)" if g.get("pct") is not None else ""
            rows.append([g["name"].upper() + pct, "", _m(g["debit"]) if g["debit"] else "",
                         _m(g["credit"]) if g["credit"] else "",
                         _solde(g["debit"] - g["credit"]) if g["kind"] in ("cat", "global") else ""])
            cur = None
            for it in g["items"]:
                if it.get("sub") and it["sub"] != cur:
                    cur = it["sub"]
                    st = g["subs"][cur]
                    rows.append([f"   › {cur} — sous-total", "",
                                 _m(st["debit"]) if st["debit"] else "", _m(st["credit"]) if st["credit"] else "",
                                 _solde(st["debit"] - st["credit"])])
                calc = ""
                if it.get("line") is not None and it["line"].components:
                    calc = " ; ".join(c.text for c in it["line"].components)
                indent = "         " if it.get("sub") else "      "
                rows.append([indent + it["label"] + (" (≈ prévu)" if it.get("est") else "")
                             + (f" · {it['note']}" if it.get("note") else ""), calc,
                             _m(it["debit"]) if it["debit"] else "", _m(it["credit"]) if it["credit"] else "", ""])
        if rows:
            pdf.table_(["Quoi", "Calcul", "Il doit", "Il a donné", "Reste"], rows, [58, 52, 21, 21, 28],
                       ["LEFT", "LEFT", "RIGHT", "RIGHT", "RIGHT"],
                       total=["Total", "", _m(A["debit"]), _m(A["credit"]), _solde(A["debit"] - A["credit"])], size=7.5)
    if trip.note:
        pdf.para(trip.note, 8.5, color=MUTED)
    return bytes(pdf.output()), f"Compte_{_slug(acc.name)}_{_slug(trip.name)}.pdf"


def accounts_zip(trip: M.Trip, mode: str = E.ACTUAL) -> bytes:
    """Tous les PDF « Son compte » (un par personne ou famille), dans un ZIP."""
    r = S.compute_trip(trip, mode)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for a in r.accounts:
            data, name = account_pdf(trip, a.key, mode)
            z.writestr(name, data)
    return buf.getvalue()
