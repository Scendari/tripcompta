#!/bin/bash
# Mac : double-clic pour lancer Trip Compta (installe ce qu'il faut au premier lancement).
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  osascript -e 'display alert "Python 3 est nécessaire" message "Installe Python depuis https://www.python.org/downloads/ puis relance."'
  exit 1
fi
if [ ! -x ".venv/bin/python" ]; then
  echo "Premier lancement : installation (1 à 2 minutes)…"
  python3 -m venv .venv && .venv/bin/python -m pip install --quiet --upgrade pip && .venv/bin/python -m pip install --quiet -r requirements.txt || { echo "Échec de l'installation"; read -r; exit 1; }
fi
exec .venv/bin/python run.py "$@"
