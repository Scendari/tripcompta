# Installer Trip Compta

Trip Compta est une application toute prête : **rien d'autre à installer** (pas de Python). Elle tourne sur ton
ordinateur et s'affiche dans ton navigateur. Tes données restent chez toi.

Une fois installée, elle **se met à jour toute seule** : quand une nouvelle version sort, un bandeau
« Trip Compta X est disponible » apparaît ; un clic sur « Mettre à jour », et c'est fait.

👉 Les fichiers à télécharger sont sur la page **[Dernière version](https://github.com/Scendari/tripcompta/releases/latest)**,
tout en bas, dans « Assets ».

| Ton ordinateur | Le fichier à prendre |
|---|---|
| Mac récent (puce Apple M1, M2, M3…) | `TripCompta-mac-arm64.zip` |
| Mac plus ancien (processeur Intel) | `TripCompta-mac-intel.zip` |
| Windows | `TripCompta-windows.zip` |
| Linux | `TripCompta-linux.tar.gz` |

> Mac : pour savoir quelle puce tu as, menu  → **À propos de ce Mac** : « Puce Apple M… » ou « Processeur Intel ».

---

## Mac

### Le plus simple : une commande, sans aucune alerte

1. Ouvre l'application **Terminal** (⌘ + Espace, tape « Terminal », Entrée).
2. Copie-colle cette ligne, puis appuie sur Entrée :

   ```
   A=$(uname -m | sed 's/x86_64/intel/'); curl -fL -o /tmp/TripCompta.zip "https://github.com/Scendari/tripcompta/releases/latest/download/TripCompta-mac-$A.zip" && ditto -x -k /tmp/TripCompta.zip /Applications && open /Applications/TripCompta.app
   ```

   Elle télécharge la bonne version pour ton Mac, la range dans **Applications** et la lance.

### Ou bien à la main

1. Télécharge le fichier `.zip` (voir le tableau plus haut) et double-clique dessus : **TripCompta** apparaît.
2. Glisse **TripCompta** dans le dossier **Applications**.
3. Double-clique sur **TripCompta**. La première fois, macOS affiche :
   *« TripCompta » n'a pas pu être ouvert car Apple ne peut pas vérifier qu'il ne contient pas de logiciel
   malveillant.* C'est normal : l'application n'est pas enregistrée (payante) chez Apple. Clique sur **Terminé**.
4. Ouvre **Réglages Système** → **Confidentialité et sécurité**, descends jusqu'à
   *« TripCompta » a été bloqué…* et clique sur **Ouvrir quand même**, puis confirme avec ton mot de passe.

Ces étapes 3 et 4 ne sont à faire **qu'une fois** : les mises à jour faites par l'application ne redemandent rien.

Au premier lancement, macOS peut demander si Trip Compta peut accéder au dossier **Documents** : réponds
**Autoriser** (c'est là que sont rangées tes données).

Trip Compta s'ouvre dans **sa propre fenêtre** (comme une application ordinaire). **Cmd+N** ouvre une 2ᵉ fenêtre
(par exemple deux voyages côte à côte), **Cmd+F** cherche dans la page, et le menu **Fichier → Ouvrir dans le
navigateur** l'affiche dans ton navigateur si tu préfères. Pour l'arrêter : ferme la fenêtre, ou **Cmd+Q**.

---

## Windows

1. Télécharge `TripCompta-windows.zip`.
2. *(Facultatif, évite l'alerte de l'étape 4)* clic droit sur le fichier → **Propriétés** → coche **Débloquer**
   en bas → **OK**.
3. Clic droit → **Extraire tout…** et choisis un dossier **à toi**, par exemple ton dossier personnel
   (`C:\Users\<ton nom>`). Évite « Program Files » : l'application ne pourrait pas se mettre à jour toute seule.
4. Dans le dossier **TripCompta**, double-clique sur **TripCompta.exe**. Si Windows affiche
   *« Windows a protégé votre ordinateur »* : clique sur **Informations complémentaires**, puis
   **Exécuter quand même**. (Une seule fois.)
5. Pour la retrouver facilement : clic droit sur **TripCompta.exe** → **Envoyer vers** → **Bureau (créer un raccourci)**.

Trip Compta s'ouvre dans **sa propre fenêtre**. Pour l'arrêter, ferme simplement cette fenêtre : tout est enregistré.
(Il utilise WebView2, le moteur d'Edge, déjà présent dans Windows 10 et 11.)

---

## Linux

```bash
curl -fL -o /tmp/TripCompta.tar.gz https://github.com/Scendari/tripcompta/releases/latest/download/TripCompta-linux.tar.gz
mkdir -p ~/Applications && tar -xzf /tmp/TripCompta.tar.gz -C ~/Applications
~/Applications/TripCompta/TripCompta
```

(Linux 64 bits, distribution de 2022 ou plus récente.)

---

## Retrouver tes voyages sur plusieurs ordinateurs

Tu peux ranger tes voyages dans **ton** Dropbox, Google Drive, OneDrive, iCloud Drive, kDrive ou Proton Drive :
ils te suivent sur tous tes ordinateurs, sans aucun serveur (rien n'est envoyé ailleurs que chez ton service).

**Sur le premier ordinateur**

1. Vérifie que l'application de ton cloud est installée sur l'ordinateur (le dossier Dropbox, Google Drive,
   OneDrive ou iCloud Drive apparaît dans le Finder ou l'Explorateur).
2. Dans Trip Compta : **⚙️ Réglages → Où sont mes voyages**.
3. Dans **Changer de dossier**, choisis ton service dans la liste (ou « Autre dossier… » puis **Parcourir…**), garde
   **Copier mes voyages dans ce dossier**, puis clique sur le bouton du même nom. Tout est copié et vérifié ; Trip Compta
   redémarre tout seul.

**Sur les autres ordinateurs**

1. Installe Trip Compta, attends que ton cloud ait fini de synchroniser le dossier **TripCompta**.
2. **⚙️ Réglages → Où sont mes voyages** → **Changer de dossier** → choisis le même dossier : Trip Compta
   indique les voyages qu'il y trouve → **Ouvrir les voyages de ce dossier**.

**Rien d'autre à régler** : Trip Compta travaille sur une copie posée sur ton ordinateur, et ton cloud envoie les
changements. Seul cas à connaître : si ton cloud a retiré le fichier de l'ordinateur (disque presque plein, ou
« Retirer le téléchargement » / « Libérer de l'espace » choisi par erreur), Trip Compta te le dit et passe en lecture
seule. Il suffit alors de le rendre de nouveau disponible : clic droit sur le dossier TripCompta →
« Toujours garder sur cet appareil » (iCloud, OneDrive) ou « Rendre disponible hors connexion » (Google Drive,
Dropbox), puis relancer Trip Compta.

**À deux ordinateurs**

- **Un seul à la fois** peut modifier les voyages. Sur l'autre, ils s'ouvrent en **lecture seule**, avec le nom de
  l'ordinateur qui a la main, et se mettent à jour quand tu reviens sur l'app.
- Quand tu as fini : **ferme simplement la fenêtre de Trip Compta**. Il enregistre, rend la main, et l'autre
  ordinateur la reprend tout seul (dans un navigateur : ferme l'onglet, Trip Compta rend la main au bout de 2 minutes) (rien n'est perdu : tout est enregistré à chaque modification).
- Ordinateur resté allumé, planté, ou pas le temps d'attendre : **Prendre la main** dans le bandeau « Lecture
  seule ». L'autre passe en lecture seule (le temps que ton cloud le prévienne). Préviens d'abord la personne si
  elle est en train de saisir : sinon tu devras choisir entre deux versions (rien n'est perdu).
- Sur un même ordinateur, Trip Compta ne s'ouvre qu'une fois : cliquer de nouveau sur l'icône montre celle qui
  est déjà ouverte.
- Si ton cloud a gardé deux versions (« copie en conflit »), Trip Compta te le dit et te laisse choisir ; rien
  n'est effacé.

**Depuis ton téléphone** : dans l'app de ton cloud, dépose les photos de tickets dans le dossier
**TripCompta / Justificatifs à trier**. Dans Trip Compta → 📎 Justificatifs, coche-les et choisis les dépenses.

---

## Protéger tes voyages par un mot de passe (facultatif)

Dans **☁️ Où ranger mes voyages** → **🔒 Protéger mes voyages…** :

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
- Les photos déposées depuis le téléphone dans « Justificatifs à trier » arrivent en clair ; elles sont chiffrées
  (et l'original supprimé) dès que tu les ranges dans un voyage.
- Les PDF et mails que tu envoies restent lisibles : ils sont faits pour être lus.
- Pour revenir en arrière : **Retirer la protection** (avec le mot de passe).

---

## Questions

- **Où sont mes données ?** Dans le dossier **Documents/TripCompta**, ou dans le dossier synchronisé que tu as
  choisi (**☁️ Où ranger mes voyages**, en bas de l'accueil). Elles ne sont jamais touchées par une mise à jour, et une copie de sécurité est faite juste avant
  (dans `sauvegardes/`).
- **Comment savoir quelle version j'ai ?** En bas de l'accueil : **À propos**. On peut aussi y vérifier les mises
  à jour à la main, ou désactiver la vérification automatique.
- **La vérification envoie-t-elle mes données ?** Non : elle demande seulement à GitHub (le site où Trip Compta est
  publié) quelle est la dernière version.
- **Et si une mise à jour se passe mal ?** L'ancienne version est remise en place toute seule, et un message
  l'explique.
