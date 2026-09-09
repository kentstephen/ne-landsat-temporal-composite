"""Medoid (v1 store) against a median trial of the same tile-year, for the
Landsat 7 SLC-off striping question.

Reads the medoid composite from data/pyramid_v1/leafon/0 and the median
from data/trials/<tile>_<year>_median_fallback.npz (runner.py --dry-run).
Writes stats/reducer_compare/<tile>_<year>.png: true colour crops of both
around a chosen point at native pixels, the clear count, and the
difference. Prints a striping score per reducer: the standard deviation of
the high-pass true-colour luminance (image minus a 31 px box mean) over
forest pixels (NDVI > 0.6), where the striping should be the only texture.

Usage: uv run python src/landsat_mosaic/compare_reducers.py r04c01 2008 [--lat 43.08 --lon -72.1 --size 1200]
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import zarr
from PIL import Image, ImageDraw
from zarr.storage import LocalStore

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from landsat_mosaic import grid, init_store

SCALE, OFFSET = 0.0000275, -0.2
OUT = grid.ROOT / "stats/reducer_compare"


def to_rgb(bands: dict, gain=1.0) -> np.ndarray:
    rgb = np.stack([bands["red"], bands["green"], bands["blue"]], -1).astype(np.float32)
    valid = (rgb > 0).all(-1)
    img = np.clip((rgb * SCALE + OFFSET) / 0.3 * gain, 0, 1) * 255
    img = img.astype(np.uint8)
    img[~valid] = (255, 0, 255)
    return img


def box_mean(a: np.ndarray, k: int) -> np.ndarray:
    p = k // 2
    c = np.pad(a, p, mode="reflect").cumsum(0).cumsum(1)
    c = np.pad(c, ((1, 0), (1, 0)))
    return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)


def stripe_score(bands: dict, k=31) -> tuple[float, float]:
    """(high-pass std over forest, share of forest pixels) of luminance."""
    r, g, b, n = (bands[x].astype(np.float32) * SCALE + OFFSET for x in ("red", "green", "blue", "nir"))
    valid = (bands["red"] > 0)
    ndvi = np.where(valid & (n + r > 0), (n - r) / np.maximum(n + r, 1e-6), 0)
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    hp = lum - box_mean(lum, k)
    forest = valid & (ndvi > 0.6)
    return float(hp[forest].std()), float(forest.mean())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("tile"); ap.add_argument("year", type=int)
    ap.add_argument("--lat", type=float, default=43.08); ap.add_argument("--lon", type=float, default=-72.10)
    ap.add_argument("--size", type=int, default=1200)
    a = ap.parse_args()
    ty, tx = int(a.tile[1:3]), int(a.tile[4:6])
    ys, xs = grid.tile_slices(ty, tx)
    t = init_store.YEARS.index(a.year)
    l0 = zarr.open_group(LocalStore(grid.ROOT / "data/pyramid_v1"), mode="r")["leafon/0"]
    names = init_store.BANDS
    medoid = {b: l0[b][t, ys, xs] for b in names}
    medoid["count"] = l0["clear_count"][t, ys, xs]
    z = np.load(grid.ROOT / f"data/trials/{a.tile}_{a.year}_median_fallback.npz")
    median = {b: z["bands"][i] for i, b in enumerate(names)}
    median["count"] = z["count"]
    for name, d in (("medoid", medoid), ("median", median)):
        s, f = stripe_score(d)
        print(f"{a.tile} {a.year} {name}: high-pass luminance std over forest {s * 1000:.2f} (x1e-3 refl), "
              f"forest share {100 * f:.0f}%, valid {100 * (d['red'] > 0).mean():.2f}%, mean count {d['count'][d['count'] > 0].mean():.2f}")
    same = (medoid["count"] == median["count"]).mean()
    print(f"clear_count identical in {100 * same:.2f}% of pixels (same looks, different reducer)")
    # crops
    r = int((grid.NORTH - a.lat) / grid.RES) - ys.start
    c = int((a.lon - grid.WEST) / grid.RES) - xs.start
    h = a.size // 2
    sl = (slice(max(r - h, 0), r + h), slice(max(c - h, 0), c + h))
    crops = [to_rgb({b: medoid[b][sl] for b in names}), to_rgb({b: median[b][sl] for b in names})]
    diff = (median["red"][sl].astype(np.int32) - medoid["red"][sl].astype(np.int32)) * SCALE
    dimg = np.clip(128 + diff / 0.05 * 127, 0, 255).astype(np.uint8)          # +-0.05 refl
    crops.append(np.stack([dimg] * 3, -1))
    cnt = medoid["count"][sl]
    cimg = (np.clip(cnt / 20, 0, 1) * 255).astype(np.uint8)
    crops.append(np.stack([cimg] * 3, -1))
    labels = ["medoid (v1 store)", "median (trial)", "median - medoid, red, +-0.05 refl", "clear count 0..20"]
    W = sum(im.shape[1] for im in crops) + 10 * 3
    H = max(im.shape[0] for im in crops) + 24
    canvas = Image.new("RGB", (W, H), (24, 24, 24))
    x = 0
    draw = ImageDraw.Draw(canvas)
    for im, lab in zip(crops, labels):
        canvas.paste(Image.fromarray(im), (x, 24))
        draw.text((x + 4, 4), lab, fill=(230, 230, 230))
        x += im.shape[1] + 10
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f"{a.tile}_{a.year}.png"
    canvas.save(p)
    print(f"wrote {p}")


if __name__ == "__main__":
    main()
