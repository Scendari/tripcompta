"""Les guides (INSTALLER.md, README.md) disent vrai : les boutons qu'ils citent existent à l'écran, les fichiers à
télécharger sont ceux que l'application attend, les dossiers sont ceux que le code utilise. Un libellé renommé
dans l'application sans corriger le guide fait échouer ces essais.
"""
import re
from pathlib import Path

import pytest

from compta import db as D
from compta import paths as P
from compta import updater as U
from compta import web

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = (ROOT / "INSTALLER.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
TEMPLATES = ROOT / "compta" / "templates"


def flat(text):
    """Un guide sans ses retours à la ligne ni son gras : un libellé coupé en fin de ligne reste trouvable."""
    return re.sub(r"\s+", " ", text.replace("**", ""))


def screen(name):
    """Le texte d'un écran, sans les balises (et sans les espaces insécables)."""
    src = (TEMPLATES / name).read_text(encoding="utf-8")
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", src).replace("\xa0", " "))


def section(text, title):
    """Le contenu d'une rubrique « ## Titre », jusqu'à la rubrique suivante de même niveau."""
    m = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", text, flags=re.M | re.S)
    assert m, f"rubrique absente : {title}"
    return m.group(1)


# ---------------------------------------------------------------- télécharger
def test_the_files_to_download_are_the_ones_the_application_expects():
    files = {U.asset_name("darwin", "arm64"), U.asset_name("darwin", "x86_64"), U.asset_name("win32", "amd64")}
    assert files == {"TripCompta-mac-arm64.zip", "TripCompta-mac-intel.zip", "TripCompta-windows-setup.exe"}
    base = f"https://github.com/{U.REPO}/releases/latest/download/"
    for f in files:
        assert f"({base}{f})" in INSTALLER, f
    assert set(re.findall(r"TripCompta-[a-z0-9-]+\.(?:zip|exe|tar\.gz)", INSTALLER)) == files
    assert "TripCompta-mac-$A.zip" in INSTALLER                    # la commande : arm64 ou intel, comme les fichiers
    assert U.asset_name("linux", "x86_64") is None and "tar.gz" not in INSTALLER
    assert U.INSTALL_HELP.endswith("/INSTALLER.md")


def test_the_french_download_page_comes_first():
    """Le site (page Télécharger, en français) avant la liste de fichiers ; plus de « tout en bas, dans Assets »."""
    assert INSTALLER.index("https://tripcompta.plongee.bio/telecharger") < INSTALLER.index("releases/latest/download")
    assert "Assets" not in INSTALLER and "releases/latest)" not in INSTALLER
    assert web.GUIDE_URL in INSTALLER and web.GUIDE_URL in README


def test_on_mac_the_manual_way_comes_before_the_terminal():
    mac = section(INSTALLER, "Mac")
    assert mac.index("### À la main") < mac.index("### Ou bien : une commande")
    assert "Le plus simple : une commande" not in INSTALLER
    command = mac[mac.index("### Ou bien : une commande"):]
    assert "rm -rf /Applications/TripCompta.app" in command        # la commande est donnée telle quelle…
    assert "retire l'ancienne" in command and "tes voyages ne sont" in command      # …et dit ce qu'elle efface
    assert "Ouvrir quand même" in mac and "Confidentialité et sécurité" in mac


def test_no_statement_from_before_remains():
    """Les écarts relevés : l'application a sa fenêtre, le menu n'a que deux commandes, « À propos » est dans les
    réglages, plus de version Linux toute prête."""
    for text in (INSTALLER, README):
        assert "Ouvrir dans le navigateur" not in text
        assert "s'affiche dans ton navigateur" not in text and "dans ton navigateur" not in text
        assert "remplacer.ps1" not in text
    assert "En bas de l'accueil" not in INSTALLER
    assert "## Linux" not in INSTALLER and "pas encore signé" not in INSTALLER
    assert "4 actions courantes" not in README and "C'est fait" not in README
    assert "modele_ajustement" not in README and "convertis automatiquement" not in README
    assert "sa propre fenêtre" in INSTALLER
    from compta import fenetre
    menu = (ROOT / "compta" / "fenetre.py").read_text(encoding="utf-8")
    assert fenetre and menu.count("MenuAction(") == 2 and "Nouvelle fenêtre" in menu and "Rechercher dans la page" in menu


# ---------------------------------------------------------------- les boutons cités existent
@pytest.mark.parametrize("label, where", [
    ("Où sont mes voyages", "settings.html"), ("Mot de passe", "settings.html"), ("Sauvegardes", "settings.html"),
    ("Voyage d'exemple", "settings.html"), ("Version et mises à jour", "settings.html"),
    ("Changer de dossier", "data_dir.html"), ("Parcourir…", "data_dir.html"), ("Ouvrir le dossier", "data_dir.html"),
    ("Ouvrir les voyages de ce dossier", "data_dir.html"), ("Copier mes voyages dans ce dossier", "data_dir.html"),
    ("Exporter cette version (.zip)", "data_dir.html"),
    ("Oublier le mot de passe sur cet ordinateur", "protection_settings.html"),
    ("Retirer la protection", "protection_settings.html"),
    ("Revenir à cette sauvegarde", "backups.html"),
    ("Exporter un diagnostic", "about.html"),
    ("Exporter le voyage", "base.html"), ("Exporter tous les voyages", "transfer.html"),
    ("Télécharger le fichier du voyage", "transfer.html"),
    ("Importer un voyage", "index.html"), ("Mot de passe oublié ?", "unlock.html"),
    ("Verrouiller", "base.html"), ("Défaire", "base.html"), ("Aide", "base.html"),
    ("Glisse Trip Compta dans le dossier Applications", "base.html"), ("Mettre à jour", "base.html"),
])
def test_every_button_named_in_the_install_guide_exists(label, where):
    assert label in flat(INSTALLER), f"le guide ne cite plus « {label} » : retirer cette ligne de l'essai"
    assert label in screen(where), f"« {label} » n'est plus sur l'écran {where} : corriger INSTALLER.md"


@pytest.mark.parametrize("label, where", [
    ("Nouveau voyage", "index.html"), ("Importer un voyage", "index.html"), ("Où j'en suis", "base.html"),
    ("Pour démarrer", "dashboard.html"), ("À regarder", "dashboard.html"),
    ("Argent reçu et rendu", "base.html"), ("Reçu", "money.html"), ("Remboursé", "money.html"),
    ("À relancer aujourd'hui", "money.html"), ("Qui paie quoi", "base.html"), ("En images", "base.html"),
    ("Envoyer les comptes", "base.html"), ("Exporter le bilan", "base.html"), ("Exporter le voyage", "base.html"),
    ("Désistement", "base.html"), ("Entre nous", "base.html"), ("Justificatifs", "base.html"),
    ("Catégories", "base.html"), ("Sauvegardes", "settings.html"), ("Mot de passe", "settings.html"),
    ("Ajouter les réglages de plongée", "organisation.html"), ("Arrêter", "base.html"),
    ("Copier mes voyages dans ce dossier", "data_dir.html"), ("Ouvrir les voyages de ce dossier", "data_dir.html"),
    ("La ranger", "data_dir.html"),
])
def test_every_screen_named_in_the_readme_exists(label, where):
    assert label in flat(README), f"le README ne cite plus « {label} » : retirer cette ligne de l'essai"
    assert label in screen(where), f"« {label} » n'est plus sur l'écran {where} : corriger README.md"


def test_categories_screen_is_described_in_its_real_order():
    page = screen("categories.html")
    assert page.index("Catégories de ce voyage") < page.index("Catégories disponibles")
    row = next(l for l in README.splitlines() if l.startswith("| 🏷️ **Catégories**"))
    assert row.index("catégories du voyage") < row.index("catégories disponibles")


# ---------------------------------------------------------------- dossiers et nombres
def test_folders_named_in_the_guides_are_the_real_ones(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPCOMPTA_CONFIG", raising=False)
    monkeypatch.setattr(P.sys, "platform", "darwin")
    monkeypatch.setattr(P.Path, "home", classmethod(lambda cls: tmp_path))
    mac = P.config_file().parent
    assert mac == tmp_path / "Library" / "Application Support" / "TripCompta"
    assert "`~/Library/Application Support/TripCompta`" in section(INSTALLER, "Désinstaller")
    monkeypatch.setattr(P.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert P.config_file().parent == tmp_path / "Roaming" / "TripCompta"
    assert "`%APPDATA%\\TripCompta`" in section(INSTALLER, "Désinstaller")
    # les sauvegardes quotidiennes sont bien dans ce dossier-là (c'est pour cela que le guide prévient)
    monkeypatch.setattr(P.sys, "platform", "darwin")
    assert D.backups_dir_for(tmp_path / "voyages").parent == mac / "sauvegardes"
    assert P.FOLDER == "TripCompta" and "Documents/TripCompta" in INSTALLER and "Documents\\TripCompta" in INSTALLER


def test_numbers_in_the_guides_are_the_real_ones():
    assert D.BACKUP_KEEP == 10 and "Les 10 dernières sont gardées" in section(INSTALLER, "Sauvegardes")
    assert "les 10\ndernières sont gardées" in README
    assert D.UNDO_KEEP == 30 and "30 dernières modifications" in README


def test_new_computer_and_uninstall_sections_say_the_essential():
    new = section(INSTALLER, "Nouvel ordinateur")
    for words in ("Exporter tous les voyages", "Importer un voyage", "Documents/TripCompta", "quitte Trip Compta",
                  "même mot de passe", "clé de secours", "sauvegardes quotidiennes ne suivent pas",
                  "Vérifie avant d'effacer", "Un seul ordinateur à la fois"):
        assert words in new, words
    out = section(INSTALLER, "Désinstaller")
    assert "n'efface pas tes voyages" in out
    mac, win = out.split("### Sur Windows")
    assert "### Sur Mac" in mac and "corbeille" in mac and "⌘Q" in mac
    assert "Applications installées" in win
    for part in (mac, win):                       # tout effacer : dans l'ordre, et en prévenant
        assert part.index("Oublier le mot de passe sur cet ordinateur") < part.index("Tes voyages")
        assert "⚠️" in part and "sauvegardes quotidiennes" in part


def test_links_inside_the_guides_lead_somewhere():
    """Chaque lien « (#rubrique) » mène à un titre qui existe (comme GitHub fabrique ses ancres)."""
    def anchors(text):
        out = set()
        text = re.sub(r"^```.*?^```", "", text, flags=re.M | re.S)          # pas les commentaires des exemples
        for title in re.findall(r"^#{1,4} (.+)$", text, flags=re.M):
            a = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
            out.add(a)
        return out
    for name, text in (("INSTALLER.md", INSTALLER), ("README.md", README)):
        known = anchors(text)
        for link in re.findall(r"\]\(#([^)]+)\)", text):
            assert link in known, f"{name} : le lien #{link} ne mène nulle part"
        for target in re.findall(r"\]\(((?!https?:|#|mailto:)[^)#]+)\)", text):
            assert (ROOT / target).exists(), f"{name} : {target} n'existe pas"


# ---------------------------------------------------------------- le README des développeurs
def test_readme_lists_every_module():
    code = section(README, "Pour les développeurs")
    for f in sorted((ROOT / "compta").glob("*.py")):
        if f.name in ("__init__.py", "_build.py"):
            continue
        assert f.name in code, f"README : {f.name} n'est pas présenté dans « Le code »"
    assert "pages/" in code and "writes.py" in code
    for option in re.findall(r'add_argument\("(--[a-z-]+)"', (ROOT / "run.py").read_text(encoding="utf-8")):
        if option in ("--apres-maj",):
            continue
        assert f"`{option}" in code, f"README : l'option {option} de run.py n'est pas expliquée"
    for f in re.findall(r"^  (test_\w+\.py)", code, flags=re.M):
        assert (ROOT / "tests" / f).exists(), f"README : {f} n'existe pas"


def test_readme_api_example_carries_the_session_token():
    api = README[README.index("### API JSON"):README.index("## Pour les développeurs")]
    assert "Authorization: Bearer" in api and "curl" in api and "/ping" in api
    assert "Authorization: Bearer" in screen("ai.html")
