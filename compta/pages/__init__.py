"""Les pages de l'application web, rangées par domaine.

Chaque module a une fonction `install(site)` qui déclare ses routes. `site` (créé dans web.py) porte ce que les
pages ont en commun : l'application, la base, `render`, `back`, `refuse`… et l'état de la protection par mot de
passe (`site.vault_box`). Une page peut aussi y prêter une fonction à une autre (`site._store_receipt`…).
"""


class Site:
    """Ce que les pages ont en commun. Rempli par create_app, complété par les pages qui prêtent une fonction."""


def install_all(site) -> None:
    """Déclare toutes les pages. L'ordre est celui des routes : il compte quand deux adresses se ressemblent."""
    # imports écrits en toutes lettres : l'outil qui fabrique l'application (PyInstaller) doit les voir
    from . import (argent, depenses, desistement, entre_nous, envoyer, general, ia, justificatifs, mises_a_jour,
                   participants, protection, reglages, repartition, transfert, voyages)
    for page in (general, mises_a_jour, reglages, protection, ia, voyages, participants, depenses, repartition,
                 desistement, argent, justificatifs, entre_nous, transfert, envoyer):
        page.install(site)
