"""Build supplemental/state_boundary.parquet.

Source: Census Bureau TIGER/Line 2024 state boundaries (tl_2024_us_state),
public domain. The raw TIGER polygons, not the cartographic boundary file:
they run to the 3 nautical mile limit, so bays, harbors and the near-shore
band are inside the boundary. The inland edges (NY, Quebec, New Brunswick)
are the legal state lines at full TIGER resolution.

Why this file: the mosaic store is not masked to the states (the tiles are
whole 4096 px squares and keep NY, Quebec, New Brunswick and the ocean). A
notebook or viewer that wants the six-state footprint clips with this
layer instead. Compared against the Census 500k cartographic file and
Overture division areas on 2026-09-07: Overture's land polygons are
triangulated and unusable at 30 m; Overture land plus maritime and TIGER
raw are the same 3 nmi envelope to within a few hundred km2, and TIGER has
no internal seam.

Seven rows: one per state (kind = "state") and the six dissolved into one
MultiPolygon (kind = "new_england"). Geometry is not simplified: the
whole file is about 22k vertices, and simplifying would move the inland
borders by up to the tolerance. Dissolved in EPSG:4326 (TIGER shared edges
are topologically consistent, so the union closes without slivers).

Usage: uv run python src/landsat_mosaic/state_boundary.py
The 9 MB zip is cached in data/supplemental/ so reruns skip the download.
"""
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[2]
TIGER_URL = "https://www2.census.gov/geo/tiger/TIGER2024/STATE/tl_2024_us_state.zip"
ZIP = ROOT / "data/supplemental/tl_2024_us_state.zip"
OUT = ROOT / "supplemental/state_boundary.parquet"
FIPS = {"09": "CT", "23": "ME", "25": "MA", "33": "NH", "44": "RI", "50": "VT"}
KEEP = ["GEOID", "STUSPS", "NAME", "ALAND", "AWATER", "geometry"]


def fetch() -> Path:
    if not ZIP.exists():
        ZIP.parent.mkdir(parents=True, exist_ok=True)
        ZIP.write_bytes(requests.get(TIGER_URL, timeout=600).content)
    return ZIP


def build() -> gpd.GeoDataFrame:
    g = gpd.read_file(f"/vsizip/{fetch()}")
    g = g[g.STATEFP.isin(FIPS)][KEEP].to_crs(4326)
    g = g.rename(columns=str.lower).sort_values("stusps").reset_index(drop=True)
    g.insert(0, "kind", "state")
    ne = gpd.GeoDataFrame(
        {"kind": ["new_england"], "geoid": [None], "stusps": [None],
         "name": ["New England"], "aland": [g.aland.sum()], "awater": [g.awater.sum()]},
        geometry=[g.union_all()], crs=4326)
    out = gpd.GeoDataFrame(pd.concat([g, ne], ignore_index=True), crs=4326)
    out["geometry"] = out.geometry.make_valid()
    return out


if __name__ == "__main__":
    g = build()
    OUT.parent.mkdir(exist_ok=True)
    g.to_parquet(OUT, compression="zstd", geometry_encoding="WKB",
                 write_covering_bbox=True)
    ne = g[g.kind == "new_england"].geometry.iloc[0]
    nv = sum(len(p.exterior.coords) + sum(len(r.coords) for r in p.interiors) for p in ne.geoms)
    print(len(g), "rows;", ne.geom_type, "with", len(ne.geoms), "parts,", nv, "vertices,",
          len([r for p in ne.geoms for r in p.interiors]), "holes;",
          round(g.to_crs(5070).area.iloc[-1] / 1e6), "km2;",
          round(OUT.stat().st_size / 1e6, 2), "MB")
