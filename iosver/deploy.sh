#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

DEVICE="root@127.0.0.1"
PORT="2224"
REMOTE_DIR="/var/jb/usr/lib/TweakInject"
PROCESS_NAME="DIGIMONUP"
SSH_OPTIONS="-o StrictHostKeyChecking=no"

echo "[1/4] Building PCJBProbe.dylib"
make clean all

echo "[2/4] Uploading tweak"
scp -P "$PORT" $SSH_OPTIONS \
    PCJBProbe.dylib PCJBProbe.plist \
    "$DEVICE:$REMOTE_DIR/"

echo "[3/4] Setting permissions"
ssh -p "$PORT" $SSH_OPTIONS "$DEVICE" \
    "chmod 755 '$REMOTE_DIR/PCJBProbe.dylib' && \
     chmod 644 '$REMOTE_DIR/PCJBProbe.plist'"

echo "[4/4] Stopping old $PROCESS_NAME process"
ssh -p "$PORT" $SSH_OPTIONS "$DEVICE" \
    "/var/jb/usr/bin/killall -9 '$PROCESS_NAME' 2>/dev/null || true"

echo "Deploy complete. Reopen $PROCESS_NAME."
