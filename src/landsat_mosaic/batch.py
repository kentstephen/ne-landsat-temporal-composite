"""Walk every (tile, year) job in order and build the ones not yet in the store.

Sequential, one process, because the local Icechunk store is not safe for
concurrent commits. Failed jobs are retried with backoff, then logged to
data/failed.txt and skipped so the run keeps going. Safe to rerun: jobs
already in the store are skipped by the runner.

Usage:
  uv run python src/landsat_mosaic/batch.py [--years 2000-2025] [--reducer medoid] [--force]
--force rebuilds tile-years already in the store (a reducer change for a
range of years); without it they are skipped.
--skip-log LOG skips the (tile, year) jobs that LOG records as "done in";
use it to restart a --force run without redoing finished jobs.
--jobs FILE runs only the 'tile year' lines listed (a targeted rebuild).
Run under caffeinate and nohup for a multi-day build:
  nohup caffeinate -i uv run python src/landsat_mosaic/batch.py > data/build.log 2>&1 &
"""
import argparse
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from landsat_mosaic import grid, runner

FAILED = grid.ROOT / "data/failed.txt"


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="2000-2025")
    ap.add_argument("--reducer", default="medoid", choices=runner.REDUCERS)
    ap.add_argument("--window", default="fallback")
    ap.add_argument("--min-clear", type=int, default=4)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--force", action="store_true", help="rebuild tile-years already in the store")
    ap.add_argument("--no-l7-fill", action="store_true", help="count every Landsat 7 look")
    ap.add_argument("--skip-log", default=None,
                    help="skip (tile, year) jobs logged as 'done in' in this earlier log")
    ap.add_argument("--fill-min-clear", type=int, default=None,
                    help="non-L7 clear looks that keep Landsat 7 out (default --min-clear)")
    ap.add_argument("--neighbours", action="store_true",
                    help="lend the neighbouring years' non-L7 looks before Landsat 7")
    ap.add_argument("--jobs", default=None,
                    help="text file of 'tile year' lines: run only these (still within --years)")
    a = ap.parse_args()
    y0, y1 = (int(v) for v in a.years.split("-"))

    jobs = pd.read_parquet(grid.ROOT / "data/jobs.parquet", columns=["tile", "year"])
    jobs = jobs.drop_duplicates().sort_values(["year", "tile"])
    jobs = jobs[jobs.year.between(y0, y1)]
    if a.jobs:
        want = {tuple(ln.split()) for ln in Path(a.jobs).read_text().splitlines() if ln.strip()}
        jobs = jobs[jobs.apply(lambda r: (r.tile, str(r.year)) in want, axis=1)]
        log(f"{len(jobs)} of the {len(want)} jobs listed in {a.jobs} are in the plan")
    if a.skip_log:
        done_before = set(re.findall(r"^(r\d\dc\d\d) (\d{4}): done in", Path(a.skip_log).read_text(), re.M))
        jobs = jobs[~jobs.apply(lambda r: (r.tile, str(r.year)) in done_before, axis=1)]
        log(f"skipping {len(done_before)} jobs already done in {a.skip_log}")
    log(f"{len(jobs)} jobs, years {y0}-{y1}, reducer {a.reducer}, force {a.force}, "
        f"l7_fill {not a.no_l7_fill}, fill_min_clear {a.fill_min_clear}, neighbours {a.neighbours}, "
        f"span {runner.YEAR_SPAN}")
    t0 = time.time()
    done = failed = 0
    for tile, year in jobs.itertuples(index=False):
        for attempt in range(1, a.retries + 1):
            try:
                runner.run_job(tile, int(year), a.reducer, a.window, a.min_clear,
                               sub=2048, pool=32, dry_run=False, only=None, force=a.force,
                               l7_fill=not a.no_l7_fill, fill_min_clear=a.fill_min_clear,
                               neighbours=a.neighbours)
                done += 1
                break
            except Exception as e:
                log(f"{tile} {year} attempt {attempt} failed: {e!r}")
                traceback.print_exc()
                if attempt < a.retries:
                    time.sleep(60 * attempt)
        else:
            failed += 1
            with open(FAILED, "a") as f:
                f.write(f"{tile} {year}\n")
        elapsed = (time.time() - t0) / 3600
        log(f"progress {done + failed}/{len(jobs)} ({failed} failed), {elapsed:.1f} h elapsed")
    log("batch finished")


if __name__ == "__main__":
    main()
