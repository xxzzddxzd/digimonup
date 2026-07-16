#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLAYCOVER_HOME="${PLAYCOVER_HOME:-$HOME/Library/Containers/io.playcover.PlayCover}"
PLAYCOVER_APPS="${PLAYCOVER_APPS:-$PLAYCOVER_HOME/Applications}"
APP_ID="${APP_ID:-jp.co.bandainamcoent.BNEI0442}"
APP_BUNDLE="${APP_BUNDLE:-$PLAYCOVER_APPS/$APP_ID.app}"
APP_EXECUTABLE_NAME="${APP_EXECUTABLE_NAME:-DIGIMONUP}"
APP_EXECUTABLE="$APP_BUNDLE/$APP_EXECUTABLE_NAME"
DYLIB_SRC="$SCRIPT_DIR/build/mac/PCMacProbe.dylib"
DYLIB_DST="$APP_BUNDLE/Frameworks/PCMacProbe.dylib"
DYLIB_LOAD_PATH="@executable_path/Frameworks/PCMacProbe.dylib"
INJECTOR="$SCRIPT_DIR/tools/inject_load_dylib.py"
BUILD_DYLIB="${BUILD_DYLIB:-1}"

require_file() {
  if [ ! -f "$1" ]; then
    echo "error: $2: $1" >&2
    exit 1
  fi
}

if [ "$BUILD_DYLIB" = "1" ]; then
  echo "==> Building PCMacProbe"
  make -C "$SCRIPT_DIR" embedded-mac-dylib
fi

require_file "$APP_EXECUTABLE" "PlayCover app is not installed"
require_file "$DYLIB_SRC" "plugin dylib is missing"
require_file "$INJECTOR" "Mach-O load-command injector is missing"

if pgrep -x "$APP_EXECUTABLE_NAME" >/dev/null 2>&1; then
  echo "error: $APP_EXECUTABLE_NAME is running; quit it before injection" >&2
  exit 1
fi

BACKUP_DIR="$APP_BUNDLE.PCMacProbeBackup.$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"
cp -p "$APP_EXECUTABLE" "$BACKUP_DIR/"
if [ -f "$DYLIB_DST" ]; then
  cp -p "$DYLIB_DST" "$BACKUP_DIR/"
fi
echo "==> Backup: $BACKUP_DIR"

mkdir -p "$APP_BUNDLE/Frameworks"
cp "$DYLIB_SRC" "$DYLIB_DST"
chmod 755 "$DYLIB_DST"

echo "==> Injecting $DYLIB_LOAD_PATH"
python3 "$INJECTOR" "$APP_EXECUTABLE" "$DYLIB_LOAD_PATH"

echo "==> Ad-hoc signing modified code"
codesign --force --sign - "$DYLIB_DST" >/dev/null
codesign --force --sign - "$APP_EXECUTABLE" >/dev/null
codesign --force --sign - "$APP_BUNDLE" >/dev/null

echo "==> Validating injection"
otool -L "$APP_EXECUTABLE" | grep -F "$DYLIB_LOAD_PATH" >/dev/null
vtool -show-build "$DYLIB_DST" | grep -F 'platform MACCATALYST' >/dev/null
codesign --verify --deep --strict "$APP_BUNDLE"

echo "==> Injected app: $APP_BUNDLE"
echo "==> Plugin: $DYLIB_DST"
