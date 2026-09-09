"""Build supplemental/protected_open_space.parquet.

Source: New England Protected Open Space v1.1 (April 2021), Harvard Forest,
Zenodo record 4688018, CC-BY-4.0. Reads the POSv1_1_April2021_min layer of
POS.gdb.zip, reprojects ESRI:102039 to EPSG:4326, simplifies to 0.0001 deg
(about 10 m, a third of a 30 m pixel), drops the Albers-unit duplicates of
Area_Ha, sorts rows along a Hilbert curve so row groups are spatially
compact, and writes GeoParquet with a bbox column.

Usage: uv run python src/landsat_mosaic/supplemental.py
The 111 MB zip is cached in data/supplemental/ so reruns skip the download.
"""
from pathlib import Path

import geopandas as gpd
import requests

ROOT = Path(__file__).resolve().parents[2]
ZENODO_URL = "https://zenodo.org/api/records/4688018/files/POS.gdb.zip/content"
ZIP = ROOT / "data/supplemental/POS.gdb.zip"
OUT = ROOT / "supplemental/protected_open_space.parquet"
LAYER = "POSv1_1_April2021_min"
TOLERANCE = 0.0001
DROP = ["Area_Ac", "Shape_Length", "Shape_Area"]


def fetch() -> Path:
    if not ZIP.exists():
        ZIP.parent.mkdir(parents=True, exist_ok=True)
        ZIP.write_bytes(requests.get(ZENODO_URL, timeout=600).content)
    return ZIP


def build() -> gpd.GeoDataFrame:
    g = gpd.read_file(f"/vsizip/{fetch()}/POS.gdb", layer=LAYER)
    g = g.drop(columns=DROP).to_crs(4326)
    g["geometry"] = g.simplify(TOLERANCE, preserve_topology=True)
    g = g[~g.geometry.is_empty]
    g = g.iloc[g.hilbert_distance().argsort()].reset_index(drop=True)
    return g


if __name__ == "__main__":
    g = build()
    OUT.parent.mkdir(exist_ok=True)
    g.to_parquet(OUT, compression="zstd", geometry_encoding="WKB",
                 write_covering_bbox=True, row_group_size=2048)
    print(len(g), "parcels,", OUT.stat().st_size / 1e6, "MB")
