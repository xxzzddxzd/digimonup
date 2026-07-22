#!/bin/zsh
# Compatibility: old keepalive name → one-shot auto in this directory.
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$DIR/install_cron_entry.sh"
