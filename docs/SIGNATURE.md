# Signer Trip Compta (Mac et Windows)

Sans signature, Trip Compta marche, mais le premier lancement demande un geste en plus :

- **Mac** : « Trip Compta ne peut pas être ouvert… » → Réglages Système › Confidentialité et sécurité ›
  **Ouvrir quand même** ;
- **Windows** : « Windows a protégé votre ordinateur » → **Informations complémentaires** › **Exécuter quand
  même**.

La signature supprime ces avertissements. Tout est prêt dans le code : il suffit de fournir les secrets.

---

## Mac : Developer ID et notarisation (prêt, en attente du compte)

### Ce qu'il faut acheter

Un compte **Apple Developer Program** : 99 $ par an, sur https://developer.apple.com/programs/ (avec ton
identifiant Apple ; en tant que particulier, c'est ton nom qui apparaît comme développeur).

### Ce qu'il faut créer (une fois)

1. **Certificat « Developer ID Application »** : sur un Mac, Trousseau d'accès › Assistant de certification ›
   Demander un certificat à une autorité… (enregistrer sur le disque), puis sur developer.apple.com ›
   Certificates › **+** › *Developer ID Application*, envoyer la demande, télécharger le certificat et
   double-cliquer dessus.
2. L'**exporter en .p12** : Trousseau d'accès › Mes certificats › clic droit sur « Developer ID Application : … »
   › Exporter… › format .p12, avec un mot de passe.
3. Un **mot de passe pour app** (notarisation) : https://account.apple.com › Connexion et sécurité › Mots de
   passe pour app › **+** (« Trip Compta notarisation »).
4. Ton **Team ID** : developer.apple.com › Account › Membership details (10 caractères, ex. `AB12CD34EF`).

### Les secrets GitHub à ajouter

Dépôt Scendari/tripcompta › Settings › Secrets and variables › Actions › **New repository secret** :

| Secret | Valeur |
|---|---|
| `APPLE_CERT_P12` | le fichier .p12 en base64 : `base64 -i certificat.p12 \| pbcopy` puis coller |
| `APPLE_CERT_PASSWORD` | le mot de passe du .p12 |
| `APPLE_SIGN_IDENTITY` | le nom exact du certificat, ex. `Developer ID Application: Ton Nom (AB12CD34EF)` (`security find-identity -v -p codesigning`) |
| `APPLE_ID` | l'adresse de ton identifiant Apple |
| `APPLE_TEAM_ID` | le Team ID |
| `APPLE_APP_PASSWORD` | le mot de passe pour app |

C'est tout : la prochaine version publiée (`release.yml`) est signée Developer ID avec le runtime renforcé
(`build_tools/mac/entitlements.plist`), envoyée à Apple, et le ticket est agrafé dans l'application. Tant que ces
secrets n'existent pas, la publication continue comme avant (signature « ad hoc »).

Comment c'est fait : `build_tools/signer_mac.py` (`signer` depuis `build.py`, `notariser` depuis `release.yml`).
Pour essayer en local : `APPLE_SIGN_IDENTITY="Developer ID Application: …" python build_tools/build.py` puis
`APPLE_ID=… APPLE_TEAM_ID=… APPLE_APP_PASSWORD=… python build_tools/signer_mac.py notariser dist.noindex/TripCompta.app`.

---

## Windows : les options (rien n'est mis en place sans accord)

L'installeur (`TripCompta-windows-setup.exe`) évite déjà tout blocage des fichiers installés ; seul reste
l'avertissement SmartScreen au premier lancement de l'installeur. Pour le supprimer, il faut signer l'installeur
(et `TripCompta.exe`) avec un certificat reconnu par Windows :

| Option | Coût | Pour qui | Remarques |
|---|---|---|---|
| **SignPath Foundation** | gratuit | projets open source | Certificat au nom de SignPath ; demande à faire (dépôt public, licence libre, projet actif). Signature depuis GitHub Actions. Le plus adapté ici. |
| **Azure Trusted Signing** (Artifact Signing) | ~10 $/mois | entreprises et particuliers des pays éligibles (vérification d'identité) | Pas de clé à garder, signature depuis GitHub Actions ; SmartScreen fait confiance immédiatement. Vérifier l'éligibilité des particuliers en France au moment de la demande. |
| Certificat **OV** (Sectigo, Certum…) | 100 à 300 €/an | particulier ou association | Clé sur un jeton matériel ou un service cloud depuis 2023 ; SmartScreen apprend la « réputation » au fil des téléchargements. Certum propose une offre « Open Source Developer » moins chère. |
| Certificat **EV** | 300 à 600 €/an | entreprises immatriculées | Confiance SmartScreen immédiate ; inaccessible à un particulier. |

Recommandation : demander **SignPath Foundation** (gratuit). S'il refuse, **Azure Trusted Signing** si tu y
es éligible, sinon un certificat **OV Certum Open Source**. Dans tous les cas, l'ajout se fait dans `release.yml`
après la fabrication de `TripCompta.exe` et de l'installeur (`signtool` ou l'action GitHub du service choisi).
