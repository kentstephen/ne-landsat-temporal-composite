"""Which reducer should coarsen clear_count in the pyramid? Measured on the store.

For each year and pyramid level k (block 2^k pixels) every block is reduced
five ways over its VALID pixels (count > 0, 0 is nodata): mean over valid
pixels, mean over all pixels (zeros included), median, max, min. The
summary compares the value histogram of each reducer to level 0 (1-Wasserstein
distance in counts: how far the colour ramp drifts as you zoom out), how
often reducers disagree, the max-min spread inside blocks, and the share of
blocks that are partly nodata (the only place the valid-only and all-pixel
means can differ). It also checks that count == 0 coincides with all bands
== 0 on one full inland tile.

Result (2026-09-05, years 2000 2008 2012 2016 2021, levels 1-5): mixed
blocks are under 0.6% of valid blocks at every level, so the two means are
indistinguishable. Mean and median both hold the level 0 distribution (W1
under 0.2 counts); max and min drift about one count per level (W1 about
2 at 960 m). Mean and median differ by one count in 3-15% of blocks and by
two in none. count == 0 iff all bands == 0.

Reads the local Icechunk store read only (safe beside batch.py). About
30 s per year.

Usage: uv run python src/landsat_mosaic/count_reducers.py [YEAR ...]
Writes stats/count_reducers.json and prints the summary. With
--summary only, re-prints the summary from the JSON.
"""
import json
import sys
import time
from pathlib import Path

import numpy as np
import zarr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from landsat_mosaic import grid, init_store

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "stats" / "count_reducers.json"
DEFAULT_YEARS = [2000, 2008, 2012, 2016, 2021]
LEVELS = [1, 2, 3, 4, 5]
STRIP = 2048              # rows per pass; keeps the k*k sort under 1 GB
REDUCERS = ("mean_valid", "mean_all", "median", "max", "min")
PAIRS = [("mean_valid", "median"), ("mean_valid", "max"), ("mean_valid", "min"),
         ("mean_valid", "mean_all"), ("median", "max")]
CHECK_TILE = (3, 3)       # r03c03, inland, full


def reduce_all(c: np.ndarray, k: int):
    """The five reducers of a uint8 count plane over k x k blocks, plus the
    valid pixel count per block and the max-min spread (-1 where no valid)."""
    H, W = c.shape[0] // k * k, c.shape[1] // k * k
    b = c[:H, :W].reshape(H // k, k, W // k, k).transpose(0, 2, 1, 3).reshape(H // k, W // k, k * k)
    n = (b > 0).sum(axis=2, dtype=np.uint32)
    tot = b.sum(axis=2, dtype=np.uint32)
    valid = n > 0
    mean_v = np.where(valid, np.rint(tot / np.maximum(n, 1)), 0).astype(np.uint8)
    mean_a = np.rint(tot / (k * k)).astype(np.uint8)
    mx = b.max(axis=2)
    s = np.where(b > 0, b, 255)
    mn = np.where(valid, s.min(axis=2), 0).astype(np.uint8)
    s.sort(axis=2)
    idx = np.maximum(n.astype(np.int64) - 1, 0) // 2      # lower median of the valid pixels
    med = np.take_along_axis(s, idx[..., None], axis=2)[..., 0]
    med = np.where(valid, med, 0).astype(np.uint8)
    spread = np.where(valid, mx.astype(np.int16) - mn.astype(np.int16), -1)
    return {"mean_valid": mean_v, "mean_all": mean_a, "median": med, "max": mx, "min": mn}, n, spread


def analyse(years: list[int]) -> dict:
    repo = init_store.open_repo()
    g = zarr.open_group(repo.readonly_session("main").store, mode="r")[init_store.GROUP]
    yrs = list(init_store.YEARS)
    H = g["clear_count"].shape[1]
    out = {}
    for year in years:
        t0 = time.time()
        c = g["clear_count"][yrs.index(year)]
        print(f"{year}: read {time.time() - t0:.1f}s, valid {100 * (c > 0).mean():.1f}%", flush=True)
        res = {"level0_hist": np.bincount(c[c > 0], minlength=256).tolist(), "levels": {}}
        for k_i in LEVELS:
            k = 2 ** k_i
            hists = {r: np.zeros(256, np.int64) for r in REDUCERS}
            dis = {p: np.zeros(3, np.int64) for p in PAIRS}
            nblk = nvalid = nmixed = nfull = 0
            spread_hist = np.zeros(64, np.int64)
            for r0 in range(0, H, STRIP):
                red, n, spread = reduce_all(c[r0:r0 + STRIP], k)
                valid = n > 0
                nblk += n.size
                nvalid += int(valid.sum())
                nmixed += int(((n > 0) & (n < k * k)).sum())
                nfull += int((n == k * k).sum())
                spread_hist += np.bincount(np.minimum(spread[valid], 63), minlength=64)
                for r, a in red.items():
                    hists[r] += np.bincount(a[valid], minlength=256)
                for p in PAIRS:
                    d = np.abs(red[p[0]][valid].astype(np.int16) - red[p[1]][valid].astype(np.int16))
                    dis[p] += np.array([(d >= 1).sum(), (d >= 2).sum(), (d >= 4).sum()])
            res["levels"][str(k_i)] = {
                "blocks": nblk, "valid": nvalid, "mixed": nmixed, "full": nfull,
                "hists": {r: h.tolist() for r, h in hists.items()},
                "spread": spread_hist.tolist(),
                "disagree": {f"{a}|{b}": v.tolist() for (a, b), v in dis.items()},
            }
            print(f"  level {k_i} ({k}x): {time.time() - t0:.0f}s", flush=True)
        out[str(year)] = res

    ty, tx = CHECK_TILE
    ys, xs = grid.tile_slices(ty, tx)
    ti = yrs.index(years[0])
    cc = g["clear_count"][ti, ys, xs]
    nd = np.ones(cc.shape, bool)
    for b in init_store.BANDS:
        nd &= g[b][ti, ys, xs] == 0
    out["nodata_check"] = {"tile": grid.tile_id(ty, tx), "year": years[0], "count0": int((cc == 0).sum()),
                           "bands_all0": int(nd.sum()), "both": int(((cc == 0) & nd).sum())}
    return out


def _stats(h) -> str:
    h = np.asarray(h, float)
    n = h.sum()
    if n == 0:
        return "n/a"
    v = np.arange(h.size)
    cum = np.cumsum(h) / n
    q = [int(np.searchsorted(cum, p)) for p in (0.1, 0.5, 0.9)]
    return f"mean {(h * v).sum() / n:5.2f} p10 {q[0]:2d} med {q[1]:2d} p90 {q[2]:2d}"


def _w1(h0, h) -> float:
    a = np.asarray(h0, float)
    b = np.asarray(h, float)
    return float(np.abs(np.cumsum(a / a.sum()) - np.cumsum(b / b.sum())).sum())


def summary(out: dict) -> str:
    lines = [f"nodata check: {out['nodata_check']}"]
    for year, r in out.items():
        if year == "nodata_check":
            continue
        lines.append(f"\n=== {year}  level0 {_stats(r['level0_hist'])}")
        for lv, L in r["levels"].items():
            k = 2 ** int(lv)
            v = L["valid"]
            lines.append(f" level {lv} ({k}x, {k * 30} m): valid blocks {v}, mixed {100 * L['mixed'] / v:.1f}% of valid")
            for red, h in L["hists"].items():
                lines.append(f"   {red:10s} {_stats(h)}  W1 vs L0 {_w1(r['level0_hist'], h):.2f}")
            sp = np.asarray(L["spread"], float)
            cs = np.cumsum(sp / sp.sum())
            lines.append(f"   spread max-min: 0:{100 * cs[0]:.0f}% <=1:{100 * cs[1]:.0f}% <=2:{100 * cs[2]:.0f}%"
                         f" <=4:{100 * cs[4]:.0f}% >8:{100 * (1 - cs[8]):.0f}%")
            for p, (d1, d2, d4) in L["disagree"].items():
                lines.append(f"   |{p}| >=1: {100 * d1 / v:5.1f}%  >=2: {100 * d2 / v:5.1f}%  >=4: {100 * d4 / v:5.1f}%")
    return "\n".join(lines)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["--summary"]:
        out = json.load(open(OUT))
    else:
        out = analyse([int(a) for a in args] or DEFAULT_YEARS)
        OUT.parent.mkdir(parents=True, exist_ok=True)
        json.dump(out, open(OUT, "w"))
        print(f"wrote {OUT}")
    print(summary(out))
