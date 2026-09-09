"""Validate one rebuilt tile-year off the store against the v3 tag and the v3
pyramid scores. One line on stdout: OK or FLAG with the numbers. Read-only.
Stripe flags use the gated nir_ratio (stripe_score.gated; 1.0 where the count
plane has no stripes, cc_ratio < CC_ANCHOR), so terrain grain cannot flag.
usage: uv run python src/landsat_mosaic/validate_tile.py r01c02 2014"""
import sys, json, numpy as np, zarr
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from landsat_mosaic import grid, init_store, stripe_score
SCALE, OFFSET, T, B = 0.0000275, -0.2, 4096, 512
tile, year = sys.argv[1], int(sys.argv[2])
r, c = int(tile[1:3]), int(tile[4:6]); t = year - init_store.YEARS[0]
repo = init_store.open_repo()
def load(sess):
    g = zarr.open_group(sess.store, mode="r")[init_store.GROUP]
    return (g["nir"][t, r*T:(r+1)*T, c*T:(c+1)*T].astype("float32"),
            g["clear_count"][t, r*T:(r+1)*T, c*T:(c+1)*T].astype("float32"))
def coarsen(a, cc):
    h, w = (cc.shape[0] // 2) * 2, (cc.shape[1] // 2) * 2
    a, v = a[:h, :w], (cc[:h, :w] > 0).astype("float32")
    s = (a * v).reshape(h // 2, 2, w // 2, 2).sum((1, 3)); k = v.reshape(h // 2, 2, w // 2, 2).sum((1, 3))
    return np.where(k > 0, s / np.maximum(k, 1), 0)
n0, c0 = load(repo.readonly_session(tag="v3")); n1, c1 = load(repo.readonly_session("main"))
flags = []
nod0, nod1 = float((c0 == 0).mean()), float((c1 == 0).mean())
if nod1 > nod0 + 0.01: flags.append(f"nodata {nod0:.3f}->{nod1:.3f}")
both = (c0 > 0) & (c1 > 0)
lt3_0, lt3_1 = float((c0[both] < 3).mean()), float((c1[both] < 3).mean())
if lt3_1 > lt3_0 + 0.02: flags.append(f"count<3 (both valid) {lt3_0:.3f}->{lt3_1:.3f}")
gained = float(((c0 == 0) & (c1 > 0)).mean())
land0 = n0[(c0 > 0) & (n0 * SCALE + OFFSET > 0.1)]; land1 = n1[(c1 > 0) & (n1 * SCALE + OFFSET > 0.1)]
med0 = float(np.median(land0)) * SCALE + OFFSET if land0.size else 0
med1 = float(np.median(land1)) * SCALE + OFFSET if land1.size else 0
if land0.size and abs(med1 - med0) > 0.05: flags.append(f"land nir median {med0:.3f}->{med1:.3f}")
changed = float((n1 != n0).mean())
if changed == 0: flags.append("no pixel changed")
# stripe score, 16 level-1 blocks, against the v3 pyramid scores
import os
BASE = Path(os.environ.get("STRIPE_BASELINE", grid.ROOT / "data/striping_v3"))  # v3 block JSONs (git 98d4b45); stats/striping holds v4 now
old = {(b["by"], b["bx"]): b for b in json.load(open(BASE / f"{year}.json"))}
nir_l1 = coarsen(n1, c1) * SCALE + OFFSET; cc_l1 = coarsen(c1, c1)
rows = []
for i in range(cc_l1.shape[0] // B):
    for j in range(cc_l1.shape[1] // B):
        by, bx = r * (T // 2) + i * B, c * (T // 2) + j * B
        blk = cc_l1[i*B:(i+1)*B, j*B:(j+1)*B]; s = stripe_score.score(nir_l1[i*B:(i+1)*B, j*B:(j+1)*B], blk, blk > 0)
        if s and (by, bx) in old:
            s["water"] = bool(np.median(nir_l1[i*B:(i+1)*B, j*B:(j+1)*B][blk > 0]) < 0.05)
            rows.append((s, old[(by, bx)], by, bx))
if rows:
    G = stripe_score.gated
    nr0 = np.array([G(o) for s, o, *_ in rows]); nr1 = np.array([G(s) for s, o, *_ in rows])
    cc5_0 = int(sum(o["cc_ratio"] > 5 for s, o, *_ in rows if not s["water"]))
    cc5_1 = int(sum(s["cc_ratio"] > 5 for s, o, *_ in rows if not s["water"]))
    seam = [(by, bx, round(o["nir_ratio"], 2), round(s["nir_ratio"], 2)) for s, o, by, bx in rows
            if 10 <= s["angle"] <= 30 and G(s) > G(o) + 1.0 and G(s) > 2.0 and not s["water"]]
    if seam: flags.append(f"seam-angle blocks up {seam}")
    nw = [k for k, (s, *_) in enumerate(rows) if s["water"]]
    land = [k for k, (s, *_) in enumerate(rows) if not s["water"]]
    if land and np.median(nr1[land]) > np.median(nr0[land]) + 0.15: flags.append(f"land gated nir median {np.median(nr0[land]):.2f}->{np.median(nr1[land]):.2f}")
    up = [(by, bx, round(G(o), 2), round(G(s), 2)) for s, o, by, bx in rows if not s["water"] and G(s) > 2.0 and G(s) > G(o) + 1.0]
    if up: flags.append(f"gated land blocks up {up}")
    if cc5_1 > cc5_0: flags.append(f"cc>5 {cc5_0}->{cc5_1}")
    score = f"blocks {len(rows)} water {len(nw)} gated nir max {nr0.max():.2f}->{nr1.max():.2f} land cc>5 {cc5_0}->{cc5_1}"
else:
    score = "no scorable blocks"
print(f"{'FLAG' if flags else 'OK'} {tile} {year} | changed {changed:.2f} nodata {nod1:.3f} gained {gained:.3f} count {c1[c1>0].mean():.1f} (v3 {c0[c0>0].mean():.1f}) land nir {med1:.3f} | {score}" + (" | " + "; ".join(flags) if flags else ""))
