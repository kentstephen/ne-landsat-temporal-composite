#!/bin/zsh
# Start the v4 source pass once tonight's refresh is done. Waits for the
# fixup chain (data/fixup.pid, which execs after_batch.sh) to exit, requires
# "refresh done" in nbr3_chain.log, then runs source_pass.py over the 379
# rebuilt tile-years as two workers on disjoint halves of the job list (one
# shard per tile-year in data/source_v4.zarr, so this is safe), and writes
# stats/source_pass/summary.json at the end.
#   nohup caffeinate -i data/logs/source_pass_wait.sh >> data/logs/source_pass.log 2>&1 &
set -uo pipefail
cd "$(dirname "$0")/.."
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
fx=$(cat data/fixup.pid)
log "source pass: waiting for fixup/after_batch pid $fx to exit"
while kill -0 $fx 2>/dev/null; do sleep 60; done
if ! grep -q "refresh done" data/logs/nbr3_chain.log; then
  log "source pass: chain exited without 'refresh done', NOT starting. Start by hand:"
  log "  nohup caffeinate -i uv run python src/landsat_mosaic/source_pass.py --jobs data/l7fill_jobs.txt --shard 0/2 > data/logs/source_pass.0.log 2>&1 &"
  exit 1
fi
log "source pass: refresh done, starting 2 workers over data/l7fill_jobs.txt"
: > data/source_pass.pids
for i in 0 1; do
  uv run python src/landsat_mosaic/source_pass.py --jobs data/l7fill_jobs.txt --shard $i/2 \
      > data/logs/source_pass.$i.log 2>&1 &
  echo $! >> data/source_pass.pids
  sleep 30
done
wait
log "source pass: workers exited: $(grep -h progress data/logs/source_pass.[01].log | tail -2 | tr '\n' ';')"
grep -h "failed:" data/logs/source_pass.[01].log | tail -2
uv run python src/landsat_mosaic/source_pass.py --summary
log "source pass done: data/source_v4.zarr, stats/source_pass/summary.json"
