#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VERSION="${VERSION:-1.0.2}"
IPA="${IPA:-$ROOT_DIR/102/$VERSION.ipa}"
DIST_DIR="$SCRIPT_DIR/dist"
MAC_PACKAGE="$DIST_DIR/PCMacProbe-mac-$VERSION"
PACKAGE_NAME="DIGIMON_UP-$VERSION-PlayCover"
PACKAGE_DIR="$DIST_DIR/$PACKAGE_NAME"
ARCHIVE="$DIST_DIR/$PACKAGE_NAME.zip"

if [ ! -f "$IPA" ]; then
  echo "error: IPA not found: $IPA" >&2
  exit 1
fi

"$SCRIPT_DIR/package_release.sh" >/dev/null

rm -rf "$PACKAGE_DIR" "$ARCHIVE"
mkdir -p "$PACKAGE_DIR/Mac插件"
cp "$IPA" "$PACKAGE_DIR/$VERSION.ipa"
cp "$SCRIPT_DIR/release_assets/README.md" "$PACKAGE_DIR/README.md"
cp "$MAC_PACKAGE/PCMacProbe.dylib" "$PACKAGE_DIR/Mac插件/"
cp "$MAC_PACKAGE/pc_macho_inject" "$PACKAGE_DIR/Mac插件/"
cp "$MAC_PACKAGE/安装插件.command" "$PACKAGE_DIR/Mac插件/"
cp "$MAC_PACKAGE/安装说明.md" "$PACKAGE_DIR/Mac插件/"
cp "$MAC_PACKAGE/THIRD_PARTY_NOTICES.md" "$PACKAGE_DIR/Mac插件/"
chmod 755 "$PACKAGE_DIR/Mac插件/安装插件.command" \
  "$PACKAGE_DIR/Mac插件/pc_macho_inject"

(
  cd "$PACKAGE_DIR"
  shasum -a 256 \
    "$VERSION.ipa" \
    "Mac插件/PCMacProbe.dylib" \
    "Mac插件/pc_macho_inject" \
    "Mac插件/安装插件.command" \
    "Mac插件/安装说明.md" \
    "Mac插件/THIRD_PARTY_NOTICES.md" \
    "README.md" > SHA256SUMS.txt
)

ditto --norsrc -c -k --keepParent "$PACKAGE_DIR" "$ARCHIVE"
echo "$ARCHIVE"
