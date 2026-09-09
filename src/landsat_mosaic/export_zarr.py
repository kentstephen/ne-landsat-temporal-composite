"""Export the leafon group from the local Icechunk repo to a plain Zarr v3 store.

The target is leafon/0 of the multiscales pyramid at data/pyramid_v1 (level
0, 30 m); pyramid.py builds levels 1-7 beside it and writes the multiscales
attrs. Same grid, chunks, shards, codecs, dtypes, fill values, coordinates, and
attrs as the Icechunk arrays; only the container changes. Each (tile, year)
shard is read from a readonly snapshot and assigned into the target store.
Tile-years that are all fill (ocean, unbuilt) are skipped, and so are
tile-years already present in the target, so the export is resumable.
Shards are separate files in plain Zarr, so workers write disjoint shards
in parallel without coordination.

Run after the build is finished and tagged; never beside batch.py writing
to a target that batch.py reads.

Usage:
  uv run python src/landsat_mosaic/export_zarr.py [--ref v1] [--out data/pyramid_v1]
      [--workers 4] [--years 2000-2025] [--tiles r03c02,r03c03]
  uv run python src/landsat_mosaic/export_zarr.py --verify [--sample 20]
"""
import argparse
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import numpy as np
import zarr
from zarr.storage import LocalStore

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from landsat_mosaic import grid, init_store

OUT_PATH = grid.ROOT / "data/pyramid_v1"
TARGET_GROUP = f"{init_store.GROUP}/0"      # level 0 of the multiscales pyramid
DATA_ARRAYS = init_store.BANDS + ["clear_count"]
COORDS = ["time", "y", "x"]
PROVENANCE = ["source", "pick_doy"]       # store_source.py planes, level 0 only, compared whole


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def source_group(ref: str) -> zarr.Group:
    """The leafon group at a tag, branch, or snapshot id."""
    repo = init_store.open_repo()
    if ref in repo.list_tags():
        session = repo.readonly_session(tag=ref)
    elif ref in repo.list_branches():
        session = repo.readonly_session(branch=ref)
    else:
        session = repo.readonly_session(snapshot_id=ref)
    return zarr.open_group(session.store, mode="r")[init_store.GROUP]


def target_group(out: Path, mode: str) -> zarr.Group:
    root = zarr.open_group(LocalStore(out), mode=mode)
    return root.require_group(TARGET_GROUP) if mode != "r" else root[TARGET_GROUP]


def init_target(src: zarr.Group, out: Path) -> zarr.Group:
    """Create the target group with the same array definitions as the source."""
    if (out / TARGET_GROUP / "zarr.json").exists():
        return target_group(out, "r+")
    dst = target_group(out, "a")
    dst.attrs.update(dict(src.attrs))
    for name in COORDS + DATA_ARRAYS:
        a = src[name]
        b = dst.create_array(
            name, shape=a.shape, dtype=a.dtype, chunks=a.chunks, shards=a.shards,
            fill_value=a.fill_value, filters=a.filters, serializer=a.serializer,
            compressors=a.compressors, dimension_names=a.metadata.dimension_names,
            attributes=dict(a.attrs),
        )
        if name in COORDS:
            b[:] = a[:]
    log(f"created {out} with {len(COORDS) + len(DATA_ARRAYS)} arrays")
    return dst


def all_tile_years(years: tuple[int, int], tiles: list[str] | None) -> list[tuple[str, int]]:
    ids = tiles or [grid.tile_id(ty, tx) for ty in range(grid.NTILE_Y) for tx in range(grid.NTILE_X)]
    ys = [y for y in init_store.YEARS if years[0] <= y <= years[1]]
    return [(t, y) for y in ys for t in ids]


def region(tile: str, year: int) -> tuple[int, slice, slice]:
    ty, tx = int(tile[1:3]), int(tile[4:6])
    ys, xs = grid.tile_slices(ty, tx)
    return year - init_store.YEARS[0], ys, xs


def copy_one(ref: str, out: Path, tile: str, year: int, force: bool) -> str:
    """Copy one tile-year for every data array. Runs in a worker process."""
    src, dst = source_group(ref), target_group(out, "r+")
    t, ys, xs = region(tile, year)
    count = src["clear_count"][t, ys, xs]
    if not count.any():
        return "empty"
    if not force and dst["clear_count"][t, ys, xs].any():
        return "present"
    for name in init_store.BANDS:
        dst[name][t, ys, xs] = src[name][t, ys, xs]
    dst["clear_count"][t, ys, xs] = count
    return "copied"


def export(ref: str, out: Path, workers: int, years: tuple[int, int],
           tiles: list[str] | None, force: bool) -> None:
    src = source_group(ref)
    init_target(src, out)
    jobs = all_tile_years(years, tiles)
    log(f"{len(jobs)} tile-years from ref {ref}, {workers} workers")
    t0 = time.time()
    tally = {"copied": 0, "present": 0, "empty": 0, "failed": 0}
    with ProcessPoolExecutor(workers) as pool:
        futs = {pool.submit(copy_one, ref, out, t, y, force): (t, y) for t, y in jobs}
        for i, f in enumerate(as_completed(futs), 1):
            tile, year = futs[f]
            try:
                r = f.result()
            except Exception as e:
                r = "failed"
                log(f"{tile} {year} failed: {e!r}")
            tally[r] += 1
            if r == "copied" or i % 50 == 0:
                log(f"{i}/{len(jobs)} {tile} {year} {r}, {(time.time() - t0) / 60:.1f} min")
    log(f"done: {tally}")


def verify(ref: str, out: Path, years: tuple[int, int], tiles: list[str] | None,
           sample: int, seed: int) -> None:
    """Attrs, coords, and array definitions; clear_count for every tile-year in
    the filter (completeness both ways); all bands for a random sample."""
    src, dst = source_group(ref), target_group(out, "r")
    # pyramid.py --finalize adds level, multiscales, spatial and proj attrs
    # on top of the exported group attrs; every source attr must survive as is.
    missing = {k: v for k, v in src.attrs.items() if dst.attrs.get(k) != v}
    assert not missing, f"group attrs differ: {missing}"
    for name in COORDS:
        assert np.array_equal(src[name][:], dst[name][:]), f"{name} coords differ"
        assert dict(src[name].attrs) == dict(dst[name].attrs), f"{name} attrs differ"
    for name in DATA_ARRAYS:
        a, b = src[name], dst[name]
        same = (a.shape, str(a.dtype), a.chunks, a.shards, a.fill_value) == \
               (b.shape, str(b.dtype), b.chunks, b.shards, b.fill_value)
        assert same, f"{name} array definition differs"
    log("attrs, coords, and array definitions match")
    prov = [n for n in PROVENANCE if n in src]
    for name in prov:
        assert name in dst, f"{name} is in the store but not the pyramid: run source_pyramid.py"
        a, b = src[name], dst[name]
        assert (a.shape, str(a.dtype), a.fill_value) == (b.shape, str(b.dtype), b.fill_value), f"{name} definition"
        for t in range(a.shape[0]):
            assert np.array_equal(a[t], b[t]), f"{name} differs in {src['time'][t]}"
        log(f"{name}: every year identical at level 0")
    built, bad = [], []
    for tile, year in all_tile_years(years, tiles):
        t, ys, xs = region(tile, year)
        a, b = src["clear_count"][t, ys, xs], dst["clear_count"][t, ys, xs]
        if not np.array_equal(a, b):
            bad.append((tile, year, "missing" if not b.any() else "extra" if not a.any() else "differs"))
        elif a.any():
            built.append((tile, year))
    log(f"clear_count identical for {len(built)} built tile-years, {len(bad)} bad: {bad[:10]}")
    rng = random.Random(seed)
    for tile, year in rng.sample(built, min(sample, len(built))):
        t, ys, xs = region(tile, year)
        for name in init_store.BANDS:
            assert np.array_equal(src[name][t, ys, xs], dst[name][t, ys, xs]), f"{tile} {year} {name}"
        log(f"{tile} {year} all bands identical")
    if bad:
        sys.exit(f"{len(bad)} tile-years differ")
    log(f"verified {min(sample, len(built))} tile-years fully, {len(built)} by clear_count")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default="main", help="tag, branch, or snapshot id")
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--years", default=f"{init_store.YEARS[0]}-{init_store.YEARS[-1]}")
    ap.add_argument("--tiles", help="comma separated tile ids, default all")
    ap.add_argument("--force", action="store_true", help="rewrite tile-years already in the target")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--sample", type=int, default=20)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    years = tuple(int(v) for v in a.years.split("-"))
    tiles = a.tiles.split(",") if a.tiles else None
    if a.verify:
        verify(a.ref, a.out, years, tiles, a.sample, a.seed)
    else:
        export(a.ref, a.out, a.workers, years, tiles, a.force)


if __name__ == "__main__":
    main()
