"""Build one (tile, year) composite and write it into the Icechunk store.

Steps: rebuild STAC items from the local catalog for the job's scene ids,
load each sub-tile of the 4096 tile onto the target grid with odc-stac
(URLs signed at read time), mask with QA_PIXEL, reduce over time, and write
the tile region into the leafon group. One commit per job. A job whose
clear_count region is already non-zero is skipped unless --force.

Landsat 7 ladder (default on, --no-l7-fill turns it off): at each pixel
the own-year non-L7 looks count where they reach --fill-min-clear (default
--min-clear); with --neighbours the non-L7 looks of the years either side
are added where they do not; Landsat 7 counts only where that still falls
short. SLC-off gaps change the set of looks every scan
line, and any per-pixel reducer stripes with it (stripe_score.py,
stripe_trials.py, stats/striping/, stats/stripe_trials/). YEAR_SPAN adds
neighbouring years' looks for a year that has no non-L7 platform (2012:
2011-2013).

Window rules (--window):
  js        June 1 to Sept 30 only
  mo        May 1 to Oct 31
  fallback  June to Sept where a pixel has at least --min-clear clear looks,
            otherwise May to Oct (default)

Usage:
  uv run python src/landsat_mosaic/runner.py r03c02 2020
  uv run python src/landsat_mosaic/runner.py r03c02 2020 --reducer median --dry-run
--dry-run writes data/trials/<tile>_<year>_<reducer>_<window>/ (npz + png)
instead of the store. --only K runs only sub-tile K (0-based, row-major).
"""
import argparse
import os
import sys
import time
from pathlib import Path

# GDAL settings for windowed COG reads over HTTPS. Halved load time in tests.
os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("GDAL_HTTP_MERGE_CONSECUTIVE_RANGES", "YES")
os.environ.setdefault("GDAL_HTTP_MULTIPLEX", "YES")
os.environ.setdefault("VSI_CACHE", "TRUE")
os.environ.setdefault("GDAL_CACHEMAX", "1024")
os.environ.setdefault("CPL_VSIL_CURL_CHUNK_SIZE", "1048576")
os.environ.setdefault("GDAL_HTTP_MAX_RETRY", "5")
os.environ.setdefault("GDAL_HTTP_RETRY_DELAY", "2")

import numpy as np
import odc.stac
import pandas as pd
import planetary_computer as pc
import pyarrow.parquet as pq
import pystac
import xarray as xr
import zarr
from affine import Affine
from odc.geo.geobox import GeoBox
from stac_geoparquet.arrow import stac_table_to_items

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from landsat_mosaic import grid, init_store

SRC_BANDS = ["blue", "green", "red", "nir08", "swir16", "swir22"]
OUT_BANDS = init_store.BANDS                     # blue green red nir swir1 swir2
SCALE, OFFSET = 0.0000275, -0.2
FILL, DILATED, CIRRUS, CLOUD, SHADOW = 0, 1, 2, 3, 4
BAD_QA = sum(1 << b for b in (FILL, DILATED, CIRRUS, CLOUD, SHADOW))
TILE_GEOBOX = GeoBox((grid.HEIGHT, grid.WIDTH),
                     Affine(grid.RES, 0, grid.WEST, 0, -grid.RES, grid.NORTH), "EPSG:4326")


# ---------------------------------------------------------------- inputs

def bad_scenes() -> set[str]:
    """Item ids listed in data/bad_scenes.txt (corrupt assets upstream)."""
    f = grid.ROOT / "data/bad_scenes.txt"
    if not f.exists():
        return set()
    return {ln.strip() for ln in f.read_text().splitlines()
            if ln.strip() and not ln.startswith("#")}


YEAR_SPAN = {2012: 1}          # years either side whose looks count as the year's own
NEIGHBOUR_SPAN = 1             # years either side lent as the ladder's second rung
L7 = "landsat-7"
L5 = "landsat-5"
L5_EDGE_ROWS = 25              # erode Landsat 5 data-present masks this many rows (see erode_l5_edges)


def own_years(year: int) -> list[int]:
    span = YEAR_SPAN.get(year, 0)
    return list(range(year - span, year + span + 1))


def job_items(tile: str, year: int, span: int | None = None) -> list[pystac.Item]:
    span = YEAR_SPAN.get(year, 0) if span is None else span
    items = []
    for y in range(year - span, year + span + 1):
        items += _year_items(tile, y)
    return items


def neighbour_items(tile: str, year: int) -> list[pystac.Item]:
    """The non-Landsat 7 scenes of the years either side (NEIGHBOUR_SPAN)
    that are not already the job's own years; the second rung of the ladder."""
    items = []
    for y in range(year - NEIGHBOUR_SPAN, year + NEIGHBOUR_SPAN + 1):
        if y in own_years(year) or y < init_store.YEARS[0] or y > init_store.YEARS[-1]:
            continue
        items += [it for it in _year_items(tile, y) if it.properties["platform"] != L7]
    return items


def _year_items(tile: str, year: int) -> list[pystac.Item]:
    jobs = pd.read_parquet(grid.ROOT / "data/jobs.parquet", columns=["tile", "year", "id"])
    ids = jobs[(jobs.tile == tile) & (jobs.year == year)].id.tolist()
    if not ids:
        sys.exit(f"no scenes for {tile} {year} in jobs.parquet")
    bad = bad_scenes()
    if bad & set(ids):
        print(f"{tile} {year}: dropping bad scenes {sorted(bad & set(ids))}")
        ids = [i for i in ids if i not in bad]
    tab = pq.read_table(grid.ROOT / f"data/catalog/landsat-c2-l2_{year}.parquet",
                        filters=[("id", "in", ids)])
    return [pystac.Item.from_dict(d) for d in stac_table_to_items(tab)]


def items_over(items: list[pystac.Item], gb: GeoBox) -> list[pystac.Item]:
    b = gb.extent.boundingbox
    return [i for i in items
            if i.bbox[2] > b.left and i.bbox[0] < b.right
            and i.bbox[3] > b.bottom and i.bbox[1] < b.top]


def load_stack(items: list[pystac.Item], gb: GeoBox, pool: int) -> xr.Dataset:
    """One time slot per (solar day, platform), merged by hand.

    odc-stac's groupby="solar_day" fuse is first-scene-wins with a nodata
    check, and for qa_pixel the fill outside a scene footprint comes back as
    0 (with nodata 1) or 1 (with nodata 0), so the first row-adjacent scene
    shadows the second one's real QA and cloud reads as clear. Loading one
    slot per scene and merging on blue != 0 avoids that.
    """
    ds = odc.stac.load(
        items, bands=SRC_BANDS + ["qa_pixel"], geobox=gb,
        groupby=None, patch_url=pc.sign, pool=pool, chunks=None,
    )
    # Slots are (solar day, platform). Until 2022 platforms never shared a
    # day here (8 days apart), but Landsat 7's orbit was lowered in 2022 and
    # it drifted to a 09:55 pass, so 2022-2023 have Landsat 7 and 9 on the
    # same day over the same tile (60 tile-years); merging them put SLC-off
    # gaps into a slot labelled landsat-9.
    day = ds.time.dt.floor("D").values
    by_time = {np.datetime64(it.datetime).astype("datetime64[ns]"): it.properties["platform"] for it in items}
    i_plat = np.array([by_time[t] for t in ds.time.values.astype("datetime64[ns]")])
    keys = sorted(set(zip(day, i_plat)), key=lambda t: (t[0], t[1]))
    out = {b: np.zeros((len(keys), *gb.shape), ds[b].dtype) for b in ds.data_vars}
    for k, (d, p) in enumerate(keys):
        for i in np.flatnonzero((day == d) & (i_plat == p)):
            has = (ds.blue.values[i] != 0) & (out["blue"][k] == 0)
            for b in out:
                out[b][k][has] = ds[b].values[i][has]
    ydim, xdim = [d for d in ds.blue.dims if d != "time"]
    days = np.array([d for d, _ in keys])
    plat = np.array([p for _, p in keys])
    for k in np.flatnonzero(plat == L5):
        keep = erode_l5_edges(out["blue"][k] != 0)
        for b in out:
            out[b][k][~keep] = 0
    merged = xr.Dataset(
        {b: (("time", ydim, xdim), out[b]) for b in out},
        coords={"time": days, ydim: ds[ydim], xdim: ds[xdim], "platform": ("time", plat)},
    )
    del ds
    return merged


def erode_l5_edges(present: np.ndarray, rows: int = L5_EDGE_ROWS) -> np.ndarray:
    """Landsat 5 ran its scan mirror in bumper mode from 2002, and its scene
    edges are combs: one tooth per 16-line swath, kilometres long. Where two
    paths overlap the teeth make the look set alternate swath by swath and
    the medoid stripes with it (stats/fill_trials, 2026-09-07). Eroding the
    data-present mask along rows by `rows` (odd, centred) removes the teeth
    (8-16 rows thick) and costs 12 rows at the true top and bottom edges,
    which the neighbouring WRS row covers."""
    out = present.copy()
    for d in range(1, rows // 2 + 1):
        out &= np.roll(present, d, axis=0)
        out &= np.roll(present, -d, axis=0)
    out[:rows // 2] = False
    out[-(rows // 2):] = False
    return out


# ------------------------------------------------------------- reducers

def clear_obs(ds: xr.Dataset, window: str, min_clear: int) -> np.ndarray:
    """Boolean [time, y, x]: observation is clear and inside the chosen window."""
    qa = ds.qa_pixel.values
    ok = ((qa & BAD_QA) == 0) & (ds.blue.values != 0)
    month = ds.time.dt.month.values
    js = (month >= 6) & (month <= 9)
    if window == "js":
        return ok & js[:, None, None]
    if window == "mo":
        return ok
    n_js = (ok & js[:, None, None]).sum(0)
    keep_all = n_js < min_clear                    # [y, x]
    return ok & (js[:, None, None] | keep_all[None])


def clear_obs_slots(ds: xr.Dataset, slots: np.ndarray, window: str, min_clear: int) -> np.ndarray:
    """clear_obs over the time slots in `slots` (bool), zeros elsewhere. The
    window fallback counts only those slots."""
    full = np.zeros((ds.sizes["time"], *ds.blue.shape[1:]), bool)
    if slots.any():
        full[np.flatnonzero(slots)] = clear_obs(ds.isel(time=np.flatnonzero(slots)), window, min_clear)
    return full


def valid_obs(ds: xr.Dataset, window: str, min_clear: int, l7_fill: bool,
              fill_min_clear: int | None = None, year: int | None = None) -> np.ndarray:
    """The Landsat 7 ladder, per pixel. Rung 1: the own-year non-L7 looks,
    where they reach fill_min_clear (default min_clear). Rung 2, only if the
    stack holds neighbour-year slots (neighbour_items): own plus neighbour
    non-L7 looks, where they reach it. Rung 3: every own-year look, Landsat 7
    included, plus the neighbour non-L7 looks. With l7_fill off every
    own-year look counts. The window logic always uses min_clear."""
    k = min_clear if fill_min_clear is None else fill_min_clear
    is7 = ds.platform.values == L7
    own = np.ones(ds.sizes["time"], bool) if year is None else \
        np.isin(ds.time.dt.year.values, own_years(year))
    if not l7_fill or not is7[own].any() or is7[own].all() and not (~own).any():
        return clear_obs_slots(ds, own, window, min_clear)
    valid = clear_obs_slots(ds, own & ~is7, window, min_clear)
    short = valid.sum(0) < k
    if (~own).any():
        v_non = clear_obs_slots(ds, ~is7, window, min_clear)
        valid[:, short] = v_non[:, short]
        short = v_non.sum(0) < k
    v_last = clear_obs_slots(ds, own | ~is7, window, min_clear)
    valid[:, short] = v_last[:, short]
    return valid


def to_reflectance(ds: xr.Dataset, valid: np.ndarray) -> np.ndarray:
    """float32 [band, time, y, x], NaN where not valid."""
    arr = np.stack([ds[b].values for b in SRC_BANDS]).astype("float32")
    arr = arr * SCALE + OFFSET
    arr[:, ~valid] = np.nan
    return arr


def median(arr: np.ndarray) -> np.ndarray:
    with np.errstate(all="ignore"):
        return np.nanmedian(arr, axis=1)


def medoid(arr: np.ndarray) -> np.ndarray:
    """Observation nearest the per-pixel median in band space."""
    with np.errstate(all="ignore"):
        med = np.nanmedian(arr, axis=1)                          # band, y, x
        dist = np.nansum((arr - med[:, None]) ** 2, axis=0)      # time, y, x
    dist[~np.isfinite(arr).all(0)] = np.inf
    idx = dist.argmin(0)                                         # y, x
    picked = np.take_along_axis(arr, idx[None, None], axis=1)[:, 0]
    picked[:, ~np.isfinite(dist).any(0)] = np.nan
    return picked


REDUCERS = {"median": median, "medoid": medoid}


def to_uint16(refl: np.ndarray) -> np.ndarray:
    out = np.round((refl - OFFSET) / SCALE)
    out = np.where(np.isfinite(out), np.clip(out, 1, 65535), 0)
    return out.astype("uint16")


def composite(ds: xr.Dataset, reducer: str, window: str, min_clear: int,
              l7_fill: bool = True, fill_min_clear: int | None = None,
              year: int | None = None) -> tuple[np.ndarray, np.ndarray]:
    valid = valid_obs(ds, window, min_clear, l7_fill, fill_min_clear, year)
    count = np.minimum(valid.sum(0), 255).astype("uint8")
    arr = to_reflectance(ds, valid)
    del valid
    return to_uint16(REDUCERS[reducer](arr)), count


# ---------------------------------------------------------------- output

def quicklook(bands: np.ndarray, count: np.ndarray, path: Path, title: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    step = max(1, bands.shape[1] // 1024)
    rgb = np.stack([bands[i, ::step, ::step] for i in (2, 1, 0)], -1).astype("float32")
    rgb = np.clip((rgb * SCALE + OFFSET) / 0.3, 0, 1)
    fig, ax = plt.subplots(1, 2, figsize=(14, 7))
    ax[0].imshow(rgb)
    ax[0].set_title(f"{title}: true color")
    im = ax[1].imshow(count[::step, ::step], cmap="viridis", vmin=0, vmax=20)
    ax[1].set_title("clear observations used")
    plt.colorbar(im, ax=ax[1], shrink=0.7)
    for a in ax:
        a.set_axis_off()
    fig.savefig(path, dpi=110, bbox_inches="tight")
    plt.close(fig)


def already_done(tile: str, year: int) -> bool:
    repo = init_store.open_repo()
    g = zarr.open_group(repo.readonly_session("main").store, mode="r")[init_store.GROUP]
    ty, tx = int(tile[1:3]), int(tile[4:6])
    ys, xs = grid.tile_slices(ty, tx)
    return bool(g["clear_count"][year - init_store.YEARS[0], ys, xs].any())


def write_store(tile: str, year: int, bands: np.ndarray, count: np.ndarray, msg: str) -> str:
    repo = init_store.open_repo()
    session = repo.writable_session("main")
    g = zarr.open_group(session.store, mode="a")[init_store.GROUP]
    ty, tx = int(tile[1:3]), int(tile[4:6])
    ys, xs = grid.tile_slices(ty, tx)
    t = year - init_store.YEARS[0]
    for i, b in enumerate(OUT_BANDS):
        g[b][t, ys, xs] = bands[i]
    g["clear_count"][t, ys, xs] = count
    return session.commit(msg)


# ------------------------------------------------------------------ main

def run_job(tile: str, year: int, reducer: str, window: str, min_clear: int,
            sub: int, pool: int, dry_run: bool, only: int | None, force: bool,
            l7_fill: bool = True, fill_min_clear: int | None = None,
            neighbours: bool = False) -> None:
    if not dry_run and not force and already_done(tile, year):
        print(f"{tile} {year}: already in store, skipping")
        return
    t0 = time.time()
    items = job_items(tile, year)
    n_own = len(items)
    if neighbours:
        items = items + neighbour_items(tile, year)
    ty, tx = int(tile[1:3]), int(tile[4:6])
    ys, xs = grid.tile_slices(ty, tx)
    tile_gb = TILE_GEOBOX[ys, xs]
    h, w = tile_gb.shape
    bands = np.zeros((len(OUT_BANDS), h, w), "uint16")
    count = np.zeros((h, w), "uint8")
    subs = [(sy, sx) for sy in range(0, h, sub) for sx in range(0, w, sub)]
    span = YEAR_SPAN.get(year, 0)
    print(f"{tile} {year}: {n_own} scenes + {len(items) - n_own} neighbour-year non-L7, "
          f"{len(subs)} sub-tiles of {sub}, reducer={reducer} window={window} l7_fill={l7_fill} "
          f"fill_min_clear={fill_min_clear} neighbours={neighbours} span={span}", flush=True)
    todo = [(k, sy, sx) for k, (sy, sx) in enumerate(subs) if only is None or k == only]

    def fetch(sy, sx):
        gb = tile_gb[sy:min(sy + sub, h), sx:min(sx + sub, w)]  # GeoBox slices do not clip
        its = items_over(items, gb)
        t1 = time.time()
        return its, load_stack(its, gb, pool), time.time() - t1

    # Prefetch the next sub-tile's reads while this one reduces.
    from concurrent.futures import ThreadPoolExecutor
    ex = ThreadPoolExecutor(1)
    futs = [ex.submit(fetch, sy, sx) for _, sy, sx in todo[:1]]
    for n, (k, sy, sx) in enumerate(todo):
        if n + 1 < len(todo):
            futs.append(ex.submit(fetch, *todo[n + 1][1:]))
        its, ds, tload = futs[n].result()
        t2 = time.time()
        b, c = composite(ds, reducer, window, min_clear, l7_fill, fill_min_clear, year)
        bands[:, sy:sy + sub, sx:sx + sub] = b
        count[sy:sy + sub, sx:sx + sub] = c
        print(f"  sub {k:2d} ({sy},{sx}): {len(its)} scenes, {ds.sizes['time']} days, "
              f"load {tload:5.0f}s, reduce {time.time() - t2:4.0f}s, "
              f"mean clear {c.mean():.1f}", flush=True)
        del ds
        futs[n] = None
    ex.shutdown()
    tag = f"{tile}_{year}_{reducer}_{window}"
    if dry_run:
        out = grid.ROOT / "data/trials"
        out.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(out / f"{tag}.npz", bands=bands, count=count)
        quicklook(bands, count, out / f"{tag}.png", tag)
        print(f"wrote data/trials/{tag}.{{npz,png}}")
    else:
        snap = write_store(tile, year, bands, count,
                           f"{tile} {year} {reducer} {window} min_clear={min_clear} "
                           f"l7_fill={l7_fill} fill_min_clear={fill_min_clear} "
                           f"neighbours={neighbours} span={span}")
        print(f"committed {snap}")
        ql = grid.ROOT / "data/quicklooks" / str(year)
        ql.mkdir(parents=True, exist_ok=True)
        quicklook(bands, count, ql / f"{tile}.png", f"{tile} {year}")
    print(f"{tile} {year}: done in {(time.time() - t0) / 60:.1f} min", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("tile")
    ap.add_argument("year", type=int)
    ap.add_argument("--reducer", choices=REDUCERS, default="medoid")
    ap.add_argument("--window", choices=["js", "mo", "fallback"], default="fallback")
    ap.add_argument("--min-clear", type=int, default=4)
    ap.add_argument("--sub", type=int, default=2048)
    ap.add_argument("--pool", type=int, default=32)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-l7-fill", action="store_true", help="count every Landsat 7 look")
    ap.add_argument("--fill-min-clear", type=int, default=None,
                    help="non-L7 clear looks that keep Landsat 7 out (default --min-clear)")
    ap.add_argument("--neighbours", action="store_true",
                    help="lend the neighbouring years' non-L7 looks before falling back to Landsat 7")
    a = ap.parse_args()
    run_job(a.tile, a.year, a.reducer, a.window, a.min_clear, a.sub, a.pool,
            a.dry_run, a.only, a.force, l7_fill=not a.no_l7_fill, fill_min_clear=a.fill_min_clear,
            neighbours=a.neighbours)


if __name__ == "__main__":
    main()
