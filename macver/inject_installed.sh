#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLAYCOVER_HOME="${PLAYCOVER_HOME:-$HOME/Library/Containers/io.playcover.PlayCover}"
PLAYCOVER_APPS="${PLAYCOVER_APPS:-$PLAYCOVER_HOME/Applications}"
APP_ID="${APP_ID:-jp.co.bandainamcoent.BNEI0442}"
APP_BUNDLE="${APP_BUNDLE:-$PLAYCOVER_APPS/$APP_ID.app}"
APP_EXECUTABLE_NAME="${APP_EXECUTABLE_NAME:-DIGIMONUP}"
APP_EXECUTABLE="$APP_BUNDLE/$APP_EXECUTABLE_NAME"
PLAYCHAIN_DIR="$PLAYCOVER_HOME/PlayChain"
PLAYCHAIN_DB="$PLAYCHAIN_DIR/$APP_ID.db"
PLAYCHAIN_KEYCOVER="$PLAYCHAIN_DIR/$APP_ID.keyCover"
GAME_PREFERENCES="$HOME/Library/Containers/$APP_ID/Data/Library/Preferences/$APP_ID.plist"
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
if [ -f "$PLAYCHAIN_KEYCOVER" ]; then
  cp -p "$PLAYCHAIN_KEYCOVER" "$BACKUP_DIR/"
elif [ -f "$PLAYCHAIN_DB" ]; then
  echo "error: PlayChain is still decrypted; launch/quit through PlayCover and retry" >&2
  exit 1
fi
if [ -f "$GAME_PREFERENCES" ]; then
  cp -p "$GAME_PREFERENCES" "$BACKUP_DIR/game-preferences.plist"
fi
ENTITLEMENTS_BACKUP="$BACKUP_DIR/$APP_EXECUTABLE_NAME.entitlements.plist"
PRESERVE_ENTITLEMENTS=0
if codesign -d --entitlements :- "$APP_EXECUTABLE" \
    >"$ENTITLEMENTS_BACKUP" 2>/dev/null &&
   plutil -lint "$ENTITLEMENTS_BACKUP" >/dev/null 2>&1; then
  PRESERVE_ENTITLEMENTS=1
else
  rm -f "$ENTITLEMENTS_BACKUP"
fi
echo "==> Backup: $BACKUP_DIR"

mkdir -p "$APP_BUNDLE/Frameworks"
cp "$DYLIB_SRC" "$DYLIB_DST"
chmod 755 "$DYLIB_DST"

echo "==> Injecting $DYLIB_LOAD_PATH"
python3 "$INJECTOR" "$APP_EXECUTABLE" "$DYLIB_LOAD_PATH"

echo "==> Ad-hoc signing modified code"
codesign --force --sign - "$DYLIB_DST" >/dev/null
if [ "$PRESERVE_ENTITLEMENTS" = "1" ]; then
  codesign --force --sign - --entitlements "$ENTITLEMENTS_BACKUP" \
    "$APP_EXECUTABLE" >/dev/null
else
  codesign --force --sign - "$APP_EXECUTABLE" >/dev/null
fi
codesign --force --sign - \
  --preserve-metadata=identifier,entitlements,requirements,flags,runtime \
  "$APP_BUNDLE" >/dev/null

if [ "$PRESERVE_ENTITLEMENTS" = "1" ]; then
  ENTITLEMENTS_AFTER="$BACKUP_DIR/$APP_EXECUTABLE_NAME.entitlements.after.plist"
  codesign -d --entitlements :- "$APP_EXECUTABLE" \
    >"$ENTITLEMENTS_AFTER" 2>/dev/null
  if ! cmp -s "$ENTITLEMENTS_BACKUP" "$ENTITLEMENTS_AFTER"; then
    echo "error: executable entitlements changed during signing" >&2
    exit 1
  fi
fi

echo "==> Validating injection"
otool -L "$APP_EXECUTABLE" | grep -F "$DYLIB_LOAD_PATH" >/dev/null
vtool -show-build "$DYLIB_DST" | grep -F 'platform MACCATALYST' >/dev/null
codesign --verify --deep --strict "$APP_BUNDLE"

echo "==> Injected app: $APP_BUNDLE"
echo "==> Plugin: $DYLIB_DST"
