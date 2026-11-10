"""Messages de refus en rouge (et non dans le bandeau vert des réussites) ; ajout de plusieurs participants d'un coup ;
« Ajouter et continuer » qui garde le formulaire ouvert."""
import html
import re

import pytest
from fastapi.testclient import TestClient

from compta.ops import Ops
from compta.web import create_app


def clean(t):
    return re.sub(r"\s+", " ", html.unescape(t).replace("\xa0", " "))


@pytest.fixture
def env(tmp_path):
    app = create_app(tmp_path / "d")
    ops = Ops(app.state.db)
    tid = ops.import_trip({"voyage": {"nom": "x"}, "participants": ["Anne", "Bob"],
                           "depenses": [{"libelle": "Gîte", "reel": "400", "paye_par": "asso", "pour": "tous"}]})["voyage_id"]
    return app, ops, tid, TestClient(app)


def dest(loc):
    """Où l'on arrive au bout du compte : l'étape « rejoindre les dépenses de groupe » renvoie ensuite à « next »."""
    from urllib.parse import parse_qs, urlparse
    return parse_qs(urlparse(loc).query)["next"][0] if "/rejoindre" in loc else loc


def names(ops, tid):
    return [p["nom"] for p in ops.trip(tid)["participants"]]


# ---------------------------------------------------------------- refus en rouge
def test_refusal_is_red_with_alert_role(env):
    app, ops, tid, c = env
    r = c.post(f"/v/{tid}/participants", data={"name": "Anne", "roles_form": "1"}, follow_redirects=False)
    assert r.status_code == 303 and "e=1" in r.headers["location"]
    page = c.get(r.headers["location"]).text
    flash = re.search(r'<div class="flash[^"]*"[^>]*>.*?</div>', page, re.S).group(0)
    assert 'class="flash err"' in flash and 'role="alert"' in flash and "flash-ico" in flash
    assert "déjà dans la liste" in clean(flash)


def test_success_stays_green(env):
    app, ops, tid, c = env
    r = c.post(f"/v/{tid}/participants", data={"name": "Chloé", "roles_form": "1"}, follow_redirects=False)
    assert "e=1" not in r.headers["location"]
    page = c.get(r.headers["location"]).text
    flash = re.search(r'<div class="flash[^"]*"[^>]*>', page).group(0)
    assert 'class="flash"' in flash and 'role="status"' in flash


def test_invalid_amount_is_red(env):
    """Le cas du rapport : « Montant invalide » ne s'affiche plus comme une réussite."""
    app, ops, tid, c = env
    pid = ops.trip(tid)["participants"][0]["id"]
    r = c.post(f"/v/{tid}/argent", data={"kind": "versement", "participant": str(pid), "amount": "abc"},
               headers={"referer": f"http://testserver/v/{tid}/argent"}, follow_redirects=False)
    assert r.status_code == 303 and "e=1" in r.headers["location"], r.headers.get("location")
    page = c.get(r.headers["location"]).text
    assert 'class="flash err"' in page and "Montant invalide" in clean(page)
    assert not ops.trip(tid).get("mouvements")


def test_error_flag_does_not_stick_to_a_later_message(env):
    """Un refus, corrigé depuis la même page : le message de réussite qui suit n'est pas rouge."""
    app, ops, tid, c = env
    bad = c.post(f"/v/{tid}/participants", data={"name": "", "roles_form": "1"}, follow_redirects=False).headers["location"]
    assert "e=1" in bad
    r = c.post(f"/v/{tid}/depenses", data={"label": "Pain", "actual": "x"},
               headers={"referer": "http://testserver" + bad}, follow_redirects=False)
    loc = r.headers["location"]
    assert loc.count("msg=") == 1 and loc.count("e=1") == 1     # l'ancien message et son drapeau sont retirés


def test_error_page_without_flag_is_not_red(env):
    app, ops, tid, c = env
    page = c.get(f"/v/{tid}/participants?msg=Bonjour").text
    assert 'class="flash"' in page and 'class="flash err"' not in page


# ---------------------------------------------------------------- plusieurs participants d'un coup
def test_bulk_form_is_on_the_page(env):
    app, ops, tid, c = env
    page = c.get(f"/v/{tid}/participants").text
    assert 'name="names"' in page and "Plusieurs personnes d'un coup" in html.unescape(page)
    assert 'id="add-many-form"' in page


def test_bulk_add_one_name_per_line(env):
    app, ops, tid, c = env
    r = c.post(f"/v/{tid}/participants", data={"names": "Chloé Roux\n  Denis Petit \n\nÉmile\tZola\n", "roles_form": "1"},
               follow_redirects=False)
    assert names(ops, tid) == ["Anne", "Bob", "Chloé Roux", "Denis Petit", "Émile Zola"]
    page = clean(c.get(r.headers["location"]).text)
    assert "3 participants ajoutés" in page and 'class="flash err"' not in page


def test_bulk_add_skips_names_already_there(env):
    app, ops, tid, c = env
    r = c.post(f"/v/{tid}/participants", data={"names": "anne\nChloé\nBOB\nChloé", "roles_form": "1"},
               follow_redirects=False)
    assert names(ops, tid) == ["Anne", "Bob", "Chloé"]
    page = clean(c.get(r.headers["location"]).text)
    assert "Chloé ajouté" in page and "(3 déjà dans la liste)" in page


def test_bulk_add_all_known_is_a_red_refusal_and_keeps_the_panel_open(env):
    app, ops, tid, c = env
    r = c.post(f"/v/{tid}/participants", data={"names": "Anne\nBob", "roles_form": "1"}, follow_redirects=False)
    loc = r.headers["location"]
    assert "liste=1" in loc and "e=1" in loc and "#" not in loc      # pas d'ancre : le message rouge reste visible en haut
    page = c.get(loc).text
    assert 'class="flash err"' in page and "Tous ces noms sont déjà dans la liste" in clean(page)
    assert re.search(r'<details class="add-many" id="ajout-liste"\s+open', page)
    assert names(ops, tid) == ["Anne", "Bob"]


def test_bulk_add_empty_list(env):
    app, ops, tid, c = env
    r = c.post(f"/v/{tid}/participants", data={"names": " \n \n", "roles_form": "1"}, follow_redirects=False)
    assert "Écris au moins un nom" in clean(c.get(r.headers["location"]).text)


def test_bulk_add_gives_no_role_like_the_single_form(env):
    app, ops, tid, c = env
    c.post(f"/v/{tid}/participants", data={"names": "Chloé\nDenis", "roles_form": "1"})
    people = {p["nom"]: p for p in ops.trip(tid)["participants"]}
    assert not people["Chloé"].get("roles") and not people["Denis"].get("roles")


def test_bulk_add_can_be_undone_in_one_go(env):
    app, ops, tid, c = env
    r = c.post(f"/v/{tid}/participants", data={"names": "Chloé\nDenis\nÉmile", "roles_form": "1"}, follow_redirects=False)
    token = re.search(r"[?&]d=([^&#]+)", r.headers["location"]).group(1)
    from urllib.parse import unquote
    c.post("/annuler", data={"d": unquote(token)}, headers={"referer": f"http://testserver/v/{tid}/participants"})
    assert names(ops, tid) == ["Anne", "Bob"]


# ---------------------------------------------------------------- « Ajouter et continuer »
def test_add_and_continue_keeps_the_form_open(env):
    app, ops, tid, c = env
    r = c.post(f"/v/{tid}/participants", data={"name": "Chloé", "roles_form": "1", "again": "1"}, follow_redirects=False)
    loc = dest(r.headers["location"])
    assert loc == f"/v/{tid}/participants?ajout=1#ajout"
    page = c.get(loc).text
    assert re.search(r'<details class="card add-person" id="ajout"\s+open', page)
    assert "autofocus" in page and "Chloé" in names(ops, tid)


def test_plain_add_still_closes_the_form(env):
    app, ops, tid, c = env
    r = c.post(f"/v/{tid}/participants", data={"name": "Chloé", "roles_form": "1"}, follow_redirects=False)
    loc = dest(r.headers["location"])
    assert "ajout=1" not in loc and "#pe" in loc


def test_enter_key_button_is_plain_add(env):
    """Le premier bouton d'envoi du formulaire (celui de la touche Entrée) est « Ajouter », pas « Ajouter et continuer »."""
    app, ops, tid, c = env
    page = c.get(f"/v/{tid}/participants?ajout=1").text
    form = re.search(r'<form[^>]*id="add-form".*?</form>', page, re.S).group(0)
    submits = [b for b in re.findall(r"<button[^>]*>", form) if 'type="button"' not in b]
    assert 'name="again"' not in submits[0] and 'name="again"' in submits[1]
