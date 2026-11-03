# Trip Compta

Application locale pour l'organisateur d'un voyage associatif : participants, dépenses, qui a payé quoi,
avances, montants fixés et remises, désistements, justificatifs, comptes « entre nous », et un bilan PDF lisible
par un trésorier qui n'est pas comptable. Une IA (Claude) peut aussi remplir l'application à partir d'un fichier.

Tout fonctionne **sur ton ordinateur**, dans ton navigateur. Rien n'est envoyé sur Internet (sauf, une fois par
jour, la question « y a-t-il une nouvelle version ? », désactivable).

> **👉 Installer Trip Compta (application toute prête, sans Python, qui se met à jour toute seule) :
> voir [INSTALLER.md](INSTALLER.md).** La suite concerne le lancement depuis le code source.

---

## Lancer l'application

### Mac

1. Installe Python 3 (une seule fois) : <https://www.python.org/downloads/>
2. Double-clique sur **`Lancer Trip Compta.command`**.
   - Le premier lancement installe ce qu'il faut (1 à 2 minutes).
   - Si macOS bloque le fichier : clic droit → **Ouvrir** → **Ouvrir**.
3. Le navigateur s'ouvre tout seul sur l'application. Pour l'arrêter : bouton **⏻ Arrêter** en haut à droite
   en bas de l'accueil, ou ferme la fenêtre du Terminal.

### Windows

1. Installe Python 3 (une seule fois) : <https://www.python.org/downloads/> — coche **« Add python.exe to PATH »**.
2. Double-clique sur **`Lancer Trip Compta.bat`**.
   - Le premier lancement installe ce qu'il faut (1 à 2 minutes).
3. Le navigateur s'ouvre tout seul. Pour arrêter : ferme la fenêtre noire (ou le bouton **⏻ Arrêter**).

### En ligne de commande (les deux systèmes)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt          # Windows : .venv\Scripts\pip install -r requirements.txt
.venv/bin/python run.py                             # Windows : .venv\Scripts\python run.py
```

Options de `run.py` :

| Option | Effet |
|---|---|
| `--demo` | ajoute les voyages de démonstration (plongée Port-Cros, 12 participants ; week-end Banyuls avec familles et accompagnants) |
| `--port 9000` | change le port (par défaut 8765, ou le suivant s'il est pris) |
| `--no-browser` | n'ouvre pas le navigateur |
| `--data-dir DOSSIER` | range les données ailleurs |
| `--mcp` | démarre le serveur MCP pour une IA (voir plus bas) |

### Version « sans Python » (exécutable autonome)

```bash
pip install -r requirements-dev.txt
python build_tools/build.py
```

Sur **Mac**, cela produit `dist/Trip Compta.app` ; sur Windows ou Linux, la même commande lancée **sur ce
système** produit `dist/Trip Compta/` (PyInstaller ne fabrique que pour le système où il tourne). Une application
fabriquée ainsi est une **version de développement** (« 1.0.0-dev (commit) ») : elle ne se met jamais à jour
toute seule. Les versions publiées sont fabriquées par GitHub (voir « Publier une version »).

---

## Où sont mes données ?

Dans le dossier **`Documents/TripCompta`** :

- `compta.db` : la base (tous les voyages) ;
- `justificatifs/` : les photos et PDF importés ;
- `sauvegardes/` : une copie automatique par jour (les 10 dernières) ;
- `.annuler/` et `.retablir/` : l'historique des boutons **↶ Défaire** et **↷ Rétablir** (30 dernières actions) ;
- `brouillons_ia/` : les textes que tu remplis sur la page 🤖 IA de chaque voyage (enregistrés à la volée).

Une base créée avec une version plus ancienne est mise à niveau toute seule à l'ouverture.

**Sauvegarder = copier ce dossier** (clé USB, Dropbox, iCloud…).

---

## Ce que fait l'application

| Écran | À quoi ça sert |
|---|---|
| 🏠 **Où j'en suis** | coût, déjà reçu, reste à recevoir, reste à rembourser, alertes en clair, les 4 actions courantes |
| 👥 **Participants** | ajout rapide (un nom par ligne), plongeurs ou **accompagnants**, **familles**, **rôles** (organisateur, co-organisateurs, DP), fiche de chacun |
| Fiche participant | « ce qu'il doit / ce qu'il a déjà donné / reste » ; clic sur une ligne = le calcul expliqué |
| 🏷️ **Catégories** | à gauche les catégories disponibles (communes à tous les voyages, on peut en créer et supprimer celles qu'on a créées), à droite celles du voyage ; un clic pour passer de l'une à l'autre |
| 🧾 **Dépenses** | par catégorie, prévu / réel (ou « montant pas encore connu »), payé par l'asso, avancé par un participant ou **par plusieurs** (montant de chacun), pour qui (parts) ; **argent reçu** (remboursement d'un fournisseur, subvention) |
| 👥 **Participants › Comptes** | débit (ce que chacun doit) / crédit (ce qu'il a donné) et solde, par personne ou famille ; « Détail des comptes » pour le compte de chacun |
| ▦ **Qui paie quoi** | tableau participants × dépenses ; clic sur une case = ajuster ce montant |
| 💶 **Argent reçu et rendu** | versements, remboursements, « qui doit quoi à qui » avec bouton « C'est fait » |
| 🔒 **Montant fixé** (dans la dépense) | tape un montant pour une personne : il est fixé, le reste se répartit tout seul entre les autres, même si la dépense change |
| 🎟️ **Remise ou supplément** (fiche du participant) | sur tout son voyage, compensé par l'asso ou par les autres |
| 🚪 **Désistement** | parts non remboursables conservées, le reste repris par le groupe ou l'asso, remboursement |
| 📎 **Justificatifs** | photos/PDF numérotés J01, J02…, liables à plusieurs dépenses |
| 🤝 **Entre nous** | petits comptes entre participants, hors asso (type Tricount), PDF à partager |
| 📄 **Exporter** | bilan PDF pour le trésorier + ZIP des justificatifs renommés |
| 🤖 **IA** (dans chaque voyage) | modèle « démarrer » ou « mettre à jour », message pour n'importe quelle IA (avec l'état du voyage), aperçu avant → après, enregistrement |

En haut : thème clair/sombre, **⏻ Arrêter** (tout à droite). Les calculs prennent toujours le **montant réel s'il est saisi, sinon le prévu** ; une dépense comptée sur son prévu est signalée (≈, en couleur) et un rappel indique combien il en reste.
Après chaque action, le message de confirmation propose **↶ Défaire** (pour cette action seulement) ; après un
« Défaire », il propose **↷ Rétablir**.

### Règles de calcul

- **Sous-catégories** (facultatives, ex. Péages / Essence dans Transport) : créées une fois, proposées ensuite pour
  toutes les dépenses de la catégorie, dans tous les voyages ; la liste des dépenses les regroupe avec un sous-total.
- « Pour qui ? » : **à parts égales** entre les personnes cochées, ou **montants différents** (un montant par
  personne, le total doit faire le montant de la dépense — contrôlé en direct).
- **Rôles** : Plongeur, Accompagnant (l'un ou l'autre), Organisateur, Co-organisateur, DP — tous des pastilles, « Aucun » possible. **« Pour qui ? »** d'une dépense : pastilles Rôles / Familles / Groupes qui cochent ou décochent leurs membres (+ Tout le monde / Personne) ; la dépense garde la liste des personnes cochées. (Les anciennes données reçoivent le rôle Plongeur automatiquement, une fois.)
- **Participants en 3 onglets** : **Liste** (ajout en haut, tableau nom · groupes · situation, « Modifier » déplie la ligne : coordonnées, famille, plongeur/acc., rôles, groupes) · **Organisation** (équipe/rôles, familles : créer, membres, qui paie, supprimer ; groupes) · **Comptes** (le compte de chacun, filtre à pastilles mémorisé ; l'ancienne fiche `/p/…` y renvoie, filtrée).
- **Groupes de personnes** (covoiturage, voiture, chambre…) : créés sur la page Participants ; chacun devient un bouton dans « Pour qui ? » qui coche exactement ses membres ; une dépense répartie sur un groupe affiche son nom. IA : `"groupes"` est enregistré dans le voyage et réutilisable ensuite.
- **Plusieurs tickets sur une dépense** (deux pleins d'essence…) : « + Plusieurs tickets ? » sous le montant réel (montant + date par ticket, total calculé), ou « 80,12 + 69,10 » tapé dans le montant. Liste des dépenses : 🧾2 et le détail au survol ; PDF : « tickets : … ». IA : `"reel": ["80,12", "69,10"]`.
- **📲 Envoyer les comptes** : pour chaque personne ou famille, un PDF « Son compte » (en bref + détail par catégorie, sous-catégorie et dépense, avec le calcul) et un message WhatsApp prêt (wa.me, au numéro de la fiche). Un clic télécharge le PDF et ouvre WhatsApp ; il reste à glisser le PDF dans la conversation. ZIP de tous les PDF. Aussi sur la fiche de chaque participant.
- **Voyage sans asso** : sur la page 🏛️, « Qui tient la caisse ? » → un participant (ex. Paul). Partout, « l'asso » devient « la caisse (Paul) » ; on verse l'argent à Paul ; son propre compte se règle avec la caisse qu'il tient (rien à se verser). Côté IA : `"voyage": {"caisse": "Paul"}`.
- **Argent reçu et rendu** : 💶 = mouvement saisi ici (modifiable / supprimable sur la ligne) ; 🧾 = paiement qui vient d'une dépense (« Modifier la dépense »).
- **🏛️ L'asso** : le compte de l'asso pour le voyage (entrées / sorties réelles, ce qu'il reste à encaisser et à rendre, ce que le voyage lui coûte ou rapporte une fois tout réglé, ce qu'elle prend à sa charge). On peut lui donner un nom (commun à tous les voyages), accepté aussi comme payeur dans le JSON de l'IA. Section « Compte de l'asso » dans le PDF.
- Chaque ligne (part d'une personne sur une dépense) est affichée **au centime le plus proche** ; le **total** de chacun est arrondi **une seule fois, au centime supérieur** (ligne « Arrondi » visible). 1 522,29 € ÷ 11 donne bien 138,39 €, comme la calculette ; les centimes éventuellement en trop reviennent à l'asso.
- Un **montant fixé** 🔒 dans une dépense ne bouge plus, même si le montant de la dépense change : le reste est
  toujours réparti entre les autres personnes cochées. Une remise (fiche du participant) est compensée par l'asso
  ou par les autres : le total reste juste. (Les anciens « ajustements » sont convertis automatiquement.)
- Une réduction plus grande que la part de la personne déclenche une alerte.
- Reste à payer = sa part − ses versements − ses avances + ce que l'asso lui a remboursé.
- Contrôle permanent : parts des participants + prises en charge asso − arrondis = coût du voyage.
- **Famille** : chaque membre a sa part, mais la famille règle ses comptes **ensemble** (un seul « reste »,
  une personne qui paie pour la famille). Les versements de la famille sont enregistrés à son nom.
- **Accompagnant** : vient au voyage mais ne plonge pas. Dans « Pour qui ? », les boutons « Plongeurs seulement » /
  « Accompagnants seulement ».
- **Rôles** : un organisateur (un seul), des co-organisateurs, un ou plusieurs DP (directeurs de plongée), choisis
  parmi les participants (cumul possible). Ils apparaissent en badge dans les listes, en tête du tableau de bord et
  du bilan PDF ; l'organisateur puis les co-organisateurs sont proposés en premier dans « Qui a payé ? ».
  S'il ne vient pas au voyage, l'organisateur reste un simple nom (« Modifier le voyage »).
- **Catégories** : par défaut Logement, Transport / Covoit, Plongée, Location matériel, Repas, Activités
  accompagnants, Divers. Retirer une catégorie utilisée range ses dépenses dans « Divers ».
- Tant que le montant réel d'une dépense n'est pas saisi, le prévu est utilisé.
- **Plusieurs payeurs** : chacun est crédité de ce qu'il a avancé ; ce qui n'est attribué à personne est compté
  pour l'asso.
- **Argent reçu** (recette) : diminue le coût pour les personnes concernées. Reçu par un participant à la place
  de l'asso, il s'ajoute à ce qu'il doit.
- **Montant pas encore connu** : la dépense est notée (qui, pour qui) et signalée dans « À regarder » ; elle
  compte pour 0 € jusqu'à ce qu'on donne le montant.
- Une personne désistée qui garde sa part d'une dépense la paie comme si elle était venue (y compris la part
  fixe d'un enfant reportée sur les autres).

---

## Faire remplir l'application par une IA

Ça marche avec **n'importe quelle IA** (Claude, ChatGPT, Le Chat…), sans rien installer. On crée d'abord le
voyage (nom, dates), puis on ouvre **🤖 IA** dans le menu du voyage. Deux onglets :

- **Démarrer le voyage** (proposé quand il est vide) : participants, rôles, familles, dépenses, versements…
- **Mettre à jour le voyage** : seulement ce qui a changé (nouvelles dépenses, montants réels, versements reçus,
  corrections, suppressions, désistements).

1. **Remplis le modèle** en texte simple, directement sur la page. Écris comme tu parles. Chaque frappe est
   enregistrée à la volée (navigateur, puis `Documents/TripCompta/brouillons_ia/`), un brouillon par voyage et
   par onglet ; « Remettre le modèle au propre » garde l'ancien brouillon, récupérable d'un clic.
2. **Copie le message pour l'IA** (un bouton) : il contient le numéro du voyage, le mode d'emploi pour l'IA
   ([`compta/static/ia/guide_ia.md`](compta/static/ia/guide_ia.md)), **l'état actuel du voyage** et ton texte.
   L'IA te pose ses questions, puis te donne un récapitulatif et un bloc JSON.
3. **Colle la réponse** : l'application montre **ce qui va changer** (liste en clair) et **qui doit quoi avant →
   après**, sans rien enregistrer. Puis **Enregistrer ces changements** : tout d'un coup, et **↶ Défaire** retire
   tout d'un coup.

Garde-fou : la réponse de l'IA doit porter le numéro du voyage (`"voyage_id"`) ; collée dans un autre voyage,
elle est refusée. Les erreurs (prénom inconnu, dépense introuvable…) sont listées d'un coup et copiables pour l'IA.

Modèles vides : [`modele_initialisation.txt`](compta/static/ia/modele_initialisation.txt),
[`modele_ajustement.txt`](compta/static/ia/modele_ajustement.txt).

### Pour aller plus loin : Claude Desktop, Claude Code, VS Code (facultatif)

Avec Claude Desktop ou Claude Code (terminal, ou son extension VS Code), un connecteur (serveur MCP) permet à
Claude de travailler directement sur tes voyages, sans copier-coller : dis-lui « travaille sur le voyage n°2 ».
La configuration exacte est dans la partie repliée en bas de la page **🤖 IA** du voyage.

- **Claude Desktop** : Réglages → Développeur → Modifier la config → coller le bloc `tripcompta` → redémarrer Claude.
- **Claude Code** : copier la commande `claude mcp add tripcompta -- …` affichée sur la page.

### Outils proposés à l'IA (serveur MCP)

`lister_voyages`, `voir_voyage`, `creer_voyage`, `ajouter_participants`, `ajouter_depense`, `modifier_depense`,
`supprimer_depense`, `enregistrer_mouvement`, `ajuster_part`, `desister`, `definir_roles`, `ajouter_justificatif`,
`creer_compte_entre_nous`, `ajouter_depense_entre_nous`, `importer_voyage` (tout le JSON du guide d'un coup),
`lire_guide`, `annuler_derniere_action`.

- Les personnes se désignent par leur prénom (accents et majuscules indifférents).
- Les montants sont en euros (`"12,50"`).
- Toutes les écritures acceptent `simulation=true`.
- Les erreurs sont rédigées comme des questions (« Personne inconnue : Zoé… Faut-il l'ajouter ? »).

### API JSON

Les mêmes opérations sont disponibles en HTTP quand l'application tourne (dont `POST /api/import` pour le JSON du guide) :
documentation interactive sur <http://127.0.0.1:8765/api/docs>.

```bash
curl -X POST http://127.0.0.1:8765/api/voyages/1/depenses -H 'Content-Type: application/json' \
     -d '{"libelle":"Péage","montant_reel":"64,80","paye_par":"Luc","pour":["Luc","Marie"],"simulation":true}'
```

---

## Pour les développeurs

```
compta/
  engine.py       moteur de calcul pur (centimes, fractions exactes, arrondis, ajustements, désistements)
  models.py       modèle SQLAlchemy / SQLite
  db.py           dossier de données, sauvegardes, « Défaire » / « Rétablir »
  services.py     pont base ↔ moteur, alertes
  web.py          application FastAPI (pages Jinja2)
  ops.py          opérations métier pour l'IA (noms, euros, simulation, erreurs-questions)
  api.py          API JSON  (/api, doc /api/docs)
  importer.py     import d'un voyage complet (JSON du guide pour l'IA), modèle et guide dans static/ia/
  mcp_server.py   serveur MCP (stdio)
  export.py       bilan PDF, ZIP des justificatifs, PDF « Entre nous »
  demo.py         voyage de démonstration
  templates/ static/
run.py            lanceur (navigateur, --demo, --mcp)
tests/
  test_engine.py     règles de calcul, cas par cas
  test_amounts.py    démo recalculée à la main, 550 voyages aléatoires (invariants), PDF = appli
  test_scenarios.py  scénarios d'organisateur complets (budget -> régularisation, erreurs de saisie…)
  test_web.py        routes et parcours web
  test_ai.py         couche IA, API JSON, serveur MCP en vrai sous-processus
  test_import.py     import par l'IA : montants à la main, erreurs groupées, aperçu web,
                     brouillon enregistré à la volée (plantage simulé), copier-coller navigateur
  test_roles.py      rôles organisateur / co-organisateur / DP : écrans, PDF, IA, migration
  test_payers_income.py plusieurs payeurs, recettes, montant à venir, « tous sauf », groupes, note du voyage
  test_categories.py catégories : deux colonnes, bibliothèque commune, retrait vers « Divers », migration
  test_subcategories.py sous-catégories (regroupement, proposées partout, IA, PDF)
  test_split.py      « Pour qui ? » à parts égales / montants différents
  test_ledger.py     tableau des comptes (débit / crédit), vérifié à la main
  test_families.py   familles et accompagnants : montants à la main, écrans, PDF, IA, migration d'une ancienne base
  test_e2e.py        bout en bout dans Chromium (chaque écran, PDF, ZIP)
  test_ux.py         ergonomie : mobile, contrastes, étiquettes, clavier, jargon, parcours débutant
```

```bash
pip install -r requirements-dev.txt
python -m playwright install chromium     # une fois, pour le test navigateur
python -m pytest                          # tous les tests
SCREENSHOTS=captures python -m pytest tests/test_e2e.py   # + captures d'écran de chaque écran
```

### Publier une version

Les versions publiées sont fabriquées par **GitHub Actions** (`.github/workflows/release.yml`) quand on pousse
une étiquette `v…` : tests (rien n'est publié s'ils échouent), puis les quatre applications (Mac Apple Silicon,
Mac Intel, Windows, Linux), chacune lancée pour vérifier qu'elle démarre, puis la Release GitHub avec
`SHA256SUMS.txt`. Les applications installées la proposent ensuite toutes seules (bandeau, une vérification par jour).

```bash
# 1. le numéro dans compta/__init__.py (__version__ = "1.2.0"), commit, push
# 2. l'étiquette, avec les nouveautés (une par ligne : c'est ce que les utilisateurs liront)
git tag -a v1.2.0 -m "- Nouveau : …
- Correction : …"
git push origin v1.2.0
gh run watch                     # suivre la fabrication (≈ 15 min)
```

Une étiquette `v1.2.0-beta1` publie une **pré-version** : fabriquée pareil, téléchargeable par lien, mais jamais
proposée aux utilisateurs. Mise à jour côté application : `compta/updater.py` (vérification, téléchargement,
empreinte, sauvegarde `avant-maj-<version>.db`) et `compta/scripts/remplacer.sh|.ps1` (remplace l'application,
la relance, remet l'ancienne si la nouvelle ne répond pas). Une base ouverte par une version plus récente
(X.Y.Z supérieur) est refusée par une version plus ancienne, sans être modifiée.

Pile : Python 3.10+, FastAPI, Jinja2, SQLAlchemy/SQLite, fpdf2 (police DejaVu embarquée), SDK MCP, PyInstaller.
Pas de framework JavaScript : un petit `static/app.js` pour les aperçus en direct.
