"""Level 0 QA of the finished Icechunk store, before the v1 tag and export.

Per (land tile, year), every data array of the shard is read once and
counted:

- completeness: every scheduled land tile-year has data; the 16 water
  tiles are empty in every year;
- nodata consistency: count == 0 iff all six bands == 0. Pixels with
  count > 0 but a band at 0 are the USGS "nodata at cloud edges the QA
  band does not flag" case and should be rare; pixels with count == 0
  but a band set would be a runner bug;
- DN range per band over valid pixels: below 7273 (reflectance < 0.0,
  aerosol overcorrection over water and shadow), above 43636 (> 1.0,
  bright targets, snow, saturation), above 65455 (outside the Collection
  2 valid range), exactly 65535 (saturated);
- the clear_count histogram.

Results per tile-year and per year go to stats/qa_level0.json; the
summary prints a per-year table and lists the tile-years with the worst
band-zero-with-count and out-of-range shares. Read only.

Usage:
  uv run python src/landsat_mosaic/qa_level0.py [--workers 8] [--years 2000-2025]
  uv run python src/landsat_mosaic/qa_level0.py --summary
"""
import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import zarr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from landsat_mosaic import grid, init_store

OUT = grid.ROOT / "stats" / "qa_level0.json"
BANDS = init_store.BANDS
LO, HI, VALID_MAX, SAT = 7273, 43636, 65455, 65535


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def open_group(ref: str = "main") -> zarr.Group:
    repo = init_store.open_repo()
    return zarr.open_group(repo.readonly_session(ref).store, mode="r")[init_store.GROUP]


def land_tiles() -> list[str]:
    return sorted(pd.read_parquet(grid.TILES_PATH).query("land").tile)


def qa_one(tile: str, year: int, ref: str) -> dict:
    g = open_group(ref)
    ty, tx = int(tile[1:3]), int(tile[4:6])
    ys, xs = grid.tile_slices(ty, tx)
    t = year - init_store.YEARS[0]
    count = g["clear_count"][t, ys, xs]
    valid = count > 0
    r = {"tile": tile, "year": year, "px": int(count.size), "valid": int(valid.sum()),
         "count_hist": np.bincount(count.ravel(), minlength=256).tolist(), "bands": {}}
    any_band = np.zeros(count.shape, bool)
    for b in BANDS:
        a = g[b][t, ys, xs]
        nz = a > 0
        any_band |= nz
        v = a[valid]
        r["bands"][b] = {
            "zero_with_count": int((valid & ~nz).sum()),
            "below_lo": int((v < LO).sum()) - int((v == 0).sum()),
            "above_hi": int((v > HI).sum()),
            "above_valid": int((v > VALID_MAX).sum()),
            "saturated": int((v == SAT).sum()),
        }
    r["band_without_count"] = int((any_band & ~valid).sum())
    return r


def water_empty(ref: str, years: list[int]) -> list:
    """(tile, year) pairs where a water tile has any data (stride-8 read)."""
    g = open_group(ref)
    land = set(land_tiles())
    bad = []
    for year in years:
        c = g["clear_count"][year - init_store.YEARS[0], ::8, ::8]
        for ty in range(grid.NTILE_Y):
            for tx in range(grid.NTILE_X):
                tid = grid.tile_id(ty, tx)
                if tid in land:
                    continue
                ys, xs = grid.tile_slices(ty, tx)
                if c[ys.start // 8:-(-ys.stop // 8), xs.start // 8:-(-xs.stop // 8)].any():
                    bad.append((tid, year))
    return bad


def run(ref: str, years: list[int], workers: int) -> dict:
    tiles = land_tiles()
    jobs = [(t, y) for y in years for t in tiles]
    log(f"{len(jobs)} land tile-years, {workers} workers")
    t0 = time.time()
    results = []
    with ProcessPoolExecutor(workers) as pool:
        futs = {pool.submit(qa_one, t, y, ref): (t, y) for t, y in jobs}
        for i, f in enumerate(as_completed(futs), 1):
            results.append(f.result())
            if i % 50 == 0 or i == len(jobs):
                log(f"{i}/{len(jobs)}, {(time.time() - t0) / 60:.1f} min")
    results.sort(key=lambda r: (r["year"], r["tile"]))
    water_bad = water_empty(ref, years)
    log(f"water tiles with data: {water_bad}")
    return {"ref": ref, "when": datetime.now().isoformat(timespec="seconds"),
            "water_tiles_with_data": water_bad, "tile_years": results}


def summary(out: dict) -> str:
    rs = out["tile_years"]
    lines = [f"ref {out['ref']} at {out['when']}: {len(rs)} land tile-years read",
             f"water tiles with data: {out['water_tiles_with_data'] or 'none'}"]
    empty = [(r["tile"], r["year"]) for r in rs if r["valid"] == 0]
    lines.append(f"empty land tile-years: {empty or 'none'}")
    bwc = sum(r["band_without_count"] for r in rs)
    lines.append(f"band set where count == 0: {bwc} pixels (must be 0)")
    lines.append("")
    hdr = f"{'year':>4} {'valid%':>6} {'cnt_mean':>8} {'cnt_p10':>7} {'cnt_p90':>7} " \
          f"{'zero_w_cnt%':>11} {'<0.0%':>7} {'>1.0%':>7} {'>valid':>7} {'sat':>7}"
    lines.append(hdr)
    for year in sorted({r["year"] for r in rs}):
        yr = [r for r in rs if r["year"] == year]
        px = sum(r["px"] for r in yr)
        valid = sum(r["valid"] for r in yr)
        h = np.sum([r["count_hist"] for r in yr], axis=0).astype(float)
        h[0] = 0
        v = np.arange(256)
        cum = np.cumsum(h) / max(h.sum(), 1)
        p10, p90 = int(np.searchsorted(cum, 0.1)), int(np.searchsorted(cum, 0.9))
        mean = (h * v).sum() / max(h.sum(), 1)
        tot = {k: sum(r["bands"][b][k] for r in yr for b in BANDS)
               for k in ("zero_with_count", "below_lo", "above_hi", "above_valid", "saturated")}
        nv = max(valid * len(BANDS), 1)
        lines.append(f"{year:>4} {100 * valid / px:6.1f} {mean:8.2f} {p10:7d} {p90:7d} "
                     f"{100 * tot['zero_with_count'] / nv:11.4f} {100 * tot['below_lo'] / nv:7.3f} "
                     f"{100 * tot['above_hi'] / nv:7.3f} {tot['above_valid']:7d} {tot['saturated']:7d}")
    lines.append("")
    lines.append("per band, all years (share of valid pixels):")
    allv = max(sum(r["valid"] for r in rs), 1)
    for b in BANDS:
        tot = {k: sum(r["bands"][b][k] for r in rs)
               for k in ("zero_with_count", "below_lo", "above_hi", "above_valid", "saturated")}
        lines.append(f"  {b:6s} zero_w_cnt {100 * tot['zero_with_count'] / allv:.4f}%  <0.0 "
                     f"{100 * tot['below_lo'] / allv:.3f}%  >1.0 {100 * tot['above_hi'] / allv:.3f}%  "
                     f">valid {tot['above_valid']}  sat {tot['saturated']}")
    lines.append("")
    worst = sorted(rs, key=lambda r: -sum(r["bands"][b]["zero_with_count"] for b in BANDS) / max(r["valid"], 1))[:8]
    lines.append("worst band-zero-with-count tile-years (share of valid px, summed over bands):")
    for r in worst:
        z = sum(r["bands"][b]["zero_with_count"] for b in BANDS)
        lines.append(f"  {r['tile']} {r['year']}: {100 * z / max(r['valid'], 1):.4f}%  ({z} px)")
    worst = sorted(rs, key=lambda r: -sum(r["bands"][b]["below_lo"] for b in BANDS) / max(r["valid"], 1))[:8]
    lines.append("worst below-0.0 tile-years:")
    for r in worst:
        z = sum(r["bands"][b]["below_lo"] for b in BANDS)
        lines.append(f"  {r['tile']} {r['year']}: {100 * z / max(6 * r['valid'], 1):.3f}%  ({z} px)")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default="main")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--years", default=f"{init_store.YEARS[0]}-{init_store.YEARS[-1]}")
    ap.add_argument("--summary", action="store_true")
    a = ap.parse_args()
    if a.summary:
        out = json.load(open(OUT))
    else:
        y0, y1 = (int(v) for v in a.years.split("-"))
        out = run(a.ref, [y for y in init_store.YEARS if y0 <= y <= y1], a.workers)
        OUT.parent.mkdir(exist_ok=True)
        json.dump(out, open(OUT, "w"))
        log(f"wrote {OUT}")
    print(summary(out))


if __name__ == "__main__":
    main()
