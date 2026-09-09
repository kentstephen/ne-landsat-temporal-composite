"""Target grid, tile scheme, and six-state land mask.

Grid: EPSG:4326, 0.00025 degree pixels, north-up, origin at the north-west
corner of the New England bbox. Tiles are 4096 x 4096 pixel blocks aligned
to the Icechunk shards, ids like r03c05 (row, col from the north-west).

Usage: uv run python src/landsat_mosaic/grid.py
Writes data/grid/tiles.parquet (tile bounds, land pixel counts) and
data/grid/state_mask.tif (uint8, 1 = inside CT/RI/MA/VT/NH/ME).
"""
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio import features
from rasterio.transform import from_origin

ROOT = Path(__file__).resolve().parents[2]
RES = 0.00025
WEST, NORTH = -73.75, 47.5
HEIGHT, WIDTH = 26200, 27600           # rows, cols
SOUTH, EAST = NORTH - HEIGHT * RES, WEST + WIDTH * RES   # 40.95, -66.85
TILE = 4096
NTILE_Y, NTILE_X = -(-HEIGHT // TILE), -(-WIDTH // TILE)   # 7, 7
TRANSFORM = from_origin(WEST, NORTH, RES, RES)
STATES = ["09", "44", "25", "50", "33", "23"]              # CT RI MA VT NH ME
CB_URL = "https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_us_state_500k.zip"
MASK_PATH = ROOT / "data/grid/state_mask.tif"
TILES_PATH = ROOT / "data/grid/tiles.parquet"


def tile_id(ty: int, tx: int) -> str:
    return f"r{ty:02d}c{tx:02d}"


def tile_slices(ty: int, tx: int) -> tuple[slice, slice]:
    """(y, x) pixel slices for a tile, clipped to the grid."""
    return (slice(ty * TILE, min((ty + 1) * TILE, HEIGHT)),
            slice(tx * TILE, min((tx + 1) * TILE, WIDTH)))


def tile_bounds(ty: int, tx: int) -> tuple[float, float, float, float]:
    """(west, south, east, north) in degrees."""
    ys, xs = tile_slices(ty, tx)
    return (WEST + xs.start * RES, NORTH - ys.stop * RES,
            WEST + xs.stop * RES, NORTH - ys.start * RES)


def x_coords() -> np.ndarray:
    return WEST + (np.arange(WIDTH) + 0.5) * RES


def y_coords() -> np.ndarray:
    return NORTH - (np.arange(HEIGHT) + 0.5) * RES


def load_states() -> gpd.GeoDataFrame:
    cache = ROOT / "data/grid/states.gpkg"
    if cache.exists():
        return gpd.read_file(cache)
    g = gpd.read_file(CB_URL)
    g = g[g.STATEFP.isin(STATES)].to_crs(4326)
    g.to_file(cache, driver="GPKG")
    return g


def build_mask() -> np.ndarray:
    if MASK_PATH.exists():
        with rasterio.open(MASK_PATH) as src:
            return src.read(1)
    states = load_states()
    mask = features.rasterize(
        ((geom, 1) for geom in states.geometry),
        out_shape=(HEIGHT, WIDTH), transform=TRANSFORM, fill=0, dtype="uint8",
        all_touched=False,
    )
    with rasterio.open(
        MASK_PATH, "w", driver="GTiff", height=HEIGHT, width=WIDTH, count=1,
        dtype="uint8", crs="EPSG:4326", transform=TRANSFORM,
        compress="deflate", tiled=True, blockxsize=512, blockysize=512,
    ) as dst:
        dst.write(mask, 1)
    return mask


def build_tiles(mask: np.ndarray) -> pd.DataFrame:
    rows = []
    for ty in range(NTILE_Y):
        for tx in range(NTILE_X):
            ys, xs = tile_slices(ty, tx)
            w, s, e, n = tile_bounds(ty, tx)
            land = int(mask[ys, xs].sum())
            rows.append(dict(tile=tile_id(ty, tx), ty=ty, tx=tx,
                             y0=ys.start, y1=ys.stop, x0=xs.start, x1=xs.stop,
                             west=w, south=s, east=e, north=n,
                             land_px=land, land=land > 0))
    df = pd.DataFrame(rows)
    df.to_parquet(TILES_PATH, index=False)
    return df


if __name__ == "__main__":
    print(f"grid {HEIGHT} x {WIDTH} px, bbox {WEST} {SOUTH:.5f} {EAST:.5f} {NORTH}")
    mask = build_mask()
    print(f"land pixels {mask.sum():,} of {mask.size:,} ({100*mask.mean():.1f}%)")
    tiles = build_tiles(mask)
    print(f"{tiles.land.sum()} of {len(tiles)} tiles have land")
    print(tiles[["tile", "west", "south", "east", "north", "land_px", "land"]].to_string(index=False))
