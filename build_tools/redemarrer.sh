#!/bin/zsh
# Arrête Trip Compta s'il tourne, puis le relance depuis le code source (mêmes données, même adresse).
# Depuis le code source : pas de nouvelle demande d'accès au dossier Documents à chaque reconstruction de l'app.
# L'onglet déjà ouvert se recharge tout seul ; un onglet n'est ouvert que si l'appli ne tournait pas.
# Option --app : relancer l'application Mac reconstruite (dist/TripCompta.app) à la place.
cd "$(dirname "$0")/.."
was_running=0
for port in {8765..8814}; do
  if curl -s -m 1 "http://127.0.0.1:$port/ping" 2>/dev/null | grep -qE '"(tripcompta|compta-voyages)"'; then
    curl -s -m 2 -X POST "http://127.0.0.1:$port/quitter" >/dev/null 2>&1
    was_running=1
    echo "Arrêté (port $port)"
  fi
done
pkill -f "TripCompta.app/Contents/MacOS/TripCompta" 2>/dev/null
pkill -f "Trip Compta.app/Contents/MacOS/Trip Compta" 2>/dev/null   # avant la 1.2
pkill -f "Compta Voyages.app/Contents/MacOS/Compta Voyages" 2>/dev/null   # ancien nom
# attendre que le port 8765 soit vraiment libéré (sinon l'appli démarrerait sur 8766 et l'onglet ouvert serait perdu)
for i in {1..40}; do
  lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1 || break
  sleep 0.25
done
args=()
[[ $was_running == 1 ]] && args+=(--no-browser)
if [[ "$1" == "--app" ]]; then
  open -n "dist/TripCompta.app" --args "${args[@]}"
else
  mkdir -p "$HOME/Library/Logs"
  nohup .venv/bin/python run.py "${args[@]}" >> "$HOME/Library/Logs/tripcompta.log" 2>&1 &
  disown
fi
for i in {1..60}; do
  if curl -s -m 1 "http://127.0.0.1:8765/ping" 2>/dev/null | grep -qE '"(tripcompta|compta-voyages)"'; then
    echo "Relancé : http://127.0.0.1:8765/"
    exit 0
  fi
  sleep 0.5
done
echo "L'application ne répond pas encore (voir ~/Library/Logs/tripcompta.log)." >&2
exit 1
