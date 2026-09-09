"""Stripe-score years straight from the Icechunk store, mid-build.

stripe_score.py reads the exported pyramid at level 1. This reads level 0
from the store (read-only session on main, safe beside a running batch.py),
coarsens 2x with a nodata-aware mean to level 1 resolution, and runs the
same score_plane, so the numbers line up with stats/striping and
data/logs/stripe_score.log. Use it to check a rebuild on the years that
have landed before the rest finish.

Usage: uv run python src/landsat_mosaic/stripe_score_store.py 2003 2004 2005
Prints one summary line per year; nothing is written.
"""
import sys
from pathlib import Path

import numpy as np
import zarr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from landsat_mosaic import grid, init_store, stripe_score

H2, W2 = grid.HEIGHT // 2, grid.WIDTH // 2


def coarsen(a: np.ndarray, cc: np.ndarray) -> np.ndarray:
    """2x2 mean over pixels with clear_count > 0; 0 where the block has none."""
    a = a[:2 * H2, :2 * W2].astype("float32")
    v = (cc[:2 * H2, :2 * W2] > 0).astype("float32")
    s = (a * v).reshape(H2, 2, W2, 2).sum((1, 3))
    n = v.reshape(H2, 2, W2, 2).sum((1, 3))
    return np.where(n > 0, s / np.maximum(n, 1), 0)


def main() -> None:
    years = [int(v) for v in sys.argv[1:]] or init_store.YEARS
    repo = init_store.open_repo()
    g = zarr.open_group(repo.readonly_session("main").store, mode="r")[init_store.GROUP]
    for y in years:
        t = y - init_store.YEARS[0]
        cc0 = g["clear_count"][t]
        nir1 = coarsen(g["nir"][t], cc0)
        cc1 = coarsen(cc0, cc0)
        blocks = stripe_score.score_plane(nir1, cc1)
        if not blocks:
            print(y, "no scorable blocks (year not built yet?)", flush=True)
            continue
        nr = np.array([b["nir_ratio"] for b in blocks])
        cr = np.array([b["cc_ratio"] for b in blocks])
        out = {
            "blocks": len(blocks),
            "nir_ratio_median": round(float(np.median(nr)), 3),
            "nir_ratio_p90": round(float(np.percentile(nr, 90)), 3),
            "nir_ratio_max": round(float(nr.max()), 3),
            "frac_nir_gt2": round(float((nr > 2).mean()), 3),
            "frac_nir_gt1_5": round(float((nr > 1.5).mean()), 3),
            "cc_ratio_median": round(float(np.median(cr)), 3),
            "mean_count": round(float(np.mean([b["mean_count"] for b in blocks])), 2),
            "frac_lt4": round(float(np.mean([b["frac_lt4"] for b in blocks])), 4),
            "frac_nodata": round(float((cc0 == 0).mean()), 4),
        }
        print(y, out, flush=True)


if __name__ == "__main__":
    main()
