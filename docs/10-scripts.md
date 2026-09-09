# The scripts

Every script is run as `uv run python src/landsat_mosaic/<name>.py`
from the repo root and describes its flags in its docstring. `grid.ROOT`
is the repo root, and every path below is relative to it. Read-only
means the script opens the Icechunk store on a read-only session and is
safe beside a running `batch.py`.

## The pipeline, in run order

    fetch_catalog.py    Downloads the Planetary Computer's stac-geoparquet snapshot of
                        landsat-c2-l2, one file per year, keeping the newest part per
                        year. Free, public SAS token. Writes data/catalog/. 6.3 GB.
    grid.py             The grid constants and the tile scheme (docs/01). Writes
                        data/grid/tiles.parquet and data/grid/state_mask.tif.
    init_store.py       Creates the empty Icechunk store and leafon group. Runs once;
                        refuses to touch an existing group.
    planner.py          DuckDB join of catalog scenes to land tiles, May to October,
                        Tier 1, land cloud cover under 80. Writes data/jobs.parquet,
                        one row per (tile, year, scene) with asset hrefs.
    runner.py           One (tile, year) composite into the store (docs/02, 03). The
                        ladder, the window rules, the Landsat 5 edge erosion and the
                        reducers live here and every other script imports them.
                        --dry-run writes an npz and PNG to data/trials/ instead.
    batch.py            Walks the jobs sequentially, retries, logs failures to
                        data/failed.txt, skips tile-years already built. --force,
                        --skip-log, --jobs, --fill-min-clear, --neighbours.
    qa_level0.py        Level 0 QA of the whole store (docs/06). Writes
                        stats/qa_level0.json. Read-only.
    tag.py              Tags main. Idempotent.
    export_zarr.py      Level 0 of the pyramid from a tag, shard by shard, resumable,
                        parallel. --verify compares the pyramid to the snapshot.
    pyramid.py          Levels 1 to 7, --finalize for the attrs and consolidated
                        metadata, --verify (docs/05).
    source_pass.py      Per-pixel provenance of the v4 picks into data/source_v4.zarr
                        (docs/04). --init, --jobs FILE --shard i/n, --summary.
    store_source.py     The source and pick_doy planes into the Icechunk store, one
                        commit per year, read back, tag. --check compares only.
    source_pyramid.py   source, pick_doy and borrowed_pct into the pyramid at every
                        level, then reconsolidates.
    gc_store.py         Collects the store down to the tags that ship. --dry-run.
    upload.py           boto3 copy to Source Coop with a listing diff (docs/07).
    remote_check.py     Reads the bucket back and compares to disk. Read-only.

## Shell

    refresh_pyramid.sh  check LOG [N]: is the batch done? build TAG YEARS: tag,
                        export, levels, finalize, verify. score: stripe_score.py.
    after_batch.sh      Waits for data/build.pid to exit, then check, build, score
                        and a viewer restart. Unattended.
    v4_monitor.sh       Watches the two v4 batch logs, validates each tile-year as
                        it lands, checks each year as it completes, prints events.
    chains/             The one-off chains that ran the rebuilds overnight
                        (docs/08). Each waits for a pid file or a log line, then
                        runs the next step.

## Measurement and trial scripts

None of these had to run for the product to exist. Each one answered a
question whose answer is in the docs and in `stats/`.

    catalog_summary.py     Scenes and bytes per year in the catalog.
    clear_counts.py        Expected clear looks per path/row per year from the
                           catalog alone, before any pixel was read. Chose the
                           window and the cloud cut. stats/clear_counts.{parquet,png}.
    pilot.py               One small AOI, one year, three reducers, into a local
                           Icechunk repo, with quicklooks. The end-to-end proof
                           before the runner was written.
    scan_assets.py         Finds corrupt Planetary Computer assets (2022) with two
                           range requests each. Feeds data/bad_scenes.txt.
    compare_reducers.py    Medoid against a median dry run of the same tile-year,
                           crops and a striping score. stats/reducer_compare/.
    count_reducers.py      Which reducer coarsens clear_count in the pyramid.
                           stats/count_reducers.json. Read-only.
    pyramid_chunk_bytes.py Compressed bytes per chunk, per level, per chunk size,
                           on finished tiles. stats/pyramid_chunk_bytes.json.
    pyramid_layout_math.py Chunks, objects, GB and requests per screen from those
                           bytes. Prints the tables in docs/05.
    stripe_score.py        The directional stripe score on a pyramid (docs/03).
                           stats/striping/.
    stripe_score_store.py  The same score off the Icechunk store mid-build. Read-only.
    stripe_trials.py       Candidate fixes for the striping on one sub-tile, one
                           load: every look, no Landsat 7, the fill rule, each with
                           medoid and median, and --span for 2012. stats/stripe_trials/.
    fill_trials.py         The fill rule against the ladder at thresholds 3 and 4 on
                           one window, with where each pixel's pick came from.
                           stats/fill_trials/.
    check_rebuilt.py       A rebuilt year off the store against the v3 scores, land
                           and water apart. Read-only.
    validate_tile.py       One rebuilt tile-year, OK or FLAG. Read-only.
    holdout_test.py        The error of a borrowed pick against an own-year reference
                           (docs/04). stats/holdout/.

## The supplemental layers

    state_boundary.py   supplemental/state_boundary.parquet from TIGER/Line 2024.
    wildlands.py        supplemental/wildlands.parquet from Harvard Forest HF435.
    water.py            supplemental/water.parquet from NHD High Resolution (18.7 MB,
                        built on demand, not tracked).
    supplemental.py     supplemental/protected_open_space.parquet from Harvard
                        Forest's Protected Open Space v1.1. Built, shipped, then
                        replaced by the wildlands layer and removed from the bucket.

`supplemental/README.md` has the sources, licenses and what changed
from each source.

## Inputs tracked in data/

    l7fill_jobs.txt          the 379 tile-years the ladder rebuilt, "tile year" per line
    l7fill_jobs_l8era.txt    the 147 of them in 2013 to 2023 (batch 1)
    l7fill_jobs_l5era.txt    the 232 in 2003 to 2012 (batch 2)
    holdout_windows.txt      the seven hold-out windows, "year lat lon" per line
    bad_scenes.txt           33 STAC item ids with corrupt assets, dropped from every job
    striping_v3/             the v3 stripe scores the validators compare against

## Not in this repo

The marimo viewers (`mosaic-viewer.py` and the CTrees pair notebooks)
and `tiles.py`, the Web Mercator tile renderer they read the pyramid
through. They consume the product; they are not its provenance.
