"""Export tableur : un classeur .xlsx (Excel, LibreOffice, Numbers, Google Sheets) avec trois feuilles :
« Dépenses », « Argent reçu et rendu » et « Qui paie quoi ».

Le fichier est écrit directement (un .xlsx est une archive de petits fichiers XML) : pas de bibliothèque de plus à
embarquer. Les montants sont de vrais nombres (en euros, deux décimales), les dates de vraies dates : on peut
trier, filtrer et additionner dans le tableur. Les totaux sont ceux de Trip Compta, au centime près.
"""
from __future__ import annotations

import datetime as dt
import io
import zipfile
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

from . import engine as E
from . import models as M
from . import services as S


@dataclass
class Money:
    cents: int


@dataclass
class Bold:
    """Cellule en gras (texte ou montant)."""
    value: object


@dataclass
class Sheet:
    name: str
    rows: list                               # lignes de cellules : str, int, Money, date, None, Bold(...)
    header_rows: int = 1                     # lignes d'en-tête (en gras, figées)
    frozen_cols: int = 0                     # colonnes figées à gauche
    widths: dict = field(default_factory=dict)   # largeur imposée par colonne (index → caractères)


# styles (index dans cellXfs, voir _STYLES) : 0 normal, 1 gras, 2 montant, 3 montant gras, 4 date, 5 en-tête
NORMAL, BOLD, MONEY, MONEY_BOLD, DATE, HEAD = range(6)

_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="1"><numFmt numFmtId="164" formatCode="#,##0.00\\ &quot;€&quot;"/></numFmts>
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFE8F1EE"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="6">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>
<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="164" fontId="1" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyFont="1"/>
<xf numFmtId="14" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def col_letter(i: int) -> str:
    """0 → A, 25 → Z, 26 → AA…"""
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def euros(cents: int) -> str:
    """Centimes → « 12.34 » (exact, sans passer par un nombre à virgule)."""
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"


def _clean(text: str) -> str:
    """Texte accepté par le XML (les caractères de contrôle ne le sont pas)."""
    return "".join(ch for ch in str(text) if ch in "\t\n" or ord(ch) >= 32)


def _cell(ref: str, v, head: bool) -> str:
    bold = isinstance(v, Bold)
    if bold:
        v = v.value
    if v is None or v == "":
        return f'<c r="{ref}" s="{HEAD}"/>' if head else ""
    if isinstance(v, Money):
        return f'<c r="{ref}" s="{MONEY_BOLD if bold or head else MONEY}"><v>{euros(v.cents)}</v></c>'
    if isinstance(v, bool):
        v = "oui" if v else ""
    if isinstance(v, int):
        return f'<c r="{ref}" s="{BOLD if bold or head else NORMAL}"><v>{v}</v></c>'
    if isinstance(v, dt.date):
        return f'<c r="{ref}" s="{DATE}"><v>{(v - dt.date(1899, 12, 30)).days}</v></c>'
    style = HEAD if head else BOLD if bold else NORMAL
    return (f'<c r="{ref}" t="inlineStr" s="{style}"><is><t xml:space="preserve">{escape(_clean(v))}</t></is></c>')


def _width(v) -> int:
    if isinstance(v, Bold):
        v = v.value
    if isinstance(v, Money):
        return len(E.fmt(v.cents)) + 2
    if isinstance(v, dt.date):
        return 11
    return max((len(x) for x in str(v if v is not None else "").split("\n")), default=0)


def _sheet_xml(sh: Sheet) -> str:
    ncols = max((len(r) for r in sh.rows), default=1)
    widths = []
    for c in range(ncols):
        if c in sh.widths:
            widths.append(sh.widths[c])
            continue
        body = max((_width(r[c]) for r in sh.rows[sh.header_rows:] if c < len(r)), default=0)
        head = max((_width(r[c]) for r in sh.rows[:sh.header_rows] if c < len(r)), default=0)
        widths.append(min(max(body + 2, min(head + 2, 16), 8), 48))
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">']
    if sh.header_rows or sh.frozen_cols:
        attrs = ""
        if sh.frozen_cols:
            attrs += f' xSplit="{sh.frozen_cols}"'
        if sh.header_rows:
            attrs += f' ySplit="{sh.header_rows}"'
        pane = "bottomRight" if sh.frozen_cols and sh.header_rows else "bottomLeft" if sh.header_rows else "topRight"
        out.append(f'<sheetViews><sheetView workbookViewId="0"><pane{attrs} '
                   f'topLeftCell="{col_letter(sh.frozen_cols)}{sh.header_rows + 1}" activePane="{pane}" '
                   f'state="frozen"/></sheetView></sheetViews>')
    out.append("<cols>" + "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>'
                                  for i, w in enumerate(widths)) + "</cols>")
    out.append("<sheetData>")
    for n, row in enumerate(sh.rows, start=1):
        head = n <= sh.header_rows
        cells = "".join(_cell(f"{col_letter(c)}{n}", v, head) for c, v in enumerate(row))
        out.append(f'<row r="{n}">{cells}</row>')
    out.append("</sheetData></worksheet>")
    return "".join(out)


def _sheet_name(name: str, used: set) -> str:
    """Nom de feuille accepté par Excel : 31 caractères au plus, sans : \\ / ? * [ ]"""
    name = "".join("-" if ch in ":\\/?*[]" else ch for ch in name).strip("'")[:31] or "Feuille"
    base, n = name, 2
    while name.lower() in used:
        name = f"{base[:28]} {n}"
        n += 1
    used.add(name.lower())
    return name


def xlsx(sheets: list, title: str = "") -> bytes:
    """Le classeur, en octets."""
    used = set()
    names = [_sheet_name(sh.name, used) for sh in sheets]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
                   + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                             for i in range(1, len(sheets) + 1)) + "</Types>")
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                   '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
                   "</Relationships>")
        z.writestr("docProps/core.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                   'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
                   'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
                   f"<dc:title>{escape(_clean(title))}</dc:title><dc:creator>Trip Compta</dc:creator>"
                   f'<dcterms:created xsi:type="dcterms:W3CDTF">{dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ}</dcterms:created>'
                   "</cp:coreProperties>")
        z.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   + "".join(f'<sheet name="{escape(_clean(n), {chr(34): "&quot;"})}" sheetId="{i}" r:id="rId{i}"/>'
                             for i, n in enumerate(names, start=1)) + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
                             for i in range(1, len(sheets) + 1))
                   + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                   "</Relationships>")
        z.writestr("xl/styles.xml", _STYLES)
        for i, sh in enumerate(sheets, start=1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(sh))
    return buf.getvalue()


# ---------------------------------------------------------------------------- les trois feuilles d'un voyage
def _ordered_expenses(trip: M.Trip) -> list:
    """Les dépenses dans l'ordre de « Qui paie quoi » : par catégorie, sous-catégorie, puis ordre de saisie."""
    order = {c.id: i for i, c in enumerate(trip.categories)}
    return sorted(trip.expenses, key=lambda e: (order.get(e.category_id, len(order)), not e.subcategory,
                                                E.sort_key(e.subcategory or ""), e.id))


def expenses_sheet(trip: M.Trip, r: E.Result, caisse: str) -> Sheet:
    names = {p.id: p.name for p in trip.participants}
    es = _ordered_expenses(trip)
    with_income = any(e.is_income for e in es)
    with_refund = any(e.refund for e in es)
    head = ["Catégorie", "Sous-catégorie", "Dépense"] + (["Type"] if with_income else []) + \
           ["Date", "Montant prévu", "Montant payé"] + (["Dont remboursé par le fournisseur"] if with_refund else []) + \
           ["Compté dans les comptes", "Payé par", "Partagé entre (personnes)", "Justificatifs", "Note"]
    rows = [head]
    tot_planned = tot_actual = tot_refund = tot_counted = 0
    for e in es:
        counted = r.expense_totals.get(e.id, 0)
        sign = -1 if e.is_income else 1
        people = sum(1 for (pid, k) in r.lines if k == e.id)
        codes = ", ".join(sorted(l.receipt.code for l in e.receipt_links))
        row = [e.category.name if e.category else "", e.subcategory or "", e.label]
        if with_income:
            row.append("Recette" if e.is_income else "Dépense")
        row += [e.date, Money(e.planned) if e.planned is not None else None,
                Money(e.actual) if e.actual is not None else None]
        if with_refund:
            row.append(Money(e.refund) if e.refund else None)
        row += [Money(counted), S.payer_label(e, names, caisse), people, codes, " ".join((e.note or "").split())]
        rows.append(row)
        tot_planned += sign * (e.planned or 0)
        tot_actual += sign * (e.actual or 0)
        tot_refund += e.refund or 0
        tot_counted += counted
    total = [Bold("Total" + (" (recettes déduites)" if with_income else "")), None, None] + ([None] if with_income else []) + \
            [None, Bold(Money(tot_planned)), Bold(Money(tot_actual))] + ([Bold(Money(tot_refund))] if with_refund else []) + \
            [Bold(Money(tot_counted))]
    rows.append(total)
    return Sheet("Dépenses", rows, widths={len(head) - 1: 40})


def money_sheet(trip: M.Trip, caisse: str) -> Sheet:
    rows = [["Date", "Personne", f"Reçu par {caisse}", f"Rendu par {caisse}", "Note"]]
    received = given = 0
    moves = sorted(trip.movements, key=lambda m: (m.date is None, m.date or dt.date.min, m.id))
    for m in moves:
        is_in = m.kind == "versement"
        rows.append([m.date, m.participant.name if m.participant else "?", Money(m.amount) if is_in else None,
                     None if is_in else Money(m.amount), " ".join((m.note or "").split())])
        received += m.amount if is_in else 0
        given += 0 if is_in else m.amount
    rows.append([Bold("Total"), None, Bold(Money(received)), Bold(Money(given))])
    rows.append([Bold("Reçu moins rendu"), None, Bold(Money(received - given))])
    return Sheet("Argent reçu et rendu", rows, widths={4: 40})


def matrix_sheet(trip: M.Trip, r: E.Result, caisse: str) -> Sheet:
    es = _ordered_expenses(trip)
    has_global = any(k == E.GLOBAL for (_pid, k) in r.lines) or any(c.expense is None for c in r.asso_charges)
    has_round = any(ps.rounding for ps in r.people.values())
    head1 = [""] + [e.category.name if e.category else "" for e in es]
    head2 = ["Participant"] + [e.label for e in es]
    if has_global:
        head1.append("")
        head2.append("Remise ou supplément")
    if has_round:
        head1.append("")
        head2.append("Arrondi")
    head1 += ["", "", ""]
    head2 += ["Part totale", "Déjà réglé", "Reste à régler (négatif = à lui rendre)"]
    rows = [head1, head2]
    people = sorted(r.people.values(), key=lambda ps: ps.sort_name)
    col_tot = [0] * len(es)
    g_tot = round_tot = share_tot = paid_tot = bal_tot = 0
    for ps in people:
        row = [ps.name + (" (désisté)" if ps.withdrawn else "")]
        for i, e in enumerate(es):
            c = r.share(ps.id, e.id)
            col_tot[i] += c
            row.append(Money(c) if (ps.id, e.id) in r.lines else None)
        if has_global:
            g = r.share(ps.id, E.GLOBAL)
            g_tot += g
            row.append(Money(g) if (ps.id, E.GLOBAL) in r.lines else None)
        if has_round:
            round_tot += ps.rounding
            row.append(Money(ps.rounding) if ps.rounding else None)
        settled = ps.paid + ps.advanced - ps.refunded
        row += [Bold(Money(ps.share)), Money(settled), Bold(Money(ps.balance))]
        share_tot += ps.share
        paid_tot += settled
        bal_tot += ps.balance
        rows.append(row)
    extra = ([Bold(Money(g_tot))] if has_global else []) + ([Bold(Money(round_tot))] if has_round else [])
    rows.append([Bold("Total des participants")] + [Bold(Money(c)) for c in col_tot] + extra +
                [Bold(Money(share_tot)), Bold(Money(paid_tot)), Bold(Money(bal_tot))])
    charges = {}
    for c in r.asso_charges:
        charges[c.expense] = charges.get(c.expense, 0) + c.cents
    if charges:
        rows.append([f"Pris en charge par {caisse}"] + [Money(charges[e.id]) if charges.get(e.id) else None for e in es]
                    + ([Money(charges[None]) if charges.get(None) else None] if has_global else [])
                    + ([None] if has_round else []) + [Money(sum(charges.values()))])
    rows.append(["Montant de la dépense"] + [Money(r.expense_totals.get(e.id, 0)) for e in es]
                + ([None] if has_global else []) + ([None] if has_round else [])
                + [Money(sum(r.expense_totals.get(e.id, 0) for e in es))])
    return Sheet("Qui paie quoi", rows, header_rows=2, frozen_cols=1)


def trip_workbook(trip: M.Trip, s=None) -> bytes:
    """Le classeur d'un voyage : dépenses, argent reçu et rendu, qui paie quoi."""
    r = S.compute_trip(trip)
    caisse = S.caisse_label(trip, s)
    return xlsx([expenses_sheet(trip, r, caisse), money_sheet(trip, caisse), matrix_sheet(trip, r, caisse)],
                title=trip.name)
