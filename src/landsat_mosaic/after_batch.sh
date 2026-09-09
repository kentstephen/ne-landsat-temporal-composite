#!/bin/zsh
# Wait for the running batch.py to exit, then refresh data/pyramid_v1 in
# place from the Icechunk tag (check, build TAG YEARS, score) and restart
# the headless viewer on :2718. Fully unattended. If the batch ended short
# (failed.txt, fewer than 693 done) nothing is built; the log says why.
#
#   nohup caffeinate -i src/landsat_mosaic/after_batch.sh v3 2003-2023 data/logs/build.l7fill.log [N jobs] \
#       > data/logs/refresh.l7fill.log 2>&1 &
#   echo $! > data/refresh.pid
set -uo pipefail
cd "$(dirname "$0")/../.."
TAG=${1:?tag}; YEARS=${2:?years}; LOG=${3:?batch log}; EXPECT=${4:-693}
R=src/landsat_mosaic/refresh_pyramid.sh
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

pid=$(cat data/build.pid)
log "waiting for batch pid $pid to exit"
while kill -0 "$pid" 2>/dev/null; do
  sleep 300
  new=$(cat data/build.pid 2>/dev/null || echo "$pid")
  [[ "$new" != "$pid" ]] && { log "batch pid changed $pid -> $new"; pid=$new; }
done
log "batch exited: $(grep progress "$LOG" | tail -1)"
sleep 30
$R check "$LOG" "$EXPECT" || { log "check failed, not building"; exit 1; }
$R build "$TAG" "$YEARS" || { log "build failed"; exit 1; }
$R score || log "score failed (non-fatal)"
log "restarting the viewer on :2718"
for p in $(lsof -nP -t -iTCP:2718 -sTCP:LISTEN 2>/dev/null); do kill "$p" 2>/dev/null; done
sleep 5
nohup caffeinate -i uv run marimo edit --headless --no-token --port 2718 mosaic-viewer.py \
    >> data/logs/viewer.log 2>&1 &
sleep 20
if lsof -nP -iTCP:2718 -sTCP:LISTEN >/dev/null 2>&1; then
  log "viewer listening on :2718"
else
  log "viewer did not come up, see data/logs/viewer.log"
fi
log "refresh done: data/pyramid_v1 holds the $TAG data for $YEARS, viewer restarted"
