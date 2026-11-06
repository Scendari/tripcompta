"""Exporter / importer des voyages d'une installation à une autre.

Un fichier .zip contient `voyages.json` (tout le voyage, colonne par colonne, et les réglages utiles) et le dossier
`justificatifs/` (photos et PDF). À l'import, chaque voyage est AJOUTÉ comme un nouveau voyage : rien n'est jamais
écrasé ; les identifiants sont renumérotés. Les réglages (nom de l'asso, catégories) ne remplissent que ce qui manque.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import uuid
import zipfile
from pathlib import Path

from sqlalchemy import Date, DateTime, inspect

from . import models as M
from . import services as S

FORMAT = "tripcompta"
VERSION = 1
SETTING_KEYS = (S.ASSO_NAME_KEY, S.DEFAULTS_KEY, S.LIBRARY_KEY, S.SUBCATS_KEY)


class TransferError(ValueError):
    pass


# ---------------------------------------------------------------- export

def _row(obj) -> dict:
    out = {}
    for col in inspect(type(obj)).columns:
        v = getattr(obj, col.key)
        if isinstance(v, (dt.date, dt.datetime)):
            v = v.isoformat()
        out[col.key] = v
    return out


def _asso(t: M.Trip) -> str:
    """Nom de l'asso tel qu'il s'affiche dans ce voyage (vide : « l'asso »)."""
    name = S.trip_asso_name(t)
    return "" if name == "l'asso" else name


def _expense_rows(t: M.Trip, expenses) -> list:
    """Les dépenses, avec le tirage des centimes d'arrondi tel que le calcul l'utilise (même s'il n'est pas
    encore enregistré) : le voyage importé donne exactement les mêmes montants."""
    r = S.compute_trip(t)
    rows = []
    for x in expenses:
        row = _row(x)
        inf = r.cent_info.get(x.id)
        if inf and inf["winners"]:
            row["cents_who"] = ",".join(map(str, inf["winners"]))
        rows.append(row)
    return rows


def trip_data(t: M.Trip) -> dict:
    expenses = list(t.expenses)
    return dict(
        voyage=dict(_row(t), asso_name=_asso(t)),
        participants=[_row(x) for x in t.participants],
        familles=[_row(x) for x in t.families],
        categories=[_row(x) for x in t.categories],
        depenses=_expense_rows(t, expenses),
        parts=[_row(x) for e in expenses for x in e.shares],
        paiements=[_row(x) for e in expenses for x in e.payments],
        ajustements=[_row(x) for x in t.adjustments],
        desistements=[_row(x) for x in t.withdrawals],
        mouvements=[_row(x) for x in t.movements],
        justificatifs=[_row(x) for x in t.receipts],
        liens_justificatifs=[_row(x) for r in t.receipts for x in r.links],
        entre_nous=[_row(x) for x in t.subaccounts],
        entre_nous_depenses=[_row(x) for a in t.subaccounts for x in a.expenses],
        entre_nous_virements=[_row(x) for a in t.subaccounts for x in a.transfers],
    )


def settings_data(s) -> dict:
    out = {}
    for k in SETTING_KEYS:
        if k == S.ASSO_NAME_KEY:     # le nom de l'asso est dans chaque voyage : rien d'autre ne sort de la base
            continue
        row = s.get(M.Setting, k)
        if row is not None and (row.value or "").strip():
            out[k] = row.value
    return out


def export_zip(s, trips: list, receipts_dir: Path) -> bytes:
    data = dict(format=FORMAT, version=VERSION, exporte_le=dt.datetime.now().isoformat(timespec="seconds"),
                reglages=settings_data(s), voyages=[trip_data(t) for t in trips])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("voyages.json", json.dumps(data, ensure_ascii=False, indent=1))
        z.writestr("LISEZ-MOI.txt", "Export de Trip Compta.\nPour le reprendre sur un autre ordinateur : "
                                    "page « Mes voyages » → « Importer un voyage », et choisis ce fichier .zip.\n")
        for t in trips:
            for r in t.receipts:
                f = receipts_dir / r.stored_name
                if f.exists():
                    z.write(f, f"justificatifs/{r.stored_name}")
    return buf.getvalue()


# ---------------------------------------------------------------- lecture

def read_zip(content: bytes) -> tuple[dict, zipfile.ZipFile]:
    try:
        z = zipfile.ZipFile(io.BytesIO(content))
        data = json.loads(z.read("voyages.json").decode("utf-8"))
    except (zipfile.BadZipFile, KeyError, ValueError):
        raise TransferError("Ce fichier n'est pas un export de Trip Compta (il faut le fichier .zip téléchargé "
                            "avec « Exporter le voyage »).")
    if data.get("format") != FORMAT or not isinstance(data.get("voyages"), list):
        raise TransferError("Ce fichier n'est pas un export de Trip Compta.")
    if int(data.get("version") or 0) > VERSION:
        raise TransferError("Cet export vient d'une version plus récente de l'application : mets-la à jour d'abord.")
    return data, z


def summary(data: dict) -> list:
    out = []
    for v in data["voyages"]:
        t = v["voyage"]
        fr = lambda d: dt.date.fromisoformat(d).strftime("%d/%m/%Y") if d else ""
        out.append(dict(nom=t.get("name"), lieu=t.get("place") or "", debut=fr(t.get("start")), fin=fr(t.get("end")),
                        participants=len(v.get("participants") or []), depenses=len(v.get("depenses") or []),
                        mouvements=len(v.get("mouvements") or []), justificatifs=len(v.get("justificatifs") or [])))
    return out


# ---------------------------------------------------------------- import

def _new(model, row: dict, **override):
    """Crée un objet à partir d'une ligne exportée : colonnes connues seulement (une version plus ancienne
    de l'application peut ne pas les avoir toutes), dates converties, identifiant laissé à la base."""
    cols = {c.key: c for c in inspect(model).columns}
    kw = {}
    for k, v in row.items():
        if k not in cols or k in override or cols[k].primary_key and k == "id":
            continue
        if v is not None and isinstance(cols[k].type, DateTime):
            v = dt.datetime.fromisoformat(v)
        elif v is not None and isinstance(cols[k].type, Date):
            v = dt.date.fromisoformat(v)
        kw[k] = v
    kw.update(override)
    return model(**kw)


def _ids(csv: str, m: dict) -> str:
    return ",".join(str(m[int(x)]) for x in (csv or "").split(",") if x.strip().isdigit() and int(x) in m)


def import_trip(s, v: dict, z: zipfile.ZipFile, receipts_dir: Path) -> M.Trip:
    names = {t.name for t in s.query(M.Trip).all()}
    tv = dict(v["voyage"])
    name = tv.get("name") or "Voyage importé"
    if name in names:
        base, i = f"{name} (importé)", 2
        name = base
        while name in names:
            name, i = f"{base} {i}", i + 1
    t = _new(M.Trip, tv, name=name, cashier_id=None, groups="", created=M.now())   # importé = créé ici, maintenant
    s.add(t)
    s.flush()
    P, F, C, E_, R, A = {}, {}, {}, {}, {}, {}
    for row in v.get("participants") or []:
        p = _new(M.Participant, row, trip_id=t.id, family_id=None)
        s.add(p)
        s.flush()
        P[row["id"]] = p.id
    for row in v.get("familles") or []:
        f = _new(M.Family, row, trip_id=t.id, referent_id=P.get(row.get("referent_id")))
        s.add(f)
        s.flush()
        F[row["id"]] = f.id
    for row in v.get("participants") or []:
        if row.get("family_id") in F:
            s.get(M.Participant, P[row["id"]]).family_id = F[row["family_id"]]
    t.cashier_id = P.get(tv.get("cashier_id"))
    try:
        groups = json.loads(tv.get("groups") or "[]")
        for g in groups:
            g["members"] = [P[i] for i in g.get("members", []) if i in P]
        t.groups = json.dumps(groups, ensure_ascii=False) if groups else ""
    except (ValueError, TypeError, AttributeError):
        t.groups = ""
    for row in v.get("categories") or []:
        c = _new(M.Category, row, trip_id=t.id)
        s.add(c)
        s.flush()
        C[row["id"]] = c.id
    for row in v.get("depenses") or []:
        e = _new(M.Expense, row, trip_id=t.id, category_id=C.get(row.get("category_id")),
                 payer_id=P.get(row.get("payer_id")), refund_to_id=P.get(row.get("refund_to_id")),
                 cents_who=_ids(row.get("cents_who"), P))
        s.add(e)
        s.flush()
        E_[row["id"]] = e.id
    for row in v.get("parts") or []:
        if row.get("expense_id") in E_ and row.get("participant_id") in P:
            s.add(_new(M.ExpenseShare, row, expense_id=E_[row["expense_id"]], participant_id=P[row["participant_id"]]))
    for row in v.get("paiements") or []:
        if row.get("expense_id") in E_:
            s.add(_new(M.ExpensePayment, row, expense_id=E_[row["expense_id"]], payer_id=P.get(row.get("payer_id"))))
    for row in v.get("ajustements") or []:
        if row.get("participant_id") in P:
            s.add(_new(M.Adjustment, row, trip_id=t.id, participant_id=P[row["participant_id"]],
                       expense_id=E_.get(row.get("expense_id")), compensator_id=P.get(row.get("compensator_id"))))
    s.flush()
    s.expire(t, ["participants"])
    S.convert_legacy_dp(t)                     # export d'avant : l'ancien rôle « DP » devient une étiquette
    for row in v.get("desistements") or []:
        if row.get("participant_id") in P:
            s.add(_new(M.Withdrawal, row, trip_id=t.id, participant_id=P[row["participant_id"]],
                       kept=_ids(row.get("kept"), E_)))
    for row in v.get("mouvements") or []:
        if row.get("participant_id") in P:
            s.add(_new(M.Movement, row, trip_id=t.id, participant_id=P[row["participant_id"]]))
    files = set(z.namelist())
    for row in v.get("justificatifs") or []:
        old = row.get("stored_name") or ""
        stored = f"{t.id}_{uuid.uuid4().hex}{Path(old).suffix.lower()[:10]}"
        if f"justificatifs/{old}" in files:
            (receipts_dir / stored).write_bytes(z.read(f"justificatifs/{old}"))
        r = _new(M.Receipt, row, trip_id=t.id, stored_name=stored)
        s.add(r)
        s.flush()
        R[row["id"]] = r.id
    for row in v.get("liens_justificatifs") or []:
        if row.get("receipt_id") in R and row.get("expense_id") in E_:
            s.add(M.ReceiptLink(receipt_id=R[row["receipt_id"]], expense_id=E_[row["expense_id"]]))
    for row in v.get("entre_nous") or []:
        a = _new(M.SubAccount, row, trip_id=t.id, member_ids=_ids(row.get("member_ids"), P))
        s.add(a)
        s.flush()
        A[row["id"]] = a.id
    for row in v.get("entre_nous_depenses") or []:
        if row.get("sub_id") in A and row.get("payer_id") in P:
            w = ",".join(f"{P[int(k)]}:{x}" for k, x in (p.split(":") for p in (row.get("weights") or "").split(",")
                                                        if ":" in p) if k.strip().isdigit() and int(k) in P)
            s.add(_new(M.SubExpense, row, sub_id=A[row["sub_id"]], payer_id=P[row["payer_id"]], weights=w))
    for row in v.get("entre_nous_virements") or []:
        if row.get("sub_id") in A and row.get("sender_id") in P and row.get("receiver_id") in P:
            s.add(_new(M.SubTransfer, row, sub_id=A[row["sub_id"]], sender_id=P[row["sender_id"]],
                       receiver_id=P[row["receiver_id"]]))
    s.flush()
    return t


def merge_settings(s, reglages: dict) -> list:
    """Ne remplit que ce qui manque ; les catégories et sous-catégories sont ajoutées à celles déjà là."""
    done = []
    for k, val in (reglages or {}).items():
        if k not in SETTING_KEYS or not str(val).strip():
            continue
        row = s.get(M.Setting, k)
        if row is None or not (row.value or "").strip():
            if row is None:
                s.add(M.Setting(key=k, value=val))
            else:
                row.value = val
            done.append(k)
            continue
        if k == S.LIBRARY_KEY:
            try:
                have, new = json.loads(row.value), json.loads(val)
                add = [x for x in new if x not in have]
                if add:
                    row.value = json.dumps(have + add, ensure_ascii=False)
                    done.append(k)
            except (ValueError, TypeError):
                pass
        elif k == S.SUBCATS_KEY:
            try:
                have, new = json.loads(row.value), json.loads(val)
                changed = False
                for cat, subs in new.items():
                    cur = have.setdefault(cat, [])
                    for x in subs:
                        if x not in cur:
                            cur.append(x)
                            changed = True
                if changed:
                    row.value = json.dumps(have, ensure_ascii=False)
                    done.append(k)
            except (ValueError, TypeError, AttributeError):
                pass
    s.flush()
    return done


EXAMPLE_FILE = Path(__file__).resolve().parent / "exemple" / "hendaye-2026-demo.zip"
EXAMPLE_NAME = "Hendaye 2026 – démo"


def import_example(s, receipts_dir: Path) -> M.Trip:
    """Le voyage d'exemple (Hendaye 2026, entièrement fictif, avec ses justificatifs), marqué « démo ». Les réglages
    de l'utilisateur ne sont pas touchés (pas de fusion des catégories de l'exemple)."""
    data, z = read_zip(EXAMPLE_FILE.read_bytes())
    v = data["voyages"][0]
    v["voyage"] = dict(v["voyage"], name=EXAMPLE_NAME)
    t = import_trip(s, v, z, receipts_dir)
    t.is_demo = True
    s.flush()
    return t


def import_zip(s, content: bytes, receipts_dir: Path) -> tuple[list, list]:
    data, z = read_zip(content)
    old_name = str((data.get("reglages") or {}).get(S.ASSO_NAME_KEY) or "").strip()[:100]
    for v in data["voyages"]:    # export d'une version plus ancienne : le nom commun devient celui du voyage
        if old_name and not str((v.get("voyage") or {}).get("asso_name") or "").strip():
            v["voyage"] = dict(v["voyage"], asso_name=old_name)
    trips = [import_trip(s, v, z, receipts_dir) for v in data["voyages"]]
    done = merge_settings(s, data.get("reglages") or {})
    named = [t.asso_name for t in trips if (t.asso_name or "").strip()]
    if named and S.asso_name(s) == "l'asso" and S.ASSO_NAME_KEY not in done:   # rien ici : nom proposé
        S.set_asso_name(s, named[-1])
        s.flush()
        done.append(S.ASSO_NAME_KEY)
    return trips, done
