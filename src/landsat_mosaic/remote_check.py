"""Read the uploaded product back through data.source.coop and compare to disk.

Pyramid: opens PREFIX/pyramid_v1 over plain https with the consolidated
metadata, checks the group and level attrs equal the local ones, and reads
one 512 chunk per level for --years (every band and the provenance planes
where present) against data/pyramid_v1. Store: opens PREFIX/build.icechunk
anonymously through the proxy (path style, no credentials), checks the tags,
and reads --sample tile-year regions of clear_count at --tag against the
local store. Read only; nothing is written anywhere.

Usage:
  uv run python src/landsat_mosaic/remote_check.py --prefix ACCOUNT/PRODUCT [--years 2003,2014,2025]
      [--sample 3] [--tag v4-provenance] [--no-store]
"""
import argparse
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import icechunk
import numpy as np
import zarr
from zarr.storage import FsspecStore, LocalStore

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from landsat_mosaic import export_zarr, grid, init_store, pyramid

PROXY = "https://data.source.coop"
PROV = ["source", "pick_doy", "borrowed_pct"]


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def check_pyramid(prefix: str, years: list[int]) -> int:
    url = f"{PROXY}/{prefix}/pyramid_v1"
    t0 = time.time()
    remote = zarr.open_group(FsspecStore.from_url(url), mode="r", use_consolidated=True)
    local = zarr.open_group(LocalStore(pyramid.OUT_PATH), mode="r", use_consolidated=True)
    assert remote.metadata.consolidated_metadata is not None, "no consolidated metadata on the bucket"
    log(f"opened {url} in {time.time() - t0:.1f} s")
    bad = 0
    g = init_store.GROUP
    if dict(remote[g].attrs) != dict(local[g].attrs):
        log("BAD: group attrs differ"); bad += 1
    for n in range(pyramid.NLEVELS):
        r, l = remote[f"{g}/{n}"], local[f"{g}/{n}"]
        if dict(r.attrs) != dict(l.attrs):
            log(f"BAD: level {n} attrs differ"); bad += 1
        if sorted(r.array_keys()) != sorted(l.array_keys()):
            log(f"BAD: level {n} arrays {sorted(r.array_keys())} vs {sorted(l.array_keys())}"); bad += 1
    names = pyramid.ARRAYS + [p for p in PROV if p in local[f"{g}/0"]]
    for year in years:
        t = init_store.YEARS.index(year)
        for n in range(pyramid.NLEVELS):
            h, w = pyramid.level_shape(n)
            ys, xs = slice(h // 2 - 256, h // 2 + 256), slice(w // 2 - 256, w // 2 + 256)
            t1 = time.time()
            for name in names:
                if name not in remote[f"{g}/{n}"]:
                    continue
                a, b = remote[f"{g}/{n}/{name}"][t, ys, xs], local[f"{g}/{n}/{name}"][t, ys, xs]
                if not np.array_equal(a, b):
                    log(f"BAD: {year} level {n} {name} differs"); bad += 1
            log(f"{year} level {n}: {len(names)} arrays, one chunk each, {time.time() - t1:.1f} s")
    return bad


def check_store(prefix: str, tag: str, sample: int, seed: int) -> int:
    bucket, _, rest = prefix.partition("/")
    storage = icechunk.s3_storage(bucket=bucket, prefix=f"{rest}/build.icechunk", endpoint_url=PROXY,
                                  anonymous=True, force_path_style=True, region="us-west-2")
    t0 = time.time()
    repo = icechunk.Repository.open(storage)
    log(f"opened remote store in {time.time() - t0:.1f} s: tags {sorted(repo.list_tags())}, "
        f"branches {sorted(repo.list_branches())}")
    local_repo = init_store.open_repo()
    bad = 0
    if repo.list_tags() != local_repo.list_tags():
        log(f"BAD: tags differ, local {sorted(local_repo.list_tags())}"); bad += 1
    if repo.lookup_tag(tag) != local_repo.lookup_tag(tag):
        log(f"BAD: {tag} points at different snapshots"); bad += 1
    r = zarr.open_group(repo.readonly_session(tag=tag).store, mode="r")[init_store.GROUP]
    l = zarr.open_group(local_repo.readonly_session(tag=tag).store, mode="r")[init_store.GROUP]
    if sorted(r.array_keys()) != sorted(l.array_keys()):
        log(f"BAD: arrays {sorted(r.array_keys())} vs {sorted(l.array_keys())}"); bad += 1
    jobs = export_zarr.all_tile_years((init_store.YEARS[0], init_store.YEARS[-1]), None)
    rng = random.Random(seed)
    for tile, year in rng.sample(jobs, sample):
        t, ys, xs = export_zarr.region(tile, year)
        t1 = time.time()
        a, b = r["clear_count"][t, ys, xs], l["clear_count"][t, ys, xs]
        ok = np.array_equal(a, b)
        bad += not ok
        log(f"{tile} {year} clear_count {'identical' if ok else 'DIFFERS'}, {time.time() - t1:.1f} s")
    return bad


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", required=True, help="ACCOUNT/PRODUCT")
    ap.add_argument("--years", default="2003,2014,2025")
    ap.add_argument("--tag", default="v4-provenance")
    ap.add_argument("--sample", type=int, default=3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--no-store", action="store_true")
    a = ap.parse_args()
    years = [int(y) for y in a.years.split(",")]
    bad = check_pyramid(a.prefix.strip("/"), years)
    if not a.no_store:
        bad += check_store(a.prefix.strip("/"), a.tag, a.sample, a.seed)
    if bad:
        sys.exit(f"{bad} problems")
    log("remote product matches disk")


if __name__ == "__main__":
    main()
