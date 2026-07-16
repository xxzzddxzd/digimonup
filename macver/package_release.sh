#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VERSION="${VERSION:-1.0.2}"
PACKAGE_NAME="PCMacProbe-mac-$VERSION"
DIST_DIR="$SCRIPT_DIR/dist"
PACKAGE_DIR="$DIST_DIR/$PACKAGE_NAME"
DYLIB="$SCRIPT_DIR/build/mac/PCMacProbe.dylib"

if [ ! -f "$DYLIB" ]; then
  echo "error: build the dylib first: make -C macver embedded-mac-dylib" >&2
  exit 1
fi

rm -rf "$PACKAGE_DIR" "$DIST_DIR/$PACKAGE_NAME.zip"
mkdir -p "$PACKAGE_DIR"

xcrun clang -arch arm64 -mmacosx-version-min=12.0 -Os \
  "$SCRIPT_DIR/tools/pc_macho_inject.c" \
  -o "$PACKAGE_DIR/pc_macho_inject"
codesign --force --sign - "$PACKAGE_DIR/pc_macho_inject" >/dev/null

cp "$DYLIB" "$PACKAGE_DIR/PCMacProbe.dylib"
cp "$SCRIPT_DIR/end_user/安装插件.command" "$PACKAGE_DIR/安装插件.command"
cp "$SCRIPT_DIR/end_user/安装说明.md" "$PACKAGE_DIR/安装说明.md"
cp "$SCRIPT_DIR/THIRD_PARTY_NOTICES.md" "$PACKAGE_DIR/THIRD_PARTY_NOTICES.md"
chmod 755 "$PACKAGE_DIR/安装插件.command" "$PACKAGE_DIR/pc_macho_inject"

ditto --norsrc -c -k --keepParent "$PACKAGE_DIR" "$DIST_DIR/$PACKAGE_NAME.zip"
echo "$DIST_DIR/$PACKAGE_NAME.zip"
