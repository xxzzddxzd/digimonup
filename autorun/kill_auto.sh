#!/bin/zsh
# Kill only auto / legacy qmdauto workers. Never broad-kill python.
set -u
DIR="/Users/xuzhengda/Documents/workspace/smbb/autorun"
LOG="$DIR/logs/auto_kill.log"
mkdir -p "$DIR/logs"
ts=$(date '+%Y-%m-%d %H:%M:%S')
matches=$(pgrep -fl 'main.py auto|main.py qmdauto' 2>/dev/null || true)
if [[ -z "${matches}" ]]; then
  echo "[$ts] no auto process" >> "$LOG"
  exit 0
fi
echo "[$ts] killing:" >> "$LOG"
echo "$matches" >> "$LOG"
pkill -f '[m]ain.py auto' 2>>"$LOG" || true
pkill -f '[m]ain.py qmdauto' 2>>"$LOG" || true
exit 0
