"""Build supplemental/nhd_water_bodies.parquet.

Source: USGS National Hydrography Dataset, High Resolution (NHD HR), the
per-HU4 GeoPackages staged on prd-tnm.s3.amazonaws.com. Public domain (U.S.
government work). Twelve subregions cover the six states: 0101 to 0110 are
all of hydrologic region 01 (New England), 0430 is Lake Champlain for the
western half of Vermont, and 0202 is the Hudson for the Batten Kill, Hoosic
and Walloomsac corners of VT and MA.

Why this file: CTrees carries a biomass value over open water (zero, or near
it, with an uncertainty), not a nodata flag, so the hexagon fold in
pyramid-ctrees-pair.py averages lake pixels in with shore forest and draws
hexagons over lakes. This layer masks water at the pixel level before the
per-cell mean, the way tiger_states.parquet masks NY, Quebec and the sea.

What is kept: NHDWaterbody LakePond (390), Reservoir (436) and Estuary
(493), and NHDArea SeaOcean (445), BayInlet (312) and StreamRiver (460, the
wide-river polygons). SwampMarsh (466) is left out: it is vegetated and has
biomass. Features under 0.01 km2 (1 ha, one CTrees pixel) are dropped;
they cannot be masked at 100 m anyway and they are most of the row count
and almost none of the area.

What changed from the source:

- Read per HU4 with a bbox on the six-state envelope, then kept only
  features that intersect the dissolved state boundary, so 0430 and 0202
  contribute their Vermont and Massachusetts parts and not New York.
- Z dropped, reprojected EPSG:4269 to EPSG:4326.
- Simplified to 0.0001 deg (about 10 m). Do not use for area; area_km2 is
  the source's own figure.
- Deduplicated on permanent_identifier across subregion overlaps.
- Written as GeoParquet with a covering bbox column, rows sorted along a
  Hilbert curve in row groups of 2048 so readers can prune by bbox.

Usage: uv run python src/landsat_mosaic/water.py
The zips (about 1 GB) are cached in data/supplemental/nhd/ so reruns skip
the download.
"""
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests
import shapely

ROOT = Path(__file__).resolve().parents[2]
BASE = "https://prd-tnm.s3.amazonaws.com/StagedProducts/Hydrography/NHD/HU4/GPKG/"
CACHE = ROOT / "data/supplemental/nhd"
OUT = ROOT / "supplemental/nhd_water_bodies.parquet"
STATES = ROOT / "supplemental/tiger_states.parquet"
HU4S = [f"01{i:02d}" for i in range(1, 11)] + ["0202", "0430"]
# layer -> the feature types kept
KEEP = {"NHDWaterbody": (390, 436, 493), "NHDArea": (445, 312, 460)}
MIN_KM2 = 0.01
TOLERANCE = 0.0001
COLS = ["permanent_identifier", "gnis_name", "ftype", "fcode", "areasqkm", "geometry"]


def fetch(hu4: str) -> Path:
    z = CACHE / f"NHD_H_{hu4}_HU4_GPKG.zip"
    if not z.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        with requests.get(BASE + z.name, stream=True, timeout=1800) as r:
            r.raise_for_status()
            tmp = z.with_suffix(".part")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 22):
                    f.write(chunk)
            tmp.rename(z)
    return z


def build() -> gpd.GeoDataFrame:
    sb = gpd.read_parquet(STATES)
    ne = sb[sb.kind == "new_england"].geometry.iloc[0]
    bbox = tuple(sb.total_bounds)
    parts = []
    for hu4 in HU4S:
        z = fetch(hu4)
        path = f"/vsizip/{z}/{z.stem}.gpkg"
        for layer, ftypes in KEEP.items():
            g = gpd.read_file(path, layer=layer, bbox=bbox, columns=COLS[:-1], use_arrow=True)
            g = g[g.ftype.isin(ftypes) & (g.areasqkm >= MIN_KM2)]
            if g.empty:
                continue
            g = g.to_crs(4326)
            g = g[g.intersects(ne)].copy()
            g["layer"] = "waterbody" if layer == "NHDWaterbody" else "area"
            g["huc4"] = hu4
            parts.append(g)
            print(f"{hu4} {layer}: {len(g):,} kept")
    out = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=4326)
    out = out.drop_duplicates("permanent_identifier")
    out = out.rename(columns={"permanent_identifier": "nhd_id", "gnis_name": "name", "areasqkm": "area_km2"})
    out["geometry"] = shapely.force_2d(out.geometry.simplify(TOLERANCE)).make_valid()
    out = out.iloc[out.geometry.hilbert_distance().argsort()].reset_index(drop=True)
    return out[["nhd_id", "name", "ftype", "fcode", "area_km2", "layer", "huc4", "geometry"]]


if __name__ == "__main__":
    g = build()
    OUT.parent.mkdir(exist_ok=True)
    g.to_parquet(OUT, compression="zstd", geometry_encoding="WKB",
                 write_covering_bbox=True, row_group_size=2048)
    print(len(g), "rows;", round(g.area_km2.sum()), "km2;", round(OUT.stat().st_size / 1e6, 2), "MB")
    print(g.groupby(["layer", "ftype"]).agg(n=("ftype", "size"), km2=("area_km2", "sum")).round(1))
    print(g.geom_type.value_counts().to_dict())
