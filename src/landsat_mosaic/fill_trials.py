"""Trial the fallback ladder for Landsat 7 filled pixels, on one window.

Loads year-1, year, year+1 once over a level 0 window, then composites
(medoid, fallback window) under:
  fill4   the store's rule: own-year non-L7 looks where they reach 4, else
          every own-year look
  fill3   same with the fill threshold at 3 (window logic stays at 4)
  nbr4    ladder: own non-L7 >= 4; else own + neighbour-year non-L7 >= 4;
          else every own look plus the neighbour non-L7 looks
  nbr3    the ladder with threshold 3
Scores each (stripe_trials.score_composite) and records where each pixel's
observation came from: own year non-L7, a neighbour year, or Landsat 7.

Usage: uv run python src/landsat_mosaic/fill_trials.py r04c01 2020 --row 16720 --col 6880
Writes data/trials/fill/<tile>_<year>.png and stats/fill_trials/<tile>_<year>.json
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from landsat_mosaic import grid, runner, stripe_trials

OUT = grid.ROOT / "data/trials/fill"
STATS = grid.ROOT / "stats/fill_trials"
L7 = runner.L7


def ladder(ds, year: int, k: int, neighbours: bool, window="fallback", min_clear=4):
    yr = ds.time.dt.year.values
    is7 = ds.platform.values == L7
    own = yr == year
    v_own_non = stripe_trials.clear_obs_slots(ds, own & ~is7, window, min_clear)
    n1 = v_own_non.sum(0)
    valid = v_own_non.copy()
    short = n1 < k
    if neighbours:
        v_all_non = stripe_trials.clear_obs_slots(ds, ~is7, window, min_clear)
        valid[:, short] = v_all_non[:, short]
        short = v_all_non.sum(0) < k
        v_last = stripe_trials.clear_obs_slots(ds, own | ~is7, window, min_clear)
    else:
        v_last = stripe_trials.clear_obs_slots(ds, own, window, min_clear)
    valid[:, short] = v_last[:, short]
    return valid


def source_of(arr, picked, ds, year):
    """Per pixel: 0 own-year non-L7, 1 neighbour year, 2 Landsat 7, 3 nodata."""
    same = np.isclose(arr, picked[:, None], equal_nan=True).all(0)      # time, y, x
    idx = same.argmax(0)
    yr = ds.time.dt.year.values[idx]
    is7 = (ds.platform.values == L7)[idx]
    src = np.where(is7, 2, np.where(yr != year, 1, 0)).astype("uint8")
    src[~np.isfinite(picked).all(0)] = 3
    return src


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tile"); ap.add_argument("year", type=int)
    ap.add_argument("--row", type=int, required=True); ap.add_argument("--col", type=int, required=True)
    ap.add_argument("--size", type=int, default=2048)
    ap.add_argument("--pool", type=int, default=32)
    ap.add_argument("--variants", default="fill4,fill3,nbr4,nbr3")
    a = ap.parse_args()
    h = a.size // 2
    gb = runner.TILE_GEOBOX[a.row - h:a.row + h, a.col - h:a.col + h]
    items = runner.items_over(runner.job_items(a.tile, a.year, span=1), gb)
    t0 = time.time()
    ds = runner.load_stack(items, gb, a.pool)
    yr = ds.time.dt.year.values
    print(f"{a.tile} {a.year}: {len(items)} scenes over 3 years, days per year "
          f"{ {int(y): int((yr == y).sum()) for y in np.unique(yr)} }, load {time.time() - t0:.0f}s", flush=True)
    variants = {"fill4": (4, False), "fill3": (3, False), "nbr4": (4, True), "nbr3": (3, True)}
    variants = {k: v for k, v in variants.items() if k in a.variants.split(",")}
    results, stats = {}, {}
    for name, (k, nb) in variants.items():
        t1 = time.time()
        valid = ladder(ds, a.year, k, nb)
        count = np.minimum(valid.sum(0), 255).astype("uint8")
        arr = runner.to_reflectance(ds, valid)
        picked = runner.medoid(arr)
        src = source_of(arr, picked, ds, a.year)
        bands = runner.to_uint16(picked)
        s = stripe_trials.score_composite(bands, count)
        s["frac_src"] = {n: round(float((src == i).mean()), 4) for i, n in enumerate(("own", "neighbour", "l7", "nodata"))}
        stats[name] = s
        results[name] = (bands, count, src)
        print(f"  {name}: nir {s['nir_ratio_mean']:.2f} max {s['nir_ratio_max']:.2f} cc {s['cc_ratio_mean']:.1f} "
              f"count {s['mean_count']:.1f} src {s['frac_src']} ({time.time() - t1:.0f}s)", flush=True)
    OUT.mkdir(parents=True, exist_ok=True); STATS.mkdir(parents=True, exist_ok=True)
    (STATS / f"{a.tile}_{a.year}.json").write_text(json.dumps(stats, indent=1))
    panel(results, OUT / f"{a.tile}_{a.year}.png", f"{a.tile} {a.year} window at ({a.row},{a.col}) {a.size} px")
    np.savez_compressed(OUT / f"{a.tile}_{a.year}.npz", **{f"{n}_{w}": v for n, (b, c, s) in results.items() for w, v in (("bands", b), ("count", c), ("src", s))})
    print(f"wrote {OUT}/{a.tile}_{a.year}.png")


def panel(results, path, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap
    names = list(results)
    fig, ax = plt.subplots(3, len(names), figsize=(4.2 * len(names), 13))
    src_cmap = ListedColormap(["#d9d9d9", "#2c7fb8", "#e6a100", "#000000"])   # own, neighbour, L7, nodata
    for j, n in enumerate(names):
        b, c, s = results[n]
        rgb = np.stack([b[i] for i in (2, 1, 0)], -1).astype("float32")
        rgb = np.clip((rgb * runner.SCALE + runner.OFFSET - 0.02) / 0.20, 0, 1)
        ax[0, j].imshow(rgb); ax[0, j].set_title(n, fontsize=11)
        ax[1, j].imshow(c, cmap="viridis", vmin=0, vmax=16); ax[1, j].set_title(f"clear count, mean {c[c > 0].mean():.1f}", fontsize=9)
        ax[2, j].imshow(s, cmap=src_cmap, vmin=-0.5, vmax=3.5)
        ax[2, j].set_title("source: gray own year, blue neighbour year, orange Landsat 7", fontsize=8)
        for a_ in ax[:, j]:
            a_.set_axis_off()
    fig.suptitle(title)
    fig.savefig(path, dpi=100, bbox_inches="tight"); plt.close(fig)


if __name__ == "__main__":
    main()
