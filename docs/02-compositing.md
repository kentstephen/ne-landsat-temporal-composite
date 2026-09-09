# The compositing rule

One tile-year at a time, `runner.py` rebuilds the STAC items for the
job's scenes from the local catalog, loads every scene onto the grid with
odc-stac (URLs signed at read time, so a job longer than the 45 minute
SAS token survives), masks with QA_PIXEL, decides per pixel which looks
count, reduces over time, and writes the tile region into the Icechunk
store as one commit. Jobs are idempotent: a tile-year whose clear_count
region is already nonzero is skipped unless `--force`.

## Inputs

Landsat Collection 2 Level-2 Tier 1 surface reflectance, Landsat 5, 7, 8
and 9, from the Planetary Computer's `landsat-c2-l2` collection. The
planner (`planner.py`) takes, per land tile and year, every Tier 1 scene
whose footprint intersects the tile, acquired May 1 to October 31, with
land cloud cover under 80 percent. That is 858 (tile, year) jobs over
10,868 distinct scenes, 33 to 70 scenes per job in June to September
because a tile spans several WRS path/rows. A cloud cut of 90 instead of
80 moved the expected clear looks by 0.2 to 0.5, so 80 stayed.

Bands: blue, green, red, nir08, swir16, swir22 (sensor names mapped to
the OLI names blue, green, red, nir, swir1, swir2) and qa_pixel. Kept as
uint16 digital numbers in the Collection 2 scaling, reflectance =
DN * 0.0000275 - 0.2, 0 as nodata. No cross-sensor harmonisation; the
2013 sensor step is a documented property of the product.

Corrupt assets: the 2022 scenes on the Planetary Computer included
header-only blobs, truncated blobs and a QA_PIXEL with no CRS.
`scan_assets.py` finds them with two range requests per asset; the ids
go in `data/bad_scenes.txt` (33 lines) and `runner.bad_scenes()` drops
them per job. 2023 to 2025 scanned clean.

## Loading the stack

One time slot per (solar day, platform), merged by hand. odc-stac's own
`groupby="solar_day"` fuse is first-scene-wins with a nodata check, and
for qa_pixel the fill outside a scene footprint comes back as 0 or 1, so
where two scenes of one path overlap a sub-tile the first scene's fill
shadowed the second scene's real QA and cloud read as clear. It showed
as a 10 km white patch with seven clear looks. The runner loads one slot
per scene and merges on blue != 0.

The platform is part of the slot key because from 2022 Landsat 7 and 9
image the same tile on the same day (Landsat 7's orbit was lowered and
it drifted to a 09:55 pass; 60 tile-years, 199 tile-days). Merged into
one slot, the SLC-off gaps sat inside slots labelled landsat-9 and the
Landsat 7 rule could not see them.

Landsat 5 slots have their data-present mask eroded by 25 rows before
anything else (`erode_l5_edges`, docs/03).

## The mask

A look is clear where QA_PIXEL has none of fill, dilated cloud, cirrus,
cloud, cloud shadow set, and blue is nonzero. Snow is not masked; the
window is summer. A look counts only if every band is valid, which is
why the finished store has no pixel with clear_count > 0 and a band at
0 (the USGS "nodata at cloud edges the QA band does not flag" case does
not occur in the composite).

## The window

June 1 to September 30 is leaf-on everywhere in the region. Per pixel,
where the June to September looks reach `min_clear` (4), only those
count; otherwise the May 1 to October 31 looks count. This is the
`fallback` window; `js` and `mo` exist as flags for trials. The
catalog-only estimate (`clear_counts.py`, `stats/clear_counts.png`)
showed why: June to September alone leaves 5 or 6 of the 24 path/rows
under four expected looks in 2000 and 2003, May to October brings every
year but 2012 to medians of 6 to 8, and April in northern New England is
leaf-off, so windows wider than May to October buy looks with the wrong
phenology.

## Which looks count

With the mask and the window applied, the Landsat 7 ladder decides per
pixel which looks enter the reducer (`valid_obs`, docs/03). In years
with no Landsat 7 every clear look counts. 2012 has Landsat 7 alone
(Landsat 5 had stopped, Landsat 8 had not launched), so `YEAR_SPAN`
makes 2011 to 2013 its own years and its stack pools three summers.

## The reducer

Medoid: per pixel, the median of each band over the valid looks, then
the look whose six-band vector is nearest that median in squared
distance. The stored pixel is that look's six bands unaltered, so every
pixel is one real observation, never a blend of dates, and `source_pass.py`
can find it again afterwards by equality (docs/04). `clear_count` is the
number of valid looks the pixel drew on.

Median was the alternative. On the trial tiles (r03c02 2020, r05c01
2012) the two were within 0.003 to 0.006 reflectance on average and
visually identical, and medoid cost about two seconds more per sub-tile.
Medoid was chosen for band consistency. When striping appeared in the
Landsat 7 years the median was tried as the fix (`compare_reducers.py`,
`stats/reducer_compare/`): it removed most of the visible tone striping
and dropped forest high-pass texture 8 to 10 percent, but it blends
dates and the count striping was unchanged by construction, and the
Landsat 7 fill rule turned out to fix the cause. The store is medoid in
every year, recorded as `reducer_by_year` in the attrs. The v2 tag
(median for 2003 to 2012) was built and then discarded (docs/08).

## Throughput

Per 4096 tile, 2020: 3.8 minutes (four sub-tiles, load 26 to 31 s each
at pool 32, reduce 26 to 29 s). GDAL environment settings (EMPTY_DIR,
merged ranges, multiplexing, VSI cache, 1 MB chunks) halved the load
time. The ladder jobs that carry neighbour-year scenes take 4 to 7
minutes. Reads from the Planetary Computer's West Europe storage account
ran at about 42 MB/s over 16 streams in the probe and held up over the
multi-day runs; the only job failures were "got 0 bytes" reads during
home internet drops, retried by `batch.py` or rerun afterwards.

`batch.py` is sequential, one process, because the local Icechunk store
is not safe for concurrent commits. A multi-day run goes under
`caffeinate -i` and `nohup`, holds about 20 GB RSS, retries a failed job
with backoff and then logs it to `data/failed.txt` and moves on.
`--skip-log` restarts a `--force` run without redoing the finished jobs
and `--jobs FILE` runs a targeted list.
