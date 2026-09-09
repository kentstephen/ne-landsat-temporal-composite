#!/bin/zsh
# Refresh data/pyramid_v1 in place from a tag of the Icechunk store after a
# batch.py rebuild. Only the rebuilt years are rewritten: level 0 export with
# --force for those years, levels 1-7 for those years, finalize, verify.
# Years outside the range are left as they are. The Icechunk store is never
# touched except for the tag.
#
#   src/landsat_mosaic/refresh_pyramid.sh check LOG [N]     batch done? N (default 693) "done in", no failed.txt
#   src/landsat_mosaic/refresh_pyramid.sh build TAG YEARS   tag main, export, levels, finalize, verify
#   src/landsat_mosaic/refresh_pyramid.sh score             stripe_score.py on the pyramid
# Example for the Landsat 7 fill rebuild:
#   src/landsat_mosaic/refresh_pyramid.sh check data/logs/build.l7fill.log
#   src/landsat_mosaic/refresh_pyramid.sh build v3 2003-2023
set -euo pipefail
cd "$(dirname "$0")/../.."
SRC=src/landsat_mosaic
run() { uv run python "$SRC/$1" "${@:2}"; }   # zsh does not word-split "$PY/x.py"
STORE=data/pyramid_v1
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

case "${1:-}" in
check)
  LOG=${2:?log file}; EXPECT=${3:-693}
  if [[ -f data/build.pid ]] && kill -0 "$(cat data/build.pid)" 2>/dev/null; then
    log "batch still running (pid $(cat data/build.pid)): $(grep progress "$LOG" | tail -1)"; exit 1
  fi
  done_n=$(grep -c ': done in' "$LOG" || true)
  log "$done_n jobs done in $LOG; $(grep -c 'batch finished' "$LOG" || true) 'batch finished' line(s)"
  if [[ -f data/failed.txt ]]; then
    log "data/failed.txt exists ($(wc -l < data/failed.txt) lines): rerun batch.py --skip-log $LOG first"; exit 1
  fi
  [[ "$done_n" -eq "$EXPECT" ]] || { log "expected $EXPECT done, got $done_n"; exit 1; }
  log "batch complete"
  ;;
build)
  TAG=${2:?icechunk tag, e.g. v3}; YEARS=${3:?years, e.g. 2003-2023}
  log "tag $TAG on main (no-op if it exists and matches)"
  run tag.py "$TAG"
  log "export level 0, years $YEARS, from tag $TAG into $STORE"
  run export_zarr.py --ref "$TAG" --out "$STORE" --years "$YEARS" --force --workers 4
  run export_zarr.py --ref "$TAG" --out "$STORE" --verify
  log "levels 1-7, years $YEARS"
  run pyramid.py --out "$STORE" --years "$YEARS" --force --workers 6
  log "finalize"
  run pyramid.py --out "$STORE" --finalize
  log "verify every level, every year"
  run pyramid.py --out "$STORE" --verify --workers 6
  log "build done: $STORE is the $TAG pyramid"
  ;;
score)
  run stripe_score.py --store "$STORE"
  log "scores in stats/striping/ (summary.json)"
  ;;
*)
  sed -n '2,14p' "$0"; exit 1
  ;;
esac
