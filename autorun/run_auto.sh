#!/bin/zsh
# Hourly one-shot: python main.py auto (no long-running loop).
# Skip if another auto is still running.
set -u
DIR="/Users/xuzhengda/Documents/workspace/smbb/autorun"
PY="/Users/xuzhengda/.pyenv/versions/3.12.8/bin/python3"
MAIN="$DIR/main.py"
LOG_DIR="$DIR/logs"
RUN_LOG="$LOG_DIR/auto_run.log"
CRON_LOG="$LOG_DIR/auto_cron.log"
mkdir -p "$LOG_DIR"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

cd "$DIR" || {
  echo "[$(ts)] ERROR cd failed: $DIR" >> "$CRON_LOG"
  exit 1
}

if pgrep -f 'main.py auto' >/dev/null 2>&1; then
  echo "[$(ts)] skip: auto already running" >> "$CRON_LOG"
  exit 0
fi

# also skip legacy qmdauto if any leftover
if pgrep -f 'main.py qmdauto' >/dev/null 2>&1; then
  echo "[$(ts)] skip: legacy qmdauto still running" >> "$CRON_LOG"
  exit 0
fi

if [[ ! -x "$PY" ]]; then
  echo "[$(ts)] ERROR python missing: $PY" >> "$CRON_LOG"
  exit 1
fi

echo "[$(ts)] start auto" >> "$CRON_LOG"
echo "[$(ts)] ===== auto start =====" >> "$RUN_LOG"
"$PY" -u "$MAIN" auto >> "$RUN_LOG" 2>&1
rc=$?
echo "[$(ts)] auto exit=$rc" >> "$CRON_LOG"
exit $rc
