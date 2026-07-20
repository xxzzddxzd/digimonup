#!/bin/zsh
# Convenience: sync cron copy (if readable) then run the outside-Documents entry.
# Crontab should call: /Users/xuzhengda/cron-jobs/run_smbb_auto.sh
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
OUT="/Users/xuzhengda/cron-jobs/run_smbb_auto.sh"

if [[ -x "$DIR/sync_cron_copy.sh" ]] && /bin/test -r "$DIR/main.py"; then
  "$DIR/sync_cron_copy.sh" || true
fi

if [[ ! -x "$OUT" ]]; then
  echo "ERROR: missing $OUT" >&2
  exit 1
fi
exec "$OUT"
