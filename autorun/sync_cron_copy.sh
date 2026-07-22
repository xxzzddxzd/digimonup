#!/bin/zsh
# Legacy no-op. Cron now runs this directory directly (like dqsg).
# Kept so old docs/muscle memory do not break.
echo "noop: cron uses Documents/.../smbb/autorun in place; no copy needed."
echo "crontab:"
echo "0 * * * * cd /Users/xuzhengda/Documents/workspace/smbb/autorun && /Users/xuzhengda/.pyenv/versions/3.12.8/bin/python3 main.py auto >> logs/auto_cron.log 2>&1"
