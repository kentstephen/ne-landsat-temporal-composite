"""Candidate fixes for Landsat 7 SLC-off striping, on one sub-tile, one load.

Loads the looks once (runner.load_stack) with the platform of every solar
day recorded, then builds the composite under each variant and scores it
with stripe_score.score() on the NIR band, plus coverage (mean clear
count, fraction of pixels under min_clear, fraction with no look at all).

Variants (all with the fallback window and min_clear from runner.py):
  medoid, median            every look (what the store has)
  medoid_nol7, median_nol7  Landsat 7 dropped
  medoid_l7fill, median_l7fill
                            per pixel: non-L7 looks only where they reach
                            min_clear, all looks elsewhere
--span N adds the looks of the N years either side (same months), so 2012
can be tried as 2011-2013. Variant names get a "_3y" suffix for N=1.

Usage:
  uv run python src/landsat_mosaic/stripe_trials.py r04c01 2008 --only 0
Writes data/trials/stripe/<tile>_<year>_s<k>[_3y]_<variant>.npz, a PNG panel
per tile-year, and stats/stripe_trials/<tile>_<year>_s<k>[_3y].json.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import xarray as xr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from landsat_mosaic import grid, runner, stripe_score

OUT = grid.ROOT / "data/trials/stripe"
STATS = grid.ROOT / "stats/stripe_trials"


def load_with_platform(items, gb, pool) -> xr.Dataset:
    ds = runner.load_stack(items, gb, pool)
    day_of = {}
    for it in items:
        d = np.datetime64(it.datetime.date())
        day_of[d] = it.properties["platform"]
    plat = np.array([day_of.get(np.datetime64(str(d)[:10]), "?") for d in ds.time.values])
    return ds.assign_coords(platform=("time", plat))


def clear_obs_slots(ds, slots: np.ndarray, window: str, min_clear: int) -> np.ndarray:
    """runner.clear_obs restricted to the time slots in `slots` (bool)."""
    v = runner.clear_obs(ds, window, min_clear) if slots.all() else \
        runner.clear_obs(ds.isel(time=np.flatnonzero(slots)), window, min_clear)
    if slots.all():
        return v
    full = np.zeros((ds.sizes["time"], *v.shape[1:]), bool)
    full[np.flatnonzero(slots)] = v
    return full


def variant_valid(ds, variant: str, window: str, min_clear: int) -> np.ndarray:
    is7 = (ds.platform.values == "landsat-7")
    if variant.endswith("_nol7"):
        return clear_obs_slots(ds, ~is7, window, min_clear)
    if variant.endswith("_l7fill"):
        v_non = clear_obs_slots(ds, ~is7, window, min_clear)
        v_all = runner.clear_obs(ds, window, min_clear)
        enough = v_non.sum(0) >= min_clear
        return np.where(enough[None], v_non, v_all)
    return runner.clear_obs(ds, window, min_clear)


def composite(ds, variant: str, window: str, min_clear: int):
    valid = variant_valid(ds, variant, window, min_clear)
    count = np.minimum(valid.sum(0), 255).astype("uint8")
    arr = runner.to_reflectance(ds, valid)
    red = variant.split("_")[0]
    return runner.to_uint16(runner.REDUCERS[red](arr)), count


def score_composite(bands: np.ndarray, count: np.ndarray) -> dict:
    """Downsample 2x2 (level 1 equivalent) and score every 512 block."""
    nir = bands[3].astype("float32") * runner.SCALE + runner.OFFSET
    cc = count.astype("float32")
    h, w = (s // 2 * 2 for s in cc.shape)
    nir1 = nir[:h, :w].reshape(h // 2, 2, w // 2, 2).mean((1, 3))
    cc1 = cc[:h, :w].reshape(h // 2, 2, w // 2, 2).mean((1, 3))
    B = stripe_score.BLOCK
    rows = []
    for by in range(0, h // 2 - B + 1, B):
        for bx in range(0, w // 2 - B + 1, B):
            c = cc1[by:by + B, bx:bx + B]
            s = stripe_score.score(nir1[by:by + B, bx:bx + B], c, c > 0)
            if s:
                rows.append(s)
    nr = [r["nir_ratio"] for r in rows] or [float("nan")]
    ns = [r["nir_self"] for r in rows] or [float("nan")]
    return {"blocks": len(rows),
            "nir_ratio_mean": round(float(np.mean(nr)), 3),
            "nir_ratio_max": round(float(np.max(nr)), 3),
            "nir_self_mean": round(float(np.mean(ns)), 3),
            "cc_ratio_mean": round(float(np.mean([r["cc_ratio"] for r in rows])), 3) if rows else None,
            "mean_count": round(float(count[count > 0].mean()), 2) if (count > 0).any() else 0,
            "frac_lt4": round(float((count < 4).mean()), 4),
            "frac_nodata": round(float((count == 0).mean()), 4)}


def panel(results: dict, path: Path, title: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    names = list(results)
    fig, ax = plt.subplots(2, len(names), figsize=(4 * len(names), 8.5))
    for j, n in enumerate(names):
        b, c, s = results[n]
        crop = (slice(0, 1024), slice(0, 1024))
        rgb = np.stack([b[i][crop] for i in (2, 1, 0)], -1).astype("float32")
        rgb = np.clip((rgb * runner.SCALE + runner.OFFSET - 0.02) / 0.20, 0, 1)
        ax[0, j].imshow(rgb)
        ax[0, j].set_title(f"{n}\nnir {s['nir_ratio_mean']:.2f} max {s['nir_ratio_max']:.2f}", fontsize=9)
        ax[1, j].imshow(c[crop], cmap="viridis", vmin=0, vmax=16)
        ax[1, j].set_title(f"count {s['mean_count']:.1f}, <4: {s['frac_lt4']:.3f}, 0: {s['frac_nodata']:.4f}", fontsize=9)
        for a in ax[:, j]:
            a.set_axis_off()
    fig.suptitle(title)
    fig.savefig(path, dpi=100, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("tile")
    ap.add_argument("year", type=int)
    ap.add_argument("--only", type=int, default=0, help="2048 sub-tile index, row-major")
    ap.add_argument("--sub", type=int, default=2048)
    ap.add_argument("--span", type=int, default=0, help="years either side to add")
    ap.add_argument("--variants", default="medoid,median,medoid_nol7,median_nol7,medoid_l7fill,median_l7fill")
    ap.add_argument("--window", default="fallback")
    ap.add_argument("--min-clear", type=int, default=4)
    ap.add_argument("--pool", type=int, default=32)
    a = ap.parse_args()
    t0 = time.time()
    items = []
    for y in range(a.year - a.span, a.year + a.span + 1):
        items += runner.job_items(a.tile, y)
    ty, tx = int(a.tile[1:3]), int(a.tile[4:6])
    ys, xs = grid.tile_slices(ty, tx)
    tile_gb = runner.TILE_GEOBOX[ys, xs]
    h, w = tile_gb.shape
    subs = [(sy, sx) for sy in range(0, h, a.sub) for sx in range(0, w, a.sub)]
    sy, sx = subs[a.only]
    gb = tile_gb[sy:min(sy + a.sub, h), sx:min(sx + a.sub, w)]
    its = runner.items_over(items, gb)
    ds = load_with_platform(its, gb, a.pool)
    plats = dict(zip(*np.unique(ds.platform.values, return_counts=True)))
    tag = f"{a.tile}_{a.year}_s{a.only}" + (f"_{2 * a.span + 1}y" if a.span else "")
    print(f"{tag}: {len(its)} scenes, {ds.sizes['time']} days, platforms {plats}, "
          f"load {time.time() - t0:.0f}s", flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    STATS.mkdir(parents=True, exist_ok=True)
    results, stats = {}, {"scenes": len(its), "days": int(ds.sizes["time"]),
                          "platforms": {str(k): int(v) for k, v in plats.items()}}
    for v in a.variants.split(","):
        t1 = time.time()
        b, c = composite(ds, v, a.window, a.min_clear)
        s = score_composite(b, c)
        s["seconds"] = round(time.time() - t1, 1)
        results[v], stats[v] = (b, c, s), s
        np.savez_compressed(OUT / f"{tag}_{v}.npz", bands=b, count=c)
        print(f"  {v:15s} {s}", flush=True)
    (STATS / f"{tag}.json").write_text(json.dumps(stats, indent=1))
    panel(results, OUT / f"{tag}.png", tag)
    print(f"{tag}: done in {(time.time() - t0) / 60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
