#!/bin/zsh
# Kill only qmdauto workers. Never broad-kill python.
set -u
DIR="/Users/xuzhengda/Documents/workspace/smbb/autorun"
LOG="$DIR/logs/qmdauto_kill.log"
mkdir -p "$DIR/logs"
ts=$(date '+%Y-%m-%d %H:%M:%S')
# list matches first
matches=$(pgrep -fl 'main.py qmdauto' 2>/dev/null || true)
if [[ -z "${matches}" ]]; then
  echo "[$ts] no qmdauto process" >> "$LOG"
  exit 0
fi
echo "[$ts] killing:" >> "$LOG"
echo "$matches" >> "$LOG"
# -f match full cmdline; [m]ain prevents self-match
/bin/pkill -f '[m]ain.py qmdauto' 2>>"$LOG"
rc=$?
# pkill returns 1 when no process — treat as ok
if [[ $rc -eq 1 ]]; then rc=0; fi
echo "[$ts] pkill exit=$rc" >> "$LOG"
exit $rc
