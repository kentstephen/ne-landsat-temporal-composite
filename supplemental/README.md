# Supplemental data

Three layers over the mosaic. `tiger_states.parquet` is the six-state
footprint for clipping the mosaic, which is not masked in the store.
`nhd_water_bodies.parquet` is the inland and near-shore water, for masking the CTrees
fold. **`hf435_wildlands.parquet` is the one the viewers draw.**

An earlier overlay, a build of Harvard Forest's New England Protected Open
Space v1.1 (Zenodo 4688018), was replaced by the wildlands layer on
2026-09-06 and removed from the bucket on 2026-09-09. It compiled every
kind of conserved land on the same footing, town commons and cemeteries
alongside Baxter State Park, with a median parcel of 2.7 ha and no land
cover attribute to filter on. `src/landsat_mosaic/supplemental.py` still
builds it from the Zenodo source if it is wanted.

## hf435_wildlands.parquet (in use)

The 2022 Wildlands of New England: conserved land that by design lets
natural processes run, with no active management. Drawn over the mosaic as
boundaries.

Source: **Wildlands of New England GIS Data 1900-2022 (HF435)**, Harvard
Forest Data Archive. Foster, D., Johnson, E., Hall, B. (2023). License CC0.
Dataset page:
https://harvardforest1.fas.harvard.edu/exist/apps/datasets/showData.html?id=HF435

Credit the original when reusing this file:
Foster D, Johnson E, Hall B. 2023. Wildlands of New England GIS Data
1900-2022. Harvard Forest Data Archive: HF435.

This is the strict end of the conservation funnel. The project screened
over 600 candidate sites against three criteria (wildland intent,
management for natural process, permanent protection) and kept 426. That
compiles to 1,321,878 acres, 3.3% of New England. Median property is 302
acres, and the list is Baxter State Park, the White Mountain and Green
Mountain wildernesses, Acadia, and the TNC reserves rather than town
commons and ballfields.

By state: ME 85 properties / 722,838 ac, NH 72 / 233,166, VT 157 / 221,307,
MA 57 / 115,933, CT 54 / 28,614, RI 1 / 21.

What changed from the source (see src/landsat_mosaic/wildlands.py):

- Reprojected from EPSG:5070 Albers to EPSG:4326.
- Geometry simplified with tolerance 0.0001 degrees, about 10 m. 628,263
  vertices down to 105,447. Adjacent polygons were simplified
  independently, so shared edges can show small slivers. Do not use this
  file for area calculations; AcresGIS is the source's own figure.
- Dropped OBJECTID.
- Written as GeoParquet with a covering bbox column. At 426 rows it is a
  single read; the bbox column is there for readers that want it.

426 polygons (231 MultiPolygon, 195 Polygon), 1.55 MB. Columns: PropID,
PropName, FeeOwner, OwnerType1, OwnerType2, OwnerType3, YearOrig (-999 when
unknown, 43 rows), W_Deed, W_Federal, W_State, W_MgmtPlan, W_Other,
ProtDocs, ThrdPartYN, ThrdPartID, MgmtPlanTy, State, AcresGIS, geometry.

The snapshot is 2022. Land made Wildland since then is not included.

## tiger_states.parquet (in use)

The six New England states from Census TIGER/Line 2024, for clipping the
mosaic to New England in notebooks and viewers. The store itself is not
masked: tiles are whole 4096 px squares and keep NY, Quebec, New Brunswick
and the ocean. Clip with this layer where the six-state footprint is wanted.

Source: **TIGER/Line Shapefiles 2024, State**, U.S. Census Bureau. Public
domain (U.S. government work).
https://www2.census.gov/geo/tiger/TIGER2024/STATE/tl_2024_us_state.zip

Why raw TIGER and not the cartographic boundary file or Overture (compared
2026-09-07): raw TIGER runs to the 3 nautical mile limit, so bays, harbors
and the near-shore band are inside and only open ocean is cut. The 500k
cartographic file is shoreline-clipped but generalized by about 100 m,
several pixels. Overture division areas for the states are triangulated at
the coast and miss about 1,300 km2 of land; their land plus maritime union
is the same 3 nmi envelope as TIGER but with a jagged internal seam.

What changed from the source (see src/landsat_mosaic/state_boundary.py):

- Six states selected, reprojected EPSG:4269 to EPSG:4326.
- Kept GEOID, STUSPS, NAME, ALAND, AWATER (lowercased). Dropped the rest.
- A seventh row, kind = "new_england", is the six dissolved into one
  MultiPolygon (2 parts, 0 holes, 186,447 km2 in EPSG:5070). aland and
  awater on that row are the sums.
- Not simplified. 22,240 vertices in the dissolved row; simplifying would
  move the inland borders by up to the tolerance.
- Written as GeoParquet with a covering bbox column.

7 rows, 1.1 MB. Columns: kind ("state" or "new_england"), geoid, stusps,
name, aland, awater, geometry.

## nhd_water_bodies.parquet (in use, not in git)

**Not tracked in this repo**: 18.7 MB. Build it once with
`uv run python src/landsat_mosaic/water.py`; the viewer says so in its
status line when the file is missing and runs without the mask until then.

Lakes, ponds, reservoirs, estuaries, bays, the near-shore ocean and the
wide-river polygons of the six states, from the USGS National Hydrography
Dataset High Resolution, for masking water out of the CTrees hexagon fold
in `pyramid-ctrees-pair.py`.

Why this file: CTrees carries a biomass value over open water (zero, or
near it, with an uncertainty), not a nodata flag. Without a mask the fold
draws hexagons over lakes, and a hexagon on the shore averages lake zeros
in with its forest and reads as a low-biomass halo round the water. With
it, water leaves the fold at the pixel level: no hexagon over open water,
and a shore hexagon is the mean of its land pixels. The viewer's WATER MASK
toggle (`m`) is this layer. It is never drawn.

Source: **NHD High Resolution, per-HU4 GeoPackages**, U.S. Geological
Survey, staged at
https://prd-tnm.s3.amazonaws.com/StagedProducts/Hydrography/NHD/HU4/GPKG/
(vintage December 2023). Public domain (U.S. government work). Twelve
subregions: 0101 to 0110 (all of hydrologic region 01, New England), 0430
(Lake Champlain, the western half of Vermont) and 0202 (the Hudson, for
the Batten Kill, Hoosic and Walloomsac corners of VT and MA).

What changed from the source (see src/landsat_mosaic/water.py):

- Kept NHDWaterbody LakePond (ftype 390), Reservoir (436) and Estuary
  (493), and NHDArea SeaOcean (445), BayInlet (312) and StreamRiver (460).
  SwampMarsh (466) is left out: it is vegetated and has biomass.
- Dropped features under 0.01 km2 (1 ha, one CTrees pixel). In HU4 0104
  that is 3,321 lake/pond polygons down to 744, and almost none of the
  area; a sub-pixel pond cannot be masked at 100 m anyway.
- Read with a bbox on the six-state envelope, then kept only features that
  intersect the dissolved state boundary, so 0430 and 0202 contribute
  their Vermont and Massachusetts parts and not New York.
- Z dropped, reprojected EPSG:4269 to EPSG:4326.
- Simplified with tolerance 0.0001 degrees, about 10 m. Do not use this
  file for area; `area_km2` is the source's own figure.
- Deduplicated on `permanent_identifier` across subregion overlaps.
- Written as GeoParquet with a covering bbox column, rows sorted along a
  Hilbert curve in row groups of 2048, so readers can prune by bbox.

19,379 polygons (3 MultiPolygon), 46,604 km2, 18.7 MB. Of that, 17,080
LakePond (7,120 km2), 255 Reservoir (36 km2), 89 Estuary (3,926 km2, Long
Island Sound the largest), 16 SeaOcean (29,931 km2, the band inside the
TIGER 3 nmi limit), 4 BayInlet (4,327 km2, the Bay of Fundy) and 1,935
StreamRiver (1,265 km2). Columns: nhd_id, name (GNIS, null for most
ponds), ftype, fcode, area_km2, layer ("waterbody" or "area"), huc4,
geometry.

The 1 GB of source zips are cached in data/supplemental/nhd/ (gitignored)
so a rebuild skips the download.
