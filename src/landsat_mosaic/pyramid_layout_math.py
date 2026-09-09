"""Pyramid layout math: chunks per level (total and touching land, from the
2016 clear_count mask), object counts per chunk size, compressed GB per
level from the bytes measured by pyramid_chunk_bytes.py, and bytes plus
requests per screen for 128/256/512 chunks. Prints tables; numbers copied
into PLAN.md "Chunking math". Usage: uv run python src/landsat_mosaic/pyramid_layout_math.py"""
import sys, math, json, time
import numpy as np, zarr, icechunk
sys.path.insert(0, "src")
from landsat_mosaic import grid, init_store
H, W = grid.HEIGHT, grid.WIDTH
repo = icechunk.Repository.open(icechunk.local_filesystem_storage(str(init_store.REPO_PATH)))
g = zarr.open_group(repo.readonly_session("main").store, mode="r")[init_store.GROUP]
t0 = time.time()
yi = init_store.YEARS.index(2016)
valid = g["clear_count"][yi, ::8, ::8] > 0          # 3275 x 3450, one cell = 8 px
print(f"mask read {time.time()-t0:.1f}s, land fraction {valid.mean():.3f}", file=sys.stderr)

def nonempty(level, cs):
    """chunks of cs at this level that touch any valid pixel."""
    k = 2 ** level
    # level pixel = k L0 px; chunk = cs*k L0 px = cs*k/8 mask cells
    cell = cs * k / 8
    ny, nx = math.ceil(H / (cs * k)), math.ceil(W / (cs * k))
    cnt = 0
    for i in range(ny):
        for j in range(nx):
            y0, y1 = int(i * cell), int(math.ceil((i + 1) * cell))
            x0, x1 = int(j * cell), int(math.ceil((j + 1) * cell))
            if valid[y0:y1, x0:x1].any(): cnt += 1
    return ny * nx, cnt

# measured compressed bytes per pixel per band (mean of 3 tiles, cs 512/256)
BPP_L0, BPP_LN = 1.31, 1.58          # reflectance uint16
CPP_L0, CPP_LN = 0.12, 0.22          # clear_count uint8 (L1+ ~0.2-0.27, use L2 value)
NY, NB = 26, 6
rows = []
for lvl in range(8):
    k = 2 ** lvl
    h, w = math.ceil(H / k), math.ceil(W / k)
    r = {"level": lvl, "h": h, "w": w, "px_m": 30 * k}
    for cs in (128, 256, 512):
        tot, ne = nonempty(lvl, cs)
        r[f"c{cs}"] = (tot, ne)
    bpp = BPP_L0 if lvl == 0 else BPP_LN
    cpp = CPP_L0 if lvl == 0 else CPP_LN
    ne512 = r["c512"][1]
    r["gb"] = ne512 * 512 * 512 * (NB * bpp + cpp) * NY / 1e9
    rows.append(r)
print("level  size            px    chunks/plane (total, nonempty) 128 | 256 | 512      GB(26y,7 arrays)")
for r in rows:
    print(f"{r['level']}   {r['h']:6d}x{r['w']:<6d} {r['px_m']:5d}m   "
          f"{r['c128'][0]:6d},{r['c128'][1]:6d} | {r['c256'][0]:5d},{r['c256'][1]:5d} | {r['c512'][0]:5d},{r['c512'][1]:5d}    {r['gb']:6.1f}")
print("total GB", sum(r["gb"] for r in rows), "levels 1+", sum(r["gb"] for r in rows[1:]))
for cs in (128, 256, 512):
    print(f"objects per array-year at cs{cs}, levels 1-7:", sum(r[f"c{cs}"][1] for r in rows[1:]),
          "x7 arrays x26 years =", sum(r[f"c{cs}"][1] for r in rows[1:]) * 7 * NY)
print("level 0 objects, sharded 4096:", 858 * 7, " unsharded 512:", rows[0]["c512"][1] * 7 * NY)
# sharded alternative for L1 (4096 shards, 512 inner), L2 (2048 shards)
for lvl, sh in ((1, 4096), (1, 2048), (2, 2048), (2, 1024)):
    tot, ne = nonempty(lvl, sh)
    print(f"L{lvl} shard {sh}: {ne} nonempty shards/plane, x7x26 = {ne*7*NY}, shard bytes ~ {sh*sh*BPP_LN/1e6:.1f} MB refl")

# viewport math: bytes and requests per band for one screen at the level
# whose pixel matches the screen pixel (best case, 1 px/px) and worst case (2 px/px)
print()
print("screen (device px)  chunk   requests/band  fetched MB/band (1x .. 2x source px per screen px)")
for name, sw, sh_ in (("laptop 1440x900 @2", 2880, 1800), ("phone 390x844 @3", 1170, 2532), ("4k 3840x2160 @1", 3840, 2160)):
    for cs in (128, 256, 512):
        out = []
        for f in (1, 2):
            w, h = sw * f, sh_ * f
            # expected chunk count with random alignment: (w/cs + 1)(h/cs + 1)
            n = (w / cs + 1) * (h / cs + 1)
            mb = n * cs * cs * BPP_LN / 1e6
            out.append((n, mb))
        print(f"{name:20s} {cs:4d}   {out[0][0]:6.0f} .. {out[1][0]:5.0f}     {out[0][1]:5.1f} .. {out[1][1]:5.1f}")
