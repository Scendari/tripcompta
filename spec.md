# Spécification — Compta Voyages

> Version 1 — à valider. Principe directeur : **simple pour un organisateur et pour un trésorier qui ne sont pas comptables.** Aucun jargon (« débit », « crédit », « écriture », « solde ») dans l'application ni dans les exports : on parle d'**argent reçu**, d'**argent dépensé**, de **qui doit encore** et de **à qui on doit**.

---

## 1. Les mots de l'application

| Mot à l'écran | Ce que ça veut dire |
|---|---|
| **Voyage** | Un séjour organisé par l'asso (ex. « Plongée Marseille mai 2027 ») |
| **Participant** | Une personne inscrite au voyage |
| **Catégorie** | Un regroupement de dépenses : Logement, Covoit, Plongée, Repas… |
| **Dépense** | Quelque chose qui coûte de l'argent (gîte, péage, sortie bateau) |
| **Part** | Ce qu'un participant doit pour une dépense |
| **Versement** | De l'argent qu'un participant donne à l'asso |
| **Avance** | Un participant a payé une dépense de sa poche pour le groupe |
| **Remboursement** | L'asso rend de l'argent à un participant |
| **Ajustement** | On change la part de quelqu'un, et on choisit qui compense |
| **Prévu / Réel** | Le montant estimé avant le voyage, le montant réellement payé |
| **Entre nous** | Un petit compte entre participants, hors asso (ex. un resto partagé) |

---

## 2. Les écrans

### 2.1 Accueil — mes voyages
Liste des voyages sous forme de cartes : nom, dates, nombre de participants, et un indicateur simple :
- 🟢 « Tout est réglé »
- 🟠 « 3 personnes doivent encore 640 € »
- 🔵 « L'asso doit rembourser 120 € »

Bouton **« + Nouveau voyage »** (nom, dates, lieu — c'est tout).

### 2.2 Tableau de bord du voyage — « Où j'en suis ? »
En haut, 4 chiffres en gros :
- **Coût du voyage** (réel, ou prévu si pas encore connu)
- **Déjà reçu** des participants
- **Reste à recevoir**
- **Reste à rembourser** (avances non encore rendues)

En dessous :
- **Les 4 boutons d'action** (toujours visibles) :
  - « + Une dépense »
  - « + Un participant m'a versé de l'argent »
  - « Ajuster la part de quelqu'un »
  - « Quelqu'un se désiste »
- **Alertes en langage clair** : « 2 dépenses sans justificatif », « Marie n'a encore rien versé », « Le logement réel dépasse le prévu de 80 € ».
- **Liste des participants** avec pastille couleur et montant : vert = à jour, orange = doit encore X €, bleu = on lui doit X €.

### 2.3 Participants
Ajout rapide : on tape les noms, un par ligne (copier-coller d'une liste possible). Pour chacun : nom, et en option téléphone / e-mail / note.

### 2.4 Fiche d'un participant
Une page qui se lit comme un relevé simple :

```
Thierry
─────────────────────────────────────────────
Ce qu'il doit
  Logement (5 nuits sur 5)            150,00 €
  Plongée (4 plongées)                200,00 €
  Covoit (voiture de Luc)              35,00 €
  Réduction accordée                  −30,00 €   ℹ compensée par l'asso
                                      ────────
  Total de sa part                    355,00 €

Ce qu'il a déjà donné
  Versement du 12/03                  200,00 €
  Avance : péage A7                    24,60 €
                                      ────────
  Total                               224,60 €

➜ Thierry doit encore 130,40 €
```

Chaque montant est cliquable et affiche **le calcul en clair** (« Logement 1 500 € ÷ 50 nuits au total × 5 nuits de Thierry = 150 € »).

### 2.5 Dépenses (par catégorie)
Dépenses regroupées par catégorie, repliables. Pour chaque dépense : libellé, prévu, réel, qui a payé (l'asso ou un participant), pour qui, justificatifs (icône trombone).

**Formulaire « + Une dépense »**, en 3 étapes :
1. **Quoi ?** Libellé, catégorie (liste + création à la volée), montant prévu et/ou réel, date.
2. **Qui a payé ?** « L'asso » (par défaut) ou « Un participant » (→ c'est une avance).
3. **Pour qui ?** Par défaut **tout le monde, à parts égales**. Options :
   - décocher des personnes (ex. les non-plongeurs),
   - donner un nombre de parts différent (ex. 3 nuits au lieu de 5, demi-part).
   Un aperçu montre immédiatement la part de chacun.

On peut joindre un ou plusieurs justificatifs (photo ou PDF), ou lier un justificatif déjà importé (une facture peut couvrir plusieurs dépenses).

### 2.6 Tableau de répartition
Grille **participants × dépenses** avec la part de chacun. Vue de contrôle et d'ajustement rapide : on clique sur une case pour modifier → lance l'assistant d'ajustement. Les cases ajustées sont marquées, les cases verrouillées ont un cadenas. Totaux en bout de ligne et de colonne.

### 2.7 Ajuster la part de quelqu'un
1. Choisir la personne et la dépense (ou « sur le total de son voyage »).
2. Saisir le nouveau montant (ou la réduction).
3. **« Qui compense la différence ? »**
   - les autres participants de cette dépense, au prorata,
   - une personne précise,
   - l'asso (prise en charge).
4. **Aperçu avant / après** pour toutes les personnes touchées → Valider.
5. Option « 🔒 verrouiller ce montant » : il ne bougera plus lors des recalculs (les verrouillés ne servent pas à compenser).

Un motif libre peut être noté (« étudiant », « encadrant »…), il apparaît dans l'export.

### 2.8 Quelqu'un se désiste
1. Choisir la personne et la date.
2. L'appli affiche ce qu'elle a versé et sa part actuelle.
3. Pour chaque dépense : « Cette part est-elle perdue ? » (ex. acompte gîte non remboursable) — oui / non.
4. Montant qu'on lui rembourse.
5. **« Qui prend en charge la part non remboursée ? »** → le groupe (réparti sur les autres) ou l'asso.
6. Aperçu → Valider. La personne reste visible, grisée, avec son historique.

### 2.9 Argent reçu et rendu
Liste chronologique simple, en deux colonnes **« Entrées dans la caisse »** / **« Sorties de la caisse »** :
versements des participants, dépenses payées par l'asso, remboursements d'avances, remboursements de désistement.

Bouton **« Qui doit quoi à qui ? »** : liste des virements à faire pour tout solder (participants → asso, asso → participants).

### 2.10 Entre nous (comptes entre participants)
Sous-comptes indépendants, créés à la demande (« Resto du jeudi », « Courses voiture 2 »). Même logique que Tricount : qui a payé, pour qui, et à la fin « Paul doit 12 € à Julie ». **N'apparaissent jamais dans l'export pour l'asso.** Ils peuvent être exportés à part (PDF simple à partager entre participants).

### 2.11 Prévu / Réel
Chaque dépense a un montant prévu et un montant réel. Tant que le réel n'est pas saisi, c'est le prévu qui compte. Un interrupteur en haut des vues permet d'afficher **« selon le prévu »** ou **« selon le réel »**, et une colonne « écart » apparaît quand les deux existent. En fin de voyage, la fiche de chacun montre la régularisation (« le réel a coûté 18 € de moins que prévu : on te doit 18 € »).

### 2.12 Exporter
Un bouton, deux fichiers :
- **Bilan du voyage (PDF)**, voir §4.
- **Justificatifs (ZIP)** : chaque fichier renommé avec son numéro (`J01_gite_acompte.pdf`, `J02_peage_A7.jpg`…), numéros repris dans le PDF.

---

## 3. Règles de calcul

1. **Part d'une dépense** = montant × (parts de la personne ÷ total des parts).
2. **Arrondis** : chaque part est arrondie **au centime supérieur**. Les quelques centimes en trop reviennent à l'asso et apparaissent sur une ligne « Écart d'arrondi (au profit de l'asso) ». L'asso n'est jamais perdante d'un centime.
3. **Ajustement** : la différence est répartie selon le choix fait (autres au prorata de leurs parts / une personne / l'asso). Les montants verrouillés sont exclus de la compensation. Si tout le monde est verrouillé, l'appli le dit et propose l'asso.
4. **Ce qu'une personne doit encore** = sa part totale − ses versements − ses avances + ce que l'asso lui a déjà remboursé.
   - Positif → elle doit encore.
   - Négatif → l'asso lui doit.
5. **Prise en charge par l'asso** (ajustements, désistements) = argent que l'asso met de sa poche ; totalisé à part dans le bilan.
6. **Contrôle d'équilibre permanent** : total des parts + prises en charge asso − écart d'arrondi = coût total. Si ce n'est pas juste, une alerte s'affiche (ça ne devrait jamais arriver, c'est un garde-fou).
7. Tous les montants sont stockés en **centimes entiers** (pas d'erreur de virgule).

---

## 4. Le bilan PDF pour le trésorier

Rédigé pour être lu par quelqu'un qui n'est pas comptable.

**Page 1 — Le voyage en un coup d'œil**
- Nom, dates, lieu, nombre de participants, organisateur.
- En phrases simples :
  - « Le voyage a coûté **4 850,00 €**. »
  - « Les participants ont payé **4 700,00 €**. »
  - « L'asso a pris en charge **150,00 €** (détail page 3). »
  - « Reste à encaisser : **0,00 €** — Reste à rembourser : **0,00 €**. » (ou la liste des personnes concernées)

**Page 2 — Les participants**
Un tableau : Nom · Sa part · A versé · A avancé · Remboursé par l'asso · **Reste** (« à jour » / « doit X € » / « on lui doit X € »).

**Page 3 — Ce que l'asso a pris en charge**
Chaque ajustement et désistement compensé par l'asso, avec le motif.

**Page 4 — Les dépenses**
Par catégorie : libellé, date, prévu, réel, payé par (asso / nom du participant), n° de justificatif. Sous-total par catégorie, total général.

**Page 5 — Les mouvements d'argent**
Liste chronologique en « Entrées » / « Sorties » de la caisse de l'asso, avec total.

**Annexe — Détail par participant**
Une fiche par personne (le relevé du §2.4).

---

## 5. Architecture technique

- **Python**, même base que BioGuesser : **FastAPI + Jinja2 + SQLAlchemy + SQLite**. Pages générées côté serveur, petites touches d'interactivité avec **htmx** (aperçus en direct sans recharger la page). Pas de framework JavaScript lourd.
- **PDF** : bibliothèque Python pure (fpdf2), pour éviter les dépendances système compliquées sous Windows.
- **Données** : un seul dossier `Compta Voyages` dans les Documents de l'utilisateur, contenant la base (`compta.db`) et les justificatifs. **Sauvegarder = copier ce dossier.** Sauvegarde automatique quotidienne (on garde les 10 dernières).
- **Annuler** : bouton « Annuler la dernière action » (historique des modifications).
- **Lancement** : un exécutable par système (PyInstaller) — double-clic → le navigateur s'ouvre sur l'application. Pas besoin d'installer Python. Pour développer : `uv run`.
- **Couleurs** : palette BioGuesser (bleu océan, turquoise, sable, corail), thème clair et sombre.
  - vert « à jour » = `--success`, orange « doit encore » = sable / ambre, bleu « on lui doit » = `--ocean-bright`, alertes = `--coral`.
- **Tests automatiques** sur toutes les règles de calcul (arrondis, ajustements en cascade, désistements, verrous).

---

## 6. Étapes de réalisation

1. **Moteur de calcul + tests** : parts, arrondis, ajustements, désistements, « qui doit quoi ».
2. **Écrans de base** : voyages, participants, dépenses, fiche participant, tableau de bord.
3. **Ajustements, désistements, tableau de répartition, prévu/réel.**
4. **Justificatifs + export PDF/ZIP.**
5. **Comptes « Entre nous ».**
6. **Packaging Windows / Mac, sauvegardes, annuler.**

Après l'étape 2, tu peux déjà t'en servir pour un vrai voyage.

---

## 7. Hors périmètre (pour l'instant)

Devises étrangères · moyens de paiement · plusieurs utilisateurs / mots de passe · annuaire de membres partagé entre voyages · échéancier d'acomptes.
