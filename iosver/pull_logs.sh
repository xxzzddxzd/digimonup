#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEVICE="root@127.0.0.1"
PORT="2224"
SSH_OPTIONS="-o StrictHostKeyChecking=no"

REMOTE_DIR=$(ssh -p "$PORT" $SSH_OPTIONS "$DEVICE" \
    'find /var/mobile/Containers/Data/Application -type d -path "*/Library/Caches/PCJBProbe" -print 2>/dev/null | head -n 1')

if [ -z "$REMOTE_DIR" ]; then
    echo "PCJBProbe log directory not found. Open DIGIMONUP once after deploying the tweak." >&2
    exit 1
fi

if [ "$#" -gt 0 ]; then
    OUTPUT_DIR=$1
else
    OUTPUT_DIR="$SCRIPT_DIR/logs/$(date '+%Y%m%d-%H%M%S')"
fi

mkdir -p "$OUTPUT_DIR"
scp -P "$PORT" $SSH_OPTIONS "$DEVICE:$REMOTE_DIR/*.log" "$OUTPUT_DIR/"

echo "Logs copied to $OUTPUT_DIR"
ls -lh "$OUTPUT_DIR"
