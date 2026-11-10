"""Pont entre la base de données et le moteur de calcul."""
from __future__ import annotations

import datetime as dt

from . import engine as E
from . import models as M

import json
import re

ALL, DIVERS, COMPANIONS = "tous", "plongeurs", "accompagnants"
AUDIENCES = {ALL: "Tout le monde", DIVERS: "Les plongeurs", COMPANIONS: "Les accompagnants"}

# Liste conseillée pour un nouveau voyage (modifiable : écran Catégories → « liste par défaut »)
RECOMMENDED_CATEGORIES = [
    ("Logement", ALL), ("Transport / Covoit", ALL), ("Plongée", DIVERS), ("Location matériel", DIVERS),
    ("Repas", ALL), ("Activités accompagnants", COMPANIONS), ("Divers", ALL),
]
# Un voyage ordinaire (randonnée, ski, week-end entre amis…) : rien de propre à une activité. Le « jeu Plongée »
# (rôles Plongeur / Accompagnant, catégories de plongée) s'ajoute à la création, ou plus tard d'un clic.
NEUTRAL_CATEGORIES = [("Logement", ALL), ("Transport / Covoit", ALL), ("Repas", ALL), ("Activités", ALL),
                      ("Divers", ALL)]
DIVING_CATEGORIES = [("Plongée", DIVERS), ("Location matériel", DIVERS), ("Activités accompagnants", COMPANIONS)]
# Proposées d'un clic sur l'écran Catégories
SUGGESTED_CATEGORIES = [
    ("Gonflage / Nitrox", DIVERS), ("Formation / Passage de niveau", DIVERS), ("Courses", ALL),
    ("Transport collectif", ALL), ("Bateau / Navette", ALL), ("Assurance", ALL),
    ("Taxes", ALL), ("Frais divers", ALL),
]
DEFAULT_CATEGORIES = [n for n, _ in RECOMMENDED_CATEGORIES]
DEFAULTS_KEY = "categories_par_defaut"
LIBRARY_KEY = "categories_bibliotheque"     # catégories créées par l'utilisateur, proposées dans tous les voyages


def _custom_categories(s) -> list:
    row = s.get(M.Setting, LIBRARY_KEY)
    try:
        return [str(x) for x in json.loads(row.value)] if row and row.value else []
    except ValueError:
        return []


def builtin_category_names() -> list:
    names = [n for n, _ in RECOMMENDED_CATEGORIES + NEUTRAL_CATEGORIES + SUGGESTED_CATEGORIES]
    return list(dict.fromkeys(names))


def is_diving_category(name: str) -> bool:
    """Catégorie du « jeu Plongée » (ou qui en a tout l'air) : proposée seulement dans un voyage de plongée."""
    return guess_audience(name) != ALL


def category_library(s) -> list:
    """Toutes les catégories disponibles : celles fournies + celles créées par l'utilisateur (sans doublon)."""
    out, seen = [], set()
    for n in builtin_category_names() + _custom_categories(s):
        k = E.sort_key(n)
        if k not in seen:
            seen.add(k)
            out.append(n)
    return out


def is_custom_category(s, name) -> bool:
    k = E.sort_key(name)
    return k not in {E.sort_key(n) for n in builtin_category_names()} and \
        k in {E.sort_key(n) for n in _custom_categories(s)}


def add_to_library(s, name) -> None:
    if E.sort_key(name) in {E.sort_key(n) for n in category_library(s)}:
        return
    custom = _custom_categories(s) + [name]
    row = s.get(M.Setting, LIBRARY_KEY)
    if row is None:
        s.add(M.Setting(key=LIBRARY_KEY, value=json.dumps(custom, ensure_ascii=False)))
    else:
        row.value = json.dumps(custom, ensure_ascii=False)


SUBCATS_KEY = "sous_categories"            # {nom de catégorie: [sous-catégories]}, commun à tous les voyages


def sub_library(s) -> dict:
    row = s.get(M.Setting, SUBCATS_KEY)
    try:
        data = json.loads(row.value) if row and row.value else {}
        return {str(k): [str(x) for x in v] for k, v in data.items() if isinstance(v, list)}
    except ValueError:
        return {}


def _save_subs(s, data: dict) -> None:
    row = s.get(M.Setting, SUBCATS_KEY)
    value = json.dumps(data, ensure_ascii=False)
    if row is None:
        s.add(M.Setting(key=SUBCATS_KEY, value=value))
    else:
        row.value = value


def _cat_key(data: dict, category: str):
    return next((k for k in data if E.sort_key(k) == E.sort_key(category)), category)


def add_subcategory(s, category: str, sub: str) -> bool:
    if not (category and sub):
        return False
    data = sub_library(s)
    k = _cat_key(data, category)
    lst = data.setdefault(k, [])
    if E.sort_key(sub) in {E.sort_key(x) for x in lst}:
        return False
    lst.append(sub)
    _save_subs(s, data)
    return True


def remove_subcategory(s, category: str, sub: str) -> None:
    data = sub_library(s)
    k = _cat_key(data, category)
    data[k] = [x for x in data.get(k, []) if E.sort_key(x) != E.sort_key(sub)]
    if not data[k]:
        data.pop(k)
    _save_subs(s, data)


def subcategories_for(s, trip: M.Trip) -> dict:
    """{nom de catégorie: [sous-catégories]} : celles enregistrées (communes à tous les voyages) + celles déjà
    utilisées dans ce voyage."""
    out: dict = {}

    def add(cat, sub):
        lst = out.setdefault(cat, [])
        if sub and E.sort_key(sub) not in {E.sort_key(x) for x in lst}:
            lst.append(sub)
    lib = sub_library(s)
    for c in trip.categories:
        for sub in lib.get(_cat_key(lib, c.name), []):
            add(c.name, sub)
    for x in trip.expenses:
        if x.subcategory and x.category:
            add(x.category.name, x.subcategory)
    return out


def remove_from_library(s, name) -> bool:
    custom = _custom_categories(s)
    keep = [n for n in custom if E.sort_key(n) != E.sort_key(name)]
    if len(keep) == len(custom):
        return False
    s.get(M.Setting, LIBRARY_KEY).value = json.dumps(keep, ensure_ascii=False)
    return True


# Rôles dans le groupe (un participant peut en cumuler plusieurs). Ce sont des ÉTIQUETTES : elles aident à s'y
# retrouver et à cocher vite les bonnes personnes ; la répartition, elle, ne suit que les personnes cochées dans
# chaque dépense. Plongeur et accompagnant sont proposés d'office mais se retirent comme les autres.
DIVER, COMPANION = "plongeur", "accompagnant"
ORGANIZER, CO_ORGANIZER = "organisateur", "co-organisateur"
ROLES = {DIVER: "Plongeur", COMPANION: "Accompagnant", ORGANIZER: "Organisateur", CO_ORGANIZER: "Co-organisateur"}
ROLE_TITLES = {DIVER: "Participe aux plongées", COMPANION: "Vient au voyage sans participer à l'activité",
               ORGANIZER: "Organisateur du voyage", CO_ORGANIZER: "Co-organisateur du voyage"}
ROLE_SHORT = {DIVER: "plong.", COMPANION: "acc.", ORGANIZER: "org.", CO_ORGANIZER: "co-org."}
TEAM_ROLES = (ORGANIZER, CO_ORGANIZER)
ROLE_PLURALS = {DIVER: "plongeurs", COMPANION: "accompagnants", ORGANIZER: "organisateurs",
                CO_ORGANIZER: "co-organisateurs"}
LEGACY_DP, DP_NAME = "dp", "DP"     # ancien rôle « DP » (directeur de plongée) : devenu une étiquette ordinaire


# Rôles propres à un voyage (« Moniteur », « DP »…) : clé « perso:Moniteur » dans participants.roles.
# Tous les rôles prévus peuvent être retirés d'un voyage (« masques »).
CUSTOM = "perso:"


def _roles_conf(trip) -> dict:
    try:
        d = json.loads(trip.roles_conf) if trip is not None and trip.roles_conf else {}
    except ValueError:
        d = {}
    return {"perso": [str(x) for x in d.get("perso", []) if str(x).strip()],
            "masques": [str(x) for x in d.get("masques", []) if x in ROLES]}


def trip_roles(trip) -> dict:
    """Rôles de ce voyage, dans l'ordre : {clé: libellé}."""
    conf = _roles_conf(trip)
    out = {k: v for k, v in ROLES.items() if k not in conf["masques"]}
    out.update({CUSTOM + n: n for n in conf["perso"]})
    return out


def role_label(key: str) -> str:
    return ROLES.get(key) or (key[len(CUSTOM):] if key.startswith(CUSTOM) else key)


def add_trip_role(trip, name: str) -> str:
    """Ajoute un rôle au voyage (ou remet un rôle prévu qu'on avait retiré). Renvoie sa clé."""
    name = " ".join(str(name or "").split())[:40]
    if not name:
        raise ValueError("Donne un nom au rôle (ex. Moniteur, Photographe).")
    if any(ch in name for ch in ",:"):
        raise ValueError("Le nom d'un rôle ne peut pas contenir de virgule ni de deux-points.")
    conf = _roles_conf(trip)
    key = E.sort_key(name).strip()
    for k, v in ROLES.items():                       # « DP », « Organisateur »… : on le remet
        if key in (E.sort_key(v), k, ROLE_PLURALS[k]):
            if k in conf["masques"]:
                conf["masques"].remove(k)
                trip.roles_conf = json.dumps(conf, ensure_ascii=False)
                return k
            raise ValueError(f"Le rôle « {v} » existe déjà.")
    if any(E.sort_key(n) == key for n in conf["perso"]):
        raise ValueError(f"Le rôle « {name} » existe déjà.")
    conf["perso"].append(name)
    trip.roles_conf = json.dumps(conf, ensure_ascii=False)
    return CUSTOM + name


def remove_trip_role(trip, key: str) -> None:
    """Retire un rôle du voyage et de toutes les personnes qui l'avaient (les dépenses ne changent pas)."""
    conf = _roles_conf(trip)
    if key in ROLES:
        if key not in conf["masques"]:
            conf["masques"].append(key)
    elif key.startswith(CUSTOM):
        conf["perso"] = [n for n in conf["perso"] if CUSTOM + n != key]
    else:
        raise ValueError("Rôle inconnu.")
    for p in trip.participants:
        if key in p.role_list:
            p.roles = ",".join(r for r in p.role_list if r != key)
    trip.roles_conf = json.dumps(conf, ensure_ascii=False)


def parse_roles(value, trip=None) -> list:
    """« Organisateur, DP » / ["dp"] / "directeur de plongée" -> ["organisateur", "dp"] (ordre canonique)."""
    items = value if isinstance(value, (list, tuple, set)) else str(value or "").replace(";", ",").split(",")
    found = set()
    for it in items:
        k = E.sort_key(str(it)).strip().replace("_", "-").replace(" ", "-")
        if not k:
            continue
        if k in ("dp", "directeur-de-plongee", "directeur-plongee", "directrice-de-plongee"):
            found.add(_dp_role(trip))              # l'ancien rôle DP : une étiquette « DP » du voyage
        elif k.startswith("plongeu"):
            found.add(DIVER)
        elif k.startswith("accompagn"):
            found.add(COMPANION)
        elif k.startswith("co-org") or k.startswith("coorg") or k == "co-organisatrice":
            found.add(CO_ORGANIZER)
        elif k.startswith("organis") or k == "orga":
            found.add(ORGANIZER)
        elif trip is not None and any(E.sort_key(n).replace(" ", "-") == k for n in _roles_conf(trip)["perso"]):
            found.add(next(CUSTOM + n for n in _roles_conf(trip)["perso"] if E.sort_key(n).replace(" ", "-") == k))
        else:
            known = ", ".join(v.lower() if k_ in ROLES else v for k_, v in trip_roles(trip).items()) if trip is not None \
                else "plongeur, accompagnant, organisateur, co-organisateur"
            raise ValueError(f"Rôle inconnu : « {it} ». Rôles possibles : {known}.")
    order = list(trip_roles(trip)) if trip is not None else list(ROLES)
    return [r for r in order if r in found] + sorted(r for r in found if r not in order)


def set_roles(trip: M.Trip, p: M.Participant, roles: list) -> None:
    """Donne ces rôles à p. Il n'y a qu'un organisateur : le donner à p le retire à l'ancien."""
    order = list(trip_roles(trip))
    roles = [r for r in order if r in roles]
    if ORGANIZER in roles:
        for o in trip.participants:
            if o.id != p.id and ORGANIZER in o.role_list:
                o.roles = ",".join(r for r in o.role_list if r != ORGANIZER)
    p.roles = ",".join(roles)
    p.companion = COMPANION in roles                   # colonne historique, tenue à jour


def _dp_role(trip) -> str:
    """Clé de l'étiquette « DP » du voyage (créée au besoin) : ce qu'est devenu l'ancien rôle DP."""
    if trip is None:
        return CUSTOM + DP_NAME
    conf = _roles_conf(trip)
    have = next((n for n in conf["perso"] if E.sort_key(n) == E.sort_key(DP_NAME)), None)
    if have:
        return CUSTOM + have
    return add_trip_role(trip, DP_NAME)



def with_role(trip: M.Trip, role: str) -> list:
    return [p for p in trip.participants if role in p.role_list]


def organizer_name(trip: M.Trip) -> str:
    """L'organisateur désigné parmi les participants, sinon le nom saisi librement pour le voyage."""
    o = with_role(trip, ORGANIZER)
    return o[0].name if o else (trip.organizer or "")


def roles_map(trip: M.Trip) -> dict:
    return {p.id: p.role_list for p in trip.participants if p.roles}


def guess_audience(name: str) -> str:
    k = E.sort_key(name or "")
    if "accompagn" in k:
        return COMPANIONS
    if any(w in k for w in ("plongee", "nitrox", "gonflage", "location materiel", "passage de niveau", "bapteme")):
        return DIVERS
    return ALL


def audience_of(cat) -> str:
    if cat is None:
        return ALL
    return cat.audience if cat.audience in AUDIENCES else guess_audience(cat.name)


def default_category_list(s, diving: bool = True) -> list:
    """[(nom, public)] pour un nouveau voyage : la liste enregistrée par l'utilisateur, sinon la liste conseillée
    (avec les catégories de plongée seulement pour un voyage de plongée)."""
    row = s.get(M.Setting, DEFAULTS_KEY) if s is not None else None
    if row and row.value:
        try:
            items = [(str(n), a if a in AUDIENCES else ALL) for n, a in json.loads(row.value) if str(n).strip()]
            if items:
                return items
        except (ValueError, TypeError):
            pass
    return list(RECOMMENDED_CATEGORIES if diving else NEUTRAL_CATEGORIES)


def new_trip_categories(s, diving: bool = True) -> list:
    return [M.Category(name=n, audience=a, position=i)
            for i, (n, a) in enumerate(default_category_list(s, diving))]


def is_diving(trip) -> bool:
    """Le voyage utilise le « jeu Plongée » : le rôle Plongeur y existe."""
    return DIVER in trip_roles(trip)


def start_trip(s, trip: M.Trip, diving: bool) -> None:
    """Réglages de départ d'un voyage tout neuf. Sans plongée : pas de rôle Plongeur / Accompagnant, pas de
    catégorie de plongée (tout peut s'ajouter ensuite)."""
    trip.categories = new_trip_categories(s, diving)
    last = last_trip(s)                            # les étiquettes du dernier voyage (« Moniteur »…) sont reprises
    conf = _roles_conf(last) if last is not None else {"perso": [], "masques": []}
    masks = [m for m in conf["masques"] if m not in (DIVER, COMPANION)]
    if diving:                                     # un accompagnant retiré du dernier voyage de plongée le reste
        masks += [COMPANION] if last is not None and is_diving(last) and COMPANION in conf["masques"] else []
    else:
        masks = [DIVER, COMPANION] + masks
    trip.roles_conf = json.dumps({"perso": conf["perso"], "masques": masks}, ensure_ascii=False)


def last_trip(s):
    """Le dernier voyage créé (hors voyage d'exemple), ou None."""
    return s.query(M.Trip).filter(M.Trip.is_demo.is_(False)).order_by(M.Trip.created.desc(), M.Trip.id.desc()).first()


def add_diving_set(trip: M.Trip) -> list:
    """Ajoute le « jeu Plongée » à un voyage : les rôles Plongeur et Accompagnant, et les catégories de plongée
    qui manquent. Les personnes déjà inscrites deviennent « Plongeur » (à changer ensuite pour les accompagnants).
    Renvoie les noms des catégories ajoutées."""
    conf = _roles_conf(trip)
    back = [r for r in (DIVER, COMPANION) if r in conf["masques"]]
    conf["masques"] = [m for m in conf["masques"] if m not in (DIVER, COMPANION)]
    trip.roles_conf = json.dumps(conf, ensure_ascii=False)
    if DIVER in back:
        for p in trip.participants:
            if DIVER not in p.role_list and COMPANION not in p.role_list:
                set_roles(trip, p, p.role_list + [DIVER])
    have = {E.sort_key(c.name) for c in trip.categories}
    added = []
    for n, a in DIVING_CATEGORIES:
        if E.sort_key(n) not in have:
            trip.categories.append(M.Category(name=n, audience=a, position=len(trip.categories)))
            added.append(n)
    return added


def getting_started(trip: M.Trip) -> list:
    """Carte « Pour démarrer » : les quatre étapes d'un voyage, cochées toutes seules.
    [{"done", "title", "text", "link", "action"}] ; vide quand il n'y a plus rien à montrer (ou voyage d'exemple)."""
    if trip.is_demo:
        return []
    tid = trip.id
    steps = [
        dict(done=bool(trip.participants), title="Ajoute les participants",
             text="Les personnes qui viennent au voyage (tu pourras en ajouter plus tard).",
             link=f"/v/{tid}/participants?ajout=1#ajout", action="Ajouter des participants"),
        dict(done=bool(trip.expenses), title="Note les dépenses",
             text="Le logement, le transport, les repas… avec le montant prévu, puis le montant payé.",
             link=f"/v/{tid}/depenses/nouvelle", action="Ajouter une dépense"),
        dict(done=bool(trip.movements), title="Note l'argent reçu",
             text="Chaque acompte ou versement d'un participant, au fur et à mesure.",
             link=f"/v/{tid}/argent", action="Noter un versement"),
        dict(done=bool(pay_info(trip)), title="Envoie à chacun son compte",
             text="Un message et un PDF par personne, avec ce qu'il reste à payer et comment payer.",
             link=f"/v/{tid}/envoyer", action="Envoyer les comptes"),
    ]
    if all(st["done"] for st in steps[:3]):
        return []                                  # le voyage est lancé : la carte s'efface
    return steps


def engine_inputs(trip: M.Trip) -> dict:
    cats = {c.id: c.name for c in trip.categories}
    return dict(
        participants=[E.Participant(p.id, p.name, p.family_id, bool(p.companion)) for p in trip.participants],
        families=[E.Family(f.id, f.name, f.referent_id) for f in trip.families],
        expenses=[
            E.Expense(e.id, e.label, cats.get(e.category_id, "Divers"), e.planned, e.actual, e.payer_id,
                      {s.participant_id: s.weight for s in e.shares}, e.kind or "depense",
                      [(p.payer_id, p.amount) for p in e.payments] or None, e.refund or 0, e.refund_to_id,
                      {s.participant_id: s.fixed for s in e.shares if s.fixed is not None},
                      e.cents_mode or "", cents_ids(e.cents_who))
            for e in trip.expenses
        ],
        adjustments=[
            E.Adjustment(a.id, a.participant_id, a.value, a.expense_id, a.fixed, a.compensation,
                         a.compensator_id, a.reason)
            for a in trip.adjustments
        ],
        withdrawals=[E.Withdrawal(w.participant_id, w.absorber, w.kept_ids,
                                  w.date.isoformat() if w.date else "") for w in trip.withdrawals],
        movements=[E.Movement(m.participant_id, m.kind, m.amount,
                              m.date.isoformat() if m.date else "", m.note) for m in trip.movements],
        rounding=trip.rounding or E.ROUND_PARTICIPANTS,
    )


def cents_ids(raw) -> tuple:
    return tuple(int(x) for x in str(raw or "").split(",") if x.strip().isdigit())


def cent_counts(result: E.Result, skip=None) -> dict:
    """Combien de centimes d'arrondi chacun a déjà payés en plus (sur toutes les dépenses, sauf `skip`)."""
    out = {}
    for k, inf in result.cent_info.items():
        if k == skip:
            continue
        for p in inf["winners"]:
            out[p] = out.get(p, 0) + 1
    return out


def store_draws(trip: M.Trip, eids=None) -> bool:
    """Enregistre le tirage des centimes d'arrondi des dépenses qui n'en ont pas de valable (nouvelle dépense
    venue de l'IA, montant ou personnes changés, désistement…) : c'est exactement celui que le calcul utilise déjà
    (équitable, toujours le même), si bien que les montants ne bougent pas ; il ne bougera plus ensuite.
    Un tirage valable n'est jamais refait (le hasard du formulaire, « Tirer d'autres personnes », est gardé).
    Renvoie True si quelque chose a changé."""
    r = compute_trip(trip)
    changed = False
    for e in trip.expenses:
        if eids is not None and e.id not in eids:
            continue
        inf = r.cent_info.get(e.id)
        if not inf or not inf["need"]:
            if e.cents_who:
                e.cents_who, changed = "", True                  # plus rien à tirer : on oublie l'ancien tirage
            continue
        if not inf["drawn"]:
            e.cents_who, changed = ",".join(map(str, inf["winners"])), True
    return changed


def compute_trip(trip: M.Trip, mode: str = E.ACTUAL, **overrides) -> E.Result:
    inputs = engine_inputs(trip)
    inputs.update(overrides)
    r = E.compute(**inputs, mode=mode)
    if trip.cashier_id and trip.cashier_id in r.people:
        r.cashier = trip.cashier_id
    return r


def everyone_expense(trip: M.Trip, expense: M.Expense) -> bool:
    ids = {p.id for p in trip.participants}
    shares = {s.participant_id: s.weight for s in expense.shares}
    return set(shares) == ids and all(w == 1 for w in shares.values())


def label_sets(trip: M.Trip) -> list:
    """[(clé, libellé, {ids})] : tout le monde, chaque étiquette (rôle), chaque groupe, chaque famille."""
    out = [("tous", "tout le monde", {p.id for p in trip.participants})]
    for rk, v in trip_roles(trip).items():
        out.append((f"role:{rk}", ROLE_PLURALS.get(rk, v).capitalize() if rk in ROLES else v,
                    {p.id for p in with_role(trip, rk)}))
    for g in trip_groups(trip):
        out.append((f"groupe:{g['name']}", f"groupe {g['name']}", set(g["ids"])))
    for f in trip.families:
        out.append((f"famille:{f.id}", f"famille {f.name}", {m.id for m in f.members}))
    return out


def normalize_shares(e: M.Expense) -> bool:
    """Pas de « parts » : chacun a soit la part égale, soit un montant fixé 🔒. Des parts venues de l'IA ou d'un
    import (« compte double », plongées, nuits, demi-part) deviennent des montants :
    - le nombre de parts le plus courant devient la part normale : ces personnes restent libres et se partagent le
      reste (elles reprennent aussi la part de quelqu'un qui se désiste) ;
    - les autres reçoivent le montant que leurs parts donnaient, fixé 🔒 (sauf s'il vaut la part égale).
    Mêmes montants qu'avant la conversion. Les montants déjà fixés ne sont pas touchés. Sans montant réel (dépense au
    prévu ou à venir), les parts attendent qu'il le soit (l'IA donne souvent le réel plus tard).
    Renvoie True si quelque chose a changé."""
    from collections import Counter
    shares = list(e.shares)
    total = e.net()
    if not shares or e.actual is None or total <= 0 or all(x.weight == 1 for x in shares):
        return False
    fixed = {x.participant_id: x.fixed for x in shares if x.fixed is not None}
    free = {x.participant_id: x.weight for x in shares if x.fixed is None}
    amounts = E.split_exact(max(total - sum(fixed.values()), 0), free) if free else {}
    equal = E.split_exact(total, {x.participant_id: 1 for x in shares})
    count = Counter(w for w in free.values())
    base = min(count, key=lambda w: (-count[w], w)) if count else 1
    for x in shares:
        if x.fixed is None and x.weight != base:
            c = amounts.get(x.participant_id, 0)
            x.fixed = None if abs(c - equal[x.participant_id]) <= 1 else c
        x.weight = 1.0
    return True


def normalize_trip(trip: M.Trip) -> int:
    """normalize_shares sur toutes les dépenses du voyage ; renvoie le nombre de dépenses changées."""
    return sum(1 for e in trip.expenses if normalize_shares(e))


def flag_checks(trip: M.Trip, suggestions: list) -> int:
    """Dépenses à parts personnalisées où quelqu'un vient d'entrer dans un ensemble allumé : marquées « à vérifier »
    (une fois par dépense et par personne). Renvoie le nombre de marques ajoutées."""
    exps = {e.id: e for e in trip.expenses}
    n = 0
    for x in suggestions:
        e = exps.get(x["eid"])
        if x["kind"] != "verifier" or e is None or any(c.participant_id == x["pid"] for c in e.checks):
            continue
        e.checks.append(M.ExpenseCheck(participant_id=x["pid"], reason=x["joined"]))
        n += 1
    return n


WITHDRAWAL = "désistement"                 # raison d'une dépense « à vérifier » après un désistement


def flag_withdrawal(trip: M.Trip, pid: int, kept, absorber: str) -> int:
    """Désistement repris par le groupe : sa part est répartie entre les montants libres. Dans une dépense qui a des
    montants fixés 🔒, ceux-ci ne bougent pas : la dépense est marquée « à vérifier ». Renvoie le nombre de marques."""
    if absorber != E.GROUP:
        return 0
    kept = {int(x) for x in kept or []}
    n = 0
    for e in trip.expenses:
        ids = {x.participant_id for x in e.shares}
        if e.id in kept or pid not in ids or not any(x.fixed is not None for x in e.shares if x.participant_id != pid):
            continue
        if not any(c.participant_id == pid and c.reason == WITHDRAWAL for c in e.checks):
            e.checks.append(M.ExpenseCheck(participant_id=pid, reason=WITHDRAWAL))
            n += 1
    return n


def checks_of(trip: M.Trip) -> dict:
    """{expense_id: [(check, nom, sans_libre)]} : dépenses « à vérifier » : quelqu'un a rejoint un groupe (personne
    encore là, pas encore dans la dépense), ou s'est désisté (sans_libre : plus personne pour reprendre sa part)."""
    names = {p.id: p.name for p in trip.participants}
    out: dict = {}
    for e in trip.expenses:
        inside = {x.participant_id for x in e.shares}
        for c in e.checks:
            if c.participant_id not in names:
                continue
            if c.reason == WITHDRAWAL:
                no_free = not any(x.fixed is None for x in e.shares if x.participant_id != c.participant_id)
                out.setdefault(e.id, []).append((c, names[c.participant_id], no_free))
            elif c.participant_id not in inside:
                out.setdefault(e.id, []).append((c, names[c.participant_id], False))
    return out


def label_members(trip: M.Trip) -> dict:
    """{clé: {ids}} : à garder AVANT un changement de personnes, d'étiquettes ou de groupes (join_suggestions)."""
    return {k: set(ids) for k, _, ids in label_sets(trip)}


def join_suggestions(trip: M.Trip, before: dict) -> list:
    """Quelqu'un vient d'entrer dans le voyage, une étiquette, un groupe ou une famille : chaque dépense où cet
    ensemble est « allumé » (tous ses membres d'avant y sont, comme dans le formulaire de dépense) lui est proposée,
    sauf s'il y est déjà. Rien n'est ajouté ici : [{pid, eid, why, joined, kind, others}] :
    - why : l'origine de la dépense (« groupe covoit », « groupe covoit + Accompagnants ») ;
    - joined : l'ensemble qu'il vient de rejoindre ;
    - kind : « groupes » (partagée exactement entre des ensembles entiers), « autres » (l'ensemble et d'autres
      personnes : others = leurs noms) ou « verifier » (parts inégales ou montant fixé : à faire à la main)."""
    after = {k: (lbl, ids) for k, lbl, ids in label_sets(trip)}
    names = {p.id: p.name for p in trip.participants}
    old = {k: ids for k, ids in before.items() if ids and k in after}   # organisateur compris : une étiquette
    out, seen = [], set()
    for e in sorted(trip.expenses, key=lambda x: (x.date or dt.date.min, x.id)):
        shares = list(e.shares)
        if not shares:
            continue
        ids = {x.participant_id for x in shares}
        inside = {k: v for k, v in old.items() if v <= ids}          # ensembles allumés dans cette dépense
        if not inside:
            continue
        covered = set().union(*inside.values())
        custom = any(x.fixed is not None or x.weight != 1 for x in shares)     # (parts : converties en 🔒)
        for k in inside:
            for pid in sorted(after[k][1] - old[k]):
                if pid not in names or pid in ids or (pid, e.id) in seen:
                    continue
                seen.add((pid, e.id))
                kind = "verifier" if custom else "groupes" if covered == ids else "autres"
                out.append(dict(pid=pid, eid=e.id, why=_origin(after, inside, covered, k), joined=after[k][0],
                                kind=kind, others=sorted((names[i] for i in ids - covered if i in names),
                                                         key=E.sort_key)))
    return out


def _origin(after: dict, inside: dict, ids: set, first: str) -> str:
    """D'où vient la dépense : l'ensemble allumé qui couvre exactement « ids », sinon la réunion d'ensembles entiers
    qui le couvre, en commençant par celui que la personne vient de rejoindre (« groupe covoit + Accompagnants »)."""
    exact = [k for k, v in inside.items() if v == ids]
    if exact:
        return after[first if first in exact else exact[0]][0]
    chosen, covered = [first], set(inside[first])
    rest = sorted((k for k in inside if k != first and k != "tous"), key=lambda k: -len(inside[k] - covered))
    while covered != ids and rest:
        k = max(rest, key=lambda k: len(inside[k] - covered))
        rest.remove(k)
        if inside[k] - covered:
            chosen.append(k)
            covered |= inside[k]
    return " + ".join(after[k][0] for k in chosen)


def who_label(trip: M.Trip, e: M.Expense) -> str:
    """« tout le monde », le nom d'un groupe ou d'une famille qui correspond exactement, un nom, ou « N pers. »."""
    shares = {s_.participant_id: s_.weight for s_ in e.shares}
    equal = len(set(shares.values())) <= 1
    ids = set(shares)
    # toujours le nombre de personnes ; le nom d'un groupe / d'une famille est dans la bulle (who_detail)
    if len(ids) == 1:
        return next((p.name for p in trip.participants if p.id in ids), "1 pers.")
    return f"{len(ids)} pers." + ("" if equal else " (montants différents)")


def is_estimated(e: M.Expense) -> bool:
    """Dépense comptée sur son montant prévu : le réel n'est pas encore saisi."""
    return e.actual is None and e.planned is not None


def who_detail(trip: M.Trip, e: M.Expense, result: E.Result) -> list:
    """Bulle « Pour qui » : le groupe ou la famille, la part de chacun, puis (après un trait « --- ») qui n'est
    pas concerné, s'il y en a peu."""
    ids = {s_.participant_id for s_ in e.shares}
    people = sorted((p for p in trip.participants if p.id in ids), key=lambda p: E.sort_key(p.name))
    out = []
    if len(ids) > 1 and ids == {p.id for p in trip.participants}:
        out.append("Tout le monde")
    else:
        g = next((g for g in trip_groups(trip) if set(g["ids"]) == ids and len(ids) > 1), None)
        f = next((f for f in trip.families if {m.id for m in f.members} == ids), None)
        if g:
            out.append(f"Groupe {g['name']}")
        elif f:
            out.append(f"Famille {f.name}")
    others = [p.name for p in sorted(trip.participants, key=lambda p: E.sort_key(p.name)) if p.id not in ids]
    for p in people:
        out.append(f"{p.name} : {E.fmt(result.share(p.id, e.id))}")
    to_asso = (result.cent_info.get(e.id) or {}).get("asso")
    if to_asso:
        out.append(f"Centimes d'arrondi : {E.fmt(to_asso)} payés par {caisse_label(trip)}")
    if others and len(others) <= len(people):
        out += ["---", "Pas concernés : " + ", ".join(others)]
    return out


def find_family(trip: M.Trip, name: str):
    key = E.sort_key((name or "").strip())
    key = key.removeprefix("famille ").strip()
    return next((f for f in trip.families if E.sort_key(f.name) == key), None) if key else None


def set_family(s, trip: M.Trip, p: M.Participant, name) -> None:
    """Range p dans la famille `name` (créée si besoin) ; name vide = plus de famille."""
    old = p.family_id
    name = (name or "").strip()
    if name.lower().startswith("famille "):
        name = name[8:].strip()
    if not name:
        p.family_id = None
    else:
        f = find_family(trip, name)
        if f is None:
            f = M.Family(name=name)
            trip.families.append(f)
            s.flush()
        p.family_id = f.id
        if not f.referent_id or f.referent_id not in {m.id for m in f.members} | {p.id}:
            f.referent_id = p.id
    s.flush()
    if old and old != p.family_id:
        f = next((x for x in trip.families if x.id == old), None)
        if f is not None:
            members = [m for m in trip.participants if m.family_id == old and m.id != p.id]
            if not members:
                trip.families.remove(f)
                s.delete(f)
            elif f.referent_id == p.id:
                f.referent_id = members[0].id
        s.flush()


def asso_cash(trip: M.Trip) -> tuple:
    """(entrées, sorties) réelles de la caisse de l'asso pour ce voyage (montants réels seulement)."""
    cash_in = sum(m.amount for m in trip.movements if m.kind == "versement")
    cash_out = sum(m.amount for m in trip.movements if m.kind == "remboursement")
    for e in trip.expenses:
        if e.actual is None:
            continue
        asso_part = sum(c for pid, c in e.splits() if pid is None)
        if e.is_income:
            cash_in += asso_part
        else:
            cash_out += asso_part
        if e.refund and e.refund_to_id is None:
            cash_in += min(e.refund, e.actual)          # remboursement reçu par l'asso
    return cash_in, cash_out


def advances(trip: M.Trip, pid=None) -> list:
    """Avances des participants (dépenses qu'ils ont payées) et recettes qu'ils ont encaissées pour l'asso."""
    out = []
    for e in trip.expenses:
        for who, cents in e.splits():
            if who is None or (pid is not None and who != pid):
                continue
            p = next((x for x in trip.participants if x.id == who), None)
            out.append(dict(date=e.date, label=e.label, eid=e.id, pid=who, name=p.name if p else "?",
                            amount=cents, income=e.is_income, partial=e.multi_payer))
        if e.refund and e.refund_to_id is not None and (pid is None or e.refund_to_id == pid) and e.best_known():
            p = next((x for x in trip.participants if x.id == e.refund_to_id), None)
            out.append(dict(date=e.date, label=f"{e.label} (remboursement)", eid=e.id, pid=e.refund_to_id,
                            name=p.name if p else "?", amount=min(e.refund, e.best_known()), income=True,
                            partial=False))
    return out


def payer_label(e: M.Expense, names: dict, asso: str = "l'asso") -> str:
    """« l'asso », « Luc », ou « Stéphane 3 116,00 € + l'asso 1 200,00 € »."""
    sp = e.splits()
    if len(sp) <= 1:
        pid = sp[0][0] if sp else e.payer_id
        return names.get(pid, asso) if pid else asso
    return " + ".join(f"{names.get(pid, asso) if pid else asso} {E.fmt(c)}" for pid, c in sp)


def alerts(trip: M.Trip, result: E.Result) -> list[dict]:
    """Alertes en langage clair : [{'level': 'warn'|'info'|'error', 'text': ..., 'link': ...}]"""
    out = []
    tid = trip.id
    if not trip.participants:
        out.append(dict(level="info", text="Commence par ajouter les participants.",
                        link=f"/v/{tid}/participants"))
    if trip.participants and not trip.expenses:
        out.append(dict(level="info", text="Aucune dépense saisie pour l'instant.",
                        link=f"/v/{tid}/depenses/nouvelle"))
    checks = checks_of(trip)
    if checks:
        n = len(checks)
        out.append(dict(level="error",
                        text=f"{n} dépense{'s' if n > 1 else ''} à vérifier : "
                             + ("des montants fixés sont à revoir après un désistement"
                                if all(c.reason == WITHDRAWAL for v in checks.values() for c, *_ in v)
                                else "la part d'un nouveau membre de groupe est à ajouter à la main"),
                        link=f"/v/{tid}/depenses"))
    no_receipt = [e for e in trip.expenses if e.actual is not None and not e.receipt_links]
    if no_receipt:
        n = len(no_receipt)
        out.append(dict(level="warn",
                        text=f"{n} dépense{'s' if n > 1 else ''} payée{'s' if n > 1 else ''} sans justificatif"
                             f" ({', '.join(e.label for e in no_receipt[:3])}{'…' if n > 3 else ''})",
                        link=f"/v/{tid}/justificatifs"))
    pending = [e for e in trip.expenses if e.pending]
    if pending:
        n = len(pending)
        out.append(dict(level="warn",
                        text=f"{n} montant{'s' if n > 1 else ''} pas encore connu{'s' if n > 1 else ''} "
                             f"({', '.join(e.label for e in pending[:3])}{'…' if n > 3 else ''}) : "
                             f"{'ils ne sont' if n > 1 else 'il n’est'} pas encore dans les comptes",
                        link=f"/v/{tid}/depenses"))
    no_actual = [e for e in trip.expenses if e.actual is None and not e.pending]
    if no_actual:
        n = len(no_actual)
        out.append(dict(level="info",
                        text=f"{n} dépense{'s' if n > 1 else ''} sans montant réel "
                             f"(on utilise le prévu en attendant)",
                        link=f"/v/{tid}/depenses"))
    for e in trip.expenses:
        if e.actual is not None and e.planned is not None and e.actual > e.planned:
            out.append(dict(level="warn",
                            text=f"« {e.label} » : le réel dépasse le prévu de {E.fmt(e.actual - e.planned)}",
                            link=f"/v/{tid}/depenses#e{e.id}"))
    for a in result.accounts:
        if a.share > 0 and a.paid == 0 and a.advanced == 0 and not a.withdrawn and not a.holds_cash:
            out.append(dict(level="warn", text=f"{a.name} n'a encore rien versé.",
                            link=f"/v/{tid}/p/{a.referent}"))
    for w in result.warnings:
        out.append(dict(level="error", text=w, link=None))
    if not result.is_balanced():
        out.append(dict(level="error", text="Le total ne tombe pas juste : merci de vérifier.", link=None))
    return out


def sub_view(sub: M.SubAccount) -> dict:
    exps = [E.SubExpense(e.id, e.label, e.amount, e.payer_id, e.weight_map) for e in sub.expenses]
    trs = [E.SubTransfer(t.sender_id, t.receiver_id, t.amount) for t in sub.transfers]
    bal = E.sub_balances(sub.members, exps, trs)
    shares = {}
    paid = {}
    for e in exps:
        paid[e.payer] = paid.get(e.payer, 0) + e.amount
        for p, c in E.split_exact(e.amount, e.weights).items():
            shares[p] = shares.get(p, 0) + c
    return dict(balances=bal, settlements=E.minimal_settlements(bal), shares=shares, paid=paid,
                total=sum(e.amount for e in exps))


def parse_date(s) -> dt.date | None:
    if not s:
        return None
    try:
        return dt.date.fromisoformat(str(s))
    except ValueError:
        return None


def ledger(trip: M.Trip, result: E.Result) -> dict:
    lbl = caisse_label(trip)
    """Tableau des comptes (débit / crédit), en centimes, pour l'onglet Participants › Comptes."""
    names = {p.id: p.name for p in trip.participants}
    adv = advances(trip)
    gross_adv, received = {}, {}
    for a in adv:
        target = received if a["income"] else gross_adv
        target[a["pid"]] = target.get(a["pid"], 0) + a["amount"]
    # 1. comptes des participants (familles regroupées), avec le détail de chaque montant (bulles)
    def d(x):
        return x.strftime("%d/%m/%Y") if x else "sans date"

    def lines_of(pid) -> list:
        out = []
        for l in sorted(result.lines_for(pid), key=lambda l: (l.expense is None, l.category, l.label)):
            if l.cents:
                out.append(f"{l.label} : {E.fmt(l.cents)}")
        ps = result.people.get(pid)
        if ps is not None and ps.rounding:
            out.append(f"Arrondi au centime : {E.fmt(ps.rounding, sign=True)}")
        return out

    def tips_for(m) -> dict:
        mv = [x for x in trip.movements if x.participant_id == m.id]
        return dict(
            share=lines_of(m.id) + [f"= {E.fmt(m.share)} au total"],
            received=[f"{a['label']} : {E.fmt(a['amount'])}" for a in adv if a["pid"] == m.id and a["income"]],
            refunded=[f"Rendu le {d(x.date)} : {E.fmt(x.amount)}" + (f" ({x.note})" if x.note else "")
                      for x in mv if x.kind == "remboursement"],
            paid=[f"Versé le {d(x.date)} : {E.fmt(x.amount)}" + (f" ({x.note})" if x.note else "")
                  for x in mv if x.kind == "versement"],
            advanced=[f"{a['label']} : {E.fmt(a['amount'])}" + (" (sa part du paiement)" if a["partial"] else "")
                      for a in adv if a["pid"] == m.id and not a["income"]],
        )

    def balance_tip(row) -> list:
        parts = [E.fmt(row["share"]) + " de part"]
        if row["received"]:
            parts.append("+ " + E.fmt(row["received"]) + " reçus pour " + lbl)
        if row["refunded"]:
            parts.append("+ " + E.fmt(row["refunded"]) + " rendus par " + lbl)
        if row["paid"]:
            parts.append("− " + E.fmt(row["paid"]) + " versés")
        if row["advanced"]:
            parts.append("− " + E.fmt(row["advanced"]) + " avancés")
        b = row["balance"]
        verdict = (f"doit encore {E.fmt(b)}" if b > 0 else f"{lbl} lui doit {E.fmt(-b)}" if b < 0 else "à jour")
        return parts + [f"= {E.fmt(b)} → {verdict}"]

    accounts = []
    for acc in result.accounts:
        rows = []
        for m in acc.members:
            debit = m.share + received.get(m.id, 0) + m.refunded
            credit = m.paid + gross_adv.get(m.id, 0)
            row = dict(id=m.id, name=m.name, share=m.share, received=received.get(m.id, 0), refunded=m.refunded,
                       paid=m.paid, advanced=gross_adv.get(m.id, 0), debit=debit, credit=credit,
                       balance=debit - credit, withdrawn=m.withdrawn, tips=tips_for(m))
            row["tips"]["balance"] = balance_tip(row)
            rows.append(row)
        tot = {k: sum(r[k] for r in rows) for k in ("share", "received", "refunded", "paid", "advanced", "debit",
                                                     "credit", "balance")}
        tips = {k: [f"{r['name']} : {E.fmt(r[k])}" for r in rows if r[k]]
                for k in ("share", "received", "refunded", "paid", "advanced")}
        tips["balance"] = balance_tip(tot) + (["Les comptes de la famille se règlent ensemble."] if acc.is_family else [])
        accounts.append(dict(name=acc.name, family=acc.is_family, members=rows, tips=tips, holds_cash=acc.holds_cash, **tot))
    # 2. dépenses : qui a payé (crédit) / comment c'est réparti (débit)
    shares_by_exp, asso_by_exp = {}, {}
    for (pid, eid), l in result.lines.items():
        if eid != E.GLOBAL:
            shares_by_exp[eid] = shares_by_exp.get(eid, 0) + l.cents
    for c in result.asso_charges:
        if c.expense is not None:
            asso_by_exp[c.expense] = asso_by_exp.get(c.expense, 0) + c.cents
    expenses = []
    for e in trip.expenses:
        amount = result.expense_totals.get(e.id, 0)
        parts = shares_by_exp.get(e.id, 0)
        asso_part = asso_by_exp.get(e.id, 0)
        rounding = parts + asso_part - amount
        n = sum(1 for (pid, eid), l in result.lines.items() if eid == e.id and l.cents)
        expenses.append(dict(id=e.id, label=e.label, income=e.is_income,
                             category=(e.category.name if e.category else "") + (f" › {e.subcategory}" if e.subcategory else ""),
                             pending=e.pending, amount=amount, payers=[(names.get(pid, lbl) if pid else lbl,
                                                                       c * (-1 if e.is_income else 1))
                                                                      for pid, c in e.splits()],
                             parts=parts, people=n, asso=asso_part, rounding=rounding,
                             refund=e.refund or 0, refund_to=(names.get(e.refund_to_id, lbl)
                                                               if e.refund_to_id else lbl),
                             ok=abs(rounding) <= max(n, 1)))   # chaque ligne est au centime le plus proche
    glob = [c for c in result.asso_charges if c.expense is None]
    glob_parts = sum(l.cents for (pid, eid), l in result.lines.items() if eid == E.GLOBAL)
    glob_n = sum(1 for (pid, eid), l in result.lines.items() if eid == E.GLOBAL and l.cents)
    # 3. caisse de l'asso
    cash_in, cash_out = asso_cash(trip)
    return dict(accounts=accounts, expenses=expenses, cash_in=cash_in, cash_out=cash_out,
                global_asso=sum(c.cents for c in glob), global_parts=glob_parts,
                global_ok=abs(glob_parts + sum(c.cents for c in glob)) <= glob_n + len(glob),
                person_rounding=sum(p.rounding for p in result.people.values()),
                total_debit=sum(a["debit"] for a in accounts), total_credit=sum(a["credit"] for a in accounts))


def person_account(trip: M.Trip, result: E.Result, pid: int) -> dict:
    """Compte d'une personne, rangé par catégorie : débit (sa part, argent reçu pour l'asso, rendu par l'asso)
    et crédit (versements, dépenses qu'elle a payées). Débit − crédit = son solde."""
    exp = {e.id: e for e in trip.expenses}
    lbl = caisse_label(trip)
    order = {c.name: i for i, c in enumerate(sorted(trip.categories, key=lambda c: (c.position, c.id)))}
    groups: dict = {}

    def group(name, kind="cat"):
        if name not in groups:
            groups[name] = dict(name=name, kind=kind, debit=0, credit=0, items=[])
        return groups[name]

    def item(g, label, debit=0, credit=0, line=None, eid=None, note=""):
        e_ = exp.get(abs(eid)) if eid else None
        sub = e_.subcategory if e_ is not None else ""
        for it in g["items"]:                       # une ligne par dépense (sa part + ce qu'il a payé)
            if eid is not None and it["eid"] == eid:
                it["debit"] += debit
                it["credit"] += credit
                it["line"] = it["line"] or line
                it["note"] = it["note"] or note
                return
        g["items"].append(dict(label=label, debit=debit, credit=credit, line=line, eid=eid, note=note, sub=sub,
                               est=e_ is not None and is_estimated(e_)))

    for l in result.lines_for(pid):
        if not l.cents and not l.components:
            continue
        if l.expense is None:
            g = group("Ajustements sur le total", "global")
        else:
            e = exp.get(l.expense)
            g = group(e.category.name if e and e.category else "Divers")
        g["debit"] += l.cents                      # sous-total = somme des lignes (chaque dépense tombe juste)
        item(g, l.label, debit=l.cents, line=l, eid=l.expense)
    for a in advances(trip, pid):
        e = exp[a["eid"]]
        g = group(e.category.name if e.category else "Divers")
        if a["income"]:
            g["debit"] += a["amount"]
            item(g, a["label"], debit=a["amount"], eid=-a["eid"], note="reçu pour " + lbl)
        else:
            g["credit"] += a["amount"]
            item(g, a["label"], credit=a["amount"], eid=a["eid"],
                 note="sa part du paiement" if a["partial"] else "payé par lui")
    for m in sorted([x for x in trip.movements if x.participant_id == pid], key=lambda x: (x.date or dt.date.min)):
        if m.kind == "versement":
            g = group("Versements à " + lbl, "versement")
            g["credit"] += m.amount
            item(g, f"Versement du {m.date.strftime('%d/%m/%Y') if m.date else '?'}", credit=m.amount, note=m.note)
        else:
            g = group("Remboursements reçus de " + lbl, "remboursement")
            g["debit"] += m.amount
            item(g, f"Remboursement du {m.date.strftime('%d/%m/%Y') if m.date else '?'}", debit=m.amount, note=m.note)

    share = result.people[pid].share if pid in result.people else 0
    rounding = share - sum(g["debit"] for g in groups.values() if g["kind"] in ("cat", "global")) \
        + sum(a["amount"] for a in advances(trip, pid) if a["income"])     # « reçu pour l'asso » n'est pas sa part
    if rounding:
        g = group("Arrondi au centime", "arrondi")
        g["debit"] += rounding
        g["explain"] = rounding_explain(result, pid, rounding)
    rank = {"cat": 0, "global": 1, "arrondi": 2, "remboursement": 3, "versement": 4}
    out = sorted(groups.values(), key=lambda g: (rank[g["kind"]], order.get(g["name"], 999), g["name"]))
    for g in out:
        g["pct"] = round(100 * g["debit"] / share) if (share > 0 and g["kind"] in ("cat", "global") and g["debit"] > 0) else None
        if any(it.get("sub") for it in g["items"]):        # catégorie avec sous-catégories : le reste = « Autres »
            for it in g["items"]:
                it["sub"] = it.get("sub") or "Autres (sans sous-catégorie)"
                it["other"] = it["sub"] == "Autres (sans sous-catégorie)"
        g["items"].sort(key=lambda it: (it["eid"] is None, bool(it.get("other")), E.sort_key(it.get("sub") or ""),
                                        it["label"]))
        subs = {}
        for it in g["items"]:
            if it.get("sub"):
                d_ = subs.setdefault(it["sub"], dict(debit=0, credit=0, n=0))
                d_["debit"] += it["debit"]
                d_["credit"] += it["credit"]
                d_["n"] += 1
        g["subs"] = subs
    debit, credit = sum(g["debit"] for g in out), sum(g["credit"] for g in out)
    return dict(groups=out, debit=debit, credit=credit, balance=debit - credit)


# ---------------------------------------------------------------- l'asso, acteur à part entière
ASSO_NAME_KEY = M.ASSO_NAME_KEY


def asso_name(s) -> str:
    """Nom proposé pour la caisse des nouveaux voyages (le dernier utilisé) ; « l'asso » s'il n'y en a pas."""
    row = s.get(M.Setting, ASSO_NAME_KEY) if s is not None else None
    return (row.value.strip() if row and row.value and row.value.strip() else "l'asso")


def set_asso_name(s, name: str) -> None:
    name = (name or "").strip()[:100]
    row = s.get(M.Setting, ASSO_NAME_KEY)
    if row is None:
        s.add(M.Setting(key=ASSO_NAME_KEY, value=name))
    else:
        row.value = name


def trip_asso_name(trip: M.Trip, s=None) -> str:
    """Nom de l'asso pour ce voyage (propre à chaque voyage) ; à défaut (voyage créé par une version plus
    ancienne), le nom proposé ; sinon « l'asso »."""
    own = (trip.asso_name or "").strip()
    if own:
        return own
    from sqlalchemy.orm import object_session
    return asso_name(s if s is not None else object_session(trip))


def set_trip_asso_name(trip: M.Trip, name: str, s=None) -> None:
    """Renomme la caisse de ce voyage seulement ; le nom devient aussi celui proposé aux prochains voyages."""
    from sqlalchemy.orm import object_session
    name = (name or "").strip()[:100]
    trip.asso_name = name
    set_asso_name(s if s is not None else object_session(trip), name)   # effacé : les nouveaux voyages aussi


def cashier(trip: M.Trip):
    """Participant qui tient la caisse (voyage sans asso), ou None si c'est l'asso."""
    return next((p for p in trip.participants if p.id == trip.cashier_id), None) if trip.cashier_id else None


def caisse_label(trip: M.Trip, s=None) -> str:
    """Comment on appelle la caisse dans ce voyage : « la caisse (Paul) », le nom de l'asso, ou « l'asso »."""
    c = cashier(trip)
    if c is not None:
        return f"la caisse ({c.name})"
    return trip_asso_name(trip, s)


def asso_account(trip: M.Trip, result: E.Result) -> dict:
    """Compte de l'asso pour ce voyage, rangé comme celui d'une personne : entrées (versements, recettes,
    remboursements de fournisseurs) et sorties (dépenses qu'elle a payées, argent rendu aux participants).
    Seuls les montants réels (vraiment payés ou reçus) comptent dans la caisse."""
    names = {p.id: p.name for p in trip.participants}
    order = {c.name: i for i, c in enumerate(sorted(trip.categories, key=lambda c: (c.position, c.id)))}
    groups: dict = {}

    def group(name, kind):
        if name not in groups:
            groups[name] = dict(name=name, kind=kind, cash_in=0, cash_out=0, items=[])
        return groups[name]

    def add(g, label, cin=0, cout=0, note="", date=None, eid=None):
        g["cash_in"] += cin
        g["cash_out"] += cout
        g["items"].append(dict(label=label, cash_in=cin, cash_out=cout, note=note, date=date, eid=eid))

    unpaid = []          # dépenses de l'asso connues seulement en prévu (pas encore payées)
    for e in trip.expenses:
        asso_part = sum(c for pid, c in e.splits() if pid is None)
        cat = e.category.name if e.category else "Divers"
        if e.actual is None:
            if asso_part and not e.pending:
                unpaid.append(dict(label=e.label, amount=asso_part * (-1 if e.is_income else 1), eid=e.id))
            continue
        others = [f"{names.get(pid, '?')} {E.fmt(c)}" for pid, c in e.splits() if pid is not None]
        note = ("le reste payé par " + ", ".join(others)) if (others and asso_part) else ""
        if asso_part:
            g = group(cat, "cat")
            if e.is_income:
                add(g, e.label, cin=asso_part, note="recette", date=e.date, eid=e.id)
            else:
                add(g, e.label, cout=asso_part, note=note, date=e.date, eid=e.id)
        if e.refund and e.refund_to_id is None:
            add(group(cat, "cat"), f"{e.label} (remboursement reçu)", cin=min(e.refund, e.actual), date=e.date,
                eid=e.id)
    for m in sorted(trip.movements, key=lambda x: (x.date or dt.date.min, x.id)):
        who = names.get(m.participant_id, "?")
        if m.kind == "versement":
            add(group("Versements des participants", "versement"), who, cin=m.amount, note=m.note or "", date=m.date)
        else:
            add(group("Argent rendu aux participants", "remboursement"), who, cout=m.amount, note=m.note or "",
                date=m.date)
    rank = {"versement": 0, "cat": 1, "remboursement": 2}
    out = sorted(groups.values(), key=lambda g: (rank[g["kind"]], order.get(g["name"], 999), g["name"]))
    cash_in, cash_out = sum(g["cash_in"] for g in out), sum(g["cash_out"] for g in out)
    unpaid_total = sum(u["amount"] for u in unpaid)
    ca = result.cashier_account
    holder_balance = ca.balance if ca is not None else 0      # ce que le teneur de caisse doit (lui-même) à la caisse
    final = cash_in - cash_out + result.to_collect - result.to_refund + holder_balance - unpaid_total
    return dict(groups=out, cash_in=cash_in, cash_out=cash_out, cash=cash_in - cash_out,
                holder=ca, holder_balance=holder_balance,
                to_collect=result.to_collect, to_refund=result.to_refund, unpaid=unpaid, unpaid_total=unpaid_total,
                final=final, charges=list(result.asso_charges), charges_total=result.asso_total,
                rounding=result.rounding_gain, expected_final=result.rounding_gain - result.asso_total)


# ---------------------------------------------------------------- envoi du compte de chacun (WhatsApp)
def whatsapp_number(phone: str) -> str:
    """« 06 12 34 56 78 » -> « 33612345678 » (format international sans +), « » si inutilisable."""
    raw = (phone or "").strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""
    if raw.startswith("+"):
        return digits
    if digits.startswith("00"):
        return digits[2:]
    if digits.startswith("0") and len(digits) == 10:          # numéro français
        return "33" + digits[1:]
    return digits


CLOSED_MSG = "Ce voyage est clôturé : il ne peut plus être modifié. Rouvre-le d'abord (page « Où j'en suis »)."


def is_closed(trip: M.Trip) -> bool:
    """Voyage clôturé : ses comptes sont figés (plus aucune modification tant qu'il n'est pas rouvert)."""
    return trip is not None and trip.closed_on is not None


def close_trip(trip: M.Trip, today: dt.date | None = None) -> None:
    trip.closed_on = today or dt.date.today()


def reopen_trip(trip: M.Trip) -> None:
    trip.closed_on = None


def next_trip_name(name: str, existing=()) -> str:
    """Nom proposé pour la copie d'un voyage : l'année du nom avance d'un an (« Hendaye 2026 » → « Hendaye 2027 ») ;
    sans année, « … (copie) ». Jamais un nom déjà pris."""
    import re
    years = list(re.finditer(r"(?<!\d)(20\d\d)(?!\d)", name))
    if years:
        m = years[-1]
        new = name[:m.start()] + str(int(m.group(1)) + 1) + name[m.end():]
    else:
        new = f"{name} (copie)"
    taken = {E.sort_key(x) for x in existing}
    base, n = new, 2
    while E.sort_key(new) in taken:
        new, n = f"{base} ({n})", n + 1
    return new[:200]


AI_NOTES_MAX = 40


def ai_notes(trip: M.Trip) -> list:
    """Points « à vérifier » laissés par l'IA et pas encore réglés : [{"texte": …, "le": date ou None}]."""
    try:
        raw = json.loads(trip.ai_notes) if trip.ai_notes else []
    except ValueError:
        raw = []
    out = []
    for x in raw if isinstance(raw, list) else []:
        if isinstance(x, dict) and str(x.get("texte") or "").strip():
            try:
                when = dt.date.fromisoformat(str(x.get("le")))
            except ValueError:
                when = None
            out.append(dict(texte=str(x["texte"]).strip(), le=when))
    return out


def _save_ai_notes(trip: M.Trip, notes: list) -> None:
    trip.ai_notes = json.dumps([dict(texte=n["texte"], le=n["le"].isoformat() if n["le"] else None)
                                for n in notes[-AI_NOTES_MAX:]], ensure_ascii=False) if notes else ""


def add_ai_notes(trip: M.Trip, texts, today: dt.date | None = None) -> int:
    """Garde les points « à vérifier » d'une réponse d'IA (sans doublon). Renvoie combien ont été ajoutés."""
    notes = ai_notes(trip)
    seen = {E.sort_key(n["texte"]) for n in notes}
    added = 0
    for t_ in texts or []:
        txt = " ".join(str(t_ or "").split())[:500]
        if txt and E.sort_key(txt) not in seen:
            seen.add(E.sort_key(txt))
            notes.append(dict(texte=txt, le=today or dt.date.today()))
            added += 1
    if added:
        _save_ai_notes(trip, notes)
    return added


def drop_ai_note(trip: M.Trip, text: str) -> bool:
    """« C'est vérifié » : retire ce point (désigné par son texte). False s'il n'y est plus."""
    notes = ai_notes(trip)
    keep = [n for n in notes if n["texte"] != str(text or "").strip()]
    if len(keep) == len(notes):
        return False
    _save_ai_notes(trip, keep)
    return True


PAY_INFO_MAX = 1500


def pay_info(trip: M.Trip) -> str:
    """« Comment payer » : le texte libre de l'organisateur (RIB, chèque, lien HelloAsso, consigne…), sans lignes
    vides en trop. Il n'est repris que pour ceux qui doivent encore de l'argent."""
    lines = [" ".join(l.split()) for l in (trip.pay_info or "").replace("\r", "").split("\n")]
    out = []
    for l in lines:
        if l or (out and out[-1]):
            out.append(l)
    return "\n".join(out).strip()


def set_pay_info(trip: M.Trip, text: str) -> None:
    trip.pay_info = str(text or "").replace("\r", "").strip()[:PAY_INFO_MAX]


def owes_money(acc) -> bool:
    """Ce compte doit encore de l'argent à la caisse (donc les consignes « Comment payer » le concernent)."""
    return acc.balance > 0 and not acc.holds_cash


def account_message(trip: M.Trip, result: E.Result, acc) -> str:
    """Message prêt à envoyer à une personne (ou à la personne qui paie pour sa famille)."""
    who = result.people[acc.referent]
    first = who.name.split()[0]
    caisse = caisse_label(trip)
    whose = f"le compte de la {acc.name.lower()[:1] + acc.name[1:]}" if acc.is_family else "ton compte"
    lines = [f"Bonjour {first}, voici {whose} pour le voyage « {trip.name} » :"]
    lines.append(f"• {'part de la famille' if acc.is_family else 'ta part'} : {E.fmt(acc.share)}")
    if acc.paid:
        lines.append(f"• déjà versé : {E.fmt(acc.paid)}")
    if acc.advanced:
        verb = "dépenses payées pour le groupe" if acc.advanced > 0 else "argent reçu pour " + caisse
        lines.append(f"• {verb} : {E.fmt(abs(acc.advanced))}")
    if acc.refunded:
        lines.append(f"• déjà rendu par {caisse} : {E.fmt(acc.refunded)}")
    b = acc.balance
    if acc.holds_cash:
        lines.append("➜ Tu tiens la caisse : ton compte se règle avec elle, rien à verser.")
    elif b > 0:
        lines.append(f"➜ Il {'vous' if acc.is_family else 'te'} reste {E.fmt(b)} à verser à {caisse}.")
        how = pay_info(trip)
        if how:
            lines += ["", "Comment payer :", how, ""]
    elif b < 0:
        lines.append(f"➜ {caisse[:1].upper() + caisse[1:]} {'vous' if acc.is_family else 'te'} doit {E.fmt(-b)}.")
    else:
        lines.append("➜ Tout est réglé ✓")
    lines.append("Le détail par catégorie est dans le PDF joint.")
    return "\n".join(lines)


def rounding_explain(result: E.Result, pid: int, rounding: int | None = None) -> str:
    """Pourquoi une ligne « Arrondi au centime », avec les chiffres de la personne."""
    ps = result.people[pid]
    rounding = ps.rounding if rounding is None else rounding
    exact = sum((l.exact for l in result.lines_for(pid)), E.Fraction(0)) / 100
    shown = ps.share - rounding
    txt = (f"Sa part exacte est de {f'{float(exact):.4f}'.replace('.', ',')}… €, arrondie à {E.fmt(ps.share)}. "
           f"Les montants ci-dessus, arrondis chacun au centime, font {E.fmt(shown)}")
    return txt + (f" : il manque {E.fmt(rounding)}." if rounding > 0 else f" : {E.fmt(-rounding)} de trop.")


# ---------------------------------------------------------------- groupes de personnes (covoiturage…)
def trip_groups(trip: M.Trip) -> list:
    """[{"name": "Minibus", "members": [participants]}] : groupes enregistrés du voyage (membres encore présents)."""
    try:
        raw = json.loads(trip.groups) if trip.groups else []
    except ValueError:
        raw = []
    by_id = {p.id: p for p in trip.participants}
    out = []
    for g in raw:
        members = sorted((by_id[i] for i in g.get("members", []) if i in by_id), key=lambda p: E.sort_key(p.name))
        out.append(dict(name=str(g.get("name") or ""), members=members, ids=[p.id for p in members]))
    return [g for g in out if g["name"]]


def find_group(trip: M.Trip, name: str):
    key = E.sort_key(str(name or "")).strip()
    return next((g for g in trip_groups(trip) if E.sort_key(g["name"]).strip() == key), None)


def save_group(trip: M.Trip, name: str, member_ids, old_name: str | None = None) -> None:
    """Crée ou met à jour un groupe (old_name : son nom avant renommage)."""
    name = " ".join(str(name or "").split())[:60]
    if not name:
        raise ValueError("Donne un nom au groupe (ex. Minibus, Voiture de Thierry).")
    if E.sort_key(name).strip() in ("tous", "tout le monde", "plongeurs", "accompagnants", "asso", "personne") \
            or E.sort_key(name).startswith("famille "):
        raise ValueError(f"« {name} » est déjà un mot réservé : choisis un autre nom.")
    ids = sorted({int(i) for i in member_ids if int(i) in {p.id for p in trip.participants}})
    groups = [dict(name=g["name"], members=g["ids"]) for g in trip_groups(trip)]
    if old_name is None and any(E.sort_key(g["name"]).strip() == E.sort_key(name).strip() for g in groups):
        raise ValueError(f"Il y a déjà un groupe « {name} ».")
    key_old = E.sort_key(old_name or name).strip()
    other = [g for g in groups if E.sort_key(g["name"]).strip() != key_old]
    if any(E.sort_key(g["name"]).strip() == E.sort_key(name).strip() for g in other):
        raise ValueError(f"Il y a déjà un groupe « {name} ».")
    pos = next((i for i, g in enumerate(groups) if E.sort_key(g["name"]).strip() == key_old), None)
    new = dict(name=name, members=ids)
    if pos is None:
        other.append(new)
    else:
        other.insert(pos, new)
    trip.groups = json.dumps(other, ensure_ascii=False)


def delete_group(trip: M.Trip, name: str) -> None:
    key = E.sort_key(name).strip()
    trip.groups = json.dumps([dict(name=g["name"], members=g["ids"]) for g in trip_groups(trip)
                              if E.sort_key(g["name"]).strip() != key], ensure_ascii=False)


def set_person_groups(trip: M.Trip, pid: int, names) -> None:
    """La personne fait partie exactement de ces groupes (les autres membres ne bougent pas)."""
    wanted = {E.sort_key(n).strip() for n in names}
    data = []
    for g in trip_groups(trip):
        ids = set(g["ids"])
        if E.sort_key(g["name"]).strip() in wanted:
            ids.add(pid)
        else:
            ids.discard(pid)
        data.append(dict(name=g["name"], members=sorted(ids)))
    trip.groups = json.dumps(data, ensure_ascii=False)


def save_family(s, trip: M.Trip, name: str, member_ids, referent_id=None, fam: M.Family | None = None) -> M.Family:
    """Crée (fam=None) ou met à jour une famille : nom, membres (une personne n'a qu'une famille), qui paie."""
    name = " ".join(str(name or "").split())
    if name.lower().startswith("famille "):
        name = name[8:].strip()
    if not name:
        raise ValueError("Donne le nom de la famille (ex. Lebrun).")
    other = find_family(trip, name)
    if other is not None and (fam is None or other.id != fam.id):
        raise ValueError(f"Il y a déjà une famille {other.name}.")
    ids = {int(i) for i in member_ids}
    if len(ids) < 2:
        raise ValueError("Une famille, c'est au moins 2 personnes : coche ses membres.")
    if fam is None:
        fam = M.Family(name=name)
        trip.families.append(fam)
        s.flush()
    fam.name = name
    old_fams = set()
    for p in trip.participants:
        if p.id in ids:
            if p.family_id and p.family_id != fam.id:
                old_fams.add(p.family_id)
            p.family_id = fam.id
        elif p.family_id == fam.id:
            p.family_id = None
    ref = int(referent_id) if referent_id else None
    fam.referent_id = ref if ref in ids else sorted(ids)[0] if not fam.referent_id or fam.referent_id not in ids \
        else fam.referent_id
    s.flush()
    for f in list(trip.families):              # une famille vidée (moins de 2 membres) disparaît
        if f.id in old_fams or f.id == fam.id:
            members = [m for m in trip.participants if m.family_id == f.id]
            if len(members) < 2 and f.id != fam.id:
                for m in members:
                    m.family_id = None
                trip.families.remove(f)
                s.delete(f)
    return fam


def delete_family(s, trip: M.Trip, fam: M.Family) -> None:
    """Les membres redeviennent des personnes seules (chacune son compte)."""
    for p in trip.participants:
        if p.family_id == fam.id:
            p.family_id = None
    trip.families.remove(fam)
    s.delete(fam)


VIZ_SLOTS = 8          # couleurs de catégories (ordre fixe) ; au-delà : gris « autres »


def trip_visuals(trip: M.Trip, result: E.Result) -> dict:
    """Données de la page « En images » (montants en centimes).
    La couleur d'une catégorie suit sa place dans le voyage (jamais son rang dans un graphique)."""
    r = result
    slot = {c.id: (i + 1 if i < VIZ_SLOTS - 1 else VIZ_SLOTS) for i, c in enumerate(trip.categories)}
    cat_name = {c.id: c.name for c in trip.categories}
    exps = [e for e in trip.expenses if not e.is_income]
    def key(e):
        return e.category_id if e.category_id in cat_name else None
    # 1. où part l'argent (dépenses seulement, sans les recettes)
    groups = {}
    for e in exps:
        groups.setdefault(key(e), []).append(e)
    spent = sum(r.expense_totals.get(e.id, 0) for e in exps) or 1
    costs = []
    for cid, es in groups.items():
        cents = sum(r.expense_totals.get(e.id, 0) for e in es)
        if cents <= 0:
            continue
        subs = {}
        for e in es:
            if e.subcategory:
                subs[e.subcategory] = subs.get(e.subcategory, 0) + r.expense_totals.get(e.id, 0)
        sub_list = sorted(subs.items(), key=lambda x: -x[1]) if len(subs) > 1 or (subs and sum(subs.values()) < cents) else []
        if sub_list and sum(subs.values()) < cents:
            sub_list.append(("Autres", cents - sum(subs.values())))
        costs.append(dict(name=cat_name.get(cid, "Sans catégorie"), slot=slot.get(cid, VIZ_SLOTS), cents=cents,
                          pct=round(cents * 100 / spent), eids=[e.id for e in es],
                          subs=[dict(name=n, cents=c, pct=round(c * 100 / cents)) for n, c in sub_list]))
    costs.sort(key=lambda x: -x["cents"])
    legend = sorted(costs, key=lambda x: x["slot"])
    # 2. ce que paie chacun, découpé par catégorie
    people = []
    for ps in r.people.values():
        segs = [dict(name=c["name"], slot=c["slot"], cents=r.subtotal(ps.id, c["eids"])) for c in legend]
        segs = [s_ for s_ in segs if s_["cents"] > 0]
        rest = ps.share - sum(s_["cents"] for s_ in segs)
        if rest > 0:
            segs.append(dict(name="Arrondis et ajustements", slot=0, cents=rest))
        people.append(dict(id=ps.id, name=ps.name, share=ps.share, withdrawn=ps.withdrawn, segs=segs))
    people.sort(key=lambda p: (-p["share"], p["name"]))
    pmax = max([sum(s_["cents"] for s_ in p["segs"]) for p in people] + [1])
    # 3. où en sont les règlements (par personne ou famille, sans celui qui tient la caisse)
    L = ledger(trip, r)
    settle = []
    for a in L["accounts"]:
        if a.get("holds_cash"):
            continue
        settle.append(dict(name=a["name"], family=a.get("family"), debit=a["debit"], credit=a["credit"],
                           balance=a["balance"], covered=min(a["credit"], a["debit"])))
    settle.sort(key=lambda x: (-x["balance"], x["name"]))
    smax = max([max(x["debit"], x["credit"]) for x in settle] + [1])
    owed = sum(x["debit"] for x in settle)
    covered = sum(x["covered"] for x in settle)
    to_collect = sum(max(x["balance"], 0) for x in settle)
    to_refund = sum(max(-x["balance"], 0) for x in settle)
    # 4. prévu ou réel (dépenses qui ont les deux montants)
    plan = []
    for cid, es in groups.items():
        both = [e for e in es if e.planned is not None and e.actual is not None]
        waiting = [e for e in es if e.planned is not None and e.actual is None]
        if not both and not waiting:
            continue
        p_ = sum(e.planned for e in both)
        a_ = sum(e.actual for e in both)
        plan.append(dict(name=cat_name.get(cid, "Sans catégorie"), slot=slot.get(cid, VIZ_SLOTS), planned=p_,
                         actual=a_, diff=a_ - p_, n=len(both), waiting=len(waiting)))
    plan.sort(key=lambda x: (-max(x["planned"], x["actual"]), x["name"]))
    plan_max = max([max(x["planned"], x["actual"]) for x in plan] + [1])
    # 5. pour un participant : part médiane par catégorie (sans les désistements)
    diver = typical_diver(trip, r, legend, people)
    # chiffres clés
    payers = [p for p in r.people.values() if not p.withdrawn and p.share > 0]
    shares = sorted(p.share for p in payers)
    wd = [p for p in r.people.values() if p.withdrawn]
    n = len(r.people)
    return dict(
        total=r.total_cost, n_people=n, n_payers=len(payers),
        median=shares[len(shares) // 2] if shares else 0,
        share_min=shares[0] if shares else 0, share_max=shares[-1] if shares else 0,
        wd_n=len(wd), wd_pct=round(len(wd) * 100 / n) if n else 0, wd_names=[p.name for p in wd],
        wd_kept=sum(p.share for p in wd), wd_refund=sum(max(-p.balance, 0) for p in wd),
        costs=costs, legend=legend, spent=spent, people=people, pmax=pmax,
        settle=settle, smax=smax, owed=owed, covered=covered, to_collect=to_collect, to_refund=to_refund,
        plan=plan, plan_max=plan_max, diver=diver,
    )


DIVER_MIN = 3        # en dessous, la médiane dévoilerait la part de quelqu'un


def _median(values: list) -> int:
    v = sorted(values)
    n = len(v)
    return v[n // 2] if n % 2 else round((v[n // 2 - 1] + v[n // 2]) / 2)


def typical_diver(trip: M.Trip, result: E.Result, legend: list, people: list) -> dict | None:
    """Ce que coûte le voyage « pour un participant » : pour chaque catégorie, la part médiane des participants
    (désistements exclus), arrondie à l'euro ; le total est la somme des lignes. La médiane écarte d'elle-même les
    cas extrêmes (une personne venue deux jours…) et ne montre la part de personne en particulier.
    None s'il y a moins de DIVER_MIN participants."""
    people_ = [p for p in people if not p["withdrawn"]]
    if len(people_) < DIVER_MIN:
        return None
    names = [c["name"] for c in legend] + ["Arrondis et ajustements"]
    slots = {c["name"]: c["slot"] for c in legend}
    rows = []
    for name in names:
        vals = [next((sg["cents"] for sg in p["segs"] if sg["name"] == name), 0) for p in people_]
        euros = round(_median(vals) / 100)
        if euros > 0:
            rows.append(dict(name=name, slot=slots.get(name, 0), euros=euros))
    rows.sort(key=lambda x: -x["euros"])                   # la plus chère d'abord (à égalité : ordre du voyage)
    total = sum(x["euros"] for x in rows)
    for x in rows:
        x["pct"] = round(x["euros"] * 100 / total) if total else 0
    lines = [f"Combien a coûté « {trip.name} » pour un participant (environ) :"]
    lines += [f"- {x['name']} : {x['euros']} €" for x in rows]
    lines += [f"Total : environ {total} €",
              f"(montant médian sur {len(people_)} participants, désistements non comptés)"]
    return dict(rows=rows, total=total, n=len(people_), text="\n".join(lines))


EXPORT_COLS = [            # clé, titre, cochée par défaut
    ("nom", "Nom", True), ("telephone", "Téléphone", True), ("email", "E-mail", True), ("note", "Note", False),
    ("roles", "Rôles", True), ("famille", "Famille", False), ("groupes", "Groupes", False),
    ("desiste", "Désisté", False), ("part", "Sa part", False), ("verse", "Déjà versé ou avancé", False),
    ("solde", "Reste à régler", False),
]


def participants_export(trip: M.Trip, result: E.Result, cols: list, with_withdrawn: bool = True) -> tuple:
    """Liste des participants pour l'export : (titres, lignes). Les montants restent en centimes (int)."""
    known = {k: t for k, t, _ in EXPORT_COLS}
    cols = [c for c in cols if c in known] or ["nom"]
    wd = {w.participant_id for w in trip.withdrawals}
    groups = trip_groups(trip)
    accounts = {m.id: a for a in result.accounts for m in a.members}
    rows = []
    for p in sorted(trip.participants, key=lambda p: E.sort_key(p.name)):
        if p.id in wd and not with_withdrawn:
            continue
        ps = result.people.get(p.id)
        acc = accounts.get(p.id)
        row = []
        for c in cols:
            if c == "nom":
                v = p.name
            elif c == "telephone":
                v = p.phone or ""
            elif c == "email":
                v = p.email or ""
            elif c == "note":
                v = p.note or ""
            elif c == "roles":
                v = ", ".join(role_label(r) for r in p.role_list)
            elif c == "famille":
                v = p.family.name if p.family else ""
            elif c == "groupes":
                v = ", ".join(g["name"] for g in groups if p.id in g["ids"])
            elif c == "desiste":
                v = "oui" if p.id in wd else ""
            elif c == "part":
                v = ps.share if ps else 0
            elif c == "verse":
                v = (ps.paid + ps.advanced) if ps else 0
            else:          # reste à régler : celui du compte (famille regroupée, sur la ligne de celui qui paie)
                if acc is None:
                    v = 0
                elif len(acc.members) > 1 and acc.referent != p.id:
                    v = "en famille"
                else:
                    v = acc.balance
            row.append(v)
        rows.append(row)
    return [known[c] for c in cols], rows


ROUNDING_CHOICES = {
    E.ROUND_PARTICIPANTS: "Payés par les participants",
    E.ROUND_ASSO: "Pris en charge par l'asso",
}


def final_sentence(m: dict, caisse: str, fmt, formula: bool = False) -> str:
    """« 1 060,36 € en caisse + 3 213,11 € à recevoir − 4 273,44 € à rembourser = 0,03 € : le voyage rapporte… »"""
    final = m["final"]
    rnd = " (arrondis au centime)" if m["rounding_only"] else ""
    txt = (f"le voyage rapporte {fmt(final)} à {caisse}{rnd}" if final > 0
           else f"le voyage coûte {fmt(-final)} à {caisse}{rnd}" if final < 0
           else f"le voyage ne coûte rien à {caisse}")
    if not (formula and m["adds_up"]):
        return txt[:1].upper() + txt[1:] + "."
    terms = [f"{fmt(m['cash'])} en caisse" if m["cash"] >= 0 else f"− {fmt(-m['cash'])} avancés par {caisse}"]
    if m["to_collect"]:
        terms.append(f"+ {fmt(m['to_collect'])} à recevoir")
    if m["to_refund"]:
        terms.append(f"− {fmt(m['to_refund'])} à rembourser")
    return " ".join(terms) + f" = {fmt(final)} : {txt}."


def money_summary(trip: M.Trip, result: E.Result) -> dict:
    """Synthèse en trois blocs (le coût, la caisse, une fois tout réglé), commune au mail au trésorier et au bilan
    PDF : mêmes chiffres, même ordre. Montants en centimes."""
    r = result
    A = asso_account(trip, r)
    refunded = sum(p.refunded for p in r.people.values())          # déjà rendu aux participants
    advanced = sum(p.advanced for p in r.people.values())          # payé directement par des participants
    paid_out = A["cash_out"] - refunded                             # payé par la caisse aux prestataires
    other_in = A["cash_in"] - r.total_paid                          # recettes (subvention, remboursements…)
    final = A["final"]
    return dict(cost=r.total_cost, paid_out=paid_out, advanced=advanced,
                cost_split=bool(advanced) and paid_out + advanced == r.total_cost,
                received=r.total_paid, other_in=other_in, refunded=refunded, cash=A["cash"],
                asso_total=r.asso_total, to_collect=r.to_collect, to_refund=r.to_refund, final=final,
                rounding_only=final == r.rounding_gain and final != 0,
                adds_up=A["cash"] + r.to_collect - r.to_refund == final)


def contact_text(p: M.Participant, missing: bool = False) -> str:
    """« 06 12 34 56 78 · anne@exemple.fr ». missing : signale ce qui manque (« pas d'e-mail ») plutôt que de le taire."""
    tel, mail = (p.phone or "").strip(), (p.email or "").strip()
    if not missing:
        return " · ".join(x for x in (tel, mail) if x)
    if not tel and not mail:
        return "(aucun contact)"
    return f"{tel or '(pas de téléphone)'} · {mail or '(pas d’e-mail)'}".replace("’", "'")


def treasurer_mail(trip: M.Trip, result: E.Result) -> tuple:
    """Mail au trésorier, prêt à coller : récapitulatif simple, ce qu'il reste à demander aux participants et
    ce qu'il faut leur rembourser, points à vérifier. Renvoie (objet, texte)."""
    r = result
    caisse = caisse_label(trip)
    Caisse = caisse[:1].upper() + caisse[1:]
    fmt = lambda c: E.fmt(c).replace(" ", " ").replace("\xa0", " ")
    names = {p.id: p.name for p in trip.participants}

    def who(acc):
        if acc.is_family and len(acc.members) > 1:
            return f"{acc.name} (à {names.get(acc.referent, '?')})"
        return acc.name + (" (désistement)" if acc.withdrawn else "")

    def why(acc, owes: bool) -> str:
        """Le calcul de la ligne : « part 709,29 € − versé 277,00 € » (ou dans l'autre sens pour un remboursement)."""
        parts = "parts" if acc.is_family and len(acc.members) > 1 else "part"
        plus, minus = [], []
        if acc.share:
            (plus if owes else minus).append(f"{parts} {fmt(acc.share)}")
        if acc.paid:
            (minus if owes else plus).append(f"versé {fmt(acc.paid)}")
        if acc.advanced > 0:
            (minus if owes else plus).append(f"avancé {fmt(acc.advanced)}")
        elif acc.advanced < 0:
            (plus if owes else minus).append(f"reçu à la place de {caisse} {fmt(-acc.advanced)}")
        if acc.refunded:
            (plus if owes else minus).append(f"déjà remboursé {fmt(acc.refunded)}")
        return " ".join([plus[0]] + [f"+ {x}" for x in plus[1:]] + [f"− {x}" for x in minus]) if plus else ""
    owe = sorted([(who(a), c, why(a, True)) for a, b, c in r.settlements() if a != E.ASSO],
                 key=lambda x: (-x[1], x[0]))
    due = sorted([(who(b), c, why(b, False)) for a, b, c in r.settlements() if a == E.ASSO],
                 key=lambda x: (-x[1], x[0]))
    dates = ""
    if trip.start:
        dates = f"du {trip.start.strftime('%d/%m/%Y')}" + (f" au {trip.end.strftime('%d/%m/%Y')}" if trip.end else "")
    info = ", ".join(x for x in [trip.place, dates, f"{len(trip.participants)} participants"] if x)
    subject = f"Voyage « {trip.name} » : bilan et règlements"
    m = money_summary(trip, r)
    # 1. ce qu'on attend du trésorier : les paiements à demander et les remboursements à faire
    if owe or due:
        L = ["Bonjour,", "", f"Pour clôturer le voyage « {trip.name} » ({info}), pouvez-vous procéder aux demandes "
             f"de paiement et aux remboursements ci-dessous ?"]
    else:
        L = ["Bonjour,", "", f"Le voyage « {trip.name} » ({info}) est réglé : il n'y a ni paiement à demander ni "
             f"remboursement à faire."]
    if owe or due:
        L += ["", f"À DEMANDER AUX PARTICIPANTS : {fmt(m['to_collect'])}" if owe else "À DEMANDER AUX PARTICIPANTS : rien"]
        L += [f"- {n} : {fmt(c)}" + (f"  ({w})" if w else "") for n, c, w in owe]
        L += ["", f"À LEUR REMBOURSER : {fmt(m['to_refund'])}" if due else "À LEUR REMBOURSER : rien"]
        L += [f"- {n} : {fmt(c)}" + (f"  ({w})" if w else "") for n, c, w in due]
    # 2. le résumé du voyage, qui justifie ces montants
    L += ["", "Voici le résumé du voyage :", "", "LE COÛT DU VOYAGE", f"- Coût total : {fmt(m['cost'])}"]
    if m["cost_split"]:
        L.append(f"  dont payé par {caisse} : {fmt(m['paid_out'])}, et avancé directement par des participants : "
                 f"{fmt(m['advanced'])}")
    # le détail de ce qu'ont avancé les participants (dépenses payées eux-mêmes, moins ce qu'ils ont reçu à la place
    # de la caisse) : le total retombe sur « avancé directement par des participants »
    adv = {}
    for x in advances(trip):
        adv.setdefault(x["pid"], []).append(x)
    people = [(pid, items, r.people[pid].advanced) for pid, items in adv.items() if pid in r.people]
    people = [x for x in people if x[2] or any(not i["income"] for i in x[1])]
    if people and any(not i["income"] for _, items, _ in people for i in items):
        L += ["", f"AVANCÉ PAR DES PARTICIPANTS : {fmt(m['advanced'])}"]
        for pid, items, net in sorted(people, key=lambda x: (-x[2], names.get(x[0], ""))):
            L.append(f"- {names.get(pid, '?')} : {fmt(net)}")
            for i in items:
                if i["income"]:
                    L.append(f"    · {i['label'].replace(' (remboursement)', '')} : − {fmt(i['amount'])} "
                             f"(remboursement reçu à la place de {caisse})")
                else:
                    L.append(f"    · {i['label']} : {fmt(i['amount'])}")
    # les désistements : ce que chacun paie quand même, qui reprend le reste, où en est son compte
    if trip.withdrawals:
        n = len(trip.withdrawals)
        L += ["", f"DÉSISTEMENTS : {n} sur {len(trip.participants)}"]
        for w in sorted(trip.withdrawals, key=lambda w: E.sort_key(w.participant.name)):
            ps = r.people.get(w.participant_id)
            if ps is None:
                continue
            kept = [(e.label, r.share(ps.id, e.id)) for e in trip.expenses if e.id in w.kept_ids and r.share(ps.id, e.id)]
            L.append(f"- {ps.name}")
            if kept:
                L.append(f"    · paie quand même {fmt(ps.share)} : " + ", ".join(f"{lb} {fmt(c)}" for lb, c in kept))
            else:
                L.append("    · ne paie rien" if not ps.share else f"    · paie quand même {fmt(ps.share)}")
            by = "le groupe" if w.absorber == E.GROUP else caisse
            L.append(f"    · le reste de sa part est repris par {by}" if ps.share else f"    · sa part est reprise par {by}")
            money = []
            if ps.paid:
                money.append(f"a versé {fmt(ps.paid)}")
            if ps.refunded:
                money.append(f"déjà remboursé {fmt(ps.refunded)}")
            acc = r.account_of(ps.id)
            if len(acc.members) > 1:
                money.append(f"se règle avec la {acc.name[:1].lower() + acc.name[1:]}")
            elif acc.balance < 0:
                money.append(f"à lui rembourser {fmt(-acc.balance)}")
            elif acc.balance > 0:
                money.append(f"doit encore {fmt(acc.balance)}")
            else:
                money.append("à jour")
            L.append("    · " + ", ".join(money))
    L += ["", f"LA CAISSE DE {caisse.upper()}" if not caisse.startswith("la caisse") else caisse.upper(),
          f"+ Reçu des participants : {fmt(m['received'])}"]
    if m["other_in"]:
        L.append(f"+ Autres recettes (subvention, remboursement d'un fournisseur…) : {fmt(m['other_in'])}")
    L.append(f"− Payé aux prestataires (hébergement, club…) : {fmt(m['paid_out'])}")
    if m["refunded"]:
        L.append(f"− Déjà remboursé aux participants : {fmt(m['refunded'])}")
    L.append(f"= Dans la caisse aujourd'hui : {fmt(m['cash'])}" if m["cash"] >= 0
             else f"= {Caisse} a avancé {fmt(-m['cash'])} de sa trésorerie (plus de sorties que d'entrées pour l'instant)")
    if m["asso_total"]:
        L.append(f"(et {caisse} prend à sa charge {fmt(m['asso_total'])} sur ce voyage)")
    L += ["", "UNE FOIS TOUT RÉGLÉ", final_sentence(m, caisse, fmt, formula=True)]
    # les contacts de chacun, pour demander les paiements et faire les virements
    if trip.participants:
        gone = {w.participant_id for w in trip.withdrawals}
        L += ["", "CONTACTS DES PARTICIPANTS"]
        for p in sorted(trip.participants, key=lambda p: E.sort_key(p.name)):
            L.append(f"- {p.name}{' (désistement)' if p.id in gone else ''} : {contact_text(p, missing=True)}")
    L += ["", "Le bilan détaillé (PDF) et les justificatifs (ZIP) sont en pièces jointes.",
          "Je me tiens à votre disposition pour toute question.", "", "Bonne journée,"]
    org = organizer_name(trip)
    if org:
        L.append(org)
    return subject, "\n".join(L)


# ---------------------------------------------------------------- personnes déjà venues sur d'autres voyages
def person_key(name: str) -> str:
    """Clé pour reconnaître une personne d'un voyage à l'autre : sans majuscules, accents ni espaces en trop."""
    return " ".join(E.sort_key(str(name or "")).split())


def known_people(trip: M.Trip) -> list:
    """Les personnes des autres voyages qui ne sont pas encore dans celui-ci, une fois chacune (reconnue par son nom),
    avec ses infos les plus récentes : [{key, name, phone, email, companion, trip, trip_id}], par ordre alphabétique.
    Un téléphone ou un e-mail absent du dernier voyage est repris d'un voyage plus ancien."""
    from sqlalchemy.orm import object_session
    s = object_session(trip)
    if s is None:
        return []
    others = [t for t in s.query(M.Trip).filter(M.Trip.id != trip.id)]
    others.sort(key=lambda t: (t.start or dt.date.min, t.id))           # du plus ancien au plus récent
    here = {person_key(p.name) for p in trip.participants}
    out = {}
    for t in others:
        for p in t.participants:
            k = person_key(p.name)
            if not k or k in here:
                continue
            old = out.get(k, {})
            out[k] = dict(key=k, name=p.name.strip(), companion=bool(p.companion), trip=t.name, trip_id=t.id,
                          phone=(p.phone or "").strip() or old.get("phone", ""),
                          email=(p.email or "").strip() or old.get("email", ""),
                          trips=sorted(set(old.get("trips", [])) | {t.id}))
    return sorted(out.values(), key=lambda x: E.sort_key(x["name"]))


def known_trips(trip: M.Trip, known: list) -> list:
    """Les autres voyages qui ont des personnes à reprendre, le plus récent d'abord : [{id, name, people}]."""
    from sqlalchemy.orm import object_session
    s = object_session(trip)
    if s is None or not known:
        return []
    by_trip = {}
    for x in known:
        for tid in x["trips"]:
            by_trip.setdefault(tid, []).append(x["key"])
    trips = [t for t in s.query(M.Trip).filter(M.Trip.id.in_(list(by_trip)))]
    trips.sort(key=lambda t: (t.start or dt.date.min, t.id), reverse=True)
    return [dict(id=t.id, name=t.name, people=by_trip[t.id]) for t in trips]


def known_person(trip: M.Trip, name: str):
    """La personne déjà venue sur un autre voyage qui porte ce nom (ou None)."""
    k = person_key(name)
    return next((x for x in known_people(trip) if x["key"] == k), None) if k else None
