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

REMOTE_IPS=$(ssh -p "$PORT" $SSH_OPTIONS "$DEVICE" \
    'find /var/mobile/Library/Logs/CrashReporter -maxdepth 1 -type f -name "DIGIMONUP-*.ips" -print 2>/dev/null | sort | tail -n 1')

if [ -n "$REMOTE_IPS" ]; then
    scp -P "$PORT" $SSH_OPTIONS "$DEVICE:$REMOTE_IPS" "$OUTPUT_DIR/"
fi

echo "Logs copied to $OUTPUT_DIR"
ls -lh "$OUTPUT_DIR"

echo
echo "Unity crash summary:"
CRASH_HISTORY="$OUTPUT_DIR/UnityCrash-history.log"
if [ -s "$CRASH_HISTORY" ]; then
    grep -E '#pc  UnityCrash\.(Managed|iOSNativeUnhandled|Termination|LastManagedThrow)' \
        "$CRASH_HISTORY" | tail -n 80 || true
else
    echo "No Unity/IL2CPP crash has been recorded since crash capture was enabled."
fi

if [ -n "$REMOTE_IPS" ]; then
    LOCAL_IPS="$OUTPUT_DIR/$(basename "$REMOTE_IPS")"
    echo
    echo "Latest iOS crash report:"
    head -n 1 "$LOCAL_IPS"
fi
