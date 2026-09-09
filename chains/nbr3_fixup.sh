#!/bin/zsh
# Tail replacement for nbr3_chain.sh after the 10:18 Sep 8 internet drop
# failed five 2007 jobs (data/failed.txt). nbr3_chain.sh will exit on its
# own once batch 2 ends, because after_batch's check refuses to build while
# failed.txt exists. This script waits for that, reruns the failed jobs with
# --skip-log (only the five are left), clears failed.txt if the rerun is
# clean, then runs after_batch.sh v4 exactly as the chain would have.
#   nohup caffeinate -i data/logs/nbr3_fixup.sh >> data/logs/nbr3_chain.log 2>&1 &
set -uo pipefail
cd "$(dirname "$0")/.."
L2=data/logs/build.nbr3.l5era.log
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
chain=$(cat data/chain.pid)
log "fixup: waiting for chain pid $chain to exit (batch 2 end, then its check fails on failed.txt)"
while kill -0 $chain 2>/dev/null; do sleep 20; done
bp=$(cat data/build.pid)
while kill -0 $bp 2>/dev/null; do sleep 20; done
log "fixup: batch 2 exited: $(grep progress $L2 | tail -1)"
[[ -f data/failed.txt ]] || { log "fixup: no data/failed.txt, nothing to rerun"; exit 1; }
log "fixup: rerunning $(wc -l < data/failed.txt | tr -d ' ') failed jobs: $(tr '\n' ' ' < data/failed.txt)"
mv data/failed.txt data/failed.nbr3.first.txt
uv run python src/landsat_mosaic/batch.py --years 2003-2012 --reducer medoid --force --fill-min-clear 3 --neighbours \
    --jobs data/l7fill_jobs_l5era.txt --skip-log $L2 >> $L2 2>&1 &
echo $! > data/build.pid; wait $!
log "fixup: rerun exited: $(grep progress $L2 | tail -1)"
if [[ -f data/failed.txt ]]; then
  log "fixup: rerun still failed: $(tr '\n' ' ' < data/failed.txt)"; log "check failed, not building"; exit 1
fi
log "refresh v4"
exec src/landsat_mosaic/after_batch.sh v4 2003-2023 $L2 232
