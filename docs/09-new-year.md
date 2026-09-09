# Appending a year

The product is one pyramid, appended in place, and one Icechunk store
that holds the published snapshot and its tags. A new year is appended
when the code that makes the pixels is unchanged: the test is whether
rerunning an old year would give the same bytes. A change to the
reducer, the mask, the window, the fill rule, the grid or the sensors is
a new method version, published under a new prefix (pyramid_v2/) and a
new tag, not an append.

The 2026 leaf-on window ends September 30 and the Tier 1 scenes land on
the Planetary Computer a few weeks after acquisition, so the first
append is a late 2026 job. Nothing below has been run yet against the
bucket; the local path is what the build used and is known to work.

## The local path, as the build ran

1. Refresh the catalog for the new year and check it for corrupt assets.
   `fetch_catalog.py` keeps only the newest part per year, so rerunning
   it replaces the year's file.

        uv run python src/landsat_mosaic/fetch_catalog.py
        uv run python src/landsat_mosaic/planner.py
        uv run python src/landsat_mosaic/scan_assets.py 2026

   `planner.py` has `YEARS = (2000, 2025)`; extend it. `init_store.py`
   has `YEARS = list(range(2000, 2026))`, the length of the time axis in
   the store; extend it and resize the arrays (the store was created
   once and `init_store.py` refuses to touch an existing group, so the
   resize is a small icechunk session by hand: `arr.resize` on each of
   the nine arrays and the time coordinate, then commit).

2. Build the year. The rule set is the v4 one: medoid, fallback window,
   min_clear 4, the Landsat 7 ladder at threshold 3 with neighbour
   years where a tile-year stripes, threshold 4 own-year elsewhere. A
   year with no Landsat 7 (2024 onward) needs neither, and the runner
   applies neither when the stack has no Landsat 7 slot, so:

        nohup caffeinate -i uv run python src/landsat_mosaic/batch.py --years 2026 > data/logs/build.2026.log 2>&1 &

   Use a branch per year and merge to main on passing QA, so main always
   equals what the pyramid holds.

3. QA the year off the store: `qa_level0.py --years 2026`, then
   `stripe_score_store.py 2026` (should sit at the clean-year level,
   nir median 1.05 to 1.10, cc_ratio under 2.2). Look at it in a viewer.
   Rerun tile-years that fail.

4. Tag and export, only the new year:

        uv run python src/landsat_mosaic/tag.py v4-2026
        uv run python src/landsat_mosaic/export_zarr.py --ref v4-2026 --years 2026 --force
        uv run python src/landsat_mosaic/pyramid.py --years 2026 --force
        uv run python src/landsat_mosaic/pyramid.py --finalize
        uv run python src/landsat_mosaic/pyramid.py --verify
        uv run python src/landsat_mosaic/export_zarr.py --verify --ref v4-2026

   `export_zarr.py` and `pyramid.py` create the target arrays at the
   store's shape, so the pyramid's time axis has to be resized to match
   before the export (the same nine `zarr.json` files per level plus the
   time coordinate). This is the "small metadata step" the store plan
   names and it is not yet a script.

   The provenance planes: a year with no Landsat 7 and no ladder has
   `source` 0 wherever `clear_count` is nonzero, `pick_doy` from the
   pick, `borrowed_pct` 0. `source_pass.py` still has to run to get
   `pick_doy`; it is cheap for one year.

5. Upload the new objects and reconsolidate. `upload.py` skips keys
   already present with the same size, so rerunning the full command
   uploads only the new year's chunks and the rewritten metadata:

        uv run python src/landsat_mosaic/upload.py --prefix ACCOUNT/PRODUCT data/pyramid_v1:pyramid_v1 data/newengland.icechunk:build.icechunk README.md
        uv run python src/landsat_mosaic/remote_check.py --prefix ACCOUNT/PRODUCT --years 2026

   Metadata last: a reader who arrives mid-way sees the old tree or a
   nodata year, never a broken one. The proxy caches data objects for
   30 minutes and product-level files for 5, so a rewritten
   `zarr.json` can serve stale for that long.

6. Add the append line to the product README (date, tag) and upload it.

## The bucket path, planned

The Icechunk store is hosted so that nothing has to live on a local
drive between updates. The append can then run against the bucket:
`runner.py` opens the remote store (icechunk `s3_storage` with the
data.source.coop endpoint, path style, the bucket-owner-full-control
write header, and a refreshable credential callback for the CLI's
one-hour STS sessions), commits the year, tags it; the export reads
that slice (about 3.5 GB down), writes its level 0 chunks at the new
time index and derives levels 1 to 7 from the slice (about 5 GB up).
The compute is still this machine in every step. Source Coop stores and
serves objects; nothing runs there.

Code this needs and does not yet have: a store argument on `runner.py`
and `init_store.py` (local path or s3 URI) with the credential callback
and the conditional-write fallback; a year subset and append mode
(resize, not create) on `export_zarr.py` and `pyramid.py` with a bucket
target; the metadata step. Before the first remote write: the STS write
test with a pilot store, and DeleteObject on the prefix, or `gc_store.py`
cannot run remotely.

## What stays fixed

The prefix `pyramid_v1/`, the group name `leafon`, the array names, the
chunking and the codecs. S3 has no rename.
