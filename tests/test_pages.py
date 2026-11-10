"""Les pages rangées par domaine (compta/pages/) : web.py garde le démarrage et ce que les pages ont en commun,
chaque domaine a son module. Ces essais vérifient le rangement lui-même : rien d'oublié, aucune adresse cachée par
une autre, deux applications qui ne se mélangent pas. Le comportement des pages, lui, est vérifié partout ailleurs.
"""
import ast
import re
from pathlib import Path

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Match

from compta import pages
from compta.web import create_app

ROOT = Path(__file__).resolve().parent.parent
PAGES = ROOT / "compta" / "pages"
MODULES = sorted(p.stem for p in PAGES.glob("*.py") if p.stem != "__init__")


def source(name):
    return (PAGES / f"{name}.py").read_text(encoding="utf-8")


def routes(app):
    return [r for r in app.router.routes if isinstance(r, APIRoute)]


# ---------------------------------------------------------------- rien d'oublié
def test_every_page_module_is_installed_once():
    """Un module ajouté dans compta/pages/ sans être nommé dans install_all n'existerait pas dans l'application
    (et l'application fabriquée ne l'embarquerait pas)."""
    tree = ast.parse(source("__init__"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "install_all")
    imported = [a.name for n in ast.walk(fn) if isinstance(n, ast.ImportFrom) for a in n.names]
    loop = next(n for n in ast.walk(fn) if isinstance(n, ast.For))
    installed = [e.id for e in loop.iter.elts]
    assert sorted(imported) == MODULES
    assert sorted(installed) == MODULES and len(set(installed)) == len(installed)
    assert len(MODULES) == 15
    for name in MODULES:
        tree = ast.parse(source(name))
        assert [n.name for n in tree.body if isinstance(n, ast.FunctionDef)] == ["install"], name
        assert ast.get_docstring(tree), f"{name} : dire en une phrase ce que ce module contient"


def test_the_built_application_takes_the_pages():
    build = (ROOT / "build_tools" / "build.py").read_text(encoding="utf-8")
    assert '"--collect-submodules", "compta.pages"' in build


def test_web_keeps_only_what_pages_share():
    """Les pages d'un voyage ne reviennent pas dans web.py : il n'y reste que les pages d'attente du démarrage."""
    src = (ROOT / "compta" / "web.py").read_text(encoding="utf-8")
    declared = re.findall(r'@app\.(?:get|post)\("([^"]+)"', src)
    assert declared and not [a for a in declared if a.startswith("/v/")]
    assert src.count("pages.install_all(site)") == 1
    assert len(src.splitlines()) < 1000


def test_everything_a_page_takes_from_site_exists():
    """Une page prend ce qu'elle partage dans `site` : chaque nom lu doit être donné par web.py ou prêté par une
    autre page (une faute de frappe dans un nom prêté ne se verrait qu'au moment d'ouvrir la page)."""
    web = (ROOT / "compta" / "web.py").read_text(encoding="utf-8")
    given = set(re.findall(r"(\w+)=\1\b", web[web.index("vars(site).update("):web.index("pages.install_all(site)")]))
    assert {"app", "db", "render", "back", "refuse", "trip_or_404"} <= given
    given.add("vault_box")
    lent = {}
    for name in MODULES:
        for n in re.findall(r"^    site\.(\w+) = \1$", source(name), flags=re.M):
            assert n not in lent and n not in given, f"{n} est donné deux fois"
            lent[n] = name
    for name in MODULES:
        read = set(re.findall(r"\bsite\.(\w+)", source(name)))
        unknown = read - given - set(lent)
        assert not unknown, f"pages/{name}.py lit site.{sorted(unknown)} que personne ne donne"
    assert lent == {"_return_path": "general", "_restart_page": "reglages", "drafts": "ia",
                    "_refused_files": "justificatifs", "_store_receipt": "justificatifs", "_send_rows": "envoyer"}


# ---------------------------------------------------------------- les adresses
def sample(path):
    """Une adresse d'exemple pour une route : /v/{tid}/argent/{mid} → /v/7/argent/7."""
    return re.sub(r"\{\w+\}", "7", re.sub(r"\{\w+:path\}", "a/b", path))


def first_match(app, path, method):
    for r in app.router.routes:
        if r.matches({"type": "http", "path": path, "method": method, "root_path": ""})[0] == Match.FULL:
            return r


def test_no_address_is_declared_twice_or_hidden_by_another(tmp_path):
    """L'ordre des routes compte : /v/7/argent/dates ne doit pas être prise pour le mouvement « dates ». Chaque
    adresse mène bien à sa propre page, pas à une autre déclarée avant elle."""
    app = create_app(tmp_path)
    seen = set()
    for r in routes(app):
        for method in r.methods - {"HEAD"}:
            assert (method, r.path) not in seen, f"{method} {r.path} est déclarée deux fois"
            seen.add((method, r.path))
            got = first_match(app, sample(r.path), method)
            assert got is r, f"{method} {r.path} est cachée par {getattr(got, 'path', got)}"
    assert len(seen) > 150


def test_the_order_that_matters(tmp_path):
    """Quelques adresses qui se ressemblent, vérifiées une à une (elles sont dans des modules différents ou
    dépendent de l'ordre dans leur module)."""
    app = create_app(tmp_path)
    for method, path, name in [
            ("GET", "/v/7/argent/dates", "pay_dates_page"), ("POST", "/v/7/argent/dates/retirer", "pay_dates_clear"),
            ("POST", "/v/7/argent/lot", "money_batch"), ("GET", "/v/7/participants/comptes", None),
            ("GET", "/v/7/participants/organisation", None), ("GET", "/v/7/depenses/nouvelle", None),
            ("GET", "/v/7/export", None), ("GET", "/v/7/export.pdf", None), ("GET", "/ping", "ping"),
            ("GET", "/reglages/diagnostic.txt", None)]:
        r = first_match(app, path, method)
        assert isinstance(r, APIRoute) and "{" not in r.path.replace("{tid}", ""), (path, r.path)
        assert r.path == path.replace("/7/", "/{tid}/"), (path, r.path)
        if name:
            assert r.name == name, (path, r.name)


def test_pages_come_in_their_order(tmp_path):
    """Les pages sont installées dans l'ordre de pages/__init__.py : général d'abord, « envoyer » à la fin."""
    paths = [r.path for r in routes(create_app(tmp_path)) if not r.path.startswith("/api")]
    order = ["/annuler", "/reglages/a-propos", "/reglages", "/deverrouiller", "/ia", "/", "/v/{tid}/participants",
             "/v/{tid}/depenses", "/v/{tid}/repartition", "/v/{tid}/desistement", "/v/{tid}/argent",
             "/v/{tid}/justificatifs", "/v/{tid}/entre-nous", "/v/{tid}/export", "/v/{tid}/envoyer"]
    where = [paths.index(p) for p in order]
    assert where == sorted(where), list(zip(order, where))


# ---------------------------------------------------------------- deux applications ne se mélangent pas
def test_two_applications_share_nothing(tmp_path):
    """Chaque application a son propre `site` : ses voyages, ses brouillons, son état. (Les modules de pages ne
    gardent rien pour eux : tout ce qui change est dans `site` ou dans la base.)"""
    a, b = create_app(tmp_path / "a"), create_app(tmp_path / "b")
    ca, cb = TestClient(a), TestClient(b)
    r = ca.post("/voyages", data=dict(name="Voyage de A", place="Ici", organizer="Asso", start="2026-05-01",
                                      end="2026-05-03"), follow_redirects=False)
    assert r.status_code == 303
    assert "Voyage de A" in ca.get("/").text
    assert "Voyage de A" not in cb.get("/").text
    assert a.state.db is not b.state.db
    for name in MODULES:                         # aucun état gardé au niveau d'un module de pages
        tree = ast.parse(source(name))
        kept = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.Expr))]
        assert not kept, f"pages/{name}.py garde quelque chose en dehors de install()"


def test_site_is_only_a_bag():
    assert [n for n in vars(pages.Site) if not n.startswith("__")] == []
