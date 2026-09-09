"""Build the job list: which scenes feed which (tile, year).

DuckDB over the MPC stac-geoparquet catalog. One row per (tile, year, scene)
for land tiles, years 2000 to 2025, May 1 to October 31, Tier 1, land cloud
cover under CC_MAX, scene footprint intersecting the tile. The month column
lets the runner prefer June to September and fall back to May and October.

Usage: uv run python src/landsat_mosaic/planner.py
Writes data/jobs.parquet.
"""
import sys
from pathlib import Path

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from landsat_mosaic import grid

CC_MAX = 80
MONTHS = (5, 10)
YEARS = (2000, 2025)
ASSETS = ["blue", "green", "red", "nir08", "swir16", "swir22", "qa_pixel"]
JOBS_PATH = grid.ROOT / "data/jobs.parquet"


def main() -> None:
    tiles = pd.read_parquet(grid.TILES_PATH)
    tiles = tiles[tiles.land]
    con = duckdb.connect()
    con.execute("load spatial;")
    con.register("tiles", tiles[["tile", "west", "south", "east", "north"]])
    hrefs = ", ".join(f'assets.{a}.href AS href_{a}' for a in ASSETS)
    df = con.sql(f"""
        with scenes as (
          select id, datetime, year(datetime) y, month(datetime) m, platform,
            "landsat:wrs_path" path, "landsat:wrs_row" wrs_row,
            "landsat:cloud_cover_land" cc_land, "proj:epsg" epsg, geometry, {hrefs}
          from '{grid.ROOT}/data/catalog/landsat-c2-l2_*.parquet'
          where "landsat:collection_category" = 'T1'
            and year(datetime) between {YEARS[0]} and {YEARS[1]}
            and month(datetime) between {MONTHS[0]} and {MONTHS[1]}
            and "landsat:cloud_cover_land" < {CC_MAX}
            and bbox.xmax > {grid.WEST} and bbox.xmin < {grid.EAST}
            and bbox.ymax > {grid.SOUTH} and bbox.ymin < {grid.NORTH}
        )
        select t.tile, s.y as year, s.id, s.datetime, s.m as month, s.platform,
          s.path, s.wrs_row, s.cc_land, s.epsg,
          {", ".join(f"s.href_{a}" for a in ASSETS)}
        from scenes s join tiles t
          on ST_Intersects(s.geometry, ST_MakeEnvelope(t.west, t.south, t.east, t.north))
        order by t.tile, s.y, s.datetime
    """).df()
    df.to_parquet(JOBS_PATH, index=False)

    print(f"{len(df):,} (tile, year, scene) rows, {df.id.nunique():,} distinct scenes")
    jobs = df.groupby(["tile", "year"]).agg(
        scenes=("id", "count"),
        js=("month", lambda m: m.between(6, 9).sum()),
    )
    print(f"{len(jobs)} (tile, year) jobs")
    print("\nscenes per job, June-Sept only, by year (min / median / max over tiles):")
    print(jobs.groupby("year").js.agg(["min", "median", "max"]).T.astype(int).to_string())
    print("\nscenes per job, May-Oct, by year:")
    print(jobs.groupby("year").scenes.agg(["min", "median", "max"]).T.astype(int).to_string())
    print("\nthinnest jobs (May-Oct):")
    print(jobs.nsmallest(10, "scenes").to_string())


if __name__ == "__main__":
    main()
