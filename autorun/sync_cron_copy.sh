#!/bin/zsh
# Sync workspace autorun -> ~/cron-jobs/smbb-autorun for macOS cron.
# Cron cannot read ~/Documents (TCC EPERM). Always run this from Terminal
# after code changes so the next hourly job picks up the new tree.
set -euo pipefail
SRC="/Users/xuzhengda/Documents/workspace/smbb/autorun"
DST="/Users/xuzhengda/cron-jobs/smbb-autorun"

if [[ ! -r "$SRC/main.py" ]]; then
  echo "ERROR: cannot read $SRC/main.py (need Terminal Full Disk Access?)" >&2
  exit 1
fi

mkdir -p "$DST"
/usr/bin/rsync -a --delete \
  --exclude 'logs/' \
  --exclude '__pycache__/' \
  --exclude '.DS_Store' \
  --exclude '*.pid' \
  "$SRC/" "$DST/"
echo "synced: $SRC -> $DST"
echo "cron entry: 0 * * * * /Users/xuzhengda/cron-jobs/run_smbb_auto.sh"
