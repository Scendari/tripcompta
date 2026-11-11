# Installer Trip Compta

Trip Compta est une application toute prête, pour **Mac** et **Windows** : **rien d'autre à installer**. Elle
tourne sur ton ordinateur, dans sa propre fenêtre, comme une application ordinaire. Tes données restent chez toi.

Une fois installée, elle **se met à jour toute seule** : quand une nouvelle version sort, un bandeau
« Trip Compta X est disponible » apparaît ; un clic sur « Mettre à jour », et c'est fait.

👉 **Le plus simple : la page [Télécharger](https://tripcompta.plongee.bio/telecharger)** du site de Trip Compta.
Elle est en français, propose un bouton par ordinateur et montre les alertes auxquelles t'attendre.

Tu peux aussi prendre le fichier directement :

| Ton ordinateur | Le fichier à télécharger |
|---|---|
| Mac récent (puce Apple M1, M2, M3…) | [`TripCompta-mac-arm64.zip`](https://github.com/Scendari/tripcompta/releases/latest/download/TripCompta-mac-arm64.zip) |
| Mac plus ancien (processeur Intel) | [`TripCompta-mac-intel.zip`](https://github.com/Scendari/tripcompta/releases/latest/download/TripCompta-mac-intel.zip) |
| Windows | [`TripCompta-windows-setup.exe`](https://github.com/Scendari/tripcompta/releases/latest/download/TripCompta-windows-setup.exe) |

> Mac : pour savoir quelle puce tu as, menu  → **À propos de ce Mac** : « Puce Apple M… » ou « Processeur Intel ».

Dans ce guide : [Mac](#mac) · [Windows](#windows) · [Mises à jour](#mises-à-jour) ·
[Nouvel ordinateur](#nouvel-ordinateur) · [Désinstaller](#désinstaller) ·
[Dossier Cloud](#mettre-tes-voyages-dans-ton-cloud-à-tes-risques) ·
[Mot de passe](#protéger-tes-voyages-par-un-mot-de-passe-facultatif) · [Sauvegardes](#sauvegardes) ·
[Questions](#questions)

---

## Mac

### À la main

1. Télécharge le fichier `.zip` (voir le tableau plus haut). Dans **Téléchargements**, double-clique dessus :
   **TripCompta** apparaît.
2. Si Trip Compta est déjà ouvert, quitte-le (**⌘Q**). Puis glisse **TripCompta** dans le dossier **Applications**
   (s'il y est déjà : **Remplacer**).
3. Dans **Applications**, double-clique sur **TripCompta**. La première fois, macOS affiche :
   *« TripCompta » n'a pas pu être ouvert car Apple ne peut pas vérifier qu'il ne contient pas de logiciel
   malveillant.* C'est normal (voir l'encadré). Clique sur **Terminé**.
4. Ouvre **Réglages Système** → **Confidentialité et sécurité**, descends jusqu'à
   *« TripCompta » a été bloqué…* et clique sur **Ouvrir quand même**, puis confirme avec ton mot de passe.

Les étapes 3 et 4 ne sont à faire **qu'une fois** : les mises à jour faites par l'application ne redemandent rien.

> **Pourquoi cette alerte ?** Apple fait payer chaque année le droit de « signer » une application. Trip Compta
> est gratuit, fait par un bénévole : il n'est pas signé, et ton Mac prévient qu'il ne connaît pas son auteur.
> Le code est public (sur GitHub) : tu l'autorises une fois, et c'est réglé.

### Ou bien : une commande, sans alerte

Si le Terminal ne te fait pas peur, une seule ligne fait tout, et macOS n'affiche alors aucune alerte.

1. Ouvre l'application **Terminal** (⌘ + Espace, tape « Terminal », Entrée).
2. Copie-colle cette ligne, puis appuie sur Entrée :

   ```
   A=$(uname -m | sed 's/x86_64/intel/'); curl -fL -o /tmp/TripCompta.zip "https://github.com/Scendari/tripcompta/releases/latest/download/TripCompta-mac-$A.zip" && rm -rf /Applications/TripCompta.app && ditto -x -k /tmp/TripCompta.zip /Applications && open /Applications/TripCompta.app
   ```

Ce qu'elle fait, dans l'ordre : elle regarde quelle puce a ton Mac, télécharge le bon fichier, **retire l'ancienne
application Trip Compta** du dossier Applications s'il y en a une (seulement l'application : tes voyages ne sont
pas dedans, ils ne sont pas touchés), range la nouvelle à sa place et l'ouvre. C'est exactement la même
application que celle du fichier à télécharger.

### Bon à savoir sur Mac

- Au premier lancement, macOS peut demander si Trip Compta peut accéder au dossier **Documents** : réponds
  **Autoriser** (c'est là que sont rangés tes voyages).
- Trip Compta doit être **dans le dossier Applications**. Ouvert depuis Téléchargements ou le Bureau, il affiche
  « 📦 Glisse Trip Compta dans le dossier Applications » : sinon la mise à jour en un clic ne marche pas.
- Oublié de quitter l'ancienne version avant de la remplacer ? La nouvelle s'en charge : à son ouverture, elle
  ferme proprement l'ancienne (tout est enregistré) et prend sa place.
- **⌘N** ouvre une 2ᵉ fenêtre (par exemple deux voyages côte à côte), **⌘F** cherche dans la page (menu
  **Fichier**).
- Pour arrêter Trip Compta : ferme sa fenêtre, ou **⌘Q**. Tout est enregistré à chaque modification.

---

## Windows

1. Télécharge `TripCompta-windows-setup.exe`. Dans **Téléchargements**, double-clique dessus.
2. Si Windows affiche *« Windows a protégé votre ordinateur »* : clique sur **Informations complémentaires**, puis
   **Exécuter quand même**. (Une seule fois. C'est normal : comme sur Mac, l'installeur n'est pas « signé »,
   parce que cette signature est payante.)
3. Clique sur **Installer**. Pas besoin d'être administrateur : Trip Compta s'installe pour toi seul, avec un
   raccourci dans le menu Démarrer et un sur le Bureau (décoche la case si tu n'en veux pas sur le Bureau).

Trip Compta s'ouvre dans **sa propre fenêtre**. Pour l'arrêter, ferme simplement cette fenêtre : tout est enregistré.
**Ctrl+N** ouvre une 2ᵉ fenêtre, **Ctrl+F** cherche dans la page.

**Le composant qui affiche la fenêtre (WebView2).** C'est le moteur d'Edge, déjà présent dans Windows 11 et la
plupart des Windows 10. S'il manque, l'installeur l'ajoute (connexion Internet nécessaire), ou Trip Compta propose
de le télécharger à son lancement. Sans Internet sur cet ordinateur : télécharge ailleurs le « programme
d'installation autonome » (Evergreen Standalone Installer, x64) sur
https://developer.microsoft.com/fr-fr/microsoft-edge/webview2/ et apporte-le sur une clé USB.

---

## Mises à jour

- Trip Compta regarde une fois par jour s'il existe une nouvelle version. Quand il y en a une, un bandeau
  l'annonce, avec ses nouveautés : **Mettre à jour**, et l'application se relance toute seule, à jour.
- **Tes voyages ne sont jamais touchés** par une mise à jour, et une copie en est faite juste avant.
- Trip Compta **vérifie que la nouvelle version vient bien de son auteur** avant de l'installer ; sinon, il la
  refuse et garde la version actuelle.
- Si la nouvelle version ne démarre pas, l'ancienne est remise en place toute seule, et un message l'explique.
- Pour voir ta version, vérifier à la main ou couper la vérification automatique :
  **⚙️ Réglages → Version et mises à jour**.

---

## Nouvel ordinateur

Tes voyages ne sont pas « dans » l'application : ils sont dans un dossier (par défaut **Documents/TripCompta**).
Installer Trip Compta sur le nouvel ordinateur ne suffit donc pas : il faut aussi y apporter tes voyages. Trois
façons, de la plus simple à la plus complète.

### 1. Un fichier à emporter (le plus simple)

**Sur l'ancien ordinateur**

1. Ouvre un voyage, puis **📦 Exporter le voyage** (dans le menu du voyage).
2. **⬇ Exporter tous les voyages** (en bas de la page, si tu en as plusieurs) : un seul fichier avec tous tes
   voyages et leurs justificatifs. Pour un seul voyage : **⬇ Télécharger le fichier du voyage**.
3. Mets ce fichier sur une clé USB, ou envoie-le-toi par mail.

**Sur le nouvel ordinateur**

1. Installe Trip Compta (voir plus haut) et ouvre-le.
2. Page **Mes voyages** → **Importer un voyage**, et choisis le fichier.
3. Un aperçu montre ce qui arrive ; tu confirmes. Les voyages sont ajoutés : rien de ce qui existe n'est remplacé.

### 2. Copier tout le dossier

Pour tout retrouver à l'identique, y compris les brouillons de la page IA.

1. Sur l'ancien ordinateur, **quitte Trip Compta**, puis copie le dossier **Documents/TripCompta** en entier sur
   une clé USB. (Tu ne sais plus où il est ? **⚙️ Réglages → Où sont mes voyages → 📂 Ouvrir le dossier**.)
2. Sur le nouvel ordinateur, colle ce dossier **dans Documents** avant d'ouvrir Trip Compta pour la première
   fois : il y trouvera tes voyages.
3. Trip Compta a déjà été ouvert sur le nouvel ordinateur ? Colle le dossier où tu veux, puis **⚙️ Réglages →
   Où sont mes voyages → Changer de dossier → Parcourir…** : Trip Compta voit les voyages du dossier →
   **Ouvrir les voyages de ce dossier** (rien n'est copié par-dessus).

### 3. Tes voyages sont déjà dans un dossier Cloud

1. Installe Trip Compta sur le nouvel ordinateur, et attends que ton cloud ait fini de recopier le dossier.
2. **⚙️ Réglages → Où sont mes voyages → Changer de dossier → Parcourir…**, choisis ce même dossier, puis
   **Ouvrir les voyages de ce dossier**.
3. **Un seul ordinateur à la fois** : ferme Trip Compta sur l'ancien avant de l'ouvrir sur le nouveau
   ([détails plus bas](#mettre-tes-voyages-dans-ton-cloud-à-tes-risques)).

### À savoir

- **Voyages protégés par un mot de passe** : le nouvel ordinateur demande le même mot de passe (façons 2 et 3).
  Garde ta clé de secours à portée de main. Un fichier exporté (façon 1), lui, n'est pas protégé : ne le laisse
  pas traîner.
- **Les sauvegardes quotidiennes ne suivent pas** : elles sont gardées à part sur chaque ordinateur, et
  recommencent sur le nouveau dès la première ouverture.
- **Vérifie avant d'effacer** : ouvre un voyage sur le nouvel ordinateur, regarde que les montants et les
  justificatifs sont là, et seulement ensuite fais le ménage sur l'ancien (voir « Désinstaller »).

---

## Désinstaller

Dans tous les cas, **désinstaller l'application n'efface pas tes voyages** : ils restent dans leur dossier, et
tu les retrouves si tu réinstalles Trip Compta.

### Sur Mac

1. Quitte Trip Compta (**⌘Q**).
2. Dans le Finder, ouvre **Applications** et glisse **TripCompta** dans la corbeille.

C'est tout. Pour **tout effacer** de cet ordinateur (facultatif) :

1. *Avant* de mettre l'application à la corbeille, si tes voyages sont protégés et que l'ordinateur se souvient du
   mot de passe : **⚙️ Réglages → Mot de passe → Oublier le mot de passe sur cet ordinateur**.
2. **Tes voyages** : mets à la corbeille le dossier **Documents/TripCompta** (ou le dossier que tu avais choisi).
   ⚠️ C'est définitif une fois la corbeille vidée : si tu veux les garder, exporte-les d'abord
   (voir « Nouvel ordinateur »).
3. **Les réglages, le journal et les sauvegardes quotidiennes** de cet ordinateur : dans le Finder, menu
   **Aller → Aller au dossier…**, colle `~/Library/Application Support/TripCompta`, Entrée, puis mets ce dossier
   **TripCompta** à la corbeille.

### Sur Windows

1. **Paramètres → Applications → Applications installées → Trip Compta → Désinstaller**.
   (Sur Windows 10 : **Paramètres → Applications → Applications et fonctionnalités → Trip Compta → Désinstaller**.)

C'est tout. Pour **tout effacer** de cet ordinateur (facultatif) :

1. *Avant* de désinstaller, si l'ordinateur se souvient du mot de passe de tes voyages :
   **⚙️ Réglages → Mot de passe → Oublier le mot de passe sur cet ordinateur**.
2. **Tes voyages** : supprime le dossier **Documents\TripCompta** (ou le dossier que tu avais choisi).
   ⚠️ Exporte-les d'abord si tu veux les garder.
3. **Les réglages, le journal et les sauvegardes quotidiennes** : dans la barre d'adresse de l'Explorateur de
   fichiers, tape `%APPDATA%\TripCompta`, Entrée, puis supprime ce dossier.

---

## Mettre tes voyages dans ton cloud (à tes risques)

Tu peux ranger tes voyages dans **ton** Dropbox, Google Drive, OneDrive, iCloud Drive, kDrive ou Proton Drive pour les
retrouver sur un autre ordinateur, sans aucun serveur (rien n'est envoyé ailleurs que chez ton service).
Trip Compta ne s'occupe pas de ton cloud : il lit et écrit simplement dans le dossier que tu lui indiques.
**Un seul ordinateur à la fois** (voir plus bas).

**Sur le premier ordinateur**

1. Vérifie que l'application de ton cloud est installée sur l'ordinateur (le dossier Dropbox, Google Drive,
   OneDrive ou iCloud Drive apparaît dans le Finder ou l'Explorateur).
2. Dans Trip Compta : **⚙️ Réglages → Où sont mes voyages**.
3. Dans **Changer de dossier**, clique sur **Parcourir…** et choisis un dossier de ton cloud (par exemple un
   nouveau dossier « TripCompta »), puis **Copier mes voyages dans ce dossier**. Tout est copié et vérifié
   (l'ancien dossier n'est pas touché) ; Trip Compta redémarre tout seul.

**Sur les autres ordinateurs**

1. Installe Trip Compta, attends que ton cloud ait fini de synchroniser le dossier **TripCompta**.
2. **⚙️ Réglages → Où sont mes voyages** → **Changer de dossier** → choisis le même dossier : Trip Compta
   indique les voyages qu'il y trouve → **Ouvrir les voyages de ce dossier** (rien n'est copié par-dessus).

**Un seul ordinateur à la fois** : Trip Compta enregistre directement dans le dossier, et ton cloud le recopie.
C'est possible, mais à tes risques :

- **ferme Trip Compta** sur un ordinateur avant de l'ouvrir sur un autre, et attends que ton cloud ait fini de
  synchroniser ; ouvert sur deux ordinateurs en même temps, les saisies de l'un peuvent effacer celles de l'autre ;
- à l'ouverture, Trip Compta te dit si tes voyages viennent d'être modifiés sur un autre ordinateur ;
- si ton cloud a gardé deux versions (« copie en conflit »), Trip Compta te le dit : **Exporter cette version
  (.zip)**, puis **Mes voyages › Importer un voyage** pour en récupérer les saisies (rien n'est écrasé) ;
- si ton cloud a retiré le fichier de l'ordinateur (disque presque plein, « Libérer de l'espace »…), Trip Compta te
  le dit sans rien toucher : clic droit sur le dossier TripCompta → « Toujours garder sur cet appareil » (iCloud,
  OneDrive) ou « Rendre disponible hors connexion » (Google Drive, Dropbox), puis **Réessayer** ;
- sur un même ordinateur, Trip Compta ne s'ouvre qu'une fois : cliquer de nouveau sur l'icône montre celle qui est
  déjà ouverte.

**Windows** : le dossier Documents est souvent dans OneDrive sans que tu le saches (Windows le fait tout seul).
C'est sans risque tant que tu utilises Trip Compta sur un seul ordinateur.

---

## Protéger tes voyages par un mot de passe (facultatif)

Dans **⚙️ Réglages → Mot de passe** :

1. Choisis un mot de passe (une phrase facile à retenir est idéale).
2. Trip Compta t'affiche une **clé de secours** (8 groupes de 4 caractères) : **imprime-la ou note-la sur papier**,
   et range-la ailleurs que sur l'ordinateur. Il te demande d'en retaper un morceau pour vérifier.
3. Il chiffre tout, vérifie, puis redémarre et te demande le mot de passe.

**Quand l'activer ?** Si tu partages le dossier, si l'ordinateur est partagé (club), ou pour mieux protéger les
données personnelles de tes adhérents. Seul sur ton ordinateur, avec un compte cloud bien protégé, ce n'est pas
indispensable.

**À savoir**

- **Si tu perds le mot de passe ET la clé de secours, tes voyages sont perdus pour toujours.** Personne (pas même
  l'auteur de Trip Compta) ne peut les ouvrir.
- Mot de passe oublié : sur l'écran de déverrouillage, **Mot de passe oublié ?** → ta clé de secours, puis un
  nouveau mot de passe.
- **Vide la corbeille et les anciennes versions de ton cloud** après l'activation (Google Drive les garde 30 jours) :
  elles contiennent encore les fichiers d'avant, lisibles.
- « Se souvenir sur cet ordinateur » : plus besoin de taper le mot de passe sur cet ordinateur (il est rangé dans
  le trousseau du système). **🔒 Verrouiller**, en haut, le redemande.
- Sur un 2e ordinateur, Trip Compta demande le même mot de passe.
- Les PDF, les messages et les fichiers que tu exportes restent lisibles : ils sont faits pour être envoyés.
- Pour revenir en arrière : **Retirer la protection** (avec le mot de passe).

---

## Sauvegardes

Tu n'as rien à faire : chaque jour où tu ouvres Trip Compta, une copie de tous tes voyages (justificatifs
compris) est mise de côté sur cet ordinateur, à part du dossier de tes voyages. Les 10 dernières sont gardées.

- **Une erreur à l'instant ?** Le bouton **↶ Défaire**, en haut, montre tes dernières modifications et permet de
  revenir en arrière.
- **Une erreur vue trop tard ?** **⚙️ Réglages → Sauvegardes → Revenir à cette sauvegarde** : tous tes voyages
  redeviennent ce qu'ils étaient ce jour-là (et ce retour lui-même peut être défait).
- **Une copie à ranger ailleurs** (clé USB, autre ordinateur) : les sauvegardes ne quittent pas l'ordinateur ;
  pour cela, **📦 Exporter le voyage**.

---

## Questions

- **Où sont mes données ?** Dans le dossier **Documents/TripCompta**, ou dans le dossier que tu as choisi :
  **⚙️ Réglages → Où sont mes voyages** l'affiche, avec un bouton pour l'ouvrir.
- **Comment savoir quelle version j'ai ?** **⚙️ Réglages → Version et mises à jour**.
- **La vérification des mises à jour envoie-t-elle mes données ?** Non : elle demande seulement à GitHub (le site
  où Trip Compta est publié) quelle est la dernière version.
- **Par où commencer ?** Dans l'application, **❓ Aide** (en haut) : les premiers pas, les mots de Trip Compta, des
  astuces. **⚙️ Réglages → Voyage d'exemple** ouvre un voyage fictif complet pour voir comment tout fonctionne.
  Le guide en ligne : [tripcompta.plongee.bio/guide](https://tripcompta.plongee.bio/guide).
- **Quelque chose ne marche pas ?** **⚙️ Réglages → Version et mises à jour → Exporter un diagnostic** : un petit
  fichier texte, sans nom ni montant, à envoyer à la personne qui t'aide.
- **Et sous Linux ?** Il n'y a pas d'application toute prête. Trip Compta peut se lancer depuis son code source
  (voir le [README](README.md)), dans le navigateur.
