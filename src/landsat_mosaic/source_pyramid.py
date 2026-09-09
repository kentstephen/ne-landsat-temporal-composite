"""Put the v4 provenance into the pyramid: `source` at level 0 and
`borrowed_pct` at every level, next to clear_count.

Reads data/source_v4.zarr (source_pass.py: 0 own-year non-L7, 1 the year
before, 2 the year after, 3 own-year Landsat 7, 4 nodata, 5 unmatched, 255
not computed; pick_doy the picked look's day of year) and writes into the
pyramid (default data/pyramid_v1):

  leafon/0/source        uint8, the class as above, 255 where the pass has
                         not run. Level 0 only.
  leafon/{0..7}/pick_doy uint16 day of year of the picked look (its own
                         year), 0 unknown; levels 1-7 the nodata-aware
                         block mean (pyramid.mean_levels).
  leafon/{0..7}/borrowed_pct
                         uint8 0-100, the share of a block's valid pixels
                         whose value is a neighbour-year observation; 255
                         where no pixel is known. Level 0 is 0 or 100 per
                         pixel. Tile-years outside the ladder list
                         (data/l7fill_jobs.txt) used their own year only,
                         so they are 0 wherever clear_count > 0. Ladder
                         tile-years the pass has not reached yet are 255.
Levels 1-7 are exact: borrowed and valid counts are summed 2x2 level to
level in uint32, pct = round(100 * borrowed / valid).

One unit per year; --years, --force (redo years that have level 7 data),
--workers. Consolidates the metadata at the end so the arrays are visible
to consolidated readers; run pyramid.py --finalize again afterwards only if
the group attrs change too.

Usage:
  uv run python src/landsat_mosaic/source_pyramid.py [--out data/pyramid_v1] [--years 2003-2023]
      [--workers 4] [--force]
"""
import argparse
import multiprocessing as mp
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import numpy as np
import zarr
from zarr.codecs import BytesCodec, ZstdCodec
from zarr.storage import LocalStore

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from landsat_mosaic import grid, init_store, pyramid, source_pass, store_source

GROUP = init_store.GROUP
YEARS = init_store.YEARS
NLEVELS = pyramid.NLEVELS
NODATA = 255
BORROWED = source_pass.NEIGHBOUR  # classes 1 (year before) and 2 (year after)
KNOWN = (0, 1, 2, 3)              # classes that are a real observation


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def ladder_jobs() -> set[tuple[str, int]]:
    return {(ln.split()[0], int(ln.split()[1])) for ln in pyramid.LADDER_JOBS.read_text().splitlines() if ln.strip()}


def init_arrays(out: Path) -> None:
    root = pyramid.open_root(out, "r+")
    made = 0
    for n in range(NLEVELS):
        g = root[f"{GROUP}/{n}"]
        h, w = pyramid.level_shape(n)
        names = ["borrowed_pct", "pick_doy"] + (["source"] if n == 0 else [])
        for name in names:
            if name in g:
                continue
            g.create_array(
                name, shape=(len(YEARS), h, w), dtype="uint16" if name == "pick_doy" else "uint8",
                chunks=pyramid.CHUNK, shards=pyramid.SHARD if n in pyramid.SHARD_LEVELS else None,
                fill_value=0 if name == "pick_doy" else NODATA,
                serializer=BytesCodec(), compressors=[ZstdCodec(level=3)],
                dimension_names=["time", "y", "x"],
            )
            made += 1
        g["pick_doy"].attrs.update({
            "nodata": 0, "units": "day of year", "spatial:dimensions": ["y", "x"],
            "long_name": "day of year of the observation the medoid stored, in that observation's "
                         "own year (see source for which year)" + ("" if n == 0 else ", mean over the block"),
            "note": "ladder tile-years only; 0 elsewhere",
        })
        g["borrowed_pct"].attrs.update({
            "nodata": NODATA, "units": "percent", "spatial:dimensions": ["y", "x"],
            "long_name": "share of valid pixels whose value is a neighbour-year (+-1) observation"
                         + (" (0 or 100 per pixel)" if n == 0 else ", exact over the block"),
            "note": "0 in tile-years built from their own year only; 255 where unknown "
                    "(no valid pixel, or a ladder tile-year the source pass has not covered)",
        })
        if n == 0:
            g.attrs["provenance_planes"] = store_source.PROVENANCE_PLANES
            g["source"].attrs.update({
                "nodata": NODATA, "spatial:dimensions": ["y", "x"],
                "classes": {str(i): c for i, c in enumerate(source_pass.CLASSES)},
                "long_name": "which look the medoid stored: 0 own-year non-Landsat 7, 1 the year "
                             "before, 2 the year after, 3 own-year Landsat 7; 4 nodata, 5 unmatched, "
                             "255 not computed",
                "note": "recovered after the build by source_pass.py by matching the stored six bands "
                        "against the reloaded scene stack under the same ladder; ladder tile-years only",
            })
    log(f"arrays: {made} created")


def borrowed_level0(out: Path, year: int, jobs: set[tuple[str, int]]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(source plane, borrowed plane, pick_doy plane) at level 0 for the year."""
    t = YEARS.index(year)
    src_root = zarr.open_group(LocalStore(source_pass.OUT_PATH), mode="r")[GROUP]
    src = src_root["source"][t]                                    # uint8, 255 where not run
    doy = src_root["pick_doy"][t]
    count = pyramid.open_root(out, "r")[f"{GROUP}/0/clear_count"][t]
    borrowed = np.full(src.shape, NODATA, np.uint8)
    for ty in range(grid.NTILE_Y):
        for tx in range(grid.NTILE_X):
            ys, xs = grid.tile_slices(ty, tx)
            tile = grid.tile_id(ty, tx)
            if (tile, year) in jobs:
                s = src[ys, xs]
                b = borrowed[ys, xs]
                b[np.isin(s, KNOWN)] = 0
                b[np.isin(s, BORROWED)] = 100
                borrowed[ys, xs] = b
            else:
                borrowed[ys, xs] = np.where(count[ys, xs] > 0, 0, NODATA).astype(np.uint8)
    return src, borrowed, doy


def pct_levels(borrowed: np.ndarray) -> dict[int, np.ndarray]:
    p = pyramid._pad(borrowed)
    known = p != NODATA
    b = (p == 100).astype(np.uint8)
    p_known = known.astype(np.uint8)
    s, n = pyramid._sum2(b), pyramid._sum2(p_known)
    out = {}
    for lv in range(1, NLEVELS):
        if lv > 1:
            s, n = pyramid._sum2(s), pyramid._sum2(n)
        m = np.full(s.shape, NODATA, np.uint8)
        ok = n > 0
        m[ok] = ((200 * s[ok] + n[ok]) // (2 * n[ok])).astype(np.uint8)   # round half up of 100*s/n
        h, w = pyramid.level_shape(lv)
        out[lv] = m[:h, :w]
    return out


def build_unit(out: Path, year: int, jobs: set, force: bool) -> str:
    root = pyramid.open_root(out, "r+")
    t = YEARS.index(year)
    top = root[f"{GROUP}/{NLEVELS - 1}/borrowed_pct"][t]
    if not force and (top != NODATA).any():
        return "present"
    src, borrowed, doy = borrowed_level0(out, year, jobs)
    if not (borrowed != NODATA).any():
        return "empty"
    root[f"{GROUP}/0/source"][t] = src
    root[f"{GROUP}/0/borrowed_pct"][t] = borrowed
    root[f"{GROUP}/0/pick_doy"][t] = doy
    levels = pct_levels(borrowed)
    for lv in range(1, NLEVELS):
        root[f"{GROUP}/{lv}/borrowed_pct"][t] = levels[lv]
    if doy.any():
        dl = pyramid.mean_levels(doy)
        for lv in range(1, NLEVELS):
            root[f"{GROUP}/{lv}/pick_doy"][t] = dl[lv]
    known = borrowed != NODATA
    return f"built, borrowed {100 * (borrowed == 100).sum() / max(known.sum(), 1):.2f}% of known, " \
           f"unknown {100 * (~known).mean():.1f}% of grid"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(pyramid.OUT_PATH))
    ap.add_argument("--years", default="2003-2023")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    y0, y1 = (int(v) for v in a.years.split("-"))
    if not source_pass.OUT_PATH.exists():
        sys.exit(f"{source_pass.OUT_PATH} missing: run source_pass.py first")
    init_arrays(out)
    jobs = ladder_jobs()
    years = [y for y in YEARS if y0 <= y <= y1]
    t0 = time.time()
    with ProcessPoolExecutor(a.workers, mp_context=mp.get_context("spawn")) as pool:
        futs = {pool.submit(build_unit, out, y, jobs, a.force): y for y in years}
        for i, f in enumerate(as_completed(futs), 1):
            y = futs[f]
            try:
                r = f.result()
            except Exception as e:
                r = f"failed: {e!r}"
            log(f"{i}/{len(years)} {y} {r}, {(time.time() - t0) / 60:.1f} min")
    zarr.consolidate_metadata(pyramid.open_root(out, "r+").store)
    log(f"consolidated metadata at {out}")


if __name__ == "__main__":
    main()
