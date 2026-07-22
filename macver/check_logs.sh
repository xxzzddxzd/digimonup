#!/bin/bash
set -euo pipefail

APP_ID="${APP_ID:-jp.co.bandainamcoent.BNEI0442}"
FOUND=0

while IFS= read -r log; do
  FOUND=1
  echo "==> $log"
  tail -n "${LINES:-80}" "$log"
done < <(find \
  "$HOME/Library/Containers/$APP_ID" \
  "$HOME/Library/Containers/io.playcover.PlayCover" \
  "$HOME/Library/Caches/PCMacProbe" \
  /private/tmp \
  -type f -path '*/PCMacProbe/*' \
  \( -name '*-current.log' -o -name 'UnityCrash-history.log' \) \
  -print 2>/dev/null | sort -u)

if [ "$FOUND" = "0" ]; then
  echo "No PCMacProbe log found. Launch DIGIMON_UP once, then retry." >&2
  exit 1
fi
