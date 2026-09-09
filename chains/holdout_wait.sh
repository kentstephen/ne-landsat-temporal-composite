#!/bin/zsh
# Run the hold-out error test over data/holdout_windows.txt once the source
# pass waiter (data/source_wait.pid) has exited, so the two do not compete
# for bandwidth. Independent of the store: it only reads scenes.
#   nohup caffeinate -i data/logs/holdout_wait.sh >> data/logs/holdout.log 2>&1 &
set -uo pipefail
cd "$(dirname "$0")/.."
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
sw=$(cat data/source_wait.pid)
log "holdout: waiting for source pass waiter pid $sw to exit"
while kill -0 $sw 2>/dev/null; do sleep 120; done
log "holdout: starting $(grep -vc '^#' data/holdout_windows.txt) windows"
uv run python src/landsat_mosaic/holdout_test.py --list data/holdout_windows.txt 2>&1 | grep -v "Warning\|_reproject"
log "holdout done: stats/holdout/summary.json, panels in data/trials/holdout/"
