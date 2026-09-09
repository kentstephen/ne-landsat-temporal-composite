"""Levels 1-7 of the multiscales pyramid, from the plain level 0 export.

Reads leafon/0 of data/pyramid_v1 (written by export_zarr.py from the v1
tag) and writes leafon/1 .. leafon/7. Every level is the exact nodata-aware
mean of level 0: a 2^N block averages only its nonzero pixels, a block with
no valid pixel stays 0, the mean is rounded half up and kept as uint16 DN
(uint8 for clear_count) with the same scale and offset. Sums and valid
counts are accumulated level to level in uint32, so no level is a mean of
a mean and level 0 is read once per (year, array).

Work unit: one (year, array), 26 x 7 = 182 units. Time chunk is 1 and the
arrays are separate objects, so units never share a chunk or shard file and
workers write without coordination. A unit is done when its level 7 plane
has data (level 7 is written last); --force redoes.

Layout (fixed, S3 has no rename):
  pyramid_v1/leafon/{0..7}/{blue green red nir swir1 swir2 clear_count time y x}
  chunks [1, 512, 512] everywhere, shards [1, 4096, 4096] on levels 0-2,
  zstd 3, fill 0.

--finalize writes the zarr-conventions multiscales (v0.1), spatial and proj
attrs on leafon/ and each level, the composite provenance (reducer, Landsat 7
fill rule, year span, min_clear per year, from runner.py), scale, offset and
nodata on the bands, and the consolidated metadata at the root, last.

--verify recomputes every level from level 0 for every (year, array) and
compares each pixel; checks array definitions, coords, count == 0 iff all
bands == 0 at every level, and that the consolidated metadata matches a walk
of the store.

Usage:
  uv run python src/landsat_mosaic/pyramid.py [--out data/pyramid_v1] [--workers 6]
      [--count-reducer mean|median] [--years 2000-2025] [--arrays red,nir] [--force]
  uv run python src/landsat_mosaic/pyramid.py --finalize
  uv run python src/landsat_mosaic/pyramid.py --verify [--workers 6] [--count-reducer mean]
"""
import argparse
import json
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
from landsat_mosaic import grid, init_store

OUT_PATH = grid.ROOT / "data/pyramid_v1"
GROUP = init_store.GROUP
YEARS = init_store.YEARS
BANDS = init_store.BANDS
ARRAYS = BANDS + ["clear_count"]
NLEVELS = 8                       # 0-7
CHUNK = (1, 512, 512)
SHARD = (1, 4096, 4096)
SHARD_LEVELS = {0, 1, 2}          # plane wider than 4096 px
PAD = 2 ** (NLEVELS - 1)          # 128: pad level 0 so every level divides evenly
PAD_H, PAD_W = -(-grid.HEIGHT // PAD) * PAD, -(-grid.WIDTH // PAD) * PAD   # 26240, 27648
SCALE, OFFSET = 0.0000275, -0.2
# How level 0 was composited (runner.py), written to the store attrs by
# --finalize. Medoid every year. The Landsat 7 fill rule (runner.valid_obs)
# was on for the 2003-2023 rebuild, the years with SLC-off looks; the years
# outside it were built before the rule existed and have no SLC-off looks
# (2000-2002 Landsat 7 was SLC-on, 2024-2025 have no Landsat 7). runner.py is
# imported lazily in finalize() so build workers do not load odc-stac.
REDUCER_BY_YEAR = {y: "medoid" for y in YEARS}
L7_FILL_YEARS = set(range(2003, 2024))
MIN_CLEAR = 4                     # batch.py --min-clear default used by every build
# The v4 ladder (2026-09-07/08) is per tile-year: the tile-years listed in
# data/l7fill_jobs.txt were rebuilt with fill_min_clear 3 and the
# neighbouring years' non-L7 looks lent before Landsat 7. ladder_attrs()
# reads the truth from the Icechunk commit messages (the last commit per
# tile-year), and falls back to the job list if the store cannot be read.
LADDER_JOBS = grid.ROOT / "data/l7fill_jobs.txt"

CONVENTIONS = {
    "multiscales": {
        "schema_url": "https://raw.githubusercontent.com/zarr-conventions/multiscales/refs/tags/v0.1/schema.json",
        "spec_url": "https://github.com/zarr-conventions/multiscales/blob/v0.1/README.md",
        "uuid": "d35379db-88df-4056-af3a-620245f8e347", "name": "multiscales",
        "description": "Multiscale layout of resolution levels"},
    "spatial": {
        "schema_url": "https://raw.githubusercontent.com/zarr-conventions/spatial/refs/tags/v0.1/schema.json",
        "spec_url": "https://github.com/zarr-conventions/spatial/blob/v0.1/README.md",
        "uuid": "689b58e2-cf7b-45e0-9fff-9cfc0883d6b4", "name": "spatial",
        "description": "Spatial coordinate information"},
    "proj": {
        "schema_url": "https://raw.githubusercontent.com/zarr-conventions/proj/refs/tags/v0.1/schema.json",
        "spec_url": "https://github.com/zarr-conventions/proj/blob/v0.1/README.md",
        "uuid": "f17cb550-5864-4468-aeb7-f3180cfb622f", "name": "proj",
        "description": "Coordinate reference system information for geospatial data"},
}


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- geometry

def level_shape(n: int) -> tuple[int, int]:
    return -(-grid.HEIGHT // 2 ** n), -(-grid.WIDTH // 2 ** n)


def level_res(n: int) -> float:
    return grid.RES * 2 ** n


def level_coords(n: int) -> tuple[np.ndarray, np.ndarray]:
    """Pixel centres from the level 0 transform, never from coarsened coords."""
    h, w = level_shape(n)
    r = level_res(n)
    return grid.NORTH - (np.arange(h) + 0.5) * r, grid.WEST + (np.arange(w) + 0.5) * r


def level_bbox(n: int) -> list[float]:
    h, w = level_shape(n)
    r = level_res(n)
    return [grid.WEST, grid.NORTH - h * r, grid.WEST + w * r, grid.NORTH]


def level_transform(n: int) -> list[float]:
    r = level_res(n)
    return [r, 0.0, grid.WEST, 0.0, -r, grid.NORTH]


# ---------------------------------------------------------------- store

def open_root(out: Path, mode: str) -> zarr.Group:
    return zarr.open_group(LocalStore(out), mode=mode, use_consolidated=False)


def init_levels(out: Path) -> None:
    """Create leafon/1..7 with the level 0 array definitions rechunked per level."""
    root = open_root(out, "r+")
    l0 = root[f"{GROUP}/0"]
    made = 0
    for n in range(1, NLEVELS):
        path = f"{GROUP}/{n}"
        if (out / path / "zarr.json").exists():
            continue
        g = root.require_group(path)
        h, w = level_shape(n)
        for name in ARRAYS:
            a = l0[name]
            g.create_array(
                name, shape=(len(YEARS), h, w), dtype=a.dtype, chunks=CHUNK,
                shards=SHARD if n in SHARD_LEVELS else None, fill_value=0,
                serializer=BytesCodec(), compressors=[ZstdCodec(level=3)],
                dimension_names=["time", "y", "x"], attributes=dict(a.attrs),
            )
        t = g.create_array("time", shape=l0["time"].shape, dtype=l0["time"].dtype,
                           dimension_names=["time"], attributes=dict(l0["time"].attrs))
        t[:] = l0["time"][:]
        y, x = level_coords(n)
        for name, v in (("y", y), ("x", x)):
            c = g.create_array(name, shape=v.shape, dtype="float64", chunks=v.shape,
                               dimension_names=[name], attributes=dict(l0[name].attrs))
            c[:] = v
        made += 1
    log(f"levels created: {made} new, {NLEVELS - 1 - made} present")


# ---------------------------------------------------------------- reducers

def _pad(plane: np.ndarray) -> np.ndarray:
    p = np.zeros((PAD_H, PAD_W), plane.dtype)
    p[:grid.HEIGHT, :grid.WIDTH] = plane
    return p


def _sum2(a: np.ndarray) -> np.ndarray:
    h, w = a.shape
    return a.reshape(h // 2, 2, w // 2, 2).sum(axis=(1, 3), dtype=np.uint32)


def mean_levels(plane: np.ndarray) -> dict[int, np.ndarray]:
    """Levels 1..7 as the exact nodata-aware mean of a level 0 plane, half-up."""
    p = _pad(plane)
    s, n = _sum2(p), _sum2((p > 0).astype(np.uint8))
    del p
    out = {}
    for lv in range(1, NLEVELS):
        if lv > 1:
            s, n = _sum2(s), _sum2(n)
        valid = n > 0
        m = np.zeros(s.shape, plane.dtype)
        m[valid] = ((2 * s[valid] + n[valid]) // (2 * n[valid])).astype(plane.dtype)
        h, w = level_shape(lv)
        out[lv] = m[:h, :w]
    return out


def median_levels(plane: np.ndarray) -> dict[int, np.ndarray]:
    """Levels 1..7 as the lower median of the valid pixels of each 2^N block,
    from level 0 directly (median does not compose). uint8 planes only."""
    assert plane.dtype == np.uint8
    p = _pad(plane)
    out = {}
    for lv in range(1, NLEVELS):
        k = 2 ** lv
        bh, bw = PAD_H // k, PAD_W // k
        b = p.reshape(bh, k, bw, k).transpose(0, 2, 1, 3).reshape(bh, bw, k * k)
        n = (b > 0).sum(axis=2, dtype=np.int32)
        s = np.where(b > 0, b, 255)
        s.sort(axis=2)
        idx = np.maximum(n - 1, 0) // 2
        med = np.take_along_axis(s, idx[..., None], axis=2)[..., 0]
        med[n == 0] = 0
        h, w = level_shape(lv)
        out[lv] = med[:h, :w].astype(np.uint8)
    return out


def compute_levels(plane: np.ndarray, name: str, count_reducer: str) -> dict[int, np.ndarray]:
    if name == "clear_count" and count_reducer == "median":
        return median_levels(plane)
    return mean_levels(plane)


# ---------------------------------------------------------------- build

def units(years: tuple[int, int], arrays: list[str]) -> list[tuple[int, str]]:
    return [(y, a) for y in YEARS if years[0] <= y <= years[1] for a in arrays]


def build_unit(out: Path, year: int, name: str, count_reducer: str, force: bool) -> str:
    root = open_root(out, "r+")
    t = YEARS.index(year)
    if not force and root[f"{GROUP}/{NLEVELS - 1}"][name][t].any():
        return "present"
    plane = root[f"{GROUP}/0"][name][t]
    if not plane.any():
        return "empty"
    levels = compute_levels(plane, name, count_reducer)
    del plane
    for lv in range(1, NLEVELS):
        root[f"{GROUP}/{lv}"][name][t] = levels[lv]
    return "built"


def run_pool(fn, out: Path, jobs: list, workers: int, *args) -> list:
    """fn(out, year, name, *args) for every (year, name), spawn context, logged."""
    t0 = time.time()
    results = []
    with ProcessPoolExecutor(workers, mp_context=mp.get_context("spawn")) as pool:
        futs = {pool.submit(fn, out, y, a, *args): (y, a) for y, a in jobs}
        for i, f in enumerate(as_completed(futs), 1):
            y, a = futs[f]
            try:
                r = f.result()
            except Exception as e:
                r = f"failed: {e!r}"
            results.append((y, a, r))
            log(f"{i}/{len(jobs)} {y} {a} {r}, {(time.time() - t0) / 60:.1f} min")
    return results


def build(out: Path, workers: int, years: tuple[int, int], arrays: list[str],
          count_reducer: str, force: bool) -> None:
    init_levels(out)
    jobs = units(years, arrays)
    log(f"{len(jobs)} units, {workers} workers, clear_count reducer {count_reducer}")
    res = run_pool(build_unit, out, jobs, workers, count_reducer, force)
    tally = {}
    for _, _, r in res:
        tally[r.split(":")[0]] = tally.get(r.split(":")[0], 0) + 1
    log(f"done: {tally}")
    if tally.get("failed"):
        sys.exit(1)


# ---------------------------------------------------------------- finalize

def wkt2_4326() -> str | None:
    try:
        from pyproj import CRS
        return CRS.from_epsg(4326).to_wkt()
    except Exception:
        return None


def ladder_attrs() -> dict:
    """Which tile-years hold the neighbour-year ladder, from the store's own
    commit messages ("<tile> <year> ... fill_min_clear=3 neighbours=True"),
    newest commit per tile-year wins. Never raises: finalize runs unattended
    at the end of a refresh chain, so a store that cannot be read gives the
    job-list answer with a note saying so."""
    import re
    from landsat_mosaic import runner
    listed = set()
    if LADDER_JOBS.exists():
        listed = {tuple(ln.split()[:2]) for ln in LADDER_JOBS.read_text().splitlines() if ln.strip()}
    pat = re.compile(r"^(r\d\dc\d\d) (\d{4}) .*fill_min_clear=(\w+) neighbours=(\w+)")
    seen, ladder, source = {}, set(), "icechunk commit messages on main"
    try:
        repo = init_store.open_repo()
        for snap in repo.ancestry(branch="main"):
            m = pat.match(snap.message)
            if not m or (m.group(1), m.group(2)) in seen:
                continue
            seen[(m.group(1), m.group(2))] = (m.group(3), m.group(4))
            if m.group(3) == "3" and m.group(4) == "True":
                ladder.add((m.group(1), m.group(2)))
        if listed and ladder != listed:
            log(f"ladder_attrs: store has {len(ladder)} ladder tile-years, job list {len(listed)}; "
                f"only in store {sorted(ladder - listed)[:5]}, only in list {sorted(listed - ladder)[:5]}")
    except Exception as e:                       # noqa: BLE001
        log(f"ladder_attrs: could not read the store ({e!r}), using {LADDER_JOBS.name}")
        ladder, source = listed, f"{LADDER_JOBS.name} (store not readable at finalize)"
    tile_years = sorted(f"{t} {y}" for t, y in ladder)
    by_year = {}
    for t, y in ladder:
        by_year.setdefault(y, []).append(t)
    return {
        "l7_ladder_tile_years": tile_years,
        "l7_ladder_tiles_by_year": {y: sorted(ts) for y, ts in sorted(by_year.items())},
        "l7_ladder_fill_min_clear": 3,
        "l7_ladder_neighbour_span": runner.NEIGHBOUR_SPAN,
        "l7_ladder_note": f"{len(tile_years)} tile-years (4096 px tiles, rows r, columns c) were rebuilt "
                          "with the ladder: at each pixel the own-year non-Landsat 7 looks where they reach "
                          "3; else own-year plus the neighbouring years' (+-1) non-Landsat 7 looks where "
                          "they reach 3; else every own-year look, Landsat 7 included, plus the neighbour "
                          "non-Landsat 7 looks. A pixel on the second rung is an observation from the year "
                          "before or after, so in these tile-years the composite year is not guaranteed per "
                          "pixel; the source array (level 0) and borrowed_pct (all levels) say where. "
                          "Every other tile-year uses its own year's looks only, threshold min_clear (4). "
                          f"Landsat 5 scene edges are eroded by {runner.L5_EDGE_ROWS} rows (bumper-mode "
                          "combs) in the ladder tile-years.",
        "l7_ladder_source": source,
    }


def composite_attrs() -> dict:
    """How each year of level 0 was composited: reducer, the Landsat 7 fill
    rule, the neighbouring years folded in, min_clear, and the per-tile-year
    ladder. Read from runner.py and the store so the attrs cannot drift from
    the code and commits that built the data."""
    from landsat_mosaic import runner
    assert set(runner.YEAR_SPAN) <= L7_FILL_YEARS, "YEAR_SPAN year outside the fill-rule rebuild"
    return {
        **ladder_attrs(),
        "reducer_by_year": {str(y): r for y, r in REDUCER_BY_YEAR.items()},
        "reducer_note": "medoid: each pixel is one real observation, the clear look nearest the "
                        "per-pixel median in band space, so spectra are never blends",
        "min_clear": MIN_CLEAR,
        "l7_fill_by_year": {str(y): y in L7_FILL_YEARS for y in YEARS},
        "l7_fill_note": f"true: at each pixel the Landsat 7 looks count only where the other "
                        f"platforms give fewer than min_clear ({MIN_CLEAR}) clear looks, so the "
                        "SLC-off gap pattern does not change the look set where Landsat 5, 8 or 9 "
                        "suffice (suppresses gap-aligned striping); false: every clear look counts "
                        "(years with no SLC-off looks). The tile-years in l7_ladder_tile_years use "
                        "the ladder instead (threshold 3, neighbour years lent), see l7_ladder_note",
        "year_span_by_year": {str(y): [y - s, y + s] for y, s in sorted(runner.YEAR_SPAN.items())},
        "year_span_note": "years whose composite also draws on the neighbouring years listed "
                          "(2012 had Landsat 7 alone, so 2011-2013 looks are pooled under the fill "
                          "rule); every other year uses its own looks only",
    }


def finalize(out: Path) -> None:
    """multiscales on leafon/, spatial and proj on every level, band attrs,
    then consolidated metadata at the root. Rerun after any structural change."""
    root = open_root(out, "r+")
    g = root[GROUP]
    base = {k: v for k, v in dict(g["0"].attrs).items()
            if not k.startswith(("spatial:", "proj:")) and k not in ("zarr_conventions", "level")}
    spatial_common = {"spatial:dimensions": ["y", "x"], "spatial:registration": "pixel"}
    proj = {"proj:code": "EPSG:4326"}
    wkt = wkt2_4326()
    if wkt:
        proj["proj:wkt2"] = wkt
    layout = []
    for n in range(NLEVELS):
        item = {"asset": str(n), "transform": {"scale": [float(2 ** n)] * 2, "translation": [0.0, 0.0]},
                "spatial:shape": list(level_shape(n)), "spatial:transform": level_transform(n),
                "spatial:bbox": level_bbox(n)}
        if n > 0:
            item["derived_from"] = "0"
            item["resampling_method"] = "average"
        layout.append(item)
    g.attrs.put({
        **base, "resolution_deg": [level_res(n) for n in range(NLEVELS)],
        "zarr_conventions": [CONVENTIONS["multiscales"], CONVENTIONS["spatial"], CONVENTIONS["proj"]],
        "multiscales": {"layout": layout},
        **spatial_common, "spatial:shape": list(level_shape(0)), "spatial:transform": level_transform(0),
        "spatial:bbox": level_bbox(0), **proj,
        "levels": "level N is the nodata-aware mean of 2^N x 2^N level 0 pixels, rounded half up; "
                  "0 (nodata) is excluded from the mean and a block with no valid pixel stays 0",
        **composite_attrs(),
    })
    for n in range(NLEVELS):
        lg = g[str(n)]
        lg.attrs.put({
            **base, "level": n, "resolution_deg": level_res(n),
            "zarr_conventions": [CONVENTIONS["spatial"], CONVENTIONS["proj"]],
            **spatial_common, "spatial:shape": list(level_shape(n)),
            "spatial:transform": level_transform(n), "spatial:bbox": level_bbox(n), **proj,
            **composite_attrs(),
        })
        for name in BANDS:
            lg[name].attrs.update({"scale": SCALE, "offset": OFFSET, "nodata": 0,
                                   "long_name": f"{name} surface reflectance, DN * scale + offset",
                                   "spatial:dimensions": ["y", "x"]})
        lg["clear_count"].attrs.update({"nodata": 0, "spatial:dimensions": ["y", "x"],
                                        "long_name": "clear observations used by the composite"
                                        + ("" if n == 0 else ", mean over the block")})
    zarr.consolidate_metadata(root.store)
    log(f"attrs written on {GROUP}/ and {NLEVELS} levels, consolidated metadata at {out}")


# ---------------------------------------------------------------- verify

def verify_unit(out: Path, year: int, name: str, count_reducer: str) -> str:
    root = open_root(out, "r")
    t = YEARS.index(year)
    plane = root[f"{GROUP}/0"][name][t]
    levels = compute_levels(plane, name, count_reducer)
    del plane
    bad = []
    for lv in range(1, NLEVELS):
        stored = root[f"{GROUP}/{lv}"][name][t]
        if not np.array_equal(stored, levels[lv]):
            bad.append((lv, int((stored != levels[lv]).sum())))
    return "ok" if not bad else f"mismatch: {bad}"


def verify_nodata(out: Path, year: int, _name: str) -> str:
    """count == 0 iff all bands == 0 at every level, for one year."""
    root = open_root(out, "r")
    t = YEARS.index(year)
    bad = []
    for lv in range(NLEVELS):
        g = root[f"{GROUP}/{lv}"]
        c0 = g["clear_count"][t] == 0
        allzero = np.ones(c0.shape, bool)
        for b in BANDS:
            allzero &= g[b][t] == 0
        if not np.array_equal(c0, allzero):
            bad.append((lv, int((c0 != allzero).sum())))
    return "ok" if not bad else f"count/band nodata differ: {bad}"


def verify_definitions(out: Path) -> None:
    root = open_root(out, "r")
    l0 = root[f"{GROUP}/0"]
    for n in range(NLEVELS):
        g = root[f"{GROUP}/{n}"]
        h, w = level_shape(n)
        for name in ARRAYS:
            a = g[name]
            want = ((len(YEARS), h, w), str(l0[name].dtype), CHUNK, SHARD if n in SHARD_LEVELS else None, 0)
            got = (a.shape, str(a.dtype), a.chunks, a.shards, a.fill_value)
            assert got == want, f"level {n} {name}: {got} != {want}"
            assert a.metadata.dimension_names == ("time", "y", "x"), f"level {n} {name} dims"
        assert np.array_equal(g["time"][:], l0["time"][:]), f"level {n} time"
        y, x = level_coords(n)
        assert np.array_equal(g["y"][:], y) and np.array_equal(g["x"][:], x), f"level {n} coords"
        assert g.attrs.get("level") == n and "spatial:transform" in g.attrs, f"level {n} attrs"
    ms = root[GROUP].attrs.get("multiscales", {}).get("layout", [])
    assert [m["asset"] for m in ms] == [str(n) for n in range(NLEVELS)], "multiscales layout"
    log("array definitions, coords, and level attrs verified on all levels")


def verify_consolidated(out: Path) -> None:
    walk = zarr.open_group(LocalStore(out), mode="r", use_consolidated=False)
    cons = zarr.open_group(LocalStore(out), mode="r", use_consolidated=True)
    assert cons.metadata.consolidated_metadata is not None, "no consolidated metadata at the root"
    def dump(node) -> str:      # group nodes in the consolidated view carry their children here
        d = {k: v for k, v in node.metadata.to_dict().items() if k != "consolidated_metadata"}
        return json.dumps(d, sort_keys=True, default=str)
    a = {p: dump(n) for p, n in walk.members(max_depth=None)}
    b = {p: dump(n) for p, n in cons.members(max_depth=None)}
    assert a.keys() == b.keys(), f"consolidated members differ: {set(a) ^ set(b)}"
    stale = [p for p in a if a[p] != b[p]]
    assert not stale, f"consolidated metadata stale for {stale}"
    log(f"consolidated metadata matches a walk of the store ({len(a)} nodes)")


def verify(out: Path, workers: int, years: tuple[int, int], arrays: list[str], count_reducer: str) -> None:
    verify_definitions(out)
    verify_consolidated(out)
    jobs = units(years, arrays)
    log(f"recomputing {len(jobs)} units from level 0")
    res = run_pool(verify_unit, out, jobs, workers, count_reducer)
    bad = [(y, a, r) for y, a, r in res if r != "ok"]
    log(f"nodata coincidence, {len(set(y for y, _ in jobs))} years")
    res2 = run_pool(verify_nodata, out, [(y, "all") for y in sorted({y for y, _ in jobs})], workers)
    bad += [(y, a, r) for y, a, r in res2 if r != "ok"]
    if bad:
        for b in bad:
            log(f"BAD {b}")
        sys.exit(f"{len(bad)} problems")
    log(f"verified: every pixel of every level for {len(jobs)} units, nodata coincidence on all levels")


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--count-reducer", choices=["mean", "median"], default="mean")
    ap.add_argument("--years", default=f"{YEARS[0]}-{YEARS[-1]}")
    ap.add_argument("--arrays", default=",".join(ARRAYS))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--finalize", action="store_true")
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    years = tuple(int(v) for v in a.years.split("-"))
    arrays = a.arrays.split(",")
    if a.finalize:
        finalize(a.out)
    elif a.verify:
        verify(a.out, a.workers, years, arrays, a.count_reducer)
    else:
        build(a.out, a.workers, years, arrays, a.count_reducer, a.force)


if __name__ == "__main__":
    main()
