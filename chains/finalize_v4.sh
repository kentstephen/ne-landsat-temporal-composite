#!/bin/zsh
# After the source pass: provenance into the store, into the pyramid, verify
# the pyramid against the snapshot, and a gc DRY RUN. The real gc is not run
# here; it is a by-hand step the morning after. Stops at the first failing step.
#   nohup caffeinate -i data/logs/finalize_v4.sh >> data/logs/finalize_v4.log 2>&1 &
#   echo $! > data/finalize.pid
set -uo pipefail
cd "$(dirname "$0")/.."
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
run() { log "STEP $*"; uv run python "$@" 2>&1 | grep -v "WARN.*LocalFileSystem\|icechunk-arrow-object-store\|^ *$" ; return ${pipestatus[1]}; }
sw=$(cat data/source_wait.pid)
log "finalize: waiting for source pass waiter pid $sw to exit"
while kill -0 $sw 2>/dev/null; do sleep 120; done
n=$(ls stats/source_pass/*.json | grep -vc summary.json)
log "source pass exited: $n of $(wc -l < data/l7fill_jobs.txt) per-job stats, summary $( [[ -f stats/source_pass/summary.json ]] && echo present || echo MISSING )"
if grep -l "COUNT MISMATCH\|UNMATCHED" data/logs/source_pass.[01].log; then
  log "flags in the source pass logs, NOT finalizing. Look first."; exit 1
fi
if [[ $n -lt $(wc -l < data/l7fill_jobs.txt) ]]; then
  log "source pass incomplete ($n jobs), NOT finalizing. Rerun the missing jobs, then run this script's steps by hand."; exit 1
fi
run src/landsat_mosaic/store_source.py --years 2003-2023 --tag v4-provenance   || { log "store_source failed"; exit 1; }
run src/landsat_mosaic/source_pyramid.py --years 2003-2023 --workers 4           || { log "source_pyramid failed"; exit 1; }
run src/landsat_mosaic/pyramid.py --finalize                                    || { log "finalize failed"; exit 1; }
run src/landsat_mosaic/export_zarr.py --verify --ref v4-provenance --sample 20  || { log "export verify failed, NOT collecting"; exit 1; }
run src/landsat_mosaic/gc_store.py --keep v4,v4-provenance --dry-run            || { log "gc dry run failed"; exit 1; }
log "finalize done (no gc): store $(du -sh data/newengland.icechunk | cut -f1) with tags v4 + v4-provenance, pyramid $(du -sh data/pyramid_v1 | cut -f1) verified against v4-provenance."
log "NEXT, by hand: uv run python src/landsat_mosaic/gc_store.py --keep v4,v4-provenance   (then export_zarr.py --verify --ref v4-provenance --sample 5)"
