# The pyramid

The product is plain Zarr v3, not Icechunk: readable by GDAL, QGIS, R,
any xarray, and a browser without icechunk-js, with no manifest fetch
before the first chunk. Icechunk stays the build store, and its gc'd
snapshot is hosted beside the pyramid as the working store for the
yearly append (docs/07). Levels are inside the store as multiscales,
not COG overviews; that was weighed and settled on 2026-09-05.

## Layout

    pyramid_v1/                 zarr v3 root, consolidated metadata here
      leafon/                   multiscales group (zarr-conventions multiscales v0.1, spatial, proj)
        0/  blue green red nir swir1 swir2 clear_count source pick_doy borrowed_pct time y x   30 m
        1/  same, minus source, 13100 x 13800                                                   60 m
        2/   6550 x  6900     120 m
        3/   3275 x  3450     240 m
        4/   1638 x  1725     480 m
        5/    819 x   863     960 m
        6/    410 x   432    1920 m
        7/    205 x   216    3840 m

Chunks [1, 512, 512] at every level; shards [1, 4096, 4096] on levels 0
to 2, plain chunks from level 3; zstd 3; uint16 fill 0 for the bands,
uint8 fill 0 for clear_count, uint8 fill 255 for borrowed_pct and
source, uint16 fill 0 for pick_doy. Scale and offset attrs on every
band at every level, so one client formula covers all levels. The
coordinates of each level are computed from the level 0 transform
(origin fixed, pixel size times 2^N), never by coarsening the coordinate
arrays. Nothing here can change: S3 has no rename, and the prefix,
group name and array names were fixed before the first PUT.

Measured on disk: level 0 91 GB, level 1 29 GB, level 2 7.3 GB, level 3
1.9 GB, then 481 MB, 122 MB, 31 MB and 8.1 MB; 130 GB in all before the
provenance planes.

## How the levels are made

Every level is the exact nodata-aware mean of level 0. A 2^N block
averages only its nonzero pixels; a block with no valid pixel stays 0;
the mean is rounded half up and kept as uint16 DN. A plain mean would
pull in zeros and paint dark halos along coastlines, cloud holes, tile
edges and the 2003 holes. Mean composes and medoid does not, so the
levels are means of the medoid composite and the multiscales
`resampling_method` is "average". Coarse levels of 2003 look filled
where level 0 has holes, because the mean bridges them.

`pyramid.py` works one (year, array) at a time, 26 x 7 = 182 units for
the bands and count. It reads the level 0 plane once (1.45 GB uint16,
5 to 8 s), pads it with zeros to a multiple of 128, and accumulates 2x2
sums and valid counts in uint32 from level to level, so level N is the
exact mean of level 0 without re-reading it and no level is a mean of a
mean: mean = 0 where count = 0, else (2 sum + count) // (2 count). Time
chunk is 1 and the arrays are separate objects, so units never share a
chunk or shard and workers write without coordination. A unit is done
when its level 7 plane has data (written last), so a killed run resumes.
The full build of levels 1 to 7 took 5.5 minutes at six workers, about
10 s per unit.

clear_count is averaged the same way. `count_reducers.py` measured mean,
median, max and min on all 26 years at levels 1 to 5
(`stats/count_reducers.json`): mean and median both hold the level 0
histogram at every level (1-Wasserstein under 0.21 counts at 960 m),
max drifts up and min down by one to 2.6 counts at 960 m, so with one
colour ramp across zooms they brighten or darken the map as you zoom
out. Partly-nodata blocks are under 0.6 percent of valid blocks, so the
valid-only mean and the all-pixel mean are the same reducer in
practice. `--count-reducer median` remains a flag.

`--finalize` writes the multiscales attrs (layers 0 to 7 with scale and
translation), spatial and proj attrs on the group and each level, the
composite provenance from `runner.py` (reducer_by_year, min_clear,
l7_fill_by_year, year_span_by_year, the ladder attrs), scale, offset and
nodata on the bands, and the consolidated metadata at the root, last.
Consolidated metadata is a snapshot of the hierarchy: it is rewritten
after any structural change or readers see the old tree.

`--verify` recomputes every level from level 0 for every unit and
compares each pixel, checks the array definitions and coordinates,
checks that count == 0 if and only if all bands == 0 at every level,
and checks that the consolidated metadata matches a walk of the store.
It ran clean after every refresh, 5.5 minutes.

## Where the chunking came from

Measured on three finished interior tiles for 2016 with
`pyramid_chunk_bytes.py` and tabulated by `pyramid_layout_math.py`
(`stats/pyramid_chunk_bytes.json`).

Compression: zstd 3 on level 0 reflectance is 1.35 to 1.58x, about 1.3
bytes per pixel per band; every coarser level is 1.21 to 1.29x, about
1.6 bytes, because block means compress worse than the source. So the
levels cost 40 percent of level 0, not the third the raw counts say.
Chunk size barely matters for bytes (128 vs 512 within 4 percent).

Band as a dimension: a [6, c, c] chunk compresses the same as six
[c, c] chunks. It would halve the request count of a three-band view
and double its bytes. Bytes are the limit, so per-band arrays. Time
chunk 26 vs 1: same bytes; a map read is one year and the yearly append
writes new objects only, so time chunk 1.

Per screen, one band, level chosen at 1 to 2 source pixels per screen
pixel:

    screen               128 px chunks          256                  512
    laptop 1440x900 @2   9-35 MB, 354-1340 req  10-37 MB, 98-354     12-41 MB, 30-98
    phone 390x844 @3     6-20 MB, 211-782       6-22 MB, 61-211      8-25 MB, 20-61
    4k 3840x2160 @1     14-55 MB, 554-2120     16-57 MB, 151-554    18-63 MB, 44-151

Chunk size moves bytes by at most a third (edge overfetch) and requests
by twelve times. A three-band view is 36 MB or more at any chunk size,
about 7 s at 40 Mbit/s; at 512 the 90 to 300 requests ride under that,
at 256 or 128 the requests become a second wait of their own. So 512 at
every level, the size level 0 already had.

Shards: level 1 in 4096 shards is 11 shards per plane and 2002 objects
of about 26 MB; level 2 is 4 per plane. From level 3 a plane is under
one shard, so plain 512 chunks. The whole pyramid is about 17.7k objects
instead of 126k, same bytes, and the browser path is the one level 0
already needs: one 1 KB index read per shard, cached, then inner-chunk
ranges. The rule: shard while the plane is wider than 4096 px.

Levels: eight. Level 6 is the first that fits one 512 chunk; level 7
costs 182 objects of about 60 KB and gives a reader one chunk per plane
for a world view. Level 0 is Web Mercator zoom 12.5 at 44N, level 7
about 5.5.

## Export

`export_zarr.py` writes level 0 from a tagged Icechunk snapshot into the
pyramid: same grid, chunks, shards, codecs, dtypes, fill values,
coordinates and attrs; only the container changes. Each (tile, year)
shard is read from a read-only session on the tag and assigned into the
target. All-fill tile-years (ocean, unbuilt) are skipped and so are
tile-years already present, so it resumes; `--force` with `--years`
rewrites only the rebuilt years, which is how the refreshes worked.
`--verify` compares the pyramid's level 0 against the snapshot: 20 full
tile-years pixel for pixel, every tile-year by clear_count, and since
v4-provenance the source and pick_doy planes whole. It never runs beside
a `batch.py` that writes what it reads.

`refresh_pyramid.sh build TAG YEARS` is the whole sequence for a
refresh: tag, export with `--force` for the years, export verify,
levels for the years, finalize, full verify. `after_batch.sh` waits for a
running batch to exit, checks its log for the expected job count and
no `failed.txt`, runs the build, scores the result and restarts
`mosaic-viewer.py` on port 2718, headless, and logs and carries on if
it does not come up.

## Readers checked

GDAL 3.13 or later reads sharding, multiscales, spatial and proj and
the consolidated metadata; QGIS via the GeoZarr plugin needs that GDAL;
rioxarray 0.22 reads proj and spatial; zarr-layer, deck.gl-raster,
OpenLayers GeoZarr and STAC Browser read multiscales in the browser.
The zarr-conventions multiscales spec is v0.1 and expected to bump once
before it stabilises; readers ignore what they do not know. No reader
in play needs the legacy ndpyramid schema, so it was not targeted.
