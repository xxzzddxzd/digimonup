#!/bin/zsh
# Optional helper: run hourly auto in THIS directory (same layout as dqsg cron).
# Prefer installing a crontab line that cds here; this script is for manual/cron use.
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="/Users/xuzhengda/.pyenv/versions/3.12.8/bin/python3"
LOG="$DIR/logs/auto_cron.log"
ts() { date '+%Y-%m-%d %H:%M:%S'; }

mkdir -p "$DIR/logs"
if [[ ! -x "$PY" ]]; then
  echo "[$(ts)] ERROR: python missing $PY" >>"$LOG"
  exit 1
fi
if [[ ! -f "$DIR/main.py" ]]; then
  echo "[$(ts)] ERROR: missing $DIR/main.py" >>"$LOG"
  exit 1
fi

cd "$DIR" || exit 1
if pgrep -f 'main.py auto' >/dev/null 2>&1; then
  echo "[$(ts)] skip: auto already running" >>"$LOG"
  exit 0
fi

echo "[$(ts)] start auto cwd=$DIR" >>"$LOG"
"$PY" -u "$DIR/main.py" auto >>"$DIR/logs/auto_run.log" 2>&1
rc=$?
echo "[$(ts)] auto exit=$rc" >>"$LOG"
exit $rc
