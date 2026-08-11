#!/bin/sh
# Pull PCJBProbe / UnityNative logs from the jailbroken device (ssh port 2224).
#
# Usage:
#   ./pull_logs.sh [OUT_DIR]              # full pull
#   ./pull_logs.sh --latest [OUT_DIR]     # only *-current.log + session-crypto
#   ./pull_logs.sh --grep PATTERN [OUT]   # pull latest current log and grep
#   ./pull_logs.sh --refresh-path         # forget cached PCJBProbe path
#
# SOP: see iosver/SOP_DEVICE_LOGS.md
set -eu

SCRIPT_PATH=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/$(basename -- "$0")
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

DEVICE="${PCJB_DEVICE:-root@127.0.0.1}"
PORT="${PCJB_PORT:-2224}"
SSH_OPTIONS="-o StrictHostKeyChecking=no -o ConnectTimeout=8 -o ServerAliveInterval=5"
CACHE_FILE="$SCRIPT_DIR/.pcjb_log_dir"
MODE="full"
GREP_PATTERN=""
OUTPUT_DIR=""

usage() {
    cat <<'USAGE'
Pull PCJBProbe / UnityNative logs from jailbroken device (ssh :2224).

Usage:
  ./pull_logs.sh [OUT_DIR]              # full pull (current+previous+crash)
  ./pull_logs.sh --latest [OUT_DIR]     # only *-current.log + session-crypto
  ./pull_logs.sh --grep PATTERN [OUT]   # pull current log and grep PATTERN
  ./pull_logs.sh --refresh-path         # forget cached PCJBProbe path
  ./pull_logs.sh --help

Env:
  PCJB_DEVICE  default root@127.0.0.1
  PCJB_PORT    default 2224

SOP: iosver/SOP_DEVICE_LOGS.md
USAGE
    exit 2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        -h|--help) usage ;;
        --latest) MODE="latest"; shift ;;
        --grep)
            MODE="grep"
            GREP_PATTERN=${2:-}
            if [ -z "$GREP_PATTERN" ]; then
                echo "--grep requires a pattern" >&2
                exit 2
            fi
            shift 2
            ;;
        --refresh-path)
            rm -f "$CACHE_FILE"
            echo "cleared cached path $CACHE_FILE"
            exit 0
            ;;
        --)
            shift
            break
            ;;
        -*)
            echo "unknown option: $1" >&2
            usage
            ;;
        *)
            OUTPUT_DIR=$1
            shift
            break
            ;;
    esac
done

if [ -z "$OUTPUT_DIR" ] && [ "$#" -gt 0 ]; then
    OUTPUT_DIR=$1
fi
if [ -z "$OUTPUT_DIR" ]; then
    OUTPUT_DIR="$SCRIPT_DIR/logs/$(date '+%Y%m%d-%H%M%S')"
fi

ssh_cmd() {
    ssh -p "$PORT" $SSH_OPTIONS "$DEVICE" "$@"
}

scp_from() {
    scp -P "$PORT" $SSH_OPTIONS "$DEVICE:$1" "$2"
}

resolve_remote_dir() {
    if [ -f "$CACHE_FILE" ]; then
        CACHED=$(cat "$CACHE_FILE")
        if [ -n "$CACHED" ] && ssh_cmd "test -d '$CACHED'"; then
            echo "$CACHED"
            return 0
        fi
        echo "cached path stale, re-scanning..." >&2
        rm -f "$CACHE_FILE"
    fi

    REMOTE=$(ssh_cmd '
        find /var/mobile/Containers/Data/Application -type d -path "*/Library/Caches/PCJBProbe" 2>/dev/null | head -n 1
    ')
    if [ -z "$REMOTE" ]; then
        echo "PCJBProbe log directory not found. Open DIGIMONUP once after deploying the tweak." >&2
        exit 1
    fi
    printf '%s\n' "$REMOTE" > "$CACHE_FILE"
    echo "cached PCJBProbe dir -> $CACHE_FILE" >&2
    echo "$REMOTE"
}

REMOTE_DIR=$(resolve_remote_dir)
echo "remote: $REMOTE_DIR"
mkdir -p "$OUTPUT_DIR"

case "$MODE" in
    latest|grep)
        for f in PCJBProbe-current.log UnityNative-current.log session-crypto.json; do
            if ssh_cmd "test -f '$REMOTE_DIR/$f'"; then
                scp_from "$REMOTE_DIR/$f" "$OUTPUT_DIR/"
            fi
        done
        ;;
    full)
        for f in \
            PCJBProbe-current.log PCJBProbe-previous.log \
            UnityNative-current.log UnityNative-previous.log \
            UnityCrash-history.log session-crypto.json
        do
            if ssh_cmd "test -f '$REMOTE_DIR/$f'"; then
                scp_from "$REMOTE_DIR/$f" "$OUTPUT_DIR/"
            fi
        done
        REMOTE_IPS=$(ssh_cmd \
            'find /var/mobile/Library/Logs/CrashReporter -maxdepth 1 -type f -name "DIGIMONUP-*.ips" -print 2>/dev/null | sort | tail -n 1' || true)
        if [ -n "${REMOTE_IPS:-}" ]; then
            scp_from "$REMOTE_IPS" "$OUTPUT_DIR/" || true
        fi
        ;;
esac

echo "Logs copied to $OUTPUT_DIR"
ls -lh "$OUTPUT_DIR" 2>/dev/null || true

if [ "$MODE" = "grep" ]; then
    TARGET="$OUTPUT_DIR/PCJBProbe-current.log"
    if [ ! -f "$TARGET" ]; then
        echo "no PCJBProbe-current.log to grep" >&2
        exit 1
    fi
    echo
    echo "=== grep -n -- $GREP_PATTERN ==="
    grep -n -- "$GREP_PATTERN" "$TARGET" | tail -n 80 || true
    echo
    echo "=== last Crypto.REQ with filterGrade (if any) ==="
    grep -n 'filterGrade' "$TARGET" | tail -n 5 || true
fi

echo
echo "Unity crash summary:"
CRASH_HISTORY="$OUTPUT_DIR/UnityCrash-history.log"
if [ -s "$CRASH_HISTORY" ]; then
    grep -E '#pc  UnityCrash\.(Managed|iOSNativeUnhandled|Termination|LastManagedThrow)' \
        "$CRASH_HISTORY" | tail -n 40 || true
else
    echo "No UnityCrash-history.log in this pull (ok for --latest)."
fi
