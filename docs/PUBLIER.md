# Publier une version de Trip Compta

## En bref

```
git tag -a v1.6.0 -m "Les nouveautés, une par ligne"
git push origin v1.6.0
```

GitHub fait le reste (`.github/workflows/release.yml`) : les tests, les trois applications (Mac Apple Silicon,
Mac Intel, installeur Windows), le fichier d'empreintes **signé**, puis la publication. Les applications déjà
installées proposent ensuite la mise à jour toutes seules.

Une étiquette avec un tiret (`v1.6.0-beta1`) donne une version d'essai : elle n'est pas proposée aux
applications installées.

## La signature des mises à jour

Avant d'installer une mise à jour, Trip Compta vérifie deux choses :

1. le fichier d'empreintes de la version (`SHA256SUMS.txt`) est **signé par Trip Compta**, pour **cette version**
   (`SHA256SUMS.txt.sig`) ;
2. le fichier téléchargé a bien l'empreinte annoncée.

Sans la première vérification, quelqu'un qui arriverait à modifier une publication pourrait remplacer à la fois
le programme et son empreinte. Avec elle, il lui faudrait aussi la **clé privée**, qui n'est ni dans le dépôt ni
dans l'application.

Ce n'est pas la signature d'Apple ou de Microsoft (celle qui évite les avertissements à l'installation) : elle
ne coûte rien, ne demande aucun compte, et ne protège que les mises à jour faites depuis l'application.

### Les deux moitiés de la clé

- **Clé publique** : dans `compta/signature.py` (`PUBLIC_KEYS`). Elle ne sert qu'à vérifier ; elle peut être lue
  par tout le monde.
- **Clé privée** : le secret GitHub `TRIPCOMPTA_CLE_SIGNATURE` du dépôt. Elle sert à signer, au moment de la
  publication seulement. **Elle ne doit jamais être écrite dans le dépôt, un message ou un document partagé.**

### À faire une seule fois : donner la clé privée à GitHub

La clé privée a été créée sur l'ordinateur de publication, dans un fichier hors du dépôt. Pour la confier à
GitHub (remplacer le chemin par celui du fichier) :

```
gh secret set TRIPCOMPTA_CLE_SIGNATURE --repo Scendari/tripcompta < ~/.config/tripcompta-publication/cle-signature-maj.txt
```

Puis **garder une copie de ce fichier en lieu sûr** (gestionnaire de mots de passe, clé USB rangée) : GitHub ne
permet pas de relire un secret.

Tant que le secret manque, la publication s'arrête dès la première étape avec un message clair : rien n'est
fabriqué ni publié sans signature.

### Si la clé privée est perdue

Les applications installées n'accepteront plus aucune mise à jour automatique : il faudra créer une nouvelle clé,
publier une version, et demander aux utilisateurs de la réinstaller à la main une fois (leurs voyages ne sont pas
touchés). D'où la copie en lieu sûr.

### Changer de clé (sans rien casser)

1. `python build_tools/signer_empreintes.py nouvelle-cle ~/un/fichier/hors/du/depot.txt` : la clé privée est écrite
   dans ce fichier (jamais affichée), la clé publique est affichée.
2. Ajouter la nouvelle clé publique dans `PUBLIC_KEYS` **en gardant l'ancienne**, et publier une version : elle est
   encore signée par l'ancienne clé, mais les applications qui l'installent connaissent désormais la nouvelle.
3. Remplacer le secret GitHub par la nouvelle clé privée ; les versions suivantes sont signées avec elle.
4. Plus tard, quand tout le monde a eu le temps de se mettre à jour, retirer l'ancienne clé publique.

### Vérifier à la main une version publiée

Télécharger les fichiers de la version dans un dossier, puis :

```
python build_tools/signer_empreintes.py verifier le/dossier 1.6.0
```

## Publications immuables (réglage du dépôt, à cocher une fois)

GitHub peut figer une version une fois publiée : ses fichiers ne peuvent plus être modifiés, remplacés ni
supprimés, et son étiquette ne peut plus être déplacée. Avec la signature, cela ferme la porte à une version
modifiée après coup.

Sur GitHub : **Settings** du dépôt → **General** → rubrique **Releases** → cocher **Enable release immutability**.

Le flux de publication est fait pour : la version est créée en brouillon, tous les fichiers y sont ajoutés, puis
elle est publiée d'un seul coup. Conséquence à connaître : une version publiée ne se corrige plus. En cas
d'erreur, on publie la suivante (`v1.6.1`).

## Ce que fait la publication, étape par étape

1. **Tests** : l'étiquette correspond à la version du code, la clé de signature est présente et c'est la bonne,
   toute la suite de tests passe.
2. **Fabrication** sur Mac (Apple Silicon et Intel) et Windows ; chaque application est démarrée pour de vrai.
   Sous Windows, l'installeur est essayé : installation, mise à jour, et retour à l'ancienne version quand la
   nouvelle ne démarre pas (`build_tools/essai_installeur.py`).
3. **Empreintes signées** (`build_tools/signer_empreintes.py signer`) : l'outil refuse de signer avec une clé que
   l'application ne connaît pas, puis revérifie tout comme le ferait l'application.
4. **Publication** : brouillon, cinq fichiers attendus (trois applications, `SHA256SUMS.txt`,
   `SHA256SUMS.txt.sig`), puis publication.

## Après une mise à jour qui se passe mal

- **Mac** : le script de remplacement relance la nouvelle version et attend qu'elle réponde ; sinon il remet
  l'ancienne et la relance (`compta/scripts/remplacer.sh`).
- **Windows** : l'installeur met l'ancienne version de côté, installe, relance la nouvelle et attend qu'elle
  dise « j'ai démarré » (deux minutes au plus) ; sinon il l'arrête, remet l'ancienne et la relance
  (`build_tools/windows/TripCompta.iss`).

Dans les deux cas l'utilisateur voit un message simple, et ses voyages ne sont jamais touchés (une copie de
sécurité est faite avant chaque mise à jour).
