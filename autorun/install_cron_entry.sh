#!/bin/zsh
# Cron entrypoint OUTSIDE ~/Documents (macOS TCC).
# Runs one-shot: python main.py auto
# Code/account live in ~/cron-jobs/smbb-autorun (sync via Terminal: autorun/sync_cron_copy.sh)
set -u
SRC="/Users/xuzhengda/Documents/workspace/smbb/autorun"
DST="/Users/xuzhengda/cron-jobs/smbb-autorun"
LOG="/Users/xuzhengda/cron-jobs/smbb-auto-cron.log"
PY="/Users/xuzhengda/.pyenv/versions/3.12.8/bin/python3"
ts() { date '+%Y-%m-%d %H:%M:%S'; }

mkdir -p "$DST/logs" "$(dirname "$LOG")"

# Best-effort sync only if this process can read Documents (Terminal / FDA).
# Under cron this almost always fails; last synced copy is used.
if /bin/test -r "$SRC/main.py"; then
  /usr/bin/rsync -a --delete \
    --exclude 'logs/' \
    --exclude '__pycache__/' \
    --exclude '.DS_Store' \
    --exclude '*.pid' \
    "$SRC/" "$DST/" >>"$LOG" 2>&1 || true
else
  echo "[$(ts)] WARN: SRC unreadable (TCC). using last copy $DST" >>"$LOG"
fi

if [[ ! -f "$DST/main.py" ]]; then
  echo "[$(ts)] ERROR: missing $DST/main.py — run autorun/sync_cron_copy.sh from Terminal once" >>"$LOG"
  exit 1
fi
if [[ ! -x "$PY" ]]; then
  echo "[$(ts)] ERROR: python missing $PY" >>"$LOG"
  exit 1
fi

cd "$DST" || exit 1
if pgrep -f 'main.py auto' >/dev/null 2>&1; then
  echo "[$(ts)] skip: auto already running" >>"$LOG"
  exit 0
fi

echo "[$(ts)] start auto cwd=$DST" >>"$LOG"
"$PY" -u "$DST/main.py" auto >>"$DST/logs/auto_run.log" 2>&1
rc=$?
echo "[$(ts)] auto exit=$rc" >>"$LOG"
exit $rc
