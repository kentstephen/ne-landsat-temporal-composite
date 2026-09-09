"""Recover, per pixel, which look the stored medoid picked: own year, a
neighbour year, or Landsat 7. The provenance plane the v4 ladder did not
write (PLAN "What the rebuild costs", 2026-09-08).

The medoid stores one real observation unaltered, so the source can be
found after the fact: reload the same scene stack the build loaded
(runner.job_items + runner.neighbour_items, runner.load_stack), rebuild the
ladder's valid mask (runner.valid_obs, the same call batch.py made), convert
each slot through the same float32 path (to_reflectance, to_uint16), and
take the first valid slot whose six bands equal the stored six bands. The
medoid's argmin also takes the first of equal candidates, so ties resolve
the same way. The ladder's count is compared against the stored
clear_count as a check that the stack was reproduced.

Output: data/source_v4.zarr, group leafon, arrays on the level 0 grid,
chunks 1x512x512 in 1x4096x4096 shards (one shard per tile-year, so
parallel workers on disjoint job lists are safe):
  source uint8 [time, y, x]
    0 own-year look, not Landsat 7
    1 look from the year before (the ladder's second rung)
    2 look from the year after (second rung)
    3 own-year Landsat 7 look (third rung)
    4 nodata in the store (all bands 0)
    5 stored value matched no valid look (should be ~0; logged)
  255 not computed
  pick_doy uint16 [time, y, x]: day of year (1-366) of the picked look, in
    its own calendar year; 0 where there is no pick. With `source` this
    gives the pick's date, so a borrowed look's season can be compared
    with the own-year looks it replaced.
Per job stats go to stats/source_pass/<tile>_<year>.json; --summary
aggregates them into stats/source_pass/summary.json and prints a table.

Usage:
  uv run python src/landsat_mosaic/source_pass.py --init
  uv run python src/landsat_mosaic/source_pass.py r01c05 2014 [--only K]
  uv run python src/landsat_mosaic/source_pass.py --jobs data/l7fill_jobs.txt [--shard 0/2]
  uv run python src/landsat_mosaic/source_pass.py --summary
Jobs already in the source store are skipped unless --force.
"""
import argparse
import json
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import numpy as np
import zarr
from zarr.codecs import BytesCodec, ZstdCodec
from zarr.storage import LocalStore

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from landsat_mosaic import grid, init_store, runner

OUT_PATH = grid.ROOT / "data/source_v4.zarr"
STATS = grid.ROOT / "stats/source_pass"
CLASSES = ["own", "before", "after", "l7", "nodata", "unmatched"]
NEIGHBOUR = (1, 2)
NOT_RUN = 255
MIN_CLEAR, FILL_MIN_CLEAR, WINDOW = 4, 3, "fallback"       # what batch.py ran for v4


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- stores

def init_out() -> None:
    root = zarr.open_group(LocalStore(OUT_PATH), mode="a")
    if init_store.GROUP in root:
        log(f"{OUT_PATH} already has {init_store.GROUP}, leaving it")
        return
    g = root.create_group(init_store.GROUP)
    g.attrs.update({
        "title": "Source of each stored medoid pick, v4 leafon composites",
        "classes": {str(i): c for i, c in enumerate(CLASSES)}, "not_computed": NOT_RUN,
        "rule": f"own-year non-L7 looks where they reach {FILL_MIN_CLEAR}; else own plus "
                f"neighbour-year (+-1) non-L7; else every own-year look plus neighbour non-L7",
        "crs": "EPSG:4326", "resolution_deg": grid.RES,
    })
    shape = (len(init_store.YEARS), grid.HEIGHT, grid.WIDTH)
    g.create_array("source", shape=shape, dtype="uint8", chunks=init_store.CHUNK,
                   shards=init_store.SHARD, fill_value=NOT_RUN,
                   serializer=BytesCodec(), compressors=[ZstdCodec(level=3)],
                   dimension_names=["time", "y", "x"])
    d = g.create_array("pick_doy", shape=shape, dtype="uint16", chunks=init_store.CHUNK,
                       shards=init_store.SHARD, fill_value=0,
                       serializer=BytesCodec(), compressors=[ZstdCodec(level=3)],
                       dimension_names=["time", "y", "x"])
    d.attrs.update({"long_name": "day of year of the picked look, in its own year", "nodata": 0})
    t = g.create_array("time", shape=(len(init_store.YEARS),), dtype="int32", dimension_names=["time"])
    t[:] = np.array(init_store.YEARS, dtype="int32")
    y = g.create_array("y", shape=(grid.HEIGHT,), dtype="float64", chunks=(grid.HEIGHT,), dimension_names=["y"])
    y[:] = grid.y_coords()
    x = g.create_array("x", shape=(grid.WIDTH,), dtype="float64", chunks=(grid.WIDTH,), dimension_names=["x"])
    x[:] = grid.x_coords()
    log(f"created {OUT_PATH}")


def out_group(mode: str) -> zarr.Group:
    return zarr.open_group(LocalStore(OUT_PATH), mode=mode)[init_store.GROUP]


def main_group() -> zarr.Group:
    repo = init_store.open_repo()
    return zarr.open_group(repo.readonly_session("main").store, mode="r")[init_store.GROUP]


# ------------------------------------------------------------ classify

def stored_uint16(ds, k: int) -> np.ndarray:
    """Slot k through the build's exact path: float32 reflectance, back to uint16."""
    raw = np.stack([ds[b].values[k] for b in runner.SRC_BANDS]).astype("float32")
    return runner.to_uint16(raw * runner.SCALE + runner.OFFSET)


def classify(ds, bands: np.ndarray, count: np.ndarray, year: int) -> tuple[np.ndarray, np.ndarray, dict]:
    """bands [6, y, x] uint16 and count [y, x] uint8 as stored; returns the
    source plane, the pick day-of-year plane and the check stats."""
    valid = runner.valid_obs(ds, WINDOW, MIN_CLEAR, True, FILL_MIN_CLEAR, year)
    n_valid = np.minimum(valid.sum(0), 255).astype("uint8")
    yr = ds.time.dt.year.values
    doy = ds.time.dt.dayofyear.values.astype("uint16")
    is7 = ds.platform.values == runner.L7
    own = np.isin(yr, runner.own_years(year))
    src = np.full(bands.shape[1:], 5, "uint8")
    pick = np.zeros(bands.shape[1:], "uint16")
    nodata = (bands == 0).all(0)
    src[nodata] = 4
    todo = ~nodata
    for k in range(ds.sizes["time"]):
        if not todo.any():
            break
        eq = (stored_uint16(ds, k) == bands).all(0) & valid[k] & todo
        if not eq.any():
            continue
        src[eq] = 3 if is7[k] else (0 if own[k] else (1 if yr[k] < year else 2))
        pick[eq] = doy[k]
        todo &= ~eq
    n = src.size
    st = {c: float((src == i).sum() / n) for i, c in enumerate(CLASSES)}
    st["neighbour"] = st["before"] + st["after"]
    st["doy_median"] = {c: (float(np.median(pick[src == i])) if (src == i).any() else None)
                        for i, c in enumerate(CLASSES[:4])}
    st["count_equal"] = float((n_valid == count).mean())
    st["count_equal_nonzero"] = float((n_valid == count)[count > 0].mean()) if (count > 0).any() else 1.0
    return src, pick, st


# ------------------------------------------------------------------ jobs

def already_done(tile: str, year: int) -> bool:
    ty, tx = int(tile[1:3]), int(tile[4:6])
    ys, xs = grid.tile_slices(ty, tx)
    return bool((out_group("r")["source"][year - init_store.YEARS[0], ys, xs] != NOT_RUN).any())


def run_job(tile: str, year: int, sub: int, pool: int, only: int | None, force: bool) -> None:
    if not force and already_done(tile, year):
        log(f"{tile} {year}: already in source store, skipping")
        return
    t0 = time.time()
    items = runner.job_items(tile, year) + runner.neighbour_items(tile, year)
    ty, tx = int(tile[1:3]), int(tile[4:6])
    ys, xs = grid.tile_slices(ty, tx)
    tile_gb = runner.TILE_GEOBOX[ys, xs]
    h, w = tile_gb.shape
    t = year - init_store.YEARS[0]
    g = main_group()
    bands = np.stack([g[b][t, ys, xs] for b in init_store.BANDS])
    count = g["clear_count"][t, ys, xs]
    src = np.full((h, w), NOT_RUN, "uint8")
    pick = np.zeros((h, w), "uint16")
    subs = [(sy, sx) for sy in range(0, h, sub) for sx in range(0, w, sub)]
    todo = [(k, sy, sx) for k, (sy, sx) in enumerate(subs) if only is None or k == only]
    log(f"{tile} {year}: {len(items)} scenes (own + neighbour), {len(todo)} sub-tiles of {sub}")

    def fetch(sy, sx):
        gb = tile_gb[sy:min(sy + sub, h), sx:min(sx + sub, w)]
        its = runner.items_over(items, gb)
        t1 = time.time()
        return its, runner.load_stack(its, gb, pool), time.time() - t1

    ex = ThreadPoolExecutor(1)
    futs = [ex.submit(fetch, sy, sx) for _, sy, sx in todo[:1]]
    stats = []
    for n, (k, sy, sx) in enumerate(todo):
        if n + 1 < len(todo):
            futs.append(ex.submit(fetch, *todo[n + 1][1:]))
        its, ds, tload = futs[n].result()
        t2 = time.time()
        s, d, st = classify(ds, bands[:, sy:sy + sub, sx:sx + sub], count[sy:sy + sub, sx:sx + sub], year)
        src[sy:sy + sub, sx:sx + sub] = s
        pick[sy:sy + sub, sx:sx + sub] = d
        st.update(sub=k, scenes=len(its), days=int(ds.sizes["time"]))
        stats.append(st)
        print(f"  sub {k:2d} ({sy},{sx}): {len(its)} scenes, {ds.sizes['time']} days, load {tload:5.0f}s, "
              f"classify {time.time() - t2:4.0f}s | own {st['own']:.3f} before {st['before']:.3f} "
              f"after {st['after']:.3f} l7 {st['l7']:.3f} nodata {st['nodata']:.3f} "
              f"unmatched {st['unmatched']:.4f} count_eq {st['count_equal']:.4f} "
              f"doy {st['doy_median']}", flush=True)
        del ds
        futs[n] = None
    ex.shutdown()
    if only is None:
        g_out = out_group("a")
        g_out["source"][t, ys, xs] = src
        g_out["pick_doy"][t, ys, xs] = pick
    done = src != NOT_RUN
    summary = {c: float(((src == i) & done).sum() / max(done.sum(), 1)) for i, c in enumerate(CLASSES)}
    summary["neighbour"] = summary["before"] + summary["after"]
    summary["doy_median"] = {c: (float(np.median(pick[src == i])) if (src == i).any() else None)
                             for i, c in enumerate(CLASSES[:4])}
    summary.update(tile=tile, year=year, pixels=int(done.sum()), partial=only is not None,
                   count_equal=float(np.mean([s["count_equal"] for s in stats])),
                   count_equal_nonzero=float(np.mean([s["count_equal_nonzero"] for s in stats])),
                   subs=stats, minutes=round((time.time() - t0) / 60, 2))
    STATS.mkdir(parents=True, exist_ok=True)
    (STATS / f"{tile}_{year}{'_partial' if only is not None else ''}.json").write_text(json.dumps(summary, indent=1))
    flag = ""
    if summary["count_equal_nonzero"] < 0.999:
        flag += " COUNT MISMATCH"
    if summary["unmatched"] > 0.001:
        flag += " UNMATCHED"
    log(f"{tile} {year}: own {summary['own']:.3f} before {summary['before']:.3f} after {summary['after']:.3f} "
        f"l7 {summary['l7']:.3f} nodata {summary['nodata']:.3f} unmatched {summary['unmatched']:.4f} "
        f"count_eq {summary['count_equal_nonzero']:.4f} doy {summary['doy_median']}, "
        f"done in {summary['minutes']:.1f} min{flag}")


def read_jobs(path: str, shard: str | None) -> list[tuple[str, int]]:
    jobs = [(ln.split()[0], int(ln.split()[1])) for ln in Path(path).read_text().splitlines() if ln.strip()]
    if shard:
        i, n = (int(v) for v in shard.split("/"))
        jobs = jobs[i::n]
    return jobs


def summary() -> None:
    files = sorted(STATS.glob("r??c??_????.json"))
    rows = [json.loads(f.read_text()) for f in files]
    if not rows:
        sys.exit("no per-job stats yet")
    by_year: dict[int, dict] = {}
    for r in rows:
        d = by_year.setdefault(r["year"], {c: 0.0 for c in CLASSES} | {"pixels": 0, "jobs": 0, "min_count_eq": 1.0})
        for c in CLASSES:
            d[c] += r[c] * r["pixels"]
        d["pixels"] += r["pixels"]; d["jobs"] += 1
        d["min_count_eq"] = min(d["min_count_eq"], r["count_equal_nonzero"])
    out = {"per_year": {}, "per_job": {f"{r['tile']} {r['year']}": {c: r[c] for c in CLASSES} for r in rows}}
    tot = {c: 0.0 for c in CLASSES}; tot_px = 0
    print(f"{'year':>4} {'jobs':>4} {'own':>6} {'before':>6} {'after':>6} {'l7':>6} {'nodata':>6} {'unmat':>7} {'min cnt_eq':>10}")
    for y in sorted(by_year):
        d = by_year[y]; px = d["pixels"]
        fr = {c: d[c] / px for c in CLASSES}
        out["per_year"][y] = fr | {"jobs": d["jobs"], "pixels": px, "min_count_equal_nonzero": d["min_count_eq"]}
        for c in CLASSES:
            tot[c] += d[c]
        tot_px += px
        print(f"{y:>4} {d['jobs']:>4} {fr['own']:6.3f} {fr['before']:6.3f} {fr['after']:6.3f} {fr['l7']:6.3f} "
              f"{fr['nodata']:6.3f} {fr['unmatched']:7.4f} {d['min_count_eq']:10.4f}")
    fr = {c: tot[c] / tot_px for c in CLASSES}
    out["all"] = fr | {"jobs": len(rows), "pixels": tot_px}
    print(f"{'all':>4} {len(rows):>4} {fr['own']:6.3f} {fr['before']:6.3f} {fr['after']:6.3f} {fr['l7']:6.3f} "
          f"{fr['nodata']:6.3f} {fr['unmatched']:7.4f}")
    (STATS / "summary.json").write_text(json.dumps(out, indent=1))
    print(f"wrote {STATS / 'summary.json'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("tile", nargs="?"); ap.add_argument("year", nargs="?", type=int)
    ap.add_argument("--init", action="store_true", help="create data/source_v4.zarr and exit")
    ap.add_argument("--jobs", default=None, help="text file of 'tile year' lines")
    ap.add_argument("--shard", default=None, help="i/n: take every n-th job starting at i")
    ap.add_argument("--only", type=int, default=None, help="one sub-tile, stats only, nothing written")
    ap.add_argument("--sub", type=int, default=2048)
    ap.add_argument("--pool", type=int, default=32)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--summary", action="store_true")
    a = ap.parse_args()
    if a.init:
        init_out(); return
    if a.summary:
        summary(); return
    if not OUT_PATH.exists():
        sys.exit(f"{OUT_PATH} missing: run --init first")
    jobs = read_jobs(a.jobs, a.shard) if a.jobs else [(a.tile, a.year)]
    if not jobs or jobs[0][0] is None:
        sys.exit("give TILE YEAR or --jobs FILE")
    log(f"{len(jobs)} jobs" + (f", shard {a.shard}" if a.shard else ""))
    t0 = time.time(); failed = []
    for i, (tile, year) in enumerate(jobs, 1):
        for attempt in range(1, a.retries + 1):
            try:
                run_job(tile, year, a.sub, a.pool, a.only, a.force)
                break
            except Exception as e:
                log(f"{tile} {year} attempt {attempt} failed: {e!r}")
                traceback.print_exc()
                if attempt < a.retries:
                    time.sleep(60 * attempt)
        else:
            failed.append(f"{tile} {year}")
        log(f"progress {i}/{len(jobs)} ({len(failed)} failed), {(time.time() - t0) / 3600:.1f} h elapsed")
    if failed:
        log(f"failed: {failed}")
    log("source pass finished")


if __name__ == "__main__":
    main()
