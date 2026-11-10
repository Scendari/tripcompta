"""Remplissage par n'importe quelle IA : modèle à remplir, guide, import JSON (aperçu puis import)."""
import asyncio
import json
import re
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from compta import importer
from compta.db import Database
from compta.demo import seed_demo, seed_family_demo
from compta.ops import Ops, OpsError
from compta.web import create_app

ROOT = Path(__file__).resolve().parents[1]

OLD_MD = (Path(__file__).parent / "data" / "ancien_modele_markdown.md").read_text(encoding="utf-8")

SMALL = {
    "voyage": {"nom": "Week-end test", "lieu": "Annecy", "debut": "12/06/2027", "fin": "2027-06-13"},
    "participants": ["Anne", {"nom": "Bob", "telephone": "0600000000"}, "Chloé"],
    "depenses": [
        {"libelle": "Gîte", "categorie": "Logement", "prevu": "320", "reel": "300", "paye_par": "asso", "pour": "tous"},
        {"libelle": "Essence", "reel": "90", "paye_par": "Anne", "pour": ["Anne", "bob"]},
        {"libelle": "Plongées", "categorie": "Plongée", "reel": "100", "parts": {"Anne": 3, "Chloe": 1}},
    ],
    "versements": [{"participant": "Chloé", "montant": "100", "date": "2027-05-01"},
                   {"participant": "Bob", "montant": "50"}],
}


def guide_example() -> dict:
    return json.loads(re.findall(r"```json\s*(\{.*?\})\s*```", importer.guide(), re.S)[0])


@pytest.fixture()
def ops(tmp_path):
    return Ops(Database(tmp_path / "d"))


def people(res):
    return {p["nom"]: (p["sa_part"], p["reste"]) for p in res["apercu"]["participants"]}


# ---------------------------------------------------------------- montants vérifiés à la main

def test_small_trip_amounts_by_hand(ops):
    r = ops.import_trip(SMALL)
    # Gîte 300 / 3 = 100 chacun ; Essence 90 / 2 = 45 (Anne, Bob) ; Plongées 100 : Anne 75, Chloé 25
    assert people(r) == {"Anne": (220.0, 130.0),      # 100 + 45 + 75 − 90 avancés
                         "Bob": (145.0, 95.0),        # 100 + 45 − 50 versés
                         "Chloé": (125.0, 25.0)}      # 100 + 25 − 100 versés
    a = r["apercu"]
    assert a["cout_total"] == 490.0 and a["deja_recu"] == 150.0 and a["equilibre"]
    assert a["debut"] == "2027-06-12" and a["lieu"] == "Annecy"
    assert {d["libelle"]: d["paye_par"] for d in a["depenses"]} == {"Gîte": "asso", "Essence": "Anne",
                                                                   "Plongées": "asso"}
    assert a["participants"][1]["telephone"] == "0600000000"
    assert r["crees"]["depenses"] == 3 and r["crees"]["versements"] == 2


def test_guide_example_imports_and_balances(ops):
    r = ops.import_trip(guide_example())
    a = r["apercu"]
    assert a["equilibre"] and not r["avertissements"]
    # gîte + plongées + bateau + essence − recette (remboursement acompte) + taxe de séjour ; péage « à venir » = 0
    assert a["cout_total"] == round(1740 + 1050 + 300 + 92.40 - 60 + 75, 2)
    deps = {e["libelle"]: e for e in a["depenses"]}
    assert deps["Sorties en mer"]["paye_par"] == {"asso": 300.0, "Stéphane": 750.0}
    assert deps["Remboursement acompte gîte"]["type"] == "recette" and deps["Péage voiture de Luc"]["a_venir"]
    assert set(deps["Taxe de séjour"]["pour"]) == {n for n in [p_["nom"] for p_ in a["participants"]]} - {"Tom Lebrun", "Antoine"}
    assert set(deps["Essence voiture de Luc"]["pour"]) == {"Luc", "Marie", "Sophie"}         # groupe nommé
    assert a["note"].startswith("Club : Port-Cros")
    p = {x["nom"]: x for x in a["participants"]}
    # Gîte 1 740 € : Tom fixé à 60 € ; les 9 autres (Antoine compris : il garde le gîte) : 1 680 / 9 = 186,67
    assert p["Antoine"]["desiste"] and p["Antoine"]["sa_part"] == 186.67
    assert p["Antoine"]["reste"] == 0                                        # 186,67 − 300 versés + 113,33 rendus
    gite = deps["Gîte 3 nuits"]
    assert gite.get("fixe") == {"Tom Lebrun": 60.0}
    # remises sur tout le voyage : Hugo −40 (asso), Stéphane −25 (les autres), Sophie +80 (les autres)
    assert sorted((x["participant"], x["valeur"], x["compense_par"]) for x in a["ajustements"]) == [
        ("Hugo", -40.0, "asso"), ("Sophie", 80.0, "others"), ("Stéphane", -25.0, "others")]
    assert a["pris_en_charge_par_asso"] and sum(c["montant"] for c in a["pris_en_charge_par_asso"]) == 40.0
    assert p["Luc"]["roles"] == ["Conducteur"] or "Conducteur" in p["Luc"]["roles"]
    # Accompagnants : pas de plongée ; Tom (60 € de gîte) est exclu de la taxe de séjour (« sauf »)
    assert p["Léa Lebrun"]["accompagnant"] and p["Tom Lebrun"]["accompagnant"]
    assert 50 < p["Tom Lebrun"]["sa_part"] < 60                             # 60 € moins sa part des remises
    bateau = next(d for d in a["depenses"] if d["libelle"] == "Bateau")
    assert set(bateau["pour"]) == {n for n, x in p.items() if not x["accompagnant"]}
    # La famille règle ensemble : un seul reste, payé par Paul
    fam = a["familles"][0]
    assert fam["nom"] == "Famille Lebrun" and fam["paie"] == "Paul Lebrun"
    assert set(fam["membres"]) == {"Paul Lebrun", "Julie Lebrun", "Léa Lebrun", "Tom Lebrun"}
    members = sum(p[m]["sa_part"] for m in fam["membres"])
    assert fam["sa_part"] == round(members, 2) and fam["reste"] == round(members - 600, 2)
    assert p["Paul Lebrun"]["a_verse"] == 600.0
    assert len(a["ajustements"]) == 3 and len(a["entre_nous"]) == 1
    assert a["entre_nous"][0]["total"] == 180.0
    assert r["a_verifier"] == guide_example()["a_verifier"]
    total = sum(x["sa_part"] for x in a["participants"]) + sum(c["montant"] for c in a["pris_en_charge_par_asso"])
    assert round(total - a["ecart_arrondi_asso"], 2) == a["cout_total"]


# ---------------------------------------------------------------- comportement

def test_simulation_writes_nothing(ops):
    r = ops.import_trip(SMALL, simulation=True)
    assert r["simulation"] and r["apercu"]["equilibre"]
    assert ops.list_trips() == []


def test_one_undo_removes_whole_import(ops):
    ops.import_trip(SMALL)
    assert len(ops.list_trips()) == 1
    assert "import" in ops.undo()["message"]
    assert ops.list_trips() == []


def test_import_into_existing_trip(tmp_path):
    db = Database(tmp_path / "d")
    tid = seed_demo(db)
    ops = Ops(db)
    before = ops.trip(tid)
    r = ops.import_trip({"participants": ["Zoé", "Luc"],
                         "depenses": [{"libelle": "Glaces", "reel": "24", "paye_par": "Zoé", "pour": ["Zoé", "Luc"]}]},
                        voyage=str(tid))
    after = ops.trip(tid)
    assert len(after["participants"]) == len(before["participants"]) + 1     # Luc existait déjà
    assert r["crees"]["participants"] == 1
    assert after["cout_total"] == round(before["cout_total"] + 24, 2) and after["equilibre"]
    zoe = next(p for p in after["participants"] if p["nom"] == "Zoé")
    # 12 € de glaces (pas ajoutée en douce aux anciennes dépenses) + quelques centimes : la réduction
    # « encadrant » de Thierry est compensée par tous les autres, Zoé comprise.
    assert 12.0 <= zoe["sa_part"] < 12.5 and zoe["a_avance"] == 24.0
    assert any("Zoé" in n and "déjà saisies" in n for n in r["a_verifier"])


def test_all_errors_reported_at_once_as_questions(ops):
    bad = json.loads(json.dumps(SMALL))
    bad["depenses"][1]["paye_par"] = "Zoé"
    bad["depenses"][2]["reel"] = "-5"
    bad["versements"][0]["montant"] = "cent"
    with pytest.raises(OpsError) as e:
        ops.import_trip(bad)
    errs = e.value.errors
    assert len(errs) == 3
    assert errs[0].startswith("Dépense n°2 « Essence »") and "Faut-il l'ajouter" in errs[0]
    assert "positif" in errs[1] and "Versement n°1" in errs[2]
    assert ops.list_trips() == []                      # rien n'est enregistré
    assert ops.db.last_action() is None                # pas d'instantané orphelin


@pytest.mark.parametrize("text,msg", [
    ("", "Colle d'abord"),
    ("Désolé, je n'ai pas compris", "pas de bloc JSON"),
    ('```json\n{"voyage": {"nom": "x"},}\n```', "mal formé"),
    ('{"participants": ["A"]}', "nom du voyage"),
])
def test_bad_input_messages(ops, text, msg):
    with pytest.raises(OpsError, match=msg):
        ops.import_trip(text)


def test_parse_full_ai_answer_with_prose_and_curly_quotes(ops):
    answer = ("Voici le récapitulatif…\n\n```json\n" + json.dumps(SMALL, ensure_ascii=False, indent=1)
              + "\n```\n\nDis-moi si tout est bon !")
    assert ops.import_trip(answer, simulation=True)["apercu"]["cout_total"] == 490.0
    curly = json.dumps(SMALL, ensure_ascii=False).replace('"', "“", 1).replace('"', "”", 1)
    assert ops.import_trip(curly, simulation=True)["apercu"]["equilibre"]


def test_unknown_sections_are_reported(ops):
    r = ops.import_trip({**SMALL, "budget": 12, "a_verifier": ["Date du péage ?"]}, simulation=True)
    assert r["a_verifier"][0] == "Date du péage ?" and "budget" in r["a_verifier"][1]


def test_templates_cover_the_format():
    t = importer.template().upper()
    for word in ["RÔLES", "PARTICIPANTS", "FAMILLES", "GROUPES", "CAISSE", "DÉPENSES", "VERSÉ", "RENDU",
                 "REMISES OU SUPPLÉMENTS", "DÉSISTEMENTS", "ENTRE NOUS", "AUTRE CHOSE", "FIXÉ"]:
        assert word in t
    assert "NOM DU VOYAGE" not in t                 # le voyage est déjà créé dans l'application
    assert importer.update_template().strip() == ""          # mise à jour : texte libre (idées sous la zone)
    blocks = [json.loads(b) for b in re.findall(r"```json\s*(\{.*?\})\s*```", importer.guide(), re.S)]
    assert len(blocks) == 2 and all(b["voyage_id"] == 2 for b in blocks)
    assert set(blocks[0]) | set(blocks[1]) | {"voyage_id"} <= importer.KNOWN
    assert {"depenses_a_modifier", "depenses_a_supprimer", "participants_a_modifier",
            "versements_a_supprimer"} <= set(blocks[1])


# ---------------------------------------------------------------- page IA d'un voyage

UPDATE = {
    "participants": [{"nom": "Zoé", "accompagnant": True}],
    "participants_a_modifier": [{"participant": "Hugo", "roles": ["dp"], "telephone": "06 00"}],
    "depenses": [{"libelle": "Pot de fin", "categorie": "Repas", "reel": "45", "paye_par": "Marie", "pour": "tous"}],
    "depenses_a_modifier": [{"depense": "Restaurant de clôture", "reel": "612"},
                            {"depense": "Péage A7 aller-retour — voiture de Luc", "paye_par": "Marie"}],
    "depenses_a_supprimer": ["Sortie palmes-masque-tuba"],
    "versements": [{"participant": "Hugo", "montant": "100", "date": "2027-05-20"}],
}


@pytest.fixture()
def demo_app(tmp_path):
    app = create_app(tmp_path / "d")
    tid = seed_demo(app.state.db)
    return app, TestClient(app), tid


def test_ia_lives_in_a_trip(demo_app):
    app, c, tid = demo_app
    r = c.get("/ia", follow_redirects=False)
    assert r.status_code == 303 and "ouvre-le" in r.headers["location"].replace("%2C", ",").replace("%20", " ")
    page = c.get(f"/v/{tid}/ia").text
    assert 'href="/v/%d/ia" class="on"' % tid in page                       # entrée du menu du voyage
    assert 'class="on" aria-selected="true">Mettre à jour le voyage' in page  # voyage rempli : mise à jour
    assert "Décris la mise à jour à faire" in page and "Des idées de ce qu'on peut demander" in page
    assert "Remplis le modèle" not in page and "Effacer le texte" in page
    state = json.loads(json.loads(re.search(r"const STATE = (.*?);\n", page).group(1)))   # état joint au message
    assert state["voyage_id"] == tid and "Gîte « Les Pins » — 3 nuits" in [e["libelle"] for e in state["depenses"]]
    assert {"nom": "Stéphane"}.items() <= next(p for p in state["participants"] if p["nom"] == "Stéphane").items()
    assert "Travaille sur le voyage n°%d" % tid in page                         # pour le connecteur Claude
    assert "Selon le prévu" not in page                                         # plus de bouton prévu / réel
    empty = Ops(app.state.db).create_trip("Vide")["voyage_id"]
    page = c.get(f"/v/{empty}/ia").text
    assert 'class="on" aria-selected="true">Démarrer le voyage' in page and "DÉMARRER LE VOYAGE" in page
    assert "MISE" not in c.get(f"/v/{empty}/ia?modele=initialisation").text.split('id="modele"')[1][:200]
    assert "Décris la mise à jour" in c.get(f"/v/{empty}/ia?modele=mise-a-jour").text
    for m in ("initialisation", "mise-a-jour"):
        r = c.get(f"/ia/modele-{m}.txt")
        assert r.status_code == 200 and "text/plain" in r.headers["content-type"] and "**" not in r.text
    assert c.get("/ia/modele-autre.txt").status_code == 404


def test_update_preview_then_save_then_undo(demo_app):
    app, c, tid = demo_app
    ops = Ops(app.state.db)
    before = ops.trip(tid)
    texte = "Voici les changements :\n```json\n" + json.dumps({"voyage_id": tid, **UPDATE}, ensure_ascii=False) + "\n```"
    r = c.post(f"/v/{tid}/ia/apercu", data={"texte": texte})
    page = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text)).replace("\u202f", " ")
    for line in ["Nouveau participant : Zoé (accompagnant)", "Hugo : téléphone → 06 00 ; rôles → DP",
                 "Suppression de la dépense « Sortie palmes-masque-tuba »",
                 "« Restaurant de clôture » : réel — → 612,00 €",
                 "« Péage A7 aller-retour — voiture de Luc » : payé par Luc → Marie",
                 "Nouvelle dépense « Pot de fin » : 45,00 €, payée par Marie", "Hugo a versé 100,00 €"]:
        assert line in page, line
    assert "Qui doit quoi : avant → après" in page and "Comptes équilibrés ✓" in page
    assert "« Avant » = ce que chacun doit aujourd" in page and "nouveau" in page      # Zoé n'existait pas avant
    assert ops.trip(tid) == before                                   # aperçu : rien n'est enregistré
    r = c.post(f"/v/{tid}/ia/importer", data={"texte": texte}, follow_redirects=False)
    assert r.headers["location"].startswith(f"/v/{tid}?msg=")
    d = ops.trip(tid)
    deps = {e["libelle"]: e for e in d["depenses"]}
    assert "Sortie palmes-masque-tuba" not in deps and deps["Restaurant de clôture"]["reel"] == 612.0
    assert deps["Péage A7 aller-retour — voiture de Luc"]["paye_par"] == "Marie"
    assert deps["Pot de fin"]["reel"] == 45.0 and "Zoé" in deps["Pot de fin"]["pour"]
    hugo = next(p for p in d["participants"] if p["nom"] == "Hugo")
    assert hugo["roles"] == ["DP"] and hugo["telephone"] == "06 00"           # l'étiquette DP du voyage
    assert next(p for p in d["participants"] if p["nom"] == "Zoé")["accompagnant"]
    resto_prevu = next(e for e in before["depenses"] if e["libelle"] == "Restaurant de clôture")["prevu"]
    tuba = next(e for e in before["depenses"] if e["libelle"] == "Sortie palmes-masque-tuba")
    tuba_amt = tuba["reel"] if tuba["reel"] is not None else tuba["prevu"]
    assert d["equilibre"] and d["cout_total"] == round(before["cout_total"] - tuba_amt - resto_prevu + 612 + 45, 2)
    c.post("/annuler")                                               # un seul « Annuler » défait tout
    assert ops.trip(tid) == before


def test_start_of_trip_shows_only_after(tmp_path):
    app = create_app(tmp_path / "d")
    tid = Ops(app.state.db).create_trip("Vide")["voyage_id"]
    c = TestClient(app)
    r = c.post(f"/v/{tid}/ia/apercu", data={"texte": json.dumps({"voyage_id": tid, **SMALL}, ensure_ascii=False)})
    page = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text))
    assert "Qui doit quoi après ces changements" in page and "avant → après" not in page
    assert "Anne doit 130,00 €" in page.replace("\u202f", " ")


def test_guard_against_wrong_trip(demo_app):
    app, c, tid = demo_app
    other = seed_family_demo(app.state.db)
    texte = json.dumps({"voyage_id": tid, **UPDATE}, ensure_ascii=False)
    r = c.post(f"/v/{other}/ia/apercu", data={"texte": texte})
    assert f"Cette réponse concerne le voyage n°{tid} « Plongée Port-Cros" in r.text and "Colle-la dans le bon voyage" in r.text
    r = c.post(f"/v/{tid}/ia/apercu", data={"texte": json.dumps(UPDATE, ensure_ascii=False)})
    assert "Il manque « voyage_id »" in r.text
    r = c.post(f"/v/{tid}/ia/importer", data={"texte": json.dumps(UPDATE, ensure_ascii=False)})
    assert "À corriger avant d" in r.text                            # l'enregistrement est refusé aussi


def test_update_errors_are_questions(demo_app):
    app, c, tid = demo_app
    ops = Ops(app.state.db)
    bad = {"voyage_id": tid,
           "depenses_a_modifier": [{"depense": "Bateau", "reel": "10"}, {"depense": "Gîte", "reel": "-5"}],
           "depenses_a_supprimer": ["Pizza"],
           "versements_a_supprimer": [{"participant": "Hugo", "montant": "999"}],
           "participants_a_modifier": [{"participant": "Hugo", "roles": ["chef"]}, {"participant": "Marie", "nouveau_nom": "Luc"}]}
    with pytest.raises(OpsError) as e:
        ops.import_trip(bad, str(tid), simulation=True, verifier_id=True)
    errs = e.value.errors
    assert any("Participant à modifier n°1 « Hugo » : Rôle inconnu" in x for x in errs)
    assert any("déjà un participant nommé « Luc »" in x for x in errs)
    assert any(x.startswith("Dépense à supprimer n°1 « Pizza » : Dépense introuvable") for x in errs)
    assert not any("Bateau" in x for x in errs)       # « Bateau » = la seule dépense qui le contient (voir aperçu)
    assert any("Gîte" in x and "positif" in x for x in errs)
    assert any("Je ne trouve pas de versement de 999,00 € pour Hugo. Ses mouvements :" in re.sub(r"[\u202f\u00a0]", " ", x)
               for x in errs)


def test_correct_a_payment(demo_app):
    app, c, tid = demo_app
    ops = Ops(app.state.db)
    ops.add_movement(tid, "Hugo", "100", date="2027-04-20")
    before = next(p for p in ops.trip(tid)["participants"] if p["nom"] == "Hugo")["a_verse"]
    fix = {"voyage_id": tid, "versements_a_supprimer": [{"participant": "Hugo", "montant": "100", "date": "20/04/2027"}],
           "versements": [{"participant": "Hugo", "montant": "150", "date": "2027-04-20"}]}
    r = ops.import_trip(fix, str(tid), verifier_id=True)
    acts = [re.sub(r"[\u202f\u00a0]", " ", a) for a in r["actions"]]
    assert acts == ["Suppression du versement de 100,00 € (Hugo)", "Hugo a versé 150,00 €"]
    after = next(p for p in ops.trip(tid)["participants"] if p["nom"] == "Hugo")["a_verse"]
    assert after == before + 50


def test_drafts_per_trip_and_template(demo_app):
    app, c, tid = demo_app
    other = Ops(app.state.db).create_trip("Autre")["voyage_id"]
    dd = app.state.db.dir / "brouillons_ia"
    page = c.get(f"/v/{tid}/ia").text
    assert "enregistré automatiquement" in page and 'id="modele-restore" hidden' in page
    texte = "Pot de fin 45 € — « été » ✓\n"
    assert c.put(f"/v/{tid}/ia/brouillon/mise-a-jour", content=texte.encode()).status_code == 200
    f = dd / f"voyage-{tid}-mise-a-jour.txt"
    assert f.read_text(encoding="utf-8") == texte
    assert "« été » ✓" in c.get(f"/v/{tid}/ia").text and "Brouillon enregistré sur ton ordinateur" in c.get(f"/v/{tid}/ia").text
    assert "« été » ✓" not in c.get(f"/v/{tid}/ia?modele=initialisation").text      # un brouillon par modèle
    assert "« été » ✓" not in c.get(f"/v/{other}/ia?modele=mise-a-jour").text       # et par voyage
    assert c.put(f"/v/{tid}/ia/brouillon/autre", content=b"x").status_code == 404
    assert c.put("/v/999/ia/brouillon/mise-a-jour", content=b"x").status_code == 404
    # sendBeacon envoie un POST
    assert c.post(f"/v/{tid}/ia/brouillon/mise-a-jour", content=(texte + "Luc a versé 50\n").encode()).status_code == 200
    # Remettre au propre : l'ancien brouillon est gardé, et récupérable
    assert c.post(f"/v/{tid}/ia/brouillon/mise-a-jour/vider").json()["modele"] == importer.update_template()
    assert not f.exists()
    assert 'id="modele-restore" hidden' not in c.get(f"/v/{tid}/ia").text
    assert "Luc a versé 50" in c.post(f"/v/{tid}/ia/brouillon/mise-a-jour/recuperer").json()["modele"]
    assert "Luc a versé 50" in f.read_text(encoding="utf-8")
    # remettre au propre un modèle encore vierge n'écrase pas le brouillon précédent
    c.post(f"/v/{tid}/ia/brouillon/mise-a-jour/vider")
    c.put(f"/v/{tid}/ia/brouillon/mise-a-jour", content=importer.update_template().encode())
    c.post(f"/v/{tid}/ia/brouillon/mise-a-jour/vider")
    assert "Luc a versé 50" in c.post(f"/v/{tid}/ia/brouillon/mise-a-jour/recuperer").json()["modele"]


def test_browser_draft_survives_crash_and_reload(tmp_path):
    """On tape, l'application « plante » (enregistrement impossible), on recharge : rien n'est perdu."""
    import socket
    import threading
    import time
    import uvicorn
    from playwright.sync_api import sync_playwright
    app = create_app(tmp_path / "d")
    tid = Ops(app.state.db).create_trip("Banyuls")["voyage_id"]
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]

    def start():
        srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        th = threading.Thread(target=srv.run, daemon=True)
        th.start()
        while not srv.started:
            time.sleep(0.05)
        return srv, th

    def stop(srv, th):
        srv.should_exit = True
        th.join(5)

    def draft_has(text, timeout=5.0):
        """Le brouillon sur l'ordinateur contient-il ce texte ? (on laisse à l'écriture le temps de se faire :
        une machine lente, comme un Windows de test, peut afficher « Enregistré » un instant avant)."""
        end = time.time() + timeout
        while time.time() < end:
            if draft.exists() and text in draft.read_text(encoding="utf-8"):
                return True
            time.sleep(0.1)
        return False

    srv, th = start()
    base = f"http://127.0.0.1:{port}/v/{tid}"
    draft = tmp_path / "d" / "brouillons_ia" / f"voyage-{tid}-initialisation.txt"
    saved = "document.getElementById('modele-status').textContent.includes('Enregistré')"
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_context().new_page()
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(base + "/ia")
            type_after(pg, "Organisateur :", " Banyuls")
            pg.wait_for_function(saved)
            assert draft_has("Banyuls")                                    # sur l'ordinateur, sans rien cliquer
            # Plantage : l'application s'arrête pendant qu'on tape
            stop(srv, th)
            pg.keyboard.type(" en juin")
            pg.wait_for_function("document.getElementById('modele-status').textContent.includes('Pas pu enregistrer')")
            pg.goto("about:blank")                    # on ferme la page, l'application est toujours arrêtée
            assert "en juin" not in draft.read_text(encoding="utf-8")
            srv, th = start()                          # on relance l'application
            pg.goto(base + "/ia")
            assert "Banyuls en juin" in pg.evaluate("window.iaModel()")   # copie gardée par le navigateur
            pg.wait_for_function(saved)                                    # et renvoyée à l'ordinateur
            assert draft_has("Banyuls en juin")
            # Un autre navigateur (sans copie locale) retrouve aussi le brouillon
            other = b.new_context().new_page()
            other.goto(base + "/ia")
            assert "Banyuls en juin" in other.evaluate("window.iaModel()")
            # Quitter la page juste après avoir tapé (avant l'enregistrement différé) : la dernière frappe part quand même
            other.close()
            type_after(pg, "DP (directeur de plongée) :", " Pyrénées")
            assert "Pyrénées" in pg.evaluate("window.iaModel()")
            pg.goto("about:blank")
            assert draft_has("Pyrénées", timeout=10)
            assert errors == []
            b.close()
    finally:
        stop(srv, th)


def test_api_import(tmp_path):
    c = TestClient(create_app(tmp_path / "d"))
    r = c.post("/api/import", json={"donnees": SMALL, "simulation": True})
    assert r.status_code == 200 and r.json()["simulation"]
    bad = {**SMALL, "versements": [{"participant": "Zoé", "montant": "5"}]}
    r = c.post("/api/import", json={"donnees": bad})
    assert r.status_code == 400 and len(r.json()["erreurs"]) == 1
    assert c.post("/api/import", json={"donnees": SMALL}).json()["voyage_id"]


def test_mcp_import_tool(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        params = StdioServerParameters(command=sys.executable,
                                       args=["-m", "compta.mcp_server", "--data-dir", str(tmp_path / "d")],
                                       cwd=str(ROOT),
                                       # les réglages de l'ordinateur isolés, comme pour les autres tests
                                       env={"TRIPCOMPTA_CONFIG": os.environ["TRIPCOMPTA_CONFIG"]})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                g = await session.call_tool("lire_guide", {})
                assert "Format du JSON" in g.content[0].text
                res = await session.call_tool("importer_voyage", {"donnees": SMALL, "simulation": False})
                assert json.loads(res.content[0].text)["apercu"]["cout_total"] == 490.0

    asyncio.run(run())


# ---------------------------------------------------------------- navigateur

def test_browser_copy_message_and_import(server):
    from playwright.sync_api import sync_playwright
    base, tid, _ = server
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(permissions=["clipboard-read", "clipboard-write"])
        pg = ctx.new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("dialog", lambda d: d.accept())
        pg.goto(f"{base}/v/{tid}")
        pg.click("nav >> text=🤖 IA")
        assert pg.locator(".tabs a.on").inner_text() == "Mettre à jour le voyage"
        assert pg.locator("#conf").is_hidden()                     # la config MCP est repliée
        assert pg.locator("textarea#modele").is_visible()
        type_after(pg, "NOUVELLES DÉPENSES", "\nPot de fin 45 € avancé par Marie pour tous")
        pg.click("#copy-msg")
        clip = pg.evaluate("navigator.clipboard.readText()")
        assert f"Voyage n°{tid} : « Plongée Port-Cros" in clip and f'"voyage_id": {tid}' in clip
        assert "MODE D'EMPLOI POUR L'IA" in clip and "Format du JSON pour mettre à jour" in clip
        assert "=== ÉTAT ACTUEL DU VOYAGE" in clip and "Gîte « Les Pins »" in clip
        assert "Pot de fin 45 € avancé par Marie" in clip and "Ne redonne pas ce qui existe déjà" in clip
        pg.wait_for_function("document.getElementById('modele-status').textContent.includes('Enregistré')")
        pg.reload()
        assert "Pot de fin 45" in pg.input_value("#modele")          # brouillon conservé
        with pg.expect_download() as dl:
            pg.click("#modele-dl")
        assert "Pot de fin 45" in open(dl.value.path(), encoding="utf-8").read()
        pg.click("#modele-reset")
        pg.wait_for_function("!document.getElementById('modele').value.includes('Pot de fin 45')")
        assert pg.locator("#modele-restore").is_visible()
        # onglet « Démarrer » : autre modèle, autre brouillon
        pg.click(".tabs >> text=Démarrer le voyage")
        assert "DÉMARRER LE VOYAGE" in pg.input_value("#modele")
        pg.click(".tabs >> text=Mettre à jour le voyage")
        upd = {"voyage_id": tid, "depenses": [{"libelle": "Pot de fin", "reel": "45", "paye_par": "Marie", "pour": "tous"}]}
        pg.fill("#texte", "Voilà :\n```json\n" + json.dumps(upd, ensure_ascii=False) + "\n```")
        pg.click("text=Voir l'aperçu")
        assert pg.locator("#resultat").is_visible()
        assert "Nouvelle dépense « Pot de fin »" in pg.locator("#actions").inner_text()
        assert pg.locator("#avant-apres tbody tr").count() >= 10
        pg.click("text=Enregistrer ces changements")
        assert f"/v/{tid}" in pg.url and "1 changement enregistré" in pg.locator(".flash").inner_text()
        pg.goto(f"{base}/v/{tid}/ia")
        pg.fill("#texte", json.dumps({**upd, "voyage_id": 999}))
        pg.click("text=Voir l'aperçu")
        assert "concerne le voyage n°999" in pg.locator("#errors").inner_text()
        pg.click("#copy-errors")
        assert f'"voyage_id": {tid}' in pg.evaluate("navigator.clipboard.readText()")
        assert errors == []
        b.close()


def type_after(pg, marker, text):
    """Place le curseur à la fin de la ligne qui contient `marker` (comme un clic) puis tape au clavier."""
    pg.evaluate("""([marker]) => { const ta = document.getElementById('modele');
        const i = ta.value.indexOf(marker); const end = ta.value.indexOf('\\n', i);
        ta.focus(); ta.setSelectionRange(end, end); }""", [marker])
    pg.keyboard.type(text)

