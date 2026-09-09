# ne-landsat-temporal-mosaic

The code that built the New England leaf-on Landsat composites: one
cloud-free composite per year, 2000 to 2025, six bands at 30 m, as a plain
Zarr v3 multiscales pyramid with the Icechunk store it was exported from.

The data is on Source Coop:
https://source.coop/kentstephen/landsat-mosaics-new-england
(read from https://data.source.coop/kentstephen/landsat-mosaics-new-england/).
The product README there describes what a reader needs to know before
using the pixels. This repo is the provenance: every script that ran, the
inputs they read, the numbers they produced, and the order they ran in.

Data: CC0-1.0. Code: MIT. Source: USGS Landsat Collection 2 Level-2 Tier 1
surface reflectance, read through the Microsoft Planetary Computer.

## What it does

    fetch_catalog.py   MPC stac-geoparquet catalog, one file per year
    grid.py            EPSG:4326 grid at 0.00025 deg, 7 x 7 tiles of 4096 px, six-state mask
    init_store.py      empty Icechunk store, arrays [26, 26200, 27600]
    planner.py         DuckDB join of catalog scenes to land tiles: 858 (tile, year) jobs
    batch.py           walks the jobs; runner.py builds one tile-year and commits it
    qa_level0.py       completeness, nodata consistency, DN ranges, count histogram
    tag.py             tag main (v3, v4, v4-provenance)
    export_zarr.py     level 0 of the pyramid, identical to the tagged snapshot
    pyramid.py         levels 1-7, multiscales attrs, consolidated metadata, verify
    source_pass.py     per pixel, which look the medoid picked (own year, year +-1, Landsat 7)
    store_source.py    the source and pick_doy planes into the Icechunk store
    source_pyramid.py  source, pick_doy and borrowed_pct into the pyramid
    gc_store.py        expire every snapshot but the tags that ship
    upload.py          boto3 copy to Source Coop, then a listing diff
    remote_check.py    read the bucket back and compare to disk

Each tile-year composite is a medoid: the clear look nearest the per-pixel
median in band space, so every pixel is one real observation. Landsat 7's
scan line corrector gaps striped every year 2003 to 2023 under any
per-pixel reducer, and most of the work in this repo is the diagnosis and
the rule that fixed it (docs/03). The rule borrows looks from the years
either side in 379 tile-years, and the provenance planes say where
(docs/04).

## Running it

Python 3.12 and uv. `uv sync` installs the dependencies. Every script is
run as `uv run python src/landsat_mosaic/<script>.py` from the repo root
and documents its own flags in its docstring. The full build from an empty
store, in order:

    uv run python src/landsat_mosaic/fetch_catalog.py
    uv run python src/landsat_mosaic/grid.py
    uv run python src/landsat_mosaic/init_store.py
    uv run python src/landsat_mosaic/planner.py
    nohup caffeinate -i uv run python src/landsat_mosaic/batch.py > data/build.log 2>&1 &
    uv run python src/landsat_mosaic/batch.py --years 2003-2023 --force --fill-min-clear 3 --neighbours --jobs data/l7fill_jobs.txt
    uv run python src/landsat_mosaic/qa_level0.py
    src/landsat_mosaic/refresh_pyramid.sh build v4 2000-2025
    uv run python src/landsat_mosaic/source_pass.py --init
    uv run python src/landsat_mosaic/source_pass.py --jobs data/l7fill_jobs.txt
    uv run python src/landsat_mosaic/store_source.py --tag v4-provenance
    uv run python src/landsat_mosaic/source_pyramid.py
    uv run python src/landsat_mosaic/pyramid.py --finalize
    uv run python src/landsat_mosaic/export_zarr.py --verify --ref v4-provenance
    uv run python src/landsat_mosaic/gc_store.py --keep v4,v4-provenance
    uv run python src/landsat_mosaic/upload.py --prefix ACCOUNT/PRODUCT README.md LICENSE data/pyramid_v1:pyramid_v1 supplemental data/newengland.icechunk:build.icechunk

That is the shape of it. The build as it actually ran was four passes
over the store (docs/08-builds.md), and the second batch line above needs
the job list the first pass produced. Everything runs on one machine:
reads stream from the Planetary Computer, the medoid is computed locally,
the store and the pyramid live in `data/` (about 130 GB each), and the
upload is the last step. The first pass took about two days, the Landsat
7 rebuilds 24 to 30 hours each.

`data/` is the working directory and is not tracked, except for the
small inputs the scripts read: the 379 ladder tile-years
(`l7fill_jobs*.txt`), the hold-out windows, the corrupt scene ids, and
the v3 stripe scores the validators compare against.

## Docs

    docs/01-grid.md               the grid and why EPSG:4326 at 0.00025 degrees
    docs/02-compositing.md        window, mask, clear-count floor, the medoid, and what the trials showed
    docs/03-landsat7-striping.md  the SLC-off diagnosis, the fill rule, the ladder, two more stripe sources
    docs/04-provenance.md         the source pass, the planes, and the hold-out cost of borrowing a year
    docs/05-pyramid.md            levels, chunking, sharding, the numbers the layout came from
    docs/06-qa.md                 level 0 QA, the stripe score, mid-build validation
    docs/07-store-and-release.md  Icechunk tags, garbage collection, Source Coop upload and read-back
    docs/08-builds.md             the four builds and the source pass, with dates, commands and chain scripts
    docs/09-new-year.md           how to append a year
    docs/10-scripts.md            every script, what it reads, what it writes

`mosaic-viewer.py` is the marimo map the builds were inspected in: one
year at a time from the local pyramid, with the provenance planes as
modes and a click readout of the source pixel. `uv sync --group viewer`
then `uv run marimo edit mosaic-viewer.py`.

`stats/` holds the measurements the docs cite, as the scripts wrote them.
`chains/` holds the unattended shell chains that ran the rebuilds
overnight. `supplemental/` holds the README and two of the three vector
layers that ship beside the store, with the scripts that build them in
`src/landsat_mosaic/`.

## Credit

Built from USGS Landsat Collection 2 Level-2 Tier 1 surface reflectance,
read through the Microsoft Planetary Computer's free archive and its
stac-geoparquet catalog. The supplemental layers credit the Census
Bureau, the USGS National Hydrography Dataset, and Harvard Forest's
Wildlands of New England (HF435, CC0) in `supplemental/README.md`.
