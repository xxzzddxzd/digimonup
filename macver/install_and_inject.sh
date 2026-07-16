#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
IPA="${IPA:-$ROOT_DIR/102/1.ipa}"
PLAYCOVER_APP="${PLAYCOVER_APP:-/Applications/PlayCover.app}"
PLAYCOVER_APPS="${PLAYCOVER_APPS:-$HOME/Library/Containers/io.playcover.PlayCover/Applications}"
APP_ID="${APP_ID:-jp.co.bandainamcoent.BNEI0442}"
APP_BUNDLE="$PLAYCOVER_APPS/$APP_ID.app"
WAIT_SECONDS="${WAIT_SECONDS:-180}"

if [ ! -f "$IPA" ]; then
  echo "error: IPA is missing: $IPA" >&2
  exit 1
fi
if [ ! -d "$PLAYCOVER_APP" ]; then
  echo "error: PlayCover is missing: $PLAYCOVER_APP" >&2
  exit 1
fi

if [ ! -d "$APP_BUNDLE" ]; then
  echo "==> Asking PlayCover to install: $IPA"
  open -a "$PLAYCOVER_APP" "$IPA"
  elapsed=0
  while [ ! -f "$APP_BUNDLE/DIGIMONUP" ] && [ "$elapsed" -lt "$WAIT_SECONDS" ]; do
    sleep 2
    elapsed=$((elapsed + 2))
  done
fi

if [ ! -f "$APP_BUNDLE/DIGIMONUP" ]; then
  echo "error: PlayCover did not finish installation within ${WAIT_SECONDS}s" >&2
  exit 1
fi

PLAYCOVER_APPS="$PLAYCOVER_APPS" BUILD_DYLIB=1 \
  "$SCRIPT_DIR/inject_installed.sh"
