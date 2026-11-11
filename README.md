# Trip Compta

Application pour l'organisateur d'un voyage associatif (club, asso, groupe d'amis avec une caisse commune) :
participants, dépenses, qui a payé quoi, avances, montants fixés et remises, désistements, justificatifs, dates de
paiement et relances, comptes « entre nous », et un bilan PDF lisible par un trésorier qui n'est pas comptable.
Une IA (Claude, ChatGPT, Le Chat…) peut aussi préparer les saisies à partir d'un texte.

Tout fonctionne **sur ton ordinateur** (Mac ou Windows), dans la fenêtre de l'application. Rien n'est envoyé sur
Internet (sauf, une fois par jour, la question « y a-t-il une nouvelle version ? », désactivable).

> **👉 Installer Trip Compta (application toute prête, qui se met à jour toute seule), changer d'ordinateur,
> désinstaller : voir [INSTALLER.md](INSTALLER.md)**, ou la page
> [Télécharger](https://tripcompta.plongee.bio/telecharger) du site.
> Premiers pas : **❓ Aide** dans l'application, et le [guide en ligne](https://tripcompta.plongee.bio/guide).

Dans ce fichier : [où sont mes données](#où-sont-mes-données-) · [ce que fait l'application](#ce-que-fait-lapplication) ·
[l'IA](#faire-remplir-lapplication-par-une-ia) · [pour les développeurs](#pour-les-développeurs)
(lancer depuis le code source, fabriquer l'application, tests, publier une version).

---

## Où sont mes données ?

Dans le dossier **`Documents/TripCompta`** (ou celui que tu as choisi : **⚙️ Réglages → Où sont mes voyages**) :

- `compta.db` : la base (tous les voyages) ;
- `justificatifs/` : les photos et PDF importés ;
- `.annuler/` et `.retablir/` : l'historique de **↶ Défaire** et **↷ Rétablir** (30 dernières modifications) ;
- `brouillons_ia/` : les textes que tu remplis sur la page 🤖 IA de chaque voyage (enregistrés à la volée).

**Sauvegardes** : chaque jour où Trip Compta est ouvert, une copie de tous les voyages, justificatifs compris, est
mise de côté **sur l'ordinateur, à part de ce dossier** (dans le dossier des réglages de Trip Compta ; les 10
dernières sont gardées). **⚙️ Réglages → Sauvegardes** les liste et permet de revenir à l'une d'elles. Elles ne
quittent pas l'ordinateur : pour une copie à ranger ailleurs ou pour changer d'ordinateur, **📦 Exporter le
voyage** (un fichier avec tout dedans, à réimporter par **Mes voyages → Importer un voyage**).

Une base dont la structure est plus ancienne est complétée toute seule à l'ouverture (tables et colonnes qui
manquent) ; une base ouverte par une version plus récente est refusée par une version plus ancienne, sans être
modifiée.

### Mettre ses voyages dans Google Drive, Dropbox, OneDrive ou iCloud (à tes risques)

Aucun serveur et aucune synchronisation propre à Trip Compta : il lit et écrit **directement** son dossier, et ne
s'occupe pas du cloud. Ce dossier peut être dans ton cloud, qui le recopie sur tes autres ordinateurs : c'est
possible, **à tes risques, un seul ordinateur à la fois**.

1. Installe l'application de ton cloud pour ordinateur (Dropbox, Google Drive pour ordinateur, OneDrive, iCloud,
   kDrive, Proton Drive…), s'il n'y est pas déjà.
2. Dans Trip Compta : **⚙️ Réglages → Où sont mes voyages → Changer de dossier** : tape le chemin, ou
   **Parcourir…**. Trip Compta regarde ce que contient le dossier :
   - vide : **Copier mes voyages dans ce dossier** (tout est copié et vérifié, l'ancien dossier n'est pas touché) ;
   - il contient déjà des voyages (par exemple rangés depuis un autre ordinateur) : **Ouvrir les voyages de ce
     dossier** (jamais de copie par-dessus).

- **Un seul ordinateur à la fois** : ferme Trip Compta avant de l'ouvrir ailleurs, et attends que ton cloud ait fini
  de synchroniser. Ouvert sur deux ordinateurs en même temps, les saisies de l'un peuvent effacer celles de l'autre.
  Sur un même ordinateur, une seule Trip Compta à la fois : relancer l'icône montre celle qui est ouverte.
- **Trip Compta te prévient** : le nom du dernier ordinateur qui a modifié les voyages est enregistré dans la base ;
  à l'ouverture, « Tes voyages ont été modifiés il y a 3 min sur « PC du club » » (s'il y a moins d'une heure) ou
  simplement « Dernière modification : sur « PC du club », hier à 18 h 30 ».
- **Copies en conflit** (« compta (conflicted copy…).db », « compta (copie en conflit…).db », « compta-PC.db »…) :
  Trip Compta les signale. Pas de fusion ni de remplacement : **Exporter cette version (.zip)**, puis **Mes voyages
  › Importer un voyage** l'ajoute à côté des tiens (rien n'est écrasé) pour en recopier les saisies ; ensuite
  **La ranger** (dossier « Anciennes versions », jamais effacée).
- **Fichier pas encore téléchargé, disque débranché** : Trip Compta le dit, sans rien toucher, avec **Réessayer**.
  **Base abîmée** : il reprend tout seul la dernière copie intacte (historique « Défaire » ou sauvegarde du jour) et
  garde le fichier abîmé à côté.
- **Windows** : le dossier Documents est souvent dans OneDrive sans qu'on le sache (Trip Compta le suit) : sans
  risque avec un seul ordinateur.
- **Revenir sur cet ordinateur** : même page ; les voyages sont recopiés dans `Documents/TripCompta`.

### Protéger ses voyages par un mot de passe (facultatif)

**⚙️ Réglages → Mot de passe** : la base et les justificatifs du dossier sont **chiffrés** (AES-256-GCM ;
clé des voyages enfermée par le mot de passe et par une clé de secours, via scrypt). Sans le mot de passe ni la
clé de secours, personne ne peut les lire, même le service de cloud. Utile si le dossier est partagé, si
l'ordinateur est partagé (club), ou pour les données personnelles des adhérents.

- Dans le dossier : `voyages.chiffre`, `justificatifs/*.chiffre`, `cle.tripcompta`, `LISEZMOI.txt`.
  Sur l'ordinateur, une copie de travail n'est en clair que pendant que
  l'app est déverrouillée ; à l'arrêt (ou au démarrage suivant après un plantage) elle est effacée, et l'historique,
  les sauvegardes et les brouillons IA sont chiffrés.
- Après chaque modification, la base est rechiffrée dans le dossier. Si un autre ordinateur l'a modifiée entre-temps,
  rien n'est écrasé : au déverrouillage, sa version est reprise ; une version de cet ordinateur pas encore
  enregistrée est déposée à côté (« voyages (version de PC-Anne …).chiffre »), exportable comme une copie en conflit.
- Au lancement : **🔒 Mot de passe de tes voyages** (ou rien, avec « Se souvenir sur cet ordinateur » : trousseau
  du système). Bouton **🔒 Verrouiller** en haut. **Mot de passe oublié ?** → la clé de secours, puis un nouveau.
- **Mot de passe ET clé de secours perdus = voyages perdus pour toujours.** Personne ne peut les récupérer.
- Après l'activation, **vide la corbeille et les anciennes versions de ton cloud** (Google Drive les garde 30
  jours) : elles contiennent encore les fichiers d'avant, lisibles.
- Changer le mot de passe, nouvelle clé de secours, **Retirer la protection** : même page. Pour ranger les voyages
  dans un autre dossier, retirer d'abord la protection (sinon une copie lisible serait créée).
- PDF, mails et exports restent en clair (faits pour être envoyés). L'IA (MCP) ne lit les voyages que déverrouillés.

---

## Ce que fait l'application

| Écran | À quoi ça sert |
|---|---|
| **Mes voyages** | la liste des voyages ; **+ Nouveau voyage**, **Importer un voyage** ; un voyage fini se **clôture** (ses comptes sont figés, on peut le rouvrir) et se **duplique** pour préparer le suivant |
| 🏠 **Où j'en suis** | « Pour démarrer » (les étapes d'un voyage tout neuf), puis les comptes : coût, déjà versé, reste à recevoir, reste à rembourser ; « À regarder » : les alertes en clair |
| 👥 **Participants** | ajout de plusieurs personnes d'un coup (un nom par ligne), reprise du groupe d'un autre voyage, **familles**, **rôles** (organisateur, co-organisateurs…), **groupes** (covoiturage, chambre…), le compte de chacun |
| 🧾 **Dépenses** | par catégorie, prévu / réel (ou « montant pas encore connu »), payé par l'asso, avancé par un participant ou **par plusieurs** (montant de chacun), pour qui (parts) ; **argent reçu** (remboursement d'un fournisseur, subvention) ; chercher, filtrer, trier |
| 💶 **Argent reçu et rendu** | ce qu'il reste à recevoir de chacun et à rendre à chacun, avec **✓ Reçu** / **✓ Remboursé** (plusieurs d'un coup si besoin), moyen de paiement, chèques à déposer ; **dates de paiement** et carte « 🔔 À relancer aujourd'hui » |
| 📎 **Justificatifs** | photos/PDF numérotés J01, J02…, liables à plusieurs dépenses |
| 🏷️ **Catégories** | en haut les catégories du voyage (avec leurs sous-catégories), en dessous les catégories disponibles, communes à tous les voyages : un clic pour en ajouter une au voyage |
| 🧮 **Qui paie quoi** | tableau participants × dépenses ; clic sur une case = la dépense, pour ajuster ce montant |
| 📊 **En images** | le voyage en graphiques |
| 🏛️ **L'asso** (ou la caisse) | le compte de l'asso pour le voyage ; qui tient la caisse ; qui paie les centimes qui restent |
| 🔒 **Montant fixé** (dans la dépense) | tape un montant pour une personne : il est fixé, le reste se répartit tout seul entre les autres, même si la dépense change |
| 🎟️ **Remise ou supplément** (compte du participant) | sur tout son voyage, compensé par l'asso ou par les autres |
| 🚪 **Désistement** | parts non remboursables conservées, le reste repris par le groupe ou l'asso, remboursement ; aperçu avant de valider |
| 🤝 **Entre nous** | petits comptes entre participants, hors asso (type Tricount), PDF à partager |
| 🤖 **IA** | modèle « démarrer » ou « mettre à jour », message pour n'importe quelle IA (avec l'état du voyage), aperçu avant → après, enregistrement |
| 📤 **Envoyer les comptes** | pour chaque personne ou famille : son compte en PDF et un message prêt (WhatsApp, e-mail, copier) |
| 📄 **Exporter le bilan** | bilan PDF pour le trésorier, ZIP des justificatifs renommés, tableur (.xlsx), texte du mail au trésorier |
| 📦 **Exporter le voyage** | un fichier avec tout le voyage et ses justificatifs, pour un autre ordinateur ou un autre organisateur |

En haut de la fenêtre : **↶ Défaire** (les dernières modifications, pour revenir en arrière), **❓ Aide** (premiers
pas, les mots de Trip Compta, astuces), **⚙️ Réglages** (dossier des voyages, mot de passe, sauvegardes, voyage
d'exemple, version et mises à jour), **AA** (agrandir le texte), thème clair / sombre, **⏻ Arrêter**.
Les calculs prennent toujours le **montant réel s'il est saisi, sinon le prévu** ; une dépense comptée sur son
prévu est signalée (≈, en couleur) et un rappel indique combien il en reste.
Après chaque action, le message de confirmation propose **↶ Défaire** (pour cette action seulement) ; après un
« Défaire », il propose **↷ Rétablir**. Un refus (montant invalide, nom déjà pris…) s'affiche en rouge, et ce qui
était saisi dans le formulaire est gardé.

### Règles de calcul

- **Sous-catégories** (facultatives, ex. Péages / Essence dans Transport) : créées une fois, proposées ensuite pour
  toutes les dépenses de la catégorie, dans tous les voyages ; la liste des dépenses les regroupe avec un sous-total.
- « Pour qui ? » : **à parts égales** entre les personnes cochées, ou **montants différents** (un montant par
  personne, le total doit faire le montant de la dépense — contrôlé en direct).
- **Rôles** : Organisateur, Co-organisateur, et les étiquettes que tu crées pour le voyage (« Moniteur », « DP »…), reprises au voyage suivant ; ce sont des pastilles qui aident à cocher vite les bonnes personnes. **Voyage de plongée** (case à cocher à la création, ou « Ajouter les réglages de plongée » dans Organisation) : les rôles Plongeur et Accompagnant et les catégories de plongée s'ajoutent. **« Pour qui ? »** d'une dépense : pastilles Rôles / Familles / Groupes qui cochent ou décochent leurs membres (+ Tout le monde / Personne) ; la dépense garde la liste des personnes cochées.
- **Participants en 3 onglets** : **Liste** (ajout en haut, tableau nom · groupes · situation, « Modifier » déplie la ligne : coordonnées, famille, plongeur/acc., rôles, groupes) · **Organisation** (équipe/rôles, familles : créer, membres, qui paie, supprimer ; groupes) · **Comptes** (le compte de chacun, filtre à pastilles mémorisé).
- **Groupes de personnes** (covoiturage, voiture, chambre…) : créés sur la page Participants ; chacun devient un bouton dans « Pour qui ? » qui coche exactement ses membres ; une dépense répartie sur un groupe affiche son nom. IA : `"groupes"` est enregistré dans le voyage et réutilisable ensuite.
- **Plusieurs tickets sur une dépense** (deux pleins d'essence…) : « + Plusieurs tickets ? » sous le montant réel (montant + date par ticket, total calculé), ou « 80,12 + 69,10 » tapé dans le montant. Liste des dépenses : 🧾2 et le détail au survol ; PDF : « tickets : … ». IA : `"reel": ["80,12", "69,10"]`.
- **📲 Envoyer les comptes** : pour chaque personne ou famille, un PDF « Son compte » (en bref + détail par catégorie, sous-catégorie et dépense, avec le calcul) et un message WhatsApp prêt (wa.me, au numéro de la fiche). Un clic télécharge le PDF et ouvre WhatsApp ; il reste à glisser le PDF dans la conversation. ZIP de tous les PDF. Aussi sur la fiche de chaque participant.
- **Voyage sans asso** : sur la page 🏛️, « Qui tient la caisse ? » → un participant (ex. Paul). Partout, « l'asso » devient « la caisse (Paul) » ; on verse l'argent à Paul ; son propre compte se règle avec la caisse qu'il tient (rien à se verser). Côté IA : `"voyage": {"caisse": "Paul"}`.
- **📅 Dates de paiement et relances** : sur « Argent reçu et rendu », on fixe un acompte avant telle date, puis le reste avant le départ (montants par personne ; « tout ce qui reste » à la dernière date). Chaque personne ou famille est alors **à jour**, **en retard** (de combien, depuis quand) ou **soldée** ; une dépense avancée pour le groupe compte comme un paiement. La carte « 🔔 À relancer aujourd'hui » prépare le message (WhatsApp, e-mail, copier) : rien ne part tout seul. Quelqu'un paie en plusieurs fois ou a un délai : il peut avoir ses propres dates. IA : `"dates_paiement"`, et l'état `paiements` dit qui relancer.
- **Argent reçu et rendu** : 💶 = mouvement saisi ici (modifiable / supprimable sur la ligne) ; 🧾 = paiement qui vient d'une dépense (« Modifier la dépense »).
- **🏛️ L'asso** : le compte de l'asso pour le voyage (entrées / sorties réelles, ce qu'il reste à encaisser et à rendre, ce que le voyage lui coûte ou rapporte une fois tout réglé, ce qu'elle prend à sa charge). On peut lui donner un nom (commun à tous les voyages), accepté aussi comme payeur dans le JSON de l'IA. Section « Compte de l'asso » dans le PDF.
- **Chaque dépense tombe juste au centime** : chaque part est arrondie au centime inférieur, et les centimes qui
  restent (189,22 € ÷ 11 = 17,2018… → 0,02 €) sont payés par des participants **tirés au sort, chacun son tour**
  (personne ne paie plus d'un centime de plus qu'un autre), ou par l'asso. Réglage du voyage (page de l'asso),
  et choix possible dépense par dépense (« Pour qui ? » → Changer). Le tirage est enregistré avec la dépense : il
  ne change que si on modifie cette dépense ou qu'on demande un nouveau tirage. Le total de chacun est la somme
  de ses lignes ; 1 522,29 € de transport à 11 donnent bien 138,39 € chacun.
- Un **montant fixé** 🔒 dans une dépense ne bouge plus, même si le montant de la dépense change : le reste est
  toujours réparti entre les autres personnes cochées. Une remise (fiche du participant) est compensée par l'asso
  ou par les autres : le total reste juste.
- Une réduction plus grande que la part de la personne déclenche une alerte.
- Reste à payer = sa part − ses versements − ses avances + ce que l'asso lui a remboursé.
- Contrôle permanent : parts des participants + prises en charge asso = coût du voyage, au centime.
- **Famille** : chaque membre a sa part, mais la famille règle ses comptes **ensemble** (un seul « reste »,
  une personne qui paie pour la famille). Les versements de la famille sont enregistrés à son nom.
- **Accompagnant** (voyage de plongée) : vient au voyage mais ne plonge pas. Dans « Pour qui ? », les pastilles
  « Plongeurs » / « Accompagnants » cochent les uns ou les autres.
- **Équipe** : un organisateur (un seul) et des co-organisateurs, choisis
  parmi les participants (cumul possible). Ils apparaissent en badge dans les listes, en tête du tableau de bord et
  du bilan PDF ; l'organisateur puis les co-organisateurs sont proposés en premier dans « Qui a payé ? ».
  S'il ne vient pas au voyage, l'organisateur reste un simple nom (« Modifier le voyage »).
- **Catégories** : par défaut Logement, Transport / Covoit, Repas, Activités, Divers ; un voyage de plongée y
  ajoute Plongée, Location matériel, Activités accompagnants. Retirer une catégorie utilisée range ses dépenses
  dans « Divers ».
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
   enregistrée à la volée (dans la page, puis `Documents/TripCompta/brouillons_ia/`), un brouillon par voyage et
   par onglet ; « Remettre le modèle au propre » garde l'ancien brouillon, récupérable d'un clic.
2. **Copie le message pour l'IA** (un bouton) : il contient le numéro du voyage, le mode d'emploi pour l'IA
   ([`compta/static/ia/guide_ia.md`](compta/static/ia/guide_ia.md)), **l'état actuel du voyage** et ton texte.
   L'IA te pose ses questions, puis te donne un récapitulatif et un bloc JSON.
3. **Colle la réponse** : l'application montre **ce qui va changer** (liste en clair) et **qui doit quoi avant →
   après**, sans rien enregistrer. Puis **Enregistrer ces changements** : tout d'un coup, et **↶ Défaire** retire
   tout d'un coup.

Garde-fou : la réponse de l'IA doit porter le numéro du voyage (`"voyage_id"`) ; collée dans un autre voyage,
elle est refusée. Les erreurs (prénom inconnu, dépense introuvable…) sont listées d'un coup et copiables pour l'IA.

Ce que tu colles dans une IA part chez le service de cette IA : la page le rappelle, et rien n'est envoyé sans toi.
Le modèle de départ : [`modele_initialisation.txt`](compta/static/ia/modele_initialisation.txt) ; pour « mettre à
jour », la page part d'une feuille blanche où tu écris ce qui a changé.

### Pour aller plus loin : Claude Desktop, Claude Code, VS Code (facultatif)

Avec Claude Desktop ou Claude Code (terminal, ou son extension VS Code), un connecteur (serveur MCP) permet à
Claude de travailler directement sur tes voyages, sans copier-coller : dis-lui « travaille sur le voyage n°2 ».
La configuration exacte est dans la partie repliée en bas de la page **🤖 IA** du voyage.

- **Claude Desktop** : Réglages → Développeur → Modifier la config → coller le bloc `tripcompta` → redémarrer Claude.
- **Claude Code** : copier la commande `claude mcp add tripcompta -- …` affichée sur la page.

### Outils proposés à l'IA (serveur MCP)

`lister_voyages`, `voir_voyage`, `creer_voyage`, `ajouter_participants`, `ajouter_depense`, `modifier_depense`,
`supprimer_depense`, `enregistrer_mouvement`, `fixer_dates_paiement`, `ajuster_part`, `desister`, `definir_roles`, `ajouter_justificatif`,
`creer_compte_entre_nous`, `ajouter_depense_entre_nous`, `importer_voyage` (tout le JSON du guide d'un coup),
`lire_guide`, `annuler_derniere_action`.

- Les personnes se désignent par leur prénom (accents et majuscules indifférents).
- Les montants sont en euros (`"12,50"`).
- Toutes les écritures acceptent `simulation=true`.
- Les erreurs sont rédigées comme des questions (« Personne inconnue : Zoé… Faut-il l'ajouter ? »).

### API JSON

Les mêmes opérations sont disponibles en HTTP quand l'application tourne (dont `POST /api/import` pour le JSON du
guide ; documentation : `/api/docs`). Un programme doit se présenter avec le **jeton** de la session, dans l'en-tête
`Authorization: Bearer …` : il est affiché en bas de la page **🤖 IA** du voyage, avec l'adresse exacte, change à
chaque lancement de Trip Compta et n'est valable que sur cet ordinateur. Sans lui, rien n'est lisible ni modifiable
(seul `/ping` répond).

```bash
curl -X POST http://127.0.0.1:8765/api/voyages/1/depenses \
     -H 'Authorization: Bearer LE_JETON_AFFICHE_SUR_LA_PAGE_IA' -H 'Content-Type: application/json' \
     -d '{"libelle":"Péage","montant_reel":"64,80","paye_par":"Luc","pour":["Luc","Marie"],"simulation":true}'
```

---

## Pour les développeurs

### Lancer depuis le code source

Pour utiliser Trip Compta, installe plutôt l'application toute prête (voir [INSTALLER.md](INSTALLER.md)).
Depuis le code source (Python 3.12 ou plus récent ; les versions publiées sont fabriquées avec Python 3.14),
l'application s'ouvre dans le navigateur :

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
| `--fenetre` | ouvre l'application dans sa propre fenêtre, même depuis le code source |
| `--navigateur` | ouvre l'application fabriquée dans le navigateur plutôt que dans sa fenêtre |
| `--mcp` | démarre le serveur MCP pour une IA (voir plus haut) |

Le navigateur est ouvert avec le jeton de la session dans l'adresse (retiré aussitôt) : un onglet ouvert à la
main, sans lui, reçoit une page qui l'explique.

### Fabriquer l'application (exécutable autonome)

```bash
pip install -r requirements-dev.txt
python build_tools/build.py
```

**Sa propre fenêtre** : l'application fabriquée (Mac, Windows) s'ouvre dans sa fenêtre (pywebview : WebKit sur Mac,
WebView2 sous Windows), avec Cmd/Ctrl+N (nouvelle fenêtre) et Cmd/Ctrl+F (rechercher dans la page) ; fermer la
dernière fenêtre ou Cmd+Q l'arrête proprement.

Sur **Mac**, cela produit `dist.noindex/TripCompta.app` (dossier que Spotlight et le Launchpad ignorent) ; sur
Windows, la même commande lancée **sur Windows** produit `dist/TripCompta/` (PyInstaller ne fabrique que pour le
système où il tourne). Une application fabriquée ainsi est une **version de développement**
(« 1.5.0-dev (commit) ») : elle ne se met jamais à jour toute seule. Les versions publiées (Mac et Windows ; pas
de version Linux publiée) sont fabriquées par GitHub (voir « Publier une version »).

### Le code

```
compta/
  engine.py       moteur de calcul pur (centimes, fractions exactes, arrondis, ajustements, désistements)
  preparation.py  calcul pur d'un voyage en préparation (postes, offres, offre retenue, budget, prix par
                  personne, marge, réserve, « si tu retiens cette offre »), sans écran pour l'instant
  models.py       modèle SQLAlchemy / SQLite
  db.py           dossier de données, sauvegardes, « Défaire » / « Rétablir »
  services.py     pont base ↔ moteur, alertes
  writes.py       le seul endroit qui crée et modifie les lignes d'un voyage (voyage, participants, dépenses,
                  versements, remises, désistements) et d'un voyage en préparation (projet, postes, offres) :
                  les formulaires et l'IA passent tous les deux par lui
  web.py          application FastAPI (pages Jinja2) : démarrage, dossier des voyages, garde-fous, erreurs,
                  ce que toutes les pages ont en commun (`render`, `back`, `refuse`…)
  pages/          les pages, un module par domaine (voyages, participants, depenses, repartition, argent,
                  justificatifs, entre_nous, envoyer, ia, reglages, protection…) : chacun lit ses formulaires,
                  puis appelle writes.py ; web.py les installe dans l'ordre (pages/__init__.py)
  ops.py          opérations pour l'IA (noms, euros, simulation, erreurs-questions) : lit, puis appelle writes.py
  api.py          API JSON  (/api, doc /api/docs)
  importer.py     import d'un voyage complet (JSON du guide pour l'IA), modèle et guide dans static/ia/
  mcp_server.py   serveur MCP (stdio)
  export.py       bilan PDF, comptes personnels en PDF, ZIP des justificatifs, PDF « Entre nous »
  tableur.py      le tableur (.xlsx) du bilan
  transfer.py     exporter / importer un voyage (fichier .zip), dupliquer un voyage
  fichiers.py     justificatifs : types de fichiers acceptés, noms sûrs
  paths.py        où sont les données et les réglages de l'ordinateur
  dossier.py      le dossier des voyages : dernier ordinateur qui a écrit, copies en conflit
  coffre.py crypto.py   voyages protégés par un mot de passe (chiffrement, copie de travail)
  updater.py signature.py version.py   mises à jour : vérification, signature, installation
  fenetre.py      la fenêtre de l'application (pywebview)
  journal.py diagnostic.py   journal de l'application, diagnostic à exporter (sans nom ni montant)
  demo.py         voyages de démonstration (--demo) ; exemple/ : le voyage d'exemple de l'application
  templates/ static/
run.py            lanceur (fenêtre ou navigateur, une seule application à la fois, --demo, --mcp)
build_tools/      fabrication de l'application (PyInstaller, installeur Windows, signature des empreintes)
docs/             PUBLIER.md (publier une version, clé de signature), SIGNATURE.md
tests/
  test_engine.py     règles de calcul, cas par cas
  test_amounts.py    démo recalculée à la main, 550 voyages aléatoires (invariants), PDF = appli
  test_scenarios.py  scénarios d'organisateur complets (budget -> régularisation, erreurs de saisie…)
  test_web.py        routes et parcours web
  test_un_seul_chemin.py  le même scénario à l'écran et par l'IA : mêmes lignes, mêmes comptes (à la main)
  test_dates_paiement.py  dates de paiement : à jour / en retard / soldé à la main, relances, dates d'une personne
  test_preparation.py  voyage en préparation : montants à la main, une offre retenue par poste, forfaits,
                     refus, suppression d'un poste, défaire / rétablir
  test_pages.py      les pages rangées par domaine : rien d'oublié, aucune adresse cachée par une autre
  test_guides.py     INSTALLER.md et README.md disent vrai : boutons cités, fichiers, dossiers, liens
  test_jeton.py      jeton de session : rien n'est lisible ni modifiable sans lui
  test_ai.py         couche IA, API JSON, serveur MCP en vrai sous-processus
  test_import.py     import par l'IA : montants à la main, erreurs groupées, aperçu web,
                     brouillon enregistré à la volée (plantage simulé), copier-coller navigateur
  test_roles.py      rôles organisateur / co-organisateur / DP : écrans, PDF, IA
  test_payers_income.py plusieurs payeurs, recettes, montant à venir, « tous sauf », groupes, note du voyage
  test_categories.py catégories : celles du voyage, bibliothèque commune, retrait vers « Divers »
  test_subcategories.py sous-catégories (regroupement, proposées partout, IA, PDF)
  test_split.py      « Pour qui ? » à parts égales / montants différents
  test_ledger.py     tableau des comptes (débit / crédit), vérifié à la main
  test_families.py   familles et accompagnants : montants à la main, écrans, PDF, IA
  test_e2e.py        bout en bout dans Chromium (chaque écran, PDF, ZIP)
  test_ux.py         ergonomie : mobile, contrastes, étiquettes, clavier, jargon, parcours débutant
  …                  et une cinquantaine d'autres fichiers, un par sujet (sauvegardes, protection, mises à jour…)
```

```bash
pip install -r requirements-dev.txt
python -m playwright install chromium     # une fois, pour le test navigateur
python -m pytest                          # tous les tests
SCREENSHOTS=captures python -m pytest tests/test_e2e.py   # + captures d'écran de chaque écran
```

### Publier une version

Les versions publiées sont fabriquées par **GitHub Actions** (`.github/workflows/release.yml`) quand on pousse
une étiquette `v…` : tests (rien n'est publié s'ils échouent), puis les trois applications (Mac Apple Silicon,
Mac Intel, installeur Windows), chacune lancée pour vérifier qu'elle démarre, puis la Release GitHub avec
`SHA256SUMS.txt` et sa signature `SHA256SUMS.txt.sig`. Les applications installées la proposent ensuite toutes
seules (bandeau, une vérification par jour) et n'installent qu'une version dont les empreintes sont signées.
Le détail (clé de signature, publications immuables, que faire si la clé est perdue) : [docs/PUBLIER.md](docs/PUBLIER.md).

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
signature et empreinte, sauvegarde `avant-maj-<version>.db`), `compta/scripts/remplacer.sh` sur Mac et
l'installeur sous Windows (`build_tools/windows/TripCompta.iss`) : ils remplacent l'application, la relancent,
et remettent l'ancienne si la nouvelle ne démarre pas. Une base ouverte par une version plus récente
(X.Y.Z supérieur) est refusée par une version plus ancienne, sans être modifiée.

Pile : Python 3.12+, FastAPI, Jinja2, SQLAlchemy/SQLite, fpdf2 (police DejaVu embarquée), SDK MCP, PyInstaller.
Pas de framework JavaScript : un petit `static/app.js` pour les aperçus en direct.

---

## Licence

Trip Compta est **gratuit et open source**, sous licence [MIT](LICENSE) : tu peux l'utiliser, le copier, le
modifier et le redistribuer librement, en gardant la mention de copyright. Il est fourni « tel quel », sans garantie.
