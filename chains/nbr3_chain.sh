#!/bin/zsh
# 2026-09-07: the two targeted rebuilds and the v4 refresh, unattended.
#   1. Landsat 8 era flagged jobs (147), nbr3 ladder, same-day slot fix; resumes the
#      earlier run of the same log with --skip-log.
#   2. Landsat 5 era flagged jobs (232), nbr3 ladder plus Landsat 5 edge erosion.
#   3. after_batch.sh v4: check, export 2003-2023, levels, finalize, verify, score, viewer.
set -uo pipefail
cd "$(dirname "$0")/.."
R=src/landsat_mosaic/refresh_pyramid.sh
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
L1=data/logs/build.nbr3.l8era.log; L2=data/logs/build.nbr3.l5era.log
log "batch 1: Landsat 8 era, resuming $L1"
uv run python src/landsat_mosaic/batch.py --years 2013-2023 --reducer medoid --force --fill-min-clear 3 --neighbours \
    --jobs data/l7fill_jobs_l8era.txt --skip-log $L1 >> $L1 2>&1 &
echo $! > data/build.pid; wait $!
$R check $L1 147 || { log "batch 1 short, stopping"; exit 1; }
log "batch 2: Landsat 5 era"
uv run python src/landsat_mosaic/batch.py --years 2003-2012 --reducer medoid --force --fill-min-clear 3 --neighbours \
    --jobs data/l7fill_jobs_l5era.txt > $L2 2>&1 &
echo $! > data/build.pid; wait $!
log "refresh v4"
exec src/landsat_mosaic/after_batch.sh v4 2003-2023 $L2 232
