# The grid

One grid for the whole region, in EPSG:4326, 0.00025 degree pixels,
north-up, origin at the north-west corner of the bounding box.

    bbox      -73.75, 40.95, -66.85, 47.5   (west, south, east, north)
    size      26200 rows x 27600 columns
    pixel     0.00025 deg: 27.8 m north-south, about 20 m east-west at 43.5N
    tiles     7 x 7 blocks of 4096 x 4096 px, ids r00c00 .. r06c06 from the north-west
    land      33 of 49 tiles touch the six states; 308 million land pixels

`grid.py` holds the constants (`RES`, `TILE`, `WEST`, `NORTH`, `HEIGHT`,
`WIDTH`, `TRANSFORM`) and writes `data/grid/tiles.parquet` (tile bounds
and land pixel counts) and `data/grid/state_mask.tif` (1 inside CT, RI,
MA, VT, NH, ME, from the Census cartographic boundary file, which is
shoreline-clipped). The mask decides which tiles are built; it does not
mask the pixels. The store keeps whole tiles, so New York, Quebec, New
Brunswick and the ocean are in it, and `supplemental/state_boundary.parquet`
is the clip for anyone who wants the six-state footprint.

## Why EPSG:4326

The composites are meant to be read from a browser and from any Zarr
reader without a reprojection step, and to sit in one array for the whole
region. Geographic coordinates give one grid across four UTM zones, a
transform that is two numbers, and coordinates a reader can compute
without a projection library. The multiscales, spatial and proj
conventions in the pyramid describe it as such (docs/05).

The cost is anisotropy. At 43.5N a 0.00025 degree pixel is 27.8 m tall
and about 20 m wide, so each 30 m source pixel covers about 1.1 by 1.5
grid pixels. The runner resamples nearest onto the grid (odc-stac, the
default), which means roughly one column in three duplicates its
neighbour east-west and one row in twelve north-south. Measured on the
finished store: 33 to 40 percent of east-west neighbours are identical,
8 to 17 percent of north-south neighbours, the nearest-neighbour
fingerprint. There is no resolution beyond 30 m in the data, and a
bilinear rebuild would only smooth the staircase. A reader who wants a
true 30 m projected grid should reproject with an averaging resampler.

## Tiles and shards

The tile size is the shard size. The Icechunk store and every sharded
pyramid level use 1 x 4096 x 4096 shards of 1 x 512 x 512 chunks, so one
(tile, year) job writes exactly one shard per array and no two jobs touch
the same object. That is what makes the build resumable per tile-year,
lets `export_zarr.py` copy shard by shard, and lets parallel workers in
`export_zarr.py`, `pyramid.py` and `source_pass.py` write without
coordination. 4096 is 2^12, so a 2^k block at any pyramid level up to
k = 7 never crosses a tile.

The grid edges (26200 and 27600) are not multiples of 4096 or 512, so
the last tile row and column are partial (r06 tiles are 1624 rows tall,
c06 tiles 3024 columns wide) and the last chunk at every level is
partial. Sizes are ceil() everywhere; `validate_tile.py` learned this the
hard way on r06c03.

## Sub-tiles

`runner.py` loads and reduces a 4096 tile as four 2048 x 2048 sub-tiles
(`--only K` runs one), because a full tile's stack of 30 to 100 looks
times seven bands does not fit comfortably beside a second job. The four
sub-tiles are assembled and written as one region in one commit.
