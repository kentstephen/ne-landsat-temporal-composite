"""Put the v4 provenance planes into the Icechunk store, beside clear_count.

Copies leafon/source and leafon/pick_doy from data/source_v4.zarr (written
by source_pass.py) into the leafon group of the Icechunk store, same chunks
and shards as the bands, one commit per year that has data. Every copied
plane is read back and compared to the side store before main is tagged.
Years the pass did not cover stay at the fill value (source 255, pick_doy
0). Idempotent: a year whose store planes already equal the side store is
skipped, and an existing tag at main is a no-op.

Usage:
  uv run python src/landsat_mosaic/store_source.py [--years 2003-2023] [--tag v4-provenance]
  uv run python src/landsat_mosaic/store_source.py --check     # compare only, no writes
"""
import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import zarr
from zarr.codecs import BytesCodec, ZstdCodec
from zarr.storage import LocalStore

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from landsat_mosaic import init_store, source_pass

GROUP = init_store.GROUP
YEARS = init_store.YEARS
PLANES = {"source": ("uint8", source_pass.NOT_RUN), "pick_doy": ("uint16", 0)}


# Group attr on the store and, via source_pyramid.py, on the pyramid's level 0;
# export_zarr.py --verify requires every store group attr to survive as is.
PROVENANCE_PLANES = ("source and pick_doy: per-pixel provenance of the v4 "
                     "ladder tile-years, from source_pass.py")


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def side_group() -> zarr.Group:
    return zarr.open_group(LocalStore(source_pass.OUT_PATH), mode="r")[GROUP]


def ensure_arrays(repo) -> None:
    session = repo.writable_session("main")
    g = zarr.open_group(session.store, mode="a")[GROUP]
    if all(name in g for name in PLANES):
        return
    shape = (len(YEARS), g["clear_count"].shape[1], g["clear_count"].shape[2])
    for name, (dtype, fill) in PLANES.items():
        if name in g:
            continue
        g.create_array(name, shape=shape, dtype=dtype, chunks=init_store.CHUNK,
                       shards=init_store.SHARD, fill_value=fill,
                       serializer=BytesCodec(), compressors=[ZstdCodec(level=3)],
                       dimension_names=["time", "y", "x"])
    g["source"].attrs.update({
        "nodata": source_pass.NOT_RUN,
        "classes": {str(i): c for i, c in enumerate(source_pass.CLASSES)},
        "long_name": "which look the medoid stored: 0 own-year non-Landsat 7, 1 the year "
                     "before, 2 the year after, 3 own-year Landsat 7; 4 nodata, 5 unmatched, "
                     "255 not computed",
        "note": "recovered after the build by source_pass.py by matching the stored six bands "
                "against the reloaded scene stack under the same ladder; ladder tile-years "
                "(l7_ladder_tile_years) only, 255 elsewhere",
    })
    g["pick_doy"].attrs.update({
        "nodata": 0, "units": "day of year",
        "long_name": "day of year of the observation the medoid stored, in that observation's "
                     "own year (see source for which year)",
        "note": "ladder tile-years only; 0 elsewhere",
    })
    g.attrs["provenance_planes"] = PROVENANCE_PLANES
    snap = session.commit("provenance: add source and pick_doy arrays (empty)")
    log(f"created source and pick_doy arrays, commit {snap}")


def same(a: np.ndarray, b: np.ndarray) -> bool:
    return a.shape == b.shape and np.array_equal(a, b)


def copy_year(repo, side: zarr.Group, year: int) -> str:
    t = YEARS.index(year)
    src = side["source"][t]
    if not (src != source_pass.NOT_RUN).any():
        return "empty"
    doy = side["pick_doy"][t]
    ro = zarr.open_group(repo.readonly_session("main").store, mode="r")[GROUP]
    if same(ro["source"][t], src) and same(ro["pick_doy"][t], doy):
        return "present"
    session = repo.writable_session("main")
    g = zarr.open_group(session.store, mode="a")[GROUP]
    g["source"][t] = src
    g["pick_doy"][t] = doy
    snap = session.commit(f"provenance {year}: source, pick_doy from source_pass "
                          f"({100 * np.isin(src, source_pass.NEIGHBOUR).mean():.2f}% borrowed of grid)")
    return f"copied, commit {snap}"


def check(repo, side: zarr.Group, years: list[int]) -> int:
    ro = zarr.open_group(repo.readonly_session("main").store, mode="r")[GROUP]
    bad = 0
    for year in years:
        t = YEARS.index(year)
        ok = all(same(ro[name][t], side[name][t]) for name in PLANES)
        bad += not ok
        log(f"check {year}: {'identical' if ok else 'DIFFERS'}")
    return bad


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="2003-2023")
    ap.add_argument("--tag", default="v4-provenance")
    ap.add_argument("--check", action="store_true", help="compare only, write nothing")
    a = ap.parse_args()
    y0, y1 = (int(v) for v in a.years.split("-"))
    years = [y for y in YEARS if y0 <= y <= y1]
    if not source_pass.OUT_PATH.exists():
        sys.exit(f"{source_pass.OUT_PATH} missing: run source_pass.py first")
    repo = init_store.open_repo()
    side = side_group()
    if a.check:
        sys.exit(check(repo, side, years))
    ensure_arrays(repo)
    t0 = time.time()
    for i, year in enumerate(years, 1):
        r = copy_year(repo, side, year)
        log(f"{i}/{len(years)} {year} {r}, {(time.time() - t0) / 60:.1f} min")
    bad = check(repo, side, years)
    if bad:
        sys.exit(f"{bad} years differ from {source_pass.OUT_PATH}; NOT tagging")
    head = repo.lookup_branch("main")
    if a.tag in repo.list_tags():
        at = repo.lookup_tag(a.tag)
        if at != head:
            sys.exit(f"tag {a.tag} exists at {at}, main is {head}; tags are immutable, use a new name")
        log(f"tag {a.tag} already at main ({head})")
    else:
        repo.create_tag(a.tag, head)
        log(f"tagged {a.tag} = {head}")
    log(f"done: {len(years)} years, {(time.time() - t0) / 60:.1f} min")


if __name__ == "__main__":
    main()
