# Icône de Trip Compta : bateau en papier, vague et €

Dessinée avec le site promo (tripcompta_webpromo, livrables/icone-app), choisie le 4 novembre 2026.

| Fichier | Usage |
|---|---|
| `TripCompta.svg` | Source vectorielle (64×64, tuile `#1f8a7a`) ; copiée en `compta/static/favicon.svg` et dans l'en-tête (`base.html`). |
| `TripCompta.icns` | Icône de l'app Mac (grille Apple, 16 à 1024 px @1x/@2x), utilisée par `build.py`. |
| `TripCompta.ico` | Icône de l'app Windows (16 à 256 px), utilisée par `build.py`. |
| `TripCompta.png` | Rendu 1024 px pleine tuile (Linux, documentation). |

Pour changer l'icône : refaire ces fichiers depuis le SVG (Mac : `iconutil -c icns TripCompta.iconset`).
