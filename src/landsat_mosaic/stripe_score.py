"""Directional stripe score for every year and block of the pyramid.

Landsat 7 SLC-off gaps are periodic lines (about 1 km apart, tilted a few
degrees off east-west) and they show in clear_count wherever a Landsat 7
look was in the composite. Per 512 px block of level 1 (about 30 km):

  cc_ratio   power of clear_count at the gap period (12-28 px) in the
             strongest 15 deg angular sector, over the mean at other angles
  nir_ratio  same sector and period band, on the NIR reflectance

A ratio near 1 is isotropic texture; the stripes push it well above. The NIR
ratio is the one that matters (what the picture shows); cc_ratio says how
much Landsat 7 was in the mix. score() is reused on trial composites.

Gate (2026-09-08): nir_ratio is NIR power in the count plane's strongest
sector. When the count has no stripes (cc_ratio under CC_ANCHOR, clean years
sit at 1.5-2.2) that sector is arbitrary and nir_ratio reads terrain grain
(east-west lakes and ridges at the gap spacing) as striping. nir_gated is
nir_ratio where cc_ratio >= CC_ANCHOR and 1.0 elsewhere: no count stripes,
no stripe claim. Rank and summarise on nir_gated; nir_ratio stays for the
history. water marks blocks whose median NIR reflectance is under 0.05.

Usage: uv run python src/landsat_mosaic/stripe_score.py [--years 2000-2025] [--level 1]
Writes stats/striping/<year>.json (per block) and stats/striping/summary.json,
plus stats/striping/<year>.png (nir_ratio per block, single-hue ramp).
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import zarr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from landsat_mosaic import grid

SCALE, OFFSET = 0.0000275, -0.2
BLOCK = 512
PERIOD = (12.0, 28.0)          # px at level 1 (60 m nominal), gap period ~18
SECTOR = 15.0                  # degrees
CC_ANCHOR = 3.0                # cc_ratio below this: the sector is unanchored
WATER_NIR = 0.05               # block median NIR reflectance under this: water
OUT = grid.ROOT / "stats/striping"


def _spectrum(a: np.ndarray) -> np.ndarray:
    a = a - a.mean()
    n = a.shape[0]
    w = np.hanning(n)
    a = a * w[:, None] * w[None, :]
    return np.abs(np.fft.fftshift(np.fft.fft2(a))) ** 2


def _grids(n: int):
    f = np.fft.fftshift(np.fft.fftfreq(n))
    fy, fx = np.meshgrid(f, f, indexing="ij")
    r = np.hypot(fy, fx)
    ang = np.degrees(np.arctan2(fy, fx)) % 180.0
    band = (r >= 1 / PERIOD[1]) & (r <= 1 / PERIOD[0])
    return band, ang


def gated(b: dict) -> float:
    """nir_ratio with the count-stripe gate applied; works on old JSON too."""
    return float(b["nir_ratio"]) if b["cc_ratio"] >= CC_ANCHOR else 1.0


def score(nir: np.ndarray, cc: np.ndarray, valid: np.ndarray) -> dict | None:
    """One block. nir float reflectance, cc float, valid bool. None if too empty."""
    if valid.mean() < 0.95:
        return None
    n = nir.shape[0]
    band, ang = _grids(n)
    ccf = np.where(valid, cc, cc[valid].mean())
    nirf = np.where(valid, nir, nir[valid].mean())
    pc, pn = _spectrum(ccf), _spectrum(nirf)
    bins = np.arange(0, 180, 5.0)
    sect = [(band & (ang >= b) & (ang < b + 5.0)) for b in bins]
    pcb = np.array([pc[s].mean() for s in sect])
    pnb = np.array([pn[s].mean() for s in sect])
    # strongest 15 deg sector of the count spectrum (3 bins, circular)
    roll = pcb + np.roll(pcb, 1) + np.roll(pcb, -1)
    k = int(roll.argmax())
    inside = {(k - 1) % len(bins), k, (k + 1) % len(bins)}
    outside = [i for i in range(len(bins)) if i not in inside]
    cc_ratio = float(pcb[list(inside)].mean() / max(pcb[outside].mean(), 1e-30))
    nir_ratio = float(pnb[list(inside)].mean() / max(pnb[outside].mean(), 1e-30))
    # NIR judged on its own strongest sector too (stripes without the count cue)
    rolln = pnb + np.roll(pnb, 1) + np.roll(pnb, -1)
    kn = int(rolln.argmax())
    insn = {(kn - 1) % len(bins), kn, (kn + 1) % len(bins)}
    outn = [i for i in range(len(bins)) if i not in insn]
    nir_self = float(pnb[list(insn)].mean() / max(pnb[outn].mean(), 1e-30))
    out = {"cc_ratio": round(cc_ratio, 3), "nir_ratio": round(nir_ratio, 3),
           "nir_self": round(nir_self, 3), "angle": float(bins[k]),
           "mean_count": round(float(cc[valid].mean()), 2),
           "frac_lt4": round(float((cc[valid] < 4).mean()), 4),
           "water": bool(np.median(nir[valid]) < WATER_NIR)}
    out["anchored"] = bool(cc_ratio >= CC_ANCHOR)
    out["nir_gated"] = round(gated(out), 3)
    return out


def score_plane(nir_dn: np.ndarray, cc: np.ndarray) -> list[dict]:
    """All blocks of one year plane at level 1 resolution."""
    H, W = cc.shape
    out = []
    for by in range(0, H - BLOCK + 1, BLOCK):
        for bx in range(0, W - BLOCK + 1, BLOCK):
            c = cc[by:by + BLOCK, bx:bx + BLOCK].astype("float32")
            valid = c > 0
            if valid.mean() < 0.95:
                continue
            n = nir_dn[by:by + BLOCK, bx:bx + BLOCK].astype("float32") * SCALE + OFFSET
            s = score(n, c, valid)
            if s:
                s.update(by=by, bx=bx)
                out.append(s)
    return out


def png(blocks: list[dict], H: int, W: int, path: Path, key="nir_gated", vmax=4.0) -> None:
    from PIL import Image
    ny, nx = H // BLOCK, W // BLOCK
    img = np.zeros((ny, nx), "float32")
    for b in blocks:
        img[b["by"] // BLOCK, b["bx"] // BLOCK] = b[key]
    v = np.clip((img - 1.0) / (vmax - 1.0), 0, 1)
    rgb = np.stack([255 - 200 * v, 255 - 120 * v, 255 - 20 * v], -1)   # white to blue
    rgb[img == 0] = 40
    Image.fromarray(rgb.astype("uint8")).resize((nx * 12, ny * 12), Image.NEAREST).save(path)


def summarize(blocks: list[dict]) -> dict:
    """Summary row for one year. Raw columns as before (all blocks, unmasked);
    gated columns on nir_gated, all blocks and land only."""
    nr = np.array([b["nir_ratio"] for b in blocks])
    ng = np.array([gated(b) for b in blocks])
    cr = np.array([b["cc_ratio"] for b in blocks])
    land = np.array([not b.get("water", False) for b in blocks])
    gl = ng[land] if land.any() else ng
    return {
        "blocks": len(blocks),
        "nir_ratio_median": round(float(np.median(nr)), 3),
        "nir_ratio_p90": round(float(np.percentile(nr, 90)), 3),
        "nir_ratio_max": round(float(nr.max()), 3),
        "frac_nir_gt2": round(float((nr > 2).mean()), 3),
        "frac_nir_gt1_5": round(float((nr > 1.5).mean()), 3),
        "cc_ratio_median": round(float(np.median(cr)), 3),
        "mean_count": round(float(np.mean([b["mean_count"] for b in blocks])), 2),
        "frac_lt4": round(float(np.mean([b["frac_lt4"] for b in blocks])), 4),
        "anchored": int((cr >= CC_ANCHOR).sum()),
        "gated_median": round(float(np.median(ng)), 3),
        "gated_p90": round(float(np.percentile(ng, 90)), 3),
        "gated_max": round(float(ng.max()), 3),
        "frac_gated_gt2": round(float((ng > 2).mean()), 3),
        "land_blocks": int(land.sum()),
        "land_anchored": int((cr[land] >= CC_ANCHOR).sum()),
        "land_gated_p90": round(float(np.percentile(gl, 90)), 3),
        "land_gated_max": round(float(gl.max()), 3),
        "land_frac_gated_gt2": round(float((gl > 2).mean()), 3),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="2000-2025")
    ap.add_argument("--level", type=int, default=1)
    ap.add_argument("--store", type=Path, default=grid.ROOT / "data/pyramid_v1")
    a = ap.parse_args()
    y0, y1 = (int(v) for v in a.years.split("-"))
    g = zarr.open_group(a.store, path=f"leafon/{a.level}", mode="r")
    years = list(g["time"][:])
    OUT.mkdir(parents=True, exist_ok=True)
    summary = {}
    for y in years:
        if not y0 <= y <= y1:
            continue
        t = years.index(y)
        cc = g["clear_count"][t]
        nir = g["nir"][t]
        blocks = score_plane(nir, cc)
        (OUT / f"{y}.json").write_text(json.dumps(blocks))
        png(blocks, *cc.shape, OUT / f"{y}.png")
        png(blocks, *cc.shape, OUT / f"{y}_raw.png", key="nir_ratio")
        summary[str(y)] = summarize(blocks)
        print(y, summary[str(y)], flush=True)
        (OUT / "summary.json").write_text(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
