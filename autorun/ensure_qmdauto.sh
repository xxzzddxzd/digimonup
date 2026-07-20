#!/bin/zsh
# Keepalive for qmdauto: start only when not already running (no duplicate).
set -u
DIR="/Users/xuzhengda/Documents/workspace/smbb/autorun"
PY="/Users/xuzhengda/.pyenv/versions/3.12.8/bin/python3"
MAIN="$DIR/main.py"
LOG_DIR="$DIR/logs"
ENSURE_LOG="$LOG_DIR/qmdauto_ensure.log"
RUN_LOG="$LOG_DIR/qmdauto_run.log"
PID_FILE="$LOG_DIR/qmdauto.pid"
mkdir -p "$LOG_DIR"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

running_pids() {
  pgrep -f 'main.py qmdauto' 2>/dev/null || true
}

pids=$(running_pids)
if [[ -n "${pids}" ]]; then
  echo "${pids}" | awk 'NR==1{print; exit}' > "$PID_FILE"
  echo "[$(ts)] already running pid(s)=$(echo $pids | tr '\n' ' ')" >> "$ENSURE_LOG"
  exit 0
fi

if [[ ! -x "$PY" ]]; then
  echo "[$(ts)] ERROR python missing: $PY" >> "$ENSURE_LOG"
  exit 1
fi
if [[ ! -f "$MAIN" ]]; then
  echo "[$(ts)] ERROR main missing: $MAIN" >> "$ENSURE_LOG"
  exit 1
fi

echo "[$(ts)] not running; starting qmdauto" >> "$ENSURE_LOG"
# absolute paths; cd for relative account.json / logs/qmdauto.log
cd "$DIR" || {
  echo "[$(ts)] ERROR cd failed: $DIR" >> "$ENSURE_LOG"
  exit 1
}
nohup "$PY" -u "$MAIN" qmdauto >> "$RUN_LOG" 2>&1 &
newpid=$!
disown "$newpid" 2>/dev/null || true
echo "$newpid" > "$PID_FILE"
echo "[$(ts)] started pid=$newpid cwd=$DIR main=$MAIN" >> "$ENSURE_LOG"
echo "[$(ts)] ===== ensure start pid=$newpid =====" >> "$RUN_LOG"
sleep 2
if kill -0 "$newpid" 2>/dev/null; then
  exit 0
fi
echo "[$(ts)] start FAILED pid=$newpid (see $RUN_LOG)" >> "$ENSURE_LOG"
tail -5 "$RUN_LOG" >> "$ENSURE_LOG" 2>/dev/null || true
exit 1
