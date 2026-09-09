"""Build supplemental/wildlands.parquet.

Source: Wildlands of New England GIS Data 1900-2022 (HF435), Harvard Forest
Data Archive, CC0. Foster, Johnson and Hall, 2023. The 2022 snapshot of the
426 properties that met all three Wildland criteria: wildland intent,
management for natural process, and permanent protection.

This is the strict end of the funnel, not the protected-open-space compile:
1.32 million acres, 3.3% of New England, median property 302 acres. Baxter,
the White Mountain wildernesses, the TNC reserves. No town parks.

Reads the shapefile out of the zip, reprojects EPSG:5070 Albers to
EPSG:4326, simplifies to 0.0001 deg (about 10 m, a third of a 30 m pixel),
drops OBJECTID, and writes GeoParquet with a covering bbox column. At 426
rows the file is one read; the bbox column is there for readers that want it,
not because a viewer needs to prune.

Usage: uv run python src/landsat_mosaic/wildlands.py
The 6.6 MB zip is cached in data/supplemental/ so reruns skip the download.
"""
from pathlib import Path

import geopandas as gpd
import requests

ROOT = Path(__file__).resolve().parents[2]
HF_URL = ("https://harvardforest.fas.harvard.edu/data/p43/hf435/"
          "hf435-04-New-England-Wildlands-2022-GIS.zip")
ZIP = ROOT / "data/supplemental/hf435-wildlands-2022.zip"
OUT = ROOT / "supplemental/wildlands.parquet"
SHP = "Wildlands_of_New_England_2022_GIS_Polygons.shp"
TOLERANCE = 0.0001
DROP = ["OBJECTID"]


def fetch() -> Path:
    if not ZIP.exists():
        ZIP.parent.mkdir(parents=True, exist_ok=True)
        ZIP.write_bytes(requests.get(HF_URL, timeout=600).content)
    return ZIP


def build() -> gpd.GeoDataFrame:
    g = gpd.read_file(f"/vsizip/{fetch()}/{SHP}")
    g = g.drop(columns=DROP).to_crs(4326)
    g["geometry"] = g.simplify(TOLERANCE, preserve_topology=True)
    g = g[~g.geometry.is_empty].reset_index(drop=True)
    return g


if __name__ == "__main__":
    g = build()
    OUT.parent.mkdir(exist_ok=True)
    g.to_parquet(OUT, compression="zstd", geometry_encoding="WKB",
                 write_covering_bbox=True)
    print(len(g), "wildlands,", round(g.AcresGIS.sum()), "acres,",
          round(OUT.stat().st_size / 1e6, 2), "MB")
