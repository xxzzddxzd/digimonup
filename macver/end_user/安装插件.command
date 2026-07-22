#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_ID="${APP_ID:-jp.co.bandainamcoent.BNEI0442}"
PLAYCOVER_HOME="${PLAYCOVER_HOME:-$HOME/Library/Containers/io.playcover.PlayCover}"
APP_BUNDLE="${APP_BUNDLE:-$PLAYCOVER_HOME/Applications/$APP_ID.app}"
DYLIB_SOURCE="$SCRIPT_DIR/PCMacProbe.dylib"
INJECTOR="$SCRIPT_DIR/pc_macho_inject"
LOAD_PATH="@executable_path/Frameworks/PCMacProbe.dylib"
PLAYCHAIN_DIR="$PLAYCOVER_HOME/PlayChain"
PLAYCHAIN_DB="$PLAYCHAIN_DIR/$APP_ID.db"
PLAYCHAIN_KEYCOVER="$PLAYCHAIN_DIR/$APP_ID.keyCover"
GAME_PREFERENCES="$HOME/Library/Containers/$APP_ID/Data/Library/Preferences/$APP_ID.plist"

pause_at_exit() {
  status=$?
  trap - EXIT
  echo
  if [ "$status" -eq 0 ]; then
    echo "安装完成，可以回到 PlayCover 启动游戏。"
  else
    echo "安装失败，请保留上面的错误信息。"
  fi
  if [ -t 0 ]; then
    read -r -p "按回车键关闭此窗口……" _
  fi
  exit "$status"
}
trap pause_at_exit EXIT

echo "PCMacProbe macOS / PlayCover 安装程序"
echo

if [ ! -d "/Applications/PlayCover.app" ]; then
  echo "error: 没有在 /Applications 中找到 PlayCover。" >&2
  exit 1
fi
if [ ! -d "$APP_BUNDLE" ]; then
  echo "error: PlayCover 中还没有安装 DIGIMON_UP。" >&2
  echo "请先使用 PlayCover 安装 1.0.2.ipa，确认应用出现在资料库中，再运行本脚本。" >&2
  exit 1
fi
if [ ! -f "$DYLIB_SOURCE" ]; then
  echo "error: 安装包中缺少 PCMacProbe.dylib。" >&2
  exit 1
fi
if [ ! -x "$INJECTOR" ]; then
  echo "error: 安装包中缺少可执行的 pc_macho_inject。" >&2
  echo "如果文件存在，请执行：chmod +x \"$INJECTOR\"" >&2
  exit 1
fi

INFO_PLIST="$APP_BUNDLE/Info.plist"
EXECUTABLE_NAME="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$INFO_PLIST")"
GAME_VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$INFO_PLIST")"
GAME_BUILD="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$INFO_PLIST")"
if [ "$GAME_VERSION" != "1.0.2" ] || [ "$GAME_BUILD" != "38" ]; then
  echo "error: 当前游戏版本是 $GAME_VERSION ($GAME_BUILD)，插件只支持 1.0.2 (38)。" >&2
  echo "为避免因 Unity 函数偏移不同而崩溃，安装已停止。" >&2
  exit 1
fi
EXECUTABLE="$APP_BUNDLE/$EXECUTABLE_NAME"
DYLIB_TARGET="$APP_BUNDLE/Frameworks/PCMacProbe.dylib"
if [ ! -f "$EXECUTABLE" ]; then
  echo "error: 找不到游戏主程序：$EXECUTABLE" >&2
  exit 1
fi

if pgrep -x "$EXECUTABLE_NAME" >/dev/null 2>&1; then
  echo "正在退出运行中的 $EXECUTABLE_NAME……"
  pkill -TERM -x "$EXECUTABLE_NAME" || true
  for _ in {1..30}; do
    pgrep -x "$EXECUTABLE_NAME" >/dev/null 2>&1 || break
    sleep 0.2
  done
  if pgrep -x "$EXECUTABLE_NAME" >/dev/null 2>&1; then
    echo "error: 游戏仍在运行，请手动退出后重试。" >&2
    exit 1
  fi
fi

for _ in {1..40}; do
  if [ -f "$PLAYCHAIN_KEYCOVER" ] && [ ! -f "$PLAYCHAIN_DB" ]; then
    break
  fi
  sleep 0.25
done
if [ -f "$PLAYCHAIN_DB" ]; then
  echo "error: PlayChain 账号库仍未完成回写。" >&2
  echo "请先从 PlayCover 启动一次游戏，再正常退出后重新安装插件。" >&2
  exit 1
fi

/usr/bin/xattr -d com.apple.quarantine "$DYLIB_SOURCE" 2>/dev/null || true
/usr/bin/xattr -d com.apple.quarantine "$INJECTOR" 2>/dev/null || true

BACKUP_BASE="$APP_BUNDLE.PCMacProbeBackup.$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="$BACKUP_BASE"
BACKUP_NUMBER=1
while [ -e "$BACKUP_DIR" ]; do
  BACKUP_DIR="$BACKUP_BASE.$BACKUP_NUMBER"
  BACKUP_NUMBER=$((BACKUP_NUMBER + 1))
done
/bin/mkdir -p "$BACKUP_DIR"
/bin/cp -p "$EXECUTABLE" "$BACKUP_DIR/"
if [ -f "$DYLIB_TARGET" ]; then
  /bin/cp -p "$DYLIB_TARGET" "$BACKUP_DIR/"
fi
if [ -f "$PLAYCHAIN_KEYCOVER" ]; then
  /bin/cp -p "$PLAYCHAIN_KEYCOVER" "$BACKUP_DIR/"
fi
if [ -f "$GAME_PREFERENCES" ]; then
  /bin/cp -p "$GAME_PREFERENCES" "$BACKUP_DIR/game-preferences.plist"
fi
ENTITLEMENTS_BACKUP="$BACKUP_DIR/$EXECUTABLE_NAME.entitlements.plist"
PRESERVE_ENTITLEMENTS=0
if /usr/bin/codesign -d --entitlements :- "$EXECUTABLE" \
    >"$ENTITLEMENTS_BACKUP" 2>/dev/null &&
   /usr/bin/plutil -lint "$ENTITLEMENTS_BACKUP" >/dev/null 2>&1; then
  PRESERVE_ENTITLEMENTS=1
else
  /bin/rm -f "$ENTITLEMENTS_BACKUP"
fi
echo "已创建备份：$BACKUP_DIR"

/bin/mkdir -p "$APP_BUNDLE/Frameworks"
/bin/cp "$DYLIB_SOURCE" "$DYLIB_TARGET"
/bin/chmod 755 "$DYLIB_TARGET"

echo "正在向游戏主程序加入 dylib……"
"$INJECTOR" "$EXECUTABLE" "$LOAD_PATH"

echo "正在进行本机临时签名……"
/usr/bin/codesign --force --sign - "$DYLIB_TARGET" >/dev/null
if [ "$PRESERVE_ENTITLEMENTS" = "1" ]; then
  /usr/bin/codesign --force --sign - \
    --entitlements "$ENTITLEMENTS_BACKUP" "$EXECUTABLE" >/dev/null
else
  /usr/bin/codesign --force --sign - "$EXECUTABLE" >/dev/null
fi
/usr/bin/codesign --force --sign - \
  --preserve-metadata=identifier,entitlements,requirements,flags,runtime \
  "$APP_BUNDLE" >/dev/null

if [ "$PRESERVE_ENTITLEMENTS" = "1" ]; then
  ENTITLEMENTS_AFTER="$BACKUP_DIR/$EXECUTABLE_NAME.entitlements.after.plist"
  /usr/bin/codesign -d --entitlements :- "$EXECUTABLE" \
    >"$ENTITLEMENTS_AFTER" 2>/dev/null
  if ! /usr/bin/cmp -s "$ENTITLEMENTS_BACKUP" "$ENTITLEMENTS_AFTER"; then
    echo "error: 重新签名后 entitlement 发生变化，安装已停止。" >&2
    exit 1
  fi
fi

echo "正在验证……"
"$INJECTOR" --check "$EXECUTABLE" "$LOAD_PATH"
/usr/bin/codesign --verify --deep --strict "$APP_BUNDLE"
echo "插件位置：$DYLIB_TARGET"
