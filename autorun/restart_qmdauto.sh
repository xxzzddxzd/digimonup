#!/bin/zsh
# Daily restart: kill only main.py qmdauto, then ensure exactly one instance.
set -u
DIR="/Users/xuzhengda/Documents/workspace/smbb/autorun"
LOG_DIR="$DIR/logs"
KILL_LOG="$LOG_DIR/qmdauto_kill.log"
mkdir -p "$LOG_DIR"
cd "$DIR" || exit 1

ts() { date '+%Y-%m-%d %H:%M:%S'; }

echo "[$(ts)] restart_qmdauto begin" >> "$KILL_LOG"

matches=$(pgrep -fl 'main.py qmdauto' 2>/dev/null || true)
if [[ -n "${matches}" ]]; then
  echo "[$(ts)] killing:" >> "$KILL_LOG"
  echo "$matches" >> "$KILL_LOG"
  /bin/pkill -f '[m]ain.py qmdauto' 2>>"$KILL_LOG" || true
  for i in 1 2 3 4 5 6 7 8 9 10; do
    pgrep -f 'main.py qmdauto' >/dev/null 2>&1 || break
    sleep 1
  done
  if pgrep -f 'main.py qmdauto' >/dev/null 2>&1; then
    echo "[$(ts)] still alive, force kill -9" >> "$KILL_LOG"
    /bin/pkill -9 -f '[m]ain.py qmdauto' 2>>"$KILL_LOG" || true
    sleep 1
  fi
else
  echo "[$(ts)] no qmdauto process" >> "$KILL_LOG"
fi

# start via ensure (single instance)
"$DIR/ensure_qmdauto.sh"
rc=$?
echo "[$(ts)] restart done ensure_exit=$rc" >> "$KILL_LOG"
exit $rc
