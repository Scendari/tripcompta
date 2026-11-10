"""Import d'un voyage complet décrit en JSON (préparé par n'importe quelle IA à partir du guide
`static/ia/guide_ia.md`). Tout est appliqué dans une seule transaction : simulable, et annulable d'un coup.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import models as M
from . import engine as E
from . import services as S
from .ops import (GROUPS, FixedShare, OpsError, do_add_expense, do_add_movement, do_add_participants, do_adjust,
                  do_create_subaccount, do_fix_share,
                  do_create_trip, do_add_subexpense, do_delete_expense, do_delete_movement, do_update_expense,
                  do_set_pay_dates, do_update_participant, do_update_trip, do_withdraw, trip_detail)

IA_DIR = Path(__file__).parent / "static" / "ia"
GUIDE_FILE = IA_DIR / "guide_ia.md"
TEMPLATE_FILE = IA_DIR / "modele_initialisation.txt"      # voyage qui démarre
UPDATE_TEMPLATE_FILE = IA_DIR / "modele_ajustement.txt"   # voyage déjà rempli : « qu'est-ce qui a changé ? »

KNOWN = {"voyage_id", "voyage", "groupes", "participants", "participants_a_modifier", "familles", "depenses",
         "depenses_a_modifier", "depenses_a_supprimer", "versements", "versements_a_supprimer", "ajustements",
         "remises", "desistements", "entre_nous", "a_verifier", "dates_paiement"}


def _apply_fixed(s, t, e, fixe) -> str:
    """« fixe » d'une dépense : {"Juliette": "60"} → montants fixés 🔒, le reste partagé entre les autres."""
    if not fixe:
        return ""
    if not isinstance(fixe, dict):
        raise OpsError("« fixe » doit être de la forme {\"Juliette\": \"60\"}.")
    done = []
    for ref, val in fixe.items():
        p, _, c = do_fix_share(s, t, ref, e, val)
        if c is not None:
            done.append(f"{p.name} {E.fmt(c)}")
    return (" — montant fixé : " + ", ".join(done)) if done else ""


class ImportErrors(OpsError):
    def __init__(self, errors: list):
        super().__init__("\n".join(errors))
        self.errors = errors


def guide() -> str:
    return GUIDE_FILE.read_text(encoding="utf-8")


def template() -> str:
    return TEMPLATE_FILE.read_text(encoding="utf-8")


def update_template() -> str:
    return UPDATE_TEMPLATE_FILE.read_text(encoding="utf-8")


def parse(text: str) -> dict:
    """Accepte la réponse brute de l'IA : on prend le bloc ```json``` s'il y en a un, sinon le premier {…}."""
    if isinstance(text, dict):
        return text
    text = (text or "").strip()
    if not text:
        raise OpsError("Colle d'abord la réponse de l'IA (le bloc qui commence par { ).")
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = blocks[-1] if blocks else text[text.find("{"): text.rfind("}") + 1]
    if not candidate:
        raise OpsError("Je ne trouve pas de bloc JSON dans ce texte. Demande à l'IA de donner le résultat "
                       "« au format JSON du guide ».")
    candidate = candidate.replace("“", '"').replace("”", '"')
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as e:
        raise OpsError(f"Le JSON est mal formé (ligne {e.lineno}, colonne {e.colno} : {e.msg}). "
                       "Demande à l'IA de le corriger et de le redonner en entier.")
    if not isinstance(data, dict):
        raise OpsError("Le JSON doit commencer par { et contenir au moins « voyage » et « participants ».")
    return data


def _g(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _list(data, key) -> list:
    v = data.get(key) or []
    if not isinstance(v, list):
        raise ImportErrors([f"« {key} » doit être une liste [ … ]."])
    return v


def _name(x) -> str:
    return str(x.get("libelle") or x.get("participant") or x.get("nom") or "") if isinstance(x, dict) else str(x)


def apply_team(s, t: M.Trip, v: dict, errors: list):
    """« voyage » : organisateur / co_organisateurs (et l'ancien « dp », devenu l'étiquette « DP »), s'ils font
    partie des participants."""
    from . import services as S
    from .ops import person
    wanted = [(S.ORGANIZER, v.get("organisateur")), (S.CO_ORGANIZER, v.get("co_organisateurs")),
              ("dp", v.get("dp"))]
    for role, refs in wanted:
        if not refs:
            continue
        for ref in (refs if isinstance(refs, list) else [refs]):
            try:
                p = person(t, ref)
            except OpsError as e:
                if role == S.ORGANIZER and "ambigu" not in str(e):
                    continue      # organisateur qui ne participe pas : reste un simple nom pour le voyage
                errors.append(f"Voyage, {S.ROLES[role].lower() if role in S.ROLES else 'DP'} « {ref} » : {e}")
                continue
            S.set_roles(t, p, p.role_list + S.parse_roles([role], t))
    s.flush()


def set_referent(s, t: M.Trip, d: dict):
    from . import services as S
    from .ops import person
    fam = S.find_family(t, str(d.get("nom") or ""))
    if fam is None:
        raise OpsError(f"famille inconnue. Indique « famille » sur chacun de ses membres dans « participants ». "
                       f"Familles : {', '.join(f.name for f in t.families) or 'aucune'}")
    if d.get("paie"):
        p = person(t, d["paie"], "personne qui paie pour la famille")
        if p.family_id != fam.id:
            raise OpsError(f"{p.name} ne fait pas partie de la famille {fam.name}.")
        fam.referent_id = p.id
    s.flush()


def run_import(s, data, t: M.Trip | None, expected_id=None, require_id: bool = True) -> dict:
    """t = None : crée un nouveau voyage. Sinon, applique les changements au voyage t.
    expected_id : si donné, le JSON doit porter le même « voyage_id » (garde-fou contre un mauvais collage)."""
    data = parse(data)
    if S.is_closed(t):
        raise ImportErrors([S.CLOSED_MSG])
    groups = data.get("groupes") or {}
    if not isinstance(groups, dict) or not all(isinstance(v, (list, str)) for v in groups.values()):
        raise ImportErrors(["« groupes » doit être de la forme {\"covoit\": [\"Luc\", \"Marie\"], …}."])
    token = GROUPS.set(groups)
    try:
        return _run_import(s, data, t, expected_id, require_id)
    finally:
        GROUPS.reset(token)


def _run_import(s, data, t, expected_id, require_id) -> dict:
    errors: list[str] = []
    actions: list[str] = []
    notes = [str(x) for x in (data.get("a_verifier") or [])] if isinstance(data.get("a_verifier"), list) \
        else ([str(data["a_verifier"])] if data.get("a_verifier") else [])
    unknown = sorted(set(data) - KNOWN)
    if unknown:
        notes.append("Ignoré (inconnu de l'application) : " + ", ".join(unknown))

    if expected_id is not None and (require_id or data.get("voyage_id") not in (None, "")):
        got = data.get("voyage_id")
        if got in (None, ""):
            raise ImportErrors(["Il manque « voyage_id » dans la réponse de l'IA. Demande-lui de recopier "
                                f"« \"voyage_id\": {expected_id} » en haut du bloc JSON."])
        if str(got).strip() != str(expected_id):
            other = s.get(M.Trip, int(got)) if str(got).isdigit() else None
            raise ImportErrors([f"Cette réponse concerne le voyage n°{got}"
                                + (f" « {other.name} »" if other else "")
                                + f", pas celui-ci (n°{expected_id} « {t.name} »). Colle-la dans le bon voyage."])

    v = data.get("voyage") or {}
    if not isinstance(v, dict):
        raise ImportErrors(["« voyage » doit être un objet { … }."])
    if t is None:
        if not _g(v, "nom", "name"):
            raise ImportErrors(["Il manque le nom du voyage (« voyage » → « nom »)."])
        t = do_create_trip(s, _g(v, "nom", "name"), _g(v, "debut"), _g(v, "fin"), _g(v, "lieu", default=""),
                           _g(v, "organisateur", default=""))
        if v.get("note"):
            t.note = str(v["note"])
        actions.append(f"Création du voyage « {t.name} »")
    else:
        changes = do_update_trip(s, t, _g(v, "nom", "name"), v.get("lieu"), _g(v, "debut"), _g(v, "fin"),
                                 note=v.get("note"))
        if changes:
            actions.append("Voyage : " + " ; ".join(changes))
    created = dict(participants=0, familles=0, depenses=0, versements=0, ajustements=0, desistements=0, entre_nous=0,
                   modifications=0, suppressions=0)

    labels = {"familles": "Famille", "depenses": "Dépense", "versements": "Versement", "ajustements": "Ajustement",
              "remises": "Remise ou supplément",
              "desistements": "Désistement", "entre_nous": "Compte « Entre nous »",
              "participants_a_modifier": "Participant à modifier", "depenses_a_modifier": "Dépense à modifier",
              "depenses_a_supprimer": "Dépense à supprimer", "versements_a_supprimer": "Versement à supprimer"}

    def step(kind, i, item, fn, count=None):
        label = _name(item) if isinstance(item, dict) else str(item)
        if isinstance(item, dict) and item.get("depense"):
            label = str(item["depense"])
        try:
            if not isinstance(item, dict) and kind not in ("participants", "depenses_a_supprimer"):
                raise OpsError("chaque élément doit être un objet { … }.")
            msg = fn(item)
            created[count or kind] += 1
            if msg:
                actions.append(msg)
        except OpsError as e:
            errors.append(f"{labels[kind]} n°{i}" + (f" « {label} »" if label else "") + f" : {e}")

    try:
        if _g(v, "asso"):                            # nom de l'asso : propre à chaque voyage
            from . import services as S
            new_name = str(v["asso"]).strip()[:100]
            if new_name and S.trip_asso_name(t) != new_name:
                S.set_trip_asso_name(t, new_name, s)
                s.flush()
                actions.append(f"Nom de l'asso pour ce voyage : {new_name}")
        rv = v.get("roles_du_voyage") or []          # rôles propres au voyage (conducteur, photographe…)
        if not isinstance(rv, list):
            rv = [rv]
        if rv:
            from . import services as S
            have = set(S.trip_roles(t).values())
            for name in rv:
                if str(name).strip() and str(name).strip() not in have:
                    try:
                        S.add_trip_role(t, str(name))
                        actions.append(f"Nouveau rôle du voyage : {str(name).strip()}")
                    except ValueError as e:
                        if "existe déjà" not in str(e):
                            errors.append(f"Rôle du voyage « {name} » : {e}")
            s.flush()
        parts = _list(data, "participants")
        if not parts and not t.participants:
            errors.append("Aucun participant : ajoute la liste « participants ».")
        had_expenses = bool(t.expenses)
        try:
            added, _skipped = do_add_participants(s, t, parts, inclure_dans_depenses_communes=False)
        except OpsError as e:
            raise ImportErrors([f"Participants : {e}"])
        created["participants"] = len(added)
        for p in added:
            extra = [x for x in [("accompagnant" if p.companion else ""),
                                 (f"famille {p.family.name}" if p.family else "")] if x]
            actions.append(f"Nouveau participant : {p.name}" + (f" ({', '.join(extra)})" if extra else ""))
        if had_expenses and added:
            notes.append(", ".join(p.name for p in added) + " : ajouté(e)s au voyage, mais seulement dans les "
                         "dépenses de ce fichier. S'il faut les compter aussi dans les dépenses déjà saisies, "
                         "modifie ces dépenses (écran Dépenses) ou demande-le à l'IA (« depenses_a_modifier »).")

        def upd_person(d):
            p, ch = do_update_participant(s, t, _g(d, "participant", "nom"), _g(d, "nouveau_nom"), d.get("telephone"),
                                          d.get("email"), d.get("note"), d.get("famille"), d.get("accompagnant"),
                                          d.get("roles"))
            return f"{p.name} : " + " ; ".join(ch) if ch else None
        for i, d in enumerate(_list(data, "participants_a_modifier"), 1):
            step("participants_a_modifier", i, d, upd_person, "modifications")

        apply_team(s, t, v, errors)

        # « groupes » : enregistrés dans le voyage (boutons de « Pour qui ? », réutilisables plus tard)
        from . import services as S
        from .ops import weights_of
        for gname, refs in (data.get("groupes") or {}).items():
            try:
                ids = list(weights_of(t, refs if isinstance(refs, list) else [refs]).keys())
                old = S.find_group(t, gname)
                if old is None or set(old["ids"]) != set(ids):
                    S.save_group(t, gname, ids, old["name"] if old else None)
                    actions.append(f"Groupe « {gname} » : " + ", ".join(p.name for p in t.participants if p.id in ids))
            except (OpsError, ValueError) as e:
                errors.append(f"Groupe « {gname} » : {e}")
        if "caisse" in v:                       # qui tient la caisse : « asso » ou un participant (voyage sans asso)
            from .ops import _is_asso, person
            ref = v.get("caisse")
            try:
                new = None if _is_asso(ref, t) else person(t, ref, "personne qui tient la caisse").id
                if new != t.cashier_id:
                    t.cashier_id = new
                    who = next((p.name for p in t.participants if p.id == new), None)
                    actions.append(f"Caisse tenue par {who}" if who else "Caisse tenue par l'asso")
            except OpsError as e:
                errors.append(f"Voyage, caisse : {e}")

        for i, d in enumerate(_list(data, "familles"), 1):
            step("familles", i, d, lambda d: set_referent(s, t, d))

        def del_exp(d):
            ref = d.get("depense") if isinstance(d, dict) else d
            return f"Suppression de la dépense « {do_delete_expense(s, t, ref)} »"
        for i, d in enumerate(_list(data, "depenses_a_supprimer"), 1):
            step("depenses_a_supprimer", i, d, del_exp, "suppressions")

        def upd_exp(d):
            e, ch = do_update_expense(s, t, _g(d, "depense", "libelle_actuel"), _g(d, "libelle", "nouveau_libelle"),
                                      _g(d, "categorie"), d.get("prevu", d.get("montant_prevu")),
                                      d.get("reel", d.get("montant_reel")), _g(d, "date"), d.get("paye_par"),
                                      d.get("pour"), d.get("parts"), d.get("note"), d.get("sauf"),
                                      d.get("rembourse"), d.get("rembourse_a", "asso"), d.get("sous_categorie"))
            fx = _apply_fixed(s, t, e, d.get("fixe"))
            if fx:
                ch = list(ch or []) + [fx.lstrip(" —")]
            return f"« {e.label} » : " + " ; ".join(ch) if ch else None
        for i, d in enumerate(_list(data, "depenses_a_modifier"), 1):
            step("depenses_a_modifier", i, d, upd_exp, "modifications")

        def add_exp(d):
            e = do_add_expense(s, t, _g(d, "libelle", "nom"), _g(d, "categorie"), _g(d, "prevu", "montant_prevu"),
                               _g(d, "reel", "montant_reel"), _g(d, "date"), _g(d, "paye_par", default="asso"),
                               _g(d, "pour", default=None if d.get("parts") else "tous"), d.get("parts"),
                               _g(d, "note", default=""), _g(d, "type", default="depense"), bool(d.get("a_venir")),
                               d.get("sauf"), d.get("rembourse"), d.get("rembourse_a", "asso"),
                               d.get("sous_categorie"))
            from . import services as S
            fx = _apply_fixed(s, t, e, d.get("fixe"))
            names = {p.id: p.name for p in t.participants}
            who = S.payer_label(e, names)
            if e.pending:
                return f"Nouvelle dépense « {e.label} » : montant à venir, payée par {who}"
            amt = E.fmt(e.best_known())
            if e.refund:
                who_r = next((p.name for p in t.participants if p.id == e.refund_to_id), "l'asso")
                amt += f" − {E.fmt(e.refund)} remboursés (reçus par {who_r})"
            if e.is_income:
                return f"Nouvelle recette « {e.label} » : {amt}, reçue par {who}"
            return f"Nouvelle dépense « {e.label} » : {amt}, payée par {who}{fx}"
        for i, d in enumerate(_list(data, "depenses"), 1):
            step("depenses", i, d, add_exp)

        def del_mov(d):
            m = do_delete_movement(s, t, _g(d, "participant", "nom"), _g(d, "montant"),
                                   _g(d, "type", default="versement"), _g(d, "date"))
            return f"Suppression du {m.kind} de {E.fmt(m.amount)} ({m.participant.name})"
        for i, d in enumerate(_list(data, "versements_a_supprimer"), 1):
            step("versements_a_supprimer", i, d, del_mov, "suppressions")

        def add_mov(d):
            m = do_add_movement(s, t, _g(d, "participant", "nom"), _g(d, "montant"), _g(d, "type", default="versement"),
                                _g(d, "date"), _g(d, "note", default=""), _g(d, "moyen", default=None))
            who = m.participant.family.label if m.participant.family else m.participant.name
            return (f"{who} a versé {E.fmt(m.amount)}" if m.kind == "versement"
                    else f"L'asso a rendu {E.fmt(m.amount)} à {who}")
        for i, d in enumerate(_list(data, "versements"), 1):
            step("versements", i, d, add_mov)

        def add_adj(d):
            a = do_adjust(s, t, _g(d, "participant", "nom"), _g(d, "montant"), _g(d, "type", default="reduction"),
                          _g(d, "sur", default="total"), _g(d, "compense_par", default="autres"), _g(d, "compensateur"),
                          _g(d, "motif", default=""), bool(d.get("verrouiller")))
            why = f" ({a.reason})" if a.reason else ""
            if isinstance(a, FixedShare):
                return f"{a.participant.name} : montant fixé à {E.fmt(a.value)} dans « {a.expense.label} »{why}"
            return (f"{'Remise' if a.value < 0 else 'Supplément'} de {E.fmt(abs(a.value))} pour "
                    f"{a.participant.name}{why}")
        for i, d in enumerate(_list(data, "ajustements"), 1):
            step("ajustements", i, d, add_adj)

        def add_remise(d):         # « remises » : sur tout le voyage d'une personne
            typ = str(_g(d, "type", default="remise")).lower()
            typ = "supplement" if typ.startswith("suppl") else "reduction"
            a = do_adjust(s, t, _g(d, "participant", "nom"), _g(d, "montant"), typ, "total",
                          _g(d, "compense_par", default="asso" if typ == "reduction" else "autres"),
                          _g(d, "compensateur"), _g(d, "motif", default=""))
            return (f"{'Remise' if a.value < 0 else 'Supplément'} de {E.fmt(abs(a.value))} pour {a.participant.name}"
                    + (f" ({a.reason})" if a.reason else ""))
        for i, d in enumerate(_list(data, "remises"), 1):
            step("remises", i, d, add_remise, "ajustements")

        def add_wd(d):
            w = do_withdraw(s, t, _g(d, "participant", "nom"), _g(d, "pris_en_charge_par", default="groupe"),
                            d.get("parts_conservees") or [], _g(d, "date"), _g(d, "remboursement"),
                            _g(d, "date_remboursement"))
            return f"Désistement de {w.participant.name}"
        for i, d in enumerate(_list(data, "desistements"), 1):
            step("desistements", i, d, add_wd)

        def sub(d):
            acc = do_create_subaccount(s, t, _g(d, "nom"), d.get("membres") or [])
            for j, x in enumerate(d.get("depenses") or [], 1):
                try:
                    do_add_subexpense(s, t, acc, _g(x, "libelle"), _g(x, "montant"), _g(x, "paye_par"),
                                      _g(x, "pour", default="tous"), _g(x, "date"))
                except OpsError as e:
                    raise OpsError(f"dépense n°{j} « {_g(x, 'libelle', default='')} » : {e}")
            return f"Compte « Entre nous » « {acc.name} »"
        for i, d in enumerate(_list(data, "entre_nous"), 1):
            step("entre_nous", i, d, sub)

        if data.get("dates_paiement"):               # acompte, solde… : qui est en retard se lit ensuite dans l'état
            try:
                actions.extend(do_set_pay_dates(s, t, data["dates_paiement"]))
                created["modifications"] += 1
            except OpsError as e:
                errors.append(f"Dates de paiement : {e}")
    except ImportErrors:
        raise
    if errors:
        raise ImportErrors(errors)

    S.add_ai_notes(t, notes)                   # gardés après l'enregistrement, jusqu'à « C'est vérifié »
    s.flush()
    s.expire_all()
    t = s.get(M.Trip, t.id)
    detail = trip_detail(t)
    created["participants_total"] = len(t.participants)
    n = len(actions)
    return dict(voyage_id=t.id, voyage=t.name, crees=created, actions=actions, a_verifier=notes, apercu=detail,
                message=f"Voyage « {t.name} » : {n} changement{'s' if n > 1 else ''} enregistré{'s' if n > 1 else ''}.")
