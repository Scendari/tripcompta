#!/bin/sh
# Trip Compta (Mac, Linux) : remplace l'application par la nouvelle version, la relance, et vérifie qu'elle
# répond. Si elle ne répond pas, l'ancienne version est remise en place et relancée.
# Lancé par l'application elle-même, juste avant qu'elle se ferme. Les données ne sont jamais touchées.
#
#   remplacer.sh PID CIBLE NOUVELLE PROGRAMME PORT VERSION TRAVAIL [options de lancement…]
#     PID        : l'application à attendre
#     CIBLE      : ce qui est remplacé (« …/TripCompta.app » sur Mac, le dossier « …/TripCompta » sur Linux)
#     NOUVELLE   : la nouvelle version, déjà dépliée
#     PROGRAMME  : le programme dans CIBLE (« Contents/MacOS/TripCompta », « TripCompta »)
#     TRAVAIL    : dossier de travail (journal, résultat)
PID=$1; TARGET=$2; NEW=$3; EXE_REL=$4; PORT=$5; VERSION=$6; WORK=$7
shift 7
OLD="$TARGET.ancienne"
LOG="$WORK/maj.log"
WAIT_PING=${TRIPCOMPTA_MAJ_ATTENTE:-90}

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

open_app() {   # $1 = transmettre l'environnement (1/0), $2 = l'app .app, le reste = options de lancement
  withenv=$1; app=$2; shift 2
  set -- "$app" --args --no-browser --port "$PORT" "$@"
  if [ "$withenv" = 1 ]; then
    # « open » ne transmet pas l'environnement : on redonne ce qui choisit le dossier des données et les réglages
    for v in TRIPCOMPTA_CONFIG TRIPCOMPTA_DATA_DIR TRIPCOMPTA_MAJ_API TRIPCOMPTA_NO_UPDATE_CHECK TRIPCOMPTA_JETON; do
      eval "isset=\${$v+x}"
      if [ -n "$isset" ]; then eval "val=\$$v"; set -- --env "$v=$val" "$@"; fi
    done
    set -- --env TRIPCOMPTA_RESTARTED=1 "$@"
  fi
  open -n "$@"
}

launch() {   # $1 = application à lancer ; le reste = options de lancement
  app=$1; shift
  case "$app" in
    *.app)
      if command -v open >/dev/null 2>&1; then
        open_app 1 "$app" "$@" 2>>"$LOG" || open_app 0 "$app" "$@"    # macOS ancien : sans --env
        return
      fi ;;
  esac
  TRIPCOMPTA_RESTARTED=1 nohup "$app/$EXE_REL" --no-browser --port "$PORT" "$@" >/dev/null 2>&1 &
}

running() {   # l'application tourne encore ? (un processus terminé mais pas encore « récupéré » ne compte pas)
  kill -0 "$PID" 2>/dev/null || return 1
  case "$(ps -o stat= -p "$PID" 2>/dev/null)" in Z*|"") return 1;; esac
  return 0
}

ping_version() {   # la version qui répond sur le port (vide si rien)
  if command -v curl >/dev/null 2>&1; then
    out=$(curl -s -m 2 "http://127.0.0.1:$PORT/ping" 2>/dev/null)
  else
    out=$(wget -q -T 2 -O - "http://127.0.0.1:$PORT/ping" 2>/dev/null)
  fi
  echo "$out" | sed -n 's/.*"version" *: *"\([^"]*\)".*/\1/p'
}

stop_on_port() {
  if command -v curl >/dev/null 2>&1; then
    curl -s -m 2 -X POST -H "X-TripCompta-Jeton: ${TRIPCOMPTA_JETON:-}" "http://127.0.0.1:$PORT/quitter" >/dev/null 2>&1
  fi
  sleep 1
  if command -v lsof >/dev/null 2>&1; then
    for p in $(lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null); do kill "$p" 2>/dev/null; done
  fi
}

fail() {
  log "ÉCHEC : $1"
  printf '%s\n' "$1" > "$WORK/echec.txt"
}

log "Mise à jour vers $VERSION : attente de la fermeture de l'application ($PID)"
i=0
while running; do
  i=$((i + 1))
  if [ $i -gt 200 ]; then log "L'application ne se ferme pas : arrêt forcé"; kill -9 "$PID" 2>/dev/null; sleep 1; break; fi
  sleep 0.3
done

rm -rf "$OLD"
if ! mv "$TARGET" "$OLD"; then
  fail "Impossible de mettre l'ancienne version de côté. Rien n'a été changé."
  launch "$TARGET" "$@"
  exit 1
fi
if ! mv "$NEW" "$TARGET"; then
  rm -rf "$TARGET"
  mv "$OLD" "$TARGET"
  fail "Impossible d'installer la nouvelle version. L'ancienne a été remise."
  launch "$TARGET" "$@"
  exit 1
fi
chmod +x "$TARGET/$EXE_REL" 2>/dev/null
command -v xattr >/dev/null 2>&1 && xattr -dr com.apple.quarantine "$TARGET" 2>/dev/null
log "Nouvelle version en place : lancement"
launch "$TARGET" "$@"

i=0
while [ $i -lt "$WAIT_PING" ]; do
  if [ "$(ping_version)" = "$VERSION" ]; then
    rm -rf "$OLD"
    printf '%s\n' "$VERSION" > "$WORK/reussi.txt"
    log "Terminé : la version $VERSION répond"
    rm -rf "$WORK/nouvelle"
    exit 0
  fi
  i=$((i + 1))
  sleep 1
done

log "La nouvelle version ne répond pas : retour à l'ancienne"
stop_on_port
rm -rf "$TARGET"
mv "$OLD" "$TARGET"
fail "La version $VERSION n'a pas démarré correctement. L'ancienne version a été remise en place."
launch "$TARGET" "$@"
exit 1
