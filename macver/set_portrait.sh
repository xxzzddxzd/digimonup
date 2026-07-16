#!/bin/bash
set -euo pipefail

APP_ID="${APP_ID:-jp.co.bandainamcoent.BNEI0442}"
APP_EXECUTABLE_NAME="${APP_EXECUTABLE_NAME:-DIGIMONUP}"
PLAYCOVER_HOME="${PLAYCOVER_HOME:-$HOME/Library/Containers/io.playcover.PlayCover}"
SETTINGS="$PLAYCOVER_HOME/App Settings/$APP_ID.plist"
APP_BUNDLE="$PLAYCOVER_HOME/Applications/$APP_ID.app"
WIDTH="${WIDTH:-900}"
HEIGHT="${HEIGHT:-1600}"

if [ ! -f "$SETTINGS" ]; then
  echo "error: PlayCover settings are missing: $SETTINGS" >&2
  exit 1
fi
if [ ! -d "$APP_BUNDLE" ]; then
  echo "error: PlayCover app is missing: $APP_BUNDLE" >&2
  exit 1
fi

if PID="$(pgrep -x "$APP_EXECUTABLE_NAME" | head -n 1)" && [ -n "$PID" ]; then
  echo "==> Stopping $APP_EXECUTABLE_NAME (pid $PID)"
  kill -TERM "$PID"
  for _ in {1..20}; do
    pgrep -x "$APP_EXECUTABLE_NAME" >/dev/null 2>&1 || break
    sleep 0.25
  done
fi

BACKUP="$SETTINGS.portrait-backup.$(date +%Y%m%d_%H%M%S)"
cp -p "$SETTINGS" "$BACKUP"

set_or_add_integer() {
  local key="$1"
  local value="$2"
  if ! /usr/libexec/PlistBuddy -c "Set :$key $value" "$SETTINGS" >/dev/null 2>&1; then
    /usr/libexec/PlistBuddy -c "Add :$key integer $value" "$SETTINGS"
  fi
}

set_or_add_string() {
  local key="$1"
  local value="$2"
  if ! /usr/libexec/PlistBuddy -c "Set :$key $value" "$SETTINGS" >/dev/null 2>&1; then
    /usr/libexec/PlistBuddy -c "Add :$key string $value" "$SETTINGS"
  fi
}

# PlayCover: resolution 5 = custom; displayRotation 1 = portrait.
set_or_add_integer resolution 5
set_or_add_integer windowWidth "$WIDTH"
set_or_add_integer windowHeight "$HEIGHT"
set_or_add_integer displayRotation 1
set_or_add_string iosDeviceModel iPhone14,3

plutil -lint "$SETTINGS" >/dev/null

echo "==> Portrait settings: ${WIDTH}x${HEIGHT}, rotation=portrait"
echo "==> Settings backup: $BACKUP"
echo "==> Relaunching: $APP_BUNDLE"
open "$APP_BUNDLE"
