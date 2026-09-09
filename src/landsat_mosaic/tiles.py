"""Web Mercator PNG tiles from the plain Zarr multiscales pyramid.

Reads data/pyramid_v1 (export_zarr.py level 0, pyramid.py levels 1-7).
For a tile at zoom z the level whose pixel is the finest one not smaller
than the tile's pixel is read (1 to 2 source pixels per screen pixel), so
a zoomed-out map costs a few 512 chunks instead of a whole year's level 0.
Nothing is cached but the open store; `refresh()` re-opens it.

Modes: `tc` true colour (red, green, blue), `fcc` false colour (swir2,
nir, red: forest dark green, cleared land bright), `ndvi` (nir - red) /
(nir + red) of the scaled reflectance on the fixed NDVI_LO..NDVI_HI scale
(Carto Emrld, dark low, bright high, as the S2 x CTrees pair), `count`
the clear observations used (viridis, 0 to COUNT_MAX; at levels 1-7 the
value the pyramid holds, the block mean), `borrowed` the share of pixels
whose value is a neighbour-year observation (source_pyramid.py
borrowed_pct, 0 to 100, single-hue blue ramp; 255 unknown is transparent,
and the mode draws nothing until that array exists), `source` the class of
the look each pixel's value came from (source_pyramid.py leafon/0/source:
own year grey, the year before blue, the year after orange, own-year
Landsat 7 purple, unmatched yellow; nodata and not-computed transparent).
The class plane exists at level 0 only, so `source` reads level 0 at any
zoom from SOURCE_MIN_Z up and draws nothing below it. Nodata (all bands 0)
is transparent.

`probe(lon, lat, year)` reads one level 0 pixel: the six bands, clear_count
and, when the pyramid has them, source, pick_doy and borrowed_pct; the
viewer calls it on a click.

Clip: the store is not masked to the states (whole tiles, so NY, Quebec,
New Brunswick and the ocean are in it). `set_clip(geom)` makes pixels whose
centre falls outside `geom` (a shapely polygon in EPSG:4326) transparent;
`clip_to_states()` loads the dissolved six-state row of
supplemental/tiger_states.parquet (raw TIGER, to the 3 nmi limit) and
sets it. Off by default, so the CLI and mosaic-viewer draw the whole tile.

Usage: from landsat_mosaic import tiles; tiles.tile_png(z, x, y, year, "tc")
       uv run python src/landsat_mosaic/tiles.py 7 38 46 2016 --mode tc --out tile.png
"""
import io
import math
import sys
import threading
import time
from pathlib import Path

import numpy as np
import zarr
from PIL import Image
from zarr.storage import LocalStore

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from landsat_mosaic import grid, init_store

STORE = grid.ROOT / "data/pyramid_v1"
GROUP = init_store.GROUP
YEARS = init_store.YEARS
NLEVELS = 8
SCALE, OFFSET = 0.0000275, -0.2
REFL_MAX = 0.3            # reflectance drawn as white at gain 1 (as the quicklooks)
# True colour is a gamma curve, not a linear stretch: leaf-on forest sits at
# 0.02 to 0.04 reflectance, a tenth of REFL_MAX, so linear it draws at 12%
# luminance and only a gain that clips fields and sand can lift it. At 2.0
# forest comes up to about a third with nothing clipped; the gain slider
# still multiplies underneath it. NDVI and count are LUTs and untouched.
GAMMA = 2.0
COUNT_MAX = 20
T = 256                   # tile pixels
MIN_Z, MAX_Z = 4, 13
DONE_LEVEL = 3            # 240 m plane, 512 px per 4096 px tile: cheap tile presence check
MODES = {"tc": ("red", "green", "blue"), "fcc": ("swir2", "nir", "red"), "ndvi": ("nir", "red"),
         "count": ("clear_count",), "borrowed": ("borrowed_pct",), "source": ("source",)}
BANDS = ("red", "green", "blue", "nir", "swir1", "swir2")
BORROWED_NODATA = 255
# the source classes (source_pass.py CLASSES; 255 not computed). The colours
# are categorical and protan-safe: no red, and no pair that differs only in
# the red leg. Own year is quiet grey so the borrowed pixels stand out.
SOURCE_CLASSES = ("own", "before", "after", "l7", "nodata", "unmatched")
SOURCE_LABELS = {"own": "own year", "before": "year before", "after": "year after",
                 "l7": "own-year Landsat 7", "nodata": "nodata", "unmatched": "unmatched"}
SOURCE_HEX = {"own": "#c9c9c4", "before": "#2c7bb6", "after": "#fdae61", "l7": "#7b3294",
              "nodata": None, "unmatched": "#ffff33"}
SOURCE_NODATA = 255
SOURCE_MIN_Z = 10         # level 0 read straight (up to 16 chunks a tile at zoom 10)
# NDVI: one fixed scale, dark low, bright high (healthy vegetation): Carto's
# Emrld reversed, the same stops as s2-ctrees-pair.py
NDVI_LO, NDVI_HI = -0.1, 0.9
EMRLD = ("#074050", "#105965", "#217a79", "#4c9b82", "#6cc08b", "#97e196", "#d3f2a3")
# viridis, 11 stops
VIRIDIS = ("#440154", "#482475", "#414487", "#355f8d", "#2a788e", "#21918c",
           "#22a884", "#44bf70", "#7ad151", "#bddf26", "#fde725")
# borrowed share: single-hue blues (ColorBrewer Blues, light 0% to dark
# 100%), a luminance ramp that survives a protan simulation
BLUES = ("#f7fbff", "#deebf7", "#c6dbef", "#9ecae1", "#6baed6", "#4292c6", "#2171b5", "#08519c", "#08306b")
_R = 6378137.0

_lock = threading.Lock()
_state = {"root": None, "opened": 0.0, "done": {}, "gen": 0}
STATES_PATH = grid.ROOT / "supplemental/tiger_states.parquet"
_clip = {"geom": None, "prep": None, "cache": {}}
_CLIP_CACHE = 4096


def set_clip(geom):
    """Clip rendered tiles to `geom` (shapely (Multi)Polygon, EPSG:4326), or
    None to draw whole tiles again."""
    import shapely

    with _lock:
        _clip["geom"] = geom
        _clip["prep"] = None
        _clip["cache"] = {}
        if geom is not None:
            shapely.prepare(geom)
            _clip["prep"] = geom


def clip_to_states():
    """Clip to the six states dissolved (supplemental/tiger_states.parquet,
    kind = new_england). Returns the polygon."""
    import geopandas as gpd

    g = gpd.read_parquet(STATES_PATH)
    geom = g[g.kind == "new_england"].geometry.iloc[0]
    set_clip(geom)
    return geom


def _inside(z, x, y):
    """(T, T) bool, True where the pixel centre is inside the clip; True
    everywhere when no clip is set. Whole-tile answers come from the tile
    box (inside, or disjoint), so the point test runs only on edge tiles,
    and each tile's answer is kept."""
    import shapely

    prep = _clip["prep"]
    if prep is None:
        return np.ones((T, T), bool)
    key = (z, x, y)
    got = _clip["cache"].get(key)
    if got is not None:
        return got
    lon, lat, (W, S, E, N) = _centers(z, x, y)
    box = shapely.box(W, S, E, N)
    if prep.contains(box):
        m = np.ones((T, T), bool)
    elif prep.disjoint(box):
        m = np.zeros((T, T), bool)
    else:
        LON, LAT = np.meshgrid(lon, lat)
        m = shapely.contains_xy(prep, LON.ravel(), LAT.ravel()).reshape(T, T)
    with _lock:
        if len(_clip["cache"]) >= _CLIP_CACHE:
            _clip["cache"].clear()
        _clip["cache"][key] = m
    return m


def _hex(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def _lut(stops, n=256):
    pts = np.array([_hex(s) for s in stops], np.float32)
    xs = np.linspace(0, 1, len(stops))
    t = np.linspace(0, 1, n)
    return np.stack([np.interp(t, xs, pts[:, k]) for k in range(3)], 1).astype(np.uint8)


COUNT_LUT = _lut(VIRIDIS)
NDVI_LUT = _lut(EMRLD)
BORROWED_LUT = _lut(BLUES)
SOURCE_LUT = np.zeros((256, 4), np.uint8)
for _i, _c in enumerate(SOURCE_CLASSES):
    if SOURCE_HEX[_c]:
        SOURCE_LUT[_i] = (*_hex(SOURCE_HEX[_c]), 255)
SOURCE_LEGEND = [[c, SOURCE_LABELS[c], SOURCE_HEX[c]] for c in SOURCE_CLASSES if SOURCE_HEX[c]]
BORROWED_HEX = ["#%02x%02x%02x" % tuple(int(v) for v in BORROWED_LUT[i]) for i in range(0, 256, 17)]
NDVI_HEX = ["#%02x%02x%02x" % tuple(int(v) for v in NDVI_LUT[i]) for i in range(0, 256, 17)]


def _open(force=False) -> zarr.Group:
    """The pyramid root, read only; re-opened on refresh."""
    with _lock:
        if _state["root"] is None or force:
            _state["root"] = zarr.open_group(LocalStore(STORE), mode="r")
            _state["opened"] = time.time()
        return _state["root"]


def array(level: int, name: str) -> zarr.Array:
    return _open()[f"{GROUP}/{level}/{name}"]


def refresh() -> dict:
    """Re-open the store and drop the tile presence cache."""
    _open(force=True)
    with _lock:
        _state["done"].clear()
        _state["gen"] += 1
    return status()


def status() -> dict:
    return {"store": str(STORE), "opened": _state["opened"], "gen": _state["gen"]}


def level_for(z: int) -> int:
    """The finest level whose pixel is not smaller than the tile pixel at zoom z."""
    tile_px = 360.0 / (T * 2 ** z)
    return int(min(NLEVELS - 1, max(0, math.floor(math.log2(tile_px / grid.RES)))))


def tiles_done(year: int) -> list[str]:
    """Tile ids with data for the year, from the DONE_LEVEL clear_count plane."""
    with _lock:
        d = _state["done"].get(year)
    if d is not None:
        return d
    c = array(DONE_LEVEL, "clear_count")[year - YEARS[0]]
    k = 2 ** DONE_LEVEL
    d = []
    for ty in range(grid.NTILE_Y):
        for tx in range(grid.NTILE_X):
            ys, xs = grid.tile_slices(ty, tx)
            if c[ys.start // k:-(-ys.stop // k), xs.start // k:-(-xs.stop // k)].any():
                d.append(grid.tile_id(ty, tx))
    with _lock:
        _state["done"][year] = d
    return d


def tile_ll(z, x, y):
    n = 2 ** z
    lat = lambda yy: math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))
    return x / n * 360 - 180, lat(y + 1), (x + 1) / n * 360 - 180, lat(y)


def _centers(z, x, y):
    """Lon and lat of the T x T tile pixel centres (Web Mercator)."""
    W, S, E, N = tile_ll(z, x, y)
    lon = W + (np.arange(T) + 0.5) * (E - W) / T
    n = 2 ** z
    world = 2 * math.pi * _R
    tpx = world / (n * T)
    ty1 = world / 2 - y * world / n
    ys = ty1 - (np.arange(T) + 0.5) * tpx
    lat = np.degrees(np.arctan(np.sinh(ys / _R)))
    return lon, lat, (W, S, E, N)


_NODATA = {"borrowed_pct": BORROWED_NODATA, "source": SOURCE_NODATA}


def _sample(z, x, y, year, bands, lv=None):
    """Nearest-neighbour sample of `bands` at the tile pixels from the level
    for z (or `lv`) -> (k, T, T) uint16/uint8 arrays, nodata outside the
    grid; None if no overlap."""
    lon, lat, (W, S, E, N) = _centers(z, x, y)
    if E <= grid.WEST or W >= grid.EAST or N <= grid.SOUTH or S >= grid.NORTH:
        return None
    if lv is None:
        lv = level_for(z)
    res = grid.RES * 2 ** lv
    H = -(-grid.HEIGHT // 2 ** lv)
    W_ = -(-grid.WIDTH // 2 ** lv)
    cols = np.floor((lon - grid.WEST) / res).astype(np.int64)
    rows = np.floor((grid.NORTH - lat) / res).astype(np.int64)
    okc, okr = (cols >= 0) & (cols < W_), (rows >= 0) & (rows < H)
    if not okc.any() or not okr.any():
        return None
    c0, c1 = int(cols[okc].min()), int(cols[okc].max()) + 1
    r0, r1 = int(rows[okr].min()), int(rows[okr].max()) + 1
    t = year - YEARS[0]
    rr = np.clip(rows - r0, 0, r1 - r0 - 1)[:, None]
    cc = np.clip(cols - c0, 0, c1 - c0 - 1)[None, :]
    ok = okr[:, None] & okc[None, :]
    out = []
    for b in bands:
        try:
            arr = array(lv, b)
        except KeyError:
            return None                      # array not in the pyramid (yet)
        a = arr[t, r0:r1, c0:c1][rr, cc]
        a[~ok] = _NODATA.get(b, 0)
        out.append(a)
    return np.stack(out)


def _encode(rgba):
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(rgba), mode="RGBA").save(buf, format="PNG")
    return buf.getvalue()


def render(z, x, y, year, mode="tc", gain=1.0):
    """PNG bytes or None (outside the grid, no data, or outside MIN_Z..MAX_Z)."""
    if z < MIN_Z or z > MAX_Z or year not in YEARS or mode not in MODES:
        return None
    if mode == "source":
        if z < SOURCE_MIN_Z:
            return None
        raw = _sample(z, x, y, year, MODES[mode], lv=0)
        if raw is None:
            return None
        out = SOURCE_LUT[raw[0]].copy()
        out[..., 3] = np.where(_inside(z, x, y), out[..., 3], 0)
        if not out[..., 3].any():
            return None
        return _encode(out)
    raw = _sample(z, x, y, year, MODES[mode])
    if raw is None:
        return None
    inside = _inside(z, x, y)
    if mode == "borrowed":
        p = raw[0]
        valid = (p != BORROWED_NODATA) & inside
        if not valid.any():
            return None
        idx = np.clip(p.astype(np.float32) / 100 * 255, 0, 255).astype(np.uint8)
        out = np.zeros((T, T, 4), np.uint8)
        out[..., :3] = BORROWED_LUT[idx]
        out[..., 3] = np.where(valid, 255, 0)
        return _encode(out)
    if mode == "count":
        c = raw[0]
        valid = (c > 0) & inside
        if not valid.any():
            return None
        idx = np.clip(c.astype(np.float32) / COUNT_MAX * 255, 0, 255).astype(np.uint8)
        out = np.zeros((T, T, 4), np.uint8)
        out[..., :3] = COUNT_LUT[idx]
        out[..., 3] = np.where(valid, 255, 0)
        return _encode(out)
    valid = (raw > 0).all(0) & inside
    if not valid.any():
        return None
    refl = raw.astype(np.float32) * SCALE + OFFSET
    if mode == "ndvi":
        nir, red = refl[0], refl[1]
        den = np.where(valid, nir + red, 1.0)
        v = np.where(valid & (den != 0), (nir - red) / den, NDVI_LO)
        idx = np.clip((v - NDVI_LO) / (NDVI_HI - NDVI_LO) * 255, 0, 255).astype(np.uint8)
        out = np.zeros((T, T, 4), np.uint8)
        out[..., :3] = NDVI_LUT[idx]
        out[..., 3] = np.where(valid, 255, 0)
        return _encode(out)
    rgb = np.clip(refl / REFL_MAX * gain, 0, 1) ** (1.0 / GAMMA) * 255
    out = np.zeros((T, T, 4), np.uint8)
    out[..., :3] = np.moveaxis(rgb, 0, -1).astype(np.uint8)
    out[..., 3] = np.where(valid, 255, 0)
    return _encode(out)


def probe(lon: float, lat: float, year: int) -> dict | None:
    """One level 0 pixel under lon, lat for the year: its tile id, row and
    column, the six bands as reflectance (None where 0), clear_count and the
    provenance planes (source class name, pick_doy, borrowed_pct; None
    where the pass has not run, and has_planes False while the pyramid
    lacks the arrays). None outside the grid or for a year the store
    lacks."""
    if year not in YEARS:
        return None
    col = math.floor((lon - grid.WEST) / grid.RES)
    row = math.floor((grid.NORTH - lat) / grid.RES)
    if not (0 <= col < grid.WIDTH and 0 <= row < grid.HEIGHT):
        return None
    t = year - YEARS[0]
    out = {"lon": lon, "lat": lat, "year": year, "row": row, "col": col,
           "tile": grid.tile_id(row // grid.TILE, col // grid.TILE),
           "clon": grid.WEST + (col + 0.5) * grid.RES, "clat": grid.NORTH - (row + 0.5) * grid.RES}
    refl = {}
    for b in BANDS:
        v = int(array(0, b)[t, row, col])
        refl[b] = None if v == 0 else round(v * SCALE + OFFSET, 4)
    out["refl"] = refl
    out["clear_count"] = int(array(0, "clear_count")[t, row, col])
    out["has_planes"] = True                # False while source_pyramid.py has not run
    for name in ("source", "pick_doy", "borrowed_pct"):
        try:
            v = int(array(0, name)[t, row, col])
        except KeyError:
            out[name] = None
            out["has_planes"] = False
            continue
        if name == "source":
            out[name] = SOURCE_CLASSES[v] if v < len(SOURCE_CLASSES) else None
        elif name == "pick_doy":
            out[name] = v or None
        else:
            out[name] = None if v == BORROWED_NODATA else v
    return out


def tile_grid() -> list[list]:
    """[[id, west, south, east, north, land], ...] for the 49 tiles; `land`
    False for the 16 all-water tiles the planner never scheduled."""
    import pandas as pd
    land = set(pd.read_parquet(grid.TILES_PATH).query("land").tile) if grid.TILES_PATH.exists() else None
    return [[grid.tile_id(ty, tx), *grid.tile_bounds(ty, tx), land is None or grid.tile_id(ty, tx) in land]
            for ty in range(grid.NTILE_Y) for tx in range(grid.NTILE_X)]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="render one tile to a PNG file")
    ap.add_argument("z", type=int); ap.add_argument("x", type=int); ap.add_argument("y", type=int)
    ap.add_argument("year", type=int); ap.add_argument("--mode", default="tc"); ap.add_argument("--out", default="tile.png")
    a = ap.parse_args()
    t0 = time.time()
    png = render(a.z, a.x, a.y, a.year, a.mode)
    print("empty" if png is None else f"level {level_for(a.z)}, {len(png)} bytes in {time.time() - t0:.2f}s -> {a.out}")
    if png:
        Path(a.out).write_bytes(png)
