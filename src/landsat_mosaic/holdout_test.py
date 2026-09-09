"""Hold-out error of the v4 ladder: what borrowing a neighbour-year look
costs, measured where the own year has plenty of clean looks.

On one 2048 px window, load year-1..year+1 once. Reference = medoid over
every clear own-year non-L7 look, at pixels that have at least --min-ref of
them. Then starve those pixels: keep m random own-year non-L7 looks per
pixel (m drawn from --keep, default 0,1,2, below the rung-1 threshold of 3)
and composite three ways:
  own3   3 random own-year non-L7 looks (the noise floor of a right-year
         3-look medoid; reported where the pixel has at least 3)
  nbr    m own looks plus every neighbour-year non-L7 look, the ladder's
         rung 2 (reported where that set reaches 3)
  v3     m own looks plus every own-year Landsat 7 look, the v3 fill
         (reported where that set reaches 1)
Errors against the reference: per band absolute reflectance difference and
NDVI difference, median and p90, split by whether the landscape changed
between year-1 and year+1 (|dNDVI| between the two neighbour-year non-L7
medoids: stable < 0.05, changed > 0.15) and by m.

Usage:
  uv run python src/landsat_mosaic/holdout_test.py 2016 --lat 45.96 --lon -68.72
  uv run python src/landsat_mosaic/holdout_test.py --list data/holdout_windows.txt
The tile is derived from the point; scenes of every tile the window touches
are loaded.
Writes stats/holdout/<tile>_<year>_<lat>_<lon>.json and a panel PNG in
data/trials/holdout/. --summary pools the JSONs into stats/holdout/summary.json.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from landsat_mosaic import grid, init_store, runner, stripe_trials

OUT = grid.ROOT / "data/trials/holdout"
STATS = grid.ROOT / "stats/holdout"
L7 = runner.L7
NIR, RED = runner.SRC_BANDS.index("nir08"), runner.SRC_BANDS.index("red")
STABLE, CHANGED = 0.05, 0.15


def ndvi(refl: np.ndarray) -> np.ndarray:
    with np.errstate(all="ignore"):
        return (refl[NIR] - refl[RED]) / (refl[NIR] + refl[RED])


def medoid_of(ds, valid: np.ndarray) -> np.ndarray:
    """Medoid over the valid looks, loading only the slots that are used
    anywhere (the float32 stack is 6 x T x y x x)."""
    idx = np.flatnonzero(valid.reshape(valid.shape[0], -1).any(1))
    if idx.size == 0:
        return np.full((len(runner.SRC_BANDS), *valid.shape[1:]), np.nan, "float32")
    return runner.medoid(runner.to_reflectance(ds.isel(time=idx), valid[idx]))


def keep_random(valid: np.ndarray, m: np.ndarray, rng) -> np.ndarray:
    """Per pixel, keep m[y, x] random True slots of valid [time, y, x]."""
    key = rng.random(valid.shape, dtype="float32")
    key[~valid] = np.inf
    keep = np.zeros_like(valid)
    yy, xx = np.indices(m.shape)
    for j in range(int(m.max())):
        k = key.argmin(0)
        hit = (m > j) & np.isfinite(key[k, yy, xx])
        keep[k[hit], yy[hit], xx[hit]] = True
        key[k, yy, xx] = np.inf
    return keep


def tile_of(row: int, col: int) -> str:
    return f"r{row // grid.TILE:02d}c{col // grid.TILE:02d}"


def window_items(year: int, row: int, col: int, h: int, gb):
    """Scenes of every tile the window touches, year-1..year+1, deduplicated."""
    seen, items = set(), []
    for r in (row - h, row + h - 1):
        for c in (col - h, col + h - 1):
            for it in runner.job_items(tile_of(r, c), year, span=1):
                if it.id not in seen:
                    seen.add(it.id); items.append(it)
    return runner.items_over(items, gb)


def errors(cand: np.ndarray, ref: np.ndarray, mask: np.ndarray) -> dict:
    ok = mask & np.isfinite(cand).all(0) & np.isfinite(ref).all(0)
    n = int(ok.sum())
    if n == 0:
        return {"n": 0}
    d = np.abs(cand - ref)[:, ok]                                  # band, n
    dn = np.abs(ndvi(cand) - ndvi(ref))[ok]
    return {"n": n,
            "band_abs_median": [round(float(v), 5) for v in np.median(d, 1)],
            "band_abs_p90": [round(float(v), 5) for v in np.percentile(d, 90, 1)],
            "band_rmse": [round(float(v), 5) for v in np.sqrt((d ** 2).mean(1))],
            "ndvi_abs_median": round(float(np.median(dn)), 4),
            "ndvi_abs_p90": round(float(np.percentile(dn, 90)), 4),
            "ndvi_abs_gt_0.1": round(float((dn > 0.1).mean()), 4)}


def run(year: int, lat: float, lon: float, size: int, pool: int,
        min_ref: int, keep: list[int], seed: int) -> dict:
    row, col = int((grid.NORTH - lat) / grid.RES), int((lon - grid.WEST) / grid.RES)
    tile = tile_of(row, col)
    h = size // 2
    gb = runner.TILE_GEOBOX[row - h:row + h, col - h:col + h]
    items = window_items(year, row, col, h, gb)
    t0 = time.time()
    ds = runner.load_stack(items, gb, pool)
    yr = ds.time.dt.year.values
    is7 = ds.platform.values == L7
    own = yr == year
    print(f"{tile} {year} at ({lat},{lon}) row {row} col {col}: {len(items)} scenes over 3 years, days per year "
          f"{ {int(y): int((yr == y).sum()) for y in np.unique(yr)} }, load {time.time() - t0:.0f}s", flush=True)
    cl = lambda slots: stripe_trials.clear_obs_slots(ds, slots, "fallback", 4)
    v_own = cl(own & ~is7)
    v_l7 = cl(own & is7)
    v_prev = cl((yr == year - 1) & ~is7)
    v_next = cl((yr == year + 1) & ~is7)
    n_own = v_own.sum(0)
    elig = n_own >= min_ref
    ref = medoid_of(ds, v_own)
    prev, nxt = medoid_of(ds, v_prev), medoid_of(ds, v_next)
    dndvi = np.abs(ndvi(nxt) - ndvi(prev))
    strata = {"all": elig, "stable": elig & (dndvi < STABLE), "changed": elig & (dndvi > CHANGED)}

    rng = np.random.default_rng(seed)
    m = rng.choice(keep, size=n_own.shape).astype("int16")
    kept = keep_random(v_own, m, rng)
    v_own3 = keep_random(v_own, np.full(n_own.shape, 3, "int16"), rng)
    v_nbr = kept | v_prev | v_next
    v_v3 = kept | v_l7
    variants = {
        "own3": (medoid_of(ds, v_own3), n_own >= 3),
        "nbr": (medoid_of(ds, v_nbr), v_nbr.sum(0) >= 3),
        "v3": (medoid_of(ds, v_v3), v_v3.sum(0) >= 1),
    }
    # the neighbour-year medoids themselves, the error of taking the wrong year outright
    variants["prev_year"] = (prev, np.isfinite(prev).all(0))
    variants["next_year"] = (nxt, np.isfinite(nxt).all(0))

    out = {"tile": tile, "year": year, "lat": lat, "lon": lon, "row": row, "col": col, "size": size,
           "min_ref": min_ref, "keep": keep, "seed": seed,
           "days": {int(y): int((yr == y).sum()) for y in np.unique(yr)},
           "own_non_l7_looks_mean": round(float(n_own.mean()), 2),
           "eligible_frac": round(float(elig.mean()), 4),
           "stable_frac": round(float(strata["stable"].sum() / max(elig.sum(), 1)), 4),
           "changed_frac": round(float(strata["changed"].sum() / max(elig.sum(), 1)), 4),
           "nbr_reaches_3_frac": round(float((v_nbr.sum(0) >= 3)[elig].mean()), 4),
           "results": {}}
    for name, (cand, ok) in variants.items():
        r = {s: errors(cand, ref, mask & ok) for s, mask in strata.items()}
        if name in ("nbr", "v3"):
            r["by_m"] = {int(mm): errors(cand, ref, elig & ok & (m == mm)) for mm in keep}
        out["results"][name] = r
    for name in ("own3", "nbr", "v3", "prev_year"):
        r = out["results"][name]
        line = " ".join(f"{s} n={r[s]['n']} ndvi med {r[s].get('ndvi_abs_median', float('nan')):.3f} "
                        f"p90 {r[s].get('ndvi_abs_p90', float('nan')):.3f} nir med {r[s].get('band_abs_median', [0]*6)[NIR]:.4f}"
                        for s in ("stable", "changed"))
        print(f"  {name:9s} {line}", flush=True)
    tag = f"{tile}_{year}_{lat}_{lon}"
    STATS.mkdir(parents=True, exist_ok=True); OUT.mkdir(parents=True, exist_ok=True)
    (STATS / f"{tag}.json").write_text(json.dumps(out, indent=1))
    panel(ref, variants["nbr"][0], variants["v3"][0], dndvi, elig, OUT / f"{tag}.png",
          f"{tile} {year} hold-out at ({lat},{lon}) {size} px")
    return out


def panel(ref, nbr, v3, dndvi, elig, path, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    def rgb(a):
        x = np.stack([a[i] for i in (2, 1, 0)], -1)
        return np.clip((x - 0.02) / 0.20, 0, 1)
    fig, ax = plt.subplots(2, 3, figsize=(15, 10))
    ax[0, 0].imshow(rgb(ref)); ax[0, 0].set_title("reference: all own-year non-L7 looks")
    ax[0, 1].imshow(rgb(nbr)); ax[0, 1].set_title("nbr: m own + neighbour-year looks")
    ax[0, 2].imshow(rgb(v3)); ax[0, 2].set_title("v3: m own + own Landsat 7 looks")
    e_n = np.where(elig, np.abs(ndvi(nbr) - ndvi(ref)), np.nan)
    e_3 = np.where(elig, np.abs(ndvi(v3) - ndvi(ref)), np.nan)
    im = ax[1, 0].imshow(dndvi, cmap="cividis", vmin=0, vmax=0.3); ax[1, 0].set_title("|dNDVI| year-1 to year+1 (change)")
    plt.colorbar(im, ax=ax[1, 0], shrink=0.7)
    im = ax[1, 1].imshow(e_n, cmap="cividis", vmin=0, vmax=0.2); ax[1, 1].set_title("|NDVI error| nbr vs reference")
    plt.colorbar(im, ax=ax[1, 1], shrink=0.7)
    im = ax[1, 2].imshow(e_3, cmap="cividis", vmin=0, vmax=0.2); ax[1, 2].set_title("|NDVI error| v3 vs reference")
    plt.colorbar(im, ax=ax[1, 2], shrink=0.7)
    for a in ax.flat:
        a.set_axis_off()
    fig.suptitle(title)
    fig.savefig(path, dpi=100, bbox_inches="tight"); plt.close(fig)


def summary() -> None:
    rows = [json.loads(f.read_text()) for f in sorted(STATS.glob("r??c??_*.json"))]
    if not rows:
        sys.exit("no window stats yet")
    print(f"{'window':28} {'own':>4} {'elig':>5} {'chg':>5} | {'own3 st':>7} {'nbr st':>7} {'v3 st':>7} | "
          f"{'own3 ch':>7} {'nbr ch':>7} {'v3 ch':>7} | {'prev ch':>7}   (median |NDVI err|)")
    pooled = {}
    for r in rows:
        res = r["results"]
        g = lambda v, s: res[v][s].get("ndvi_abs_median", float("nan"))
        print(f"{r['tile']}_{r['year']}_{r['lat']}_{r['lon']:<8} {r['own_non_l7_looks_mean']:4.1f} {r['eligible_frac']:5.2f} "
              f"{r['changed_frac']:5.2f} | {g('own3','stable'):7.3f} {g('nbr','stable'):7.3f} {g('v3','stable'):7.3f} | "
              f"{g('own3','changed'):7.3f} {g('nbr','changed'):7.3f} {g('v3','changed'):7.3f} | {g('prev_year','changed'):7.3f}")
        for v in res:
            for s in ("stable", "changed"):
                e = res[v][s]
                if e["n"]:
                    p = pooled.setdefault(f"{v}/{s}", {"n": 0, "ndvi_w": 0.0, "p90_w": 0.0, "gt_w": 0.0})
                    p["n"] += e["n"]; p["ndvi_w"] += e["ndvi_abs_median"] * e["n"]
                    p["p90_w"] += e["ndvi_abs_p90"] * e["n"]; p["gt_w"] += e["ndvi_abs_gt_0.1"] * e["n"]
    out = {"windows": [f"{r['tile']}_{r['year']}_{r['lat']}_{r['lon']}" for r in rows], "pooled": {}}
    print("\npooled over windows, weighted by pixels (median of medians, p90, frac |NDVI err| > 0.1):")
    for k in sorted(pooled):
        p = pooled[k]
        out["pooled"][k] = {"n": p["n"], "ndvi_abs_median": round(p["ndvi_w"] / p["n"], 4),
                            "ndvi_abs_p90": round(p["p90_w"] / p["n"], 4), "ndvi_abs_gt_0.1": round(p["gt_w"] / p["n"], 4)}
        print(f"  {k:18s} n={p['n']:>9,} median {p['ndvi_w'] / p['n']:.3f} p90 {p['p90_w'] / p['n']:.3f} >0.1 {p['gt_w'] / p['n']:.3f}")
    (STATS / "summary.json").write_text(json.dumps(out, indent=1))
    print(f"wrote {STATS / 'summary.json'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("year", nargs="?", type=int)
    ap.add_argument("--lat", type=float); ap.add_argument("--lon", type=float)
    ap.add_argument("--list", default=None, help="file of 'year lat lon' lines, # comments")
    ap.add_argument("--size", type=int, default=2048)
    ap.add_argument("--pool", type=int, default=32)
    ap.add_argument("--min-ref", type=int, default=5, help="own-year non-L7 looks a pixel needs to be a reference")
    ap.add_argument("--keep", default="0,1,2", help="own-year looks left per pixel, drawn per pixel")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--summary", action="store_true")
    a = ap.parse_args()
    if a.summary:
        summary(); return
    keep = [int(v) for v in a.keep.split(",")]
    if a.list:
        jobs = [ln.split() for ln in Path(a.list).read_text().splitlines() if ln.strip() and not ln.startswith("#")]
        jobs = [(int(y), float(la), float(lo)) for y, la, lo, *_ in jobs]
    else:
        if not (a.year and a.lat is not None and a.lon is not None):
            sys.exit("give YEAR --lat --lon, or --list FILE")
        jobs = [(a.year, a.lat, a.lon)]
    for year, lat, lon in jobs:
        try:
            run(year, lat, lon, a.size, a.pool, a.min_ref, keep, a.seed)
        except Exception as e:
            print(f"{year} ({lat},{lon}) failed: {e!r}", flush=True)
    if len(jobs) > 1:
        summary()


if __name__ == "__main__":
    main()
