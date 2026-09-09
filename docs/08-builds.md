# The builds

The store was written four times between September 4 and September 9,
2026, each pass over a subset of the tile-years, and then the provenance
pass read it all back. Every pass is a tag, or was one. This is the
order things ran in, with the commands, so that the numbers in the other
docs can be placed.

## v1: medoid, every look (September 4 to 6)

The full build from the empty store. 858 land tile-years, sequential,
about two days.

    uv run python src/landsat_mosaic/fetch_catalog.py
    uv run python src/landsat_mosaic/grid.py
    uv run python src/landsat_mosaic/init_store.py
    uv run python src/landsat_mosaic/planner.py
    nohup caffeinate -i uv run python src/landsat_mosaic/batch.py > data/build.log 2>&1 &

It was interrupted once for the 2022 corrupt assets (`scan_assets.py`,
`data/bad_scenes.txt`, then a restart that skipped the finished jobs)
and finished 2026-09-06 at 00:10, 858 of 858, none failed. Level 0 QA
ran clean (docs/06), main was tagged v1 at 00:20, level 0 was exported
in 2.9 minutes at six workers, levels 1 to 7 were built in 5.5 minutes,
finalized and verified. The pyramid went into the viewer that night.

## v2: median for 2003 to 2012 (September 6)

Striping was visible in the viewer in every Landsat 7 year. It was read
as a medoid problem and the median was tried (docs/03).

    uv run python src/landsat_mosaic/batch.py --years 2003-2012 --reducer median --force
    chains/pyramid_v2_chain.sh

330 jobs, about 12 hours; the chain waited on the export pid, verified,
built the levels for those years, finalized and verified. Scored, the
striping was in every Landsat 7 year under either reducer, so v2 was
superseded the same day and later expired. It is the reason `pyramid.py`
writes `reducer_by_year`.

## v3: the Landsat 7 fill rule (September 6 to 7)

Medoid everywhere, Landsat 7 counting only where the other platforms
fall short of four looks, 2012 pooled from 2011 to 2013.

    nohup caffeinate -i uv run python src/landsat_mosaic/batch.py --years 2003-2023 --reducer medoid --force \
        > data/logs/build.l7fill.log 2>&1 &
    nohup caffeinate -i src/landsat_mosaic/after_batch.sh v3 2003-2023 data/logs/build.l7fill.log \
        > data/logs/refresh.l7fill.log 2>&1 &

693 jobs, started 11:39, finished 11:22 the next day, 23.7 hours, none
failed. A checkpoint at 185 jobs with `stripe_score_store.py` showed
the count striping gone and the NIR striping most of the way there in
2003 to 2007, so the run was left alone. `after_batch.sh` failed at its
first step (zsh does not word-split a quoted command string; fixed with
a `run()` helper) with nothing built and the store untouched, so the
refresh was run by hand: tag v3, export the 693 tile-years (2.5
minutes), levels (4 minutes), finalize, verify (5.5 minutes), score.

## v4: the ladder, in two batches (September 7 to 8)

The 379 tile-years that still had a block with count stripes
(`data/l7fill_jobs.txt`) rebuilt under the ladder at threshold 3 with
neighbour-year looks, the Landsat 5 edge erosion and the same-day
platform split. Split into the Landsat 8 era (147 jobs, 2013 to 2023)
and the Landsat 5 era (232 jobs, 2003 to 2012) so the era that was
expected to fix cleanly landed first. `chains/nbr3_chain.sh` ran it:

    batch.py --years 2013-2023 --reducer medoid --force --fill-min-clear 3 --neighbours \
        --jobs data/l7fill_jobs_l8era.txt --skip-log data/logs/build.nbr3.l8era.log
    refresh_pyramid.sh check data/logs/build.nbr3.l8era.log 147
    batch.py --years 2003-2012 --reducer medoid --force --fill-min-clear 3 --neighbours \
        --jobs data/l7fill_jobs_l5era.txt
    after_batch.sh v4 2003-2023 data/logs/build.nbr3.l5era.log 232

The `--skip-log` on batch 1 is because a first run of the same jobs had
started at 12:19 before the two extra stripe sources were found, and was
restarted at 13:30 with the fixed runner; the handful of 2013 jobs it
had finished were skipped rather than redone. `src/landsat_mosaic/v4_monitor.sh`
watched the logs throughout and validated each tile-year as it landed
(docs/06).

Batch 1 finished 2026-09-08 at 02:40, 147 of 147, 13.6 hours after the
restart, every year 2013 to 2023 clean on land by the aggregate. Batch 2
started 02:34 and lost five 2007 jobs to an internet drop at 10:18; the
chain's check refused to build with `data/failed.txt` present, as it
should, and `chains/nbr3_fixup.sh` was armed to wait for the chain to
exit, rerun the five with `--skip-log`, and then run `after_batch.sh`
exactly as the chain would have. Batch 2 ended at 19:34, 232 of 232;
the fixup reran the five by 20:00, and `after_batch.sh v4` ran 20:00 to
20:17 unattended: tag v4, export 1029 tile-years in 3 minutes, levels
in 3.9, finalize, verify every pixel of every level, score, viewer
restart. `data/pyramid_v1` was the v4 pyramid.

Jobs ran 3 to 7 minutes each, the 2014 ones slowest because they carry
75 to 85 neighbour-year scenes on top of 80 to 100 of their own.

## The source pass and the hold-out test (September 8 to 9)

Both were written on September 8 while batch 2 ran, and queued behind
it. `chains/source_pass_wait.sh` waited for the fixup chain to exit and
for "refresh done" in its log, then ran two workers over the 379
tile-years:

    source_pass.py --jobs data/l7fill_jobs.txt --shard 0/2
    source_pass.py --jobs data/l7fill_jobs.txt --shard 1/2
    source_pass.py --summary

About 3.4 minutes a job, 379 of 379 by the morning of September 9, no
COUNT MISMATCH or UNMATCHED flag in either log. `chains/holdout_wait.sh`
waited for the source pass to exit so the two did not compete for
bandwidth, then ran the seven windows, about four minutes each.

## v4-provenance and the release (September 9)

`chains/finalize_v4.sh` waited for the source pass, refused to continue
unless all 379 job stats were present and the logs carried no flag, and
ran:

    store_source.py --years 2003-2023 --tag v4-provenance
    source_pyramid.py --years 2003-2023 --workers 4
    pyramid.py --finalize
    export_zarr.py --verify --ref v4-provenance --sample 20
    gc_store.py --keep v4,v4-provenance --dry-run

It ran at 07:54. The store planes matched the side store in all 21
years and the pyramid got its three planes at every level. The verify
then failed on one group attribute: `store_source.py` had written
`provenance_planes` on the store group after the export had already
copied the attrs, and nothing wrote it to the pyramid. The text became
one constant, `store_source.PROVENANCE_PLANES`, written by
`source_pyramid.py` on level 0 when it makes the arrays; a rerun (years
skipped as present), finalize and verify came back clean.

Garbage collection was by hand after reading the dry run, with the
decision to keep v3 as well:

    uv run python src/landsat_mosaic/gc_store.py --keep v3,v4,v4-provenance

240 GB to 130 GB in 4 seconds (docs/07). Verify against v4-provenance
after it: clean.

The upload was Stephen's, from his terminal, at about 11:00: 38,522
files, 261.7 GiB, an hour, seven Cloudflare drops retried by a second
run, final diff clean, `remote_check.py` clean. The product is at
https://source.coop/kentstephen/landsat-mosaics-new-england.

## What each pass left behind

    pass            tile-years written   tag              kept
    v1              858                  v1               expired
    v2              330                  v2               expired
    v3              693                  v3               yes, own-year only
    v4              379 (+5 reruns)      v4               yes, the pixels
    provenance      21 years of planes   v4-provenance    yes, the snapshot the pyramid was verified against

Disk at the end: the store 130 GB, the pyramid 130 GB, the catalog 6 GB,
trials 2.5 GB, the source side store 40 MB. The logs of every run are
in the working repo's `data/logs/`, not here; the chains in `chains/`
are the scripts that wrote them, with the absolute paths made relative.
