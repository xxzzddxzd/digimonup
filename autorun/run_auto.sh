#!/bin/zsh
# Run one-shot auto in this directory (Documents/.../smbb/autorun).
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$DIR/install_cron_entry.sh"
