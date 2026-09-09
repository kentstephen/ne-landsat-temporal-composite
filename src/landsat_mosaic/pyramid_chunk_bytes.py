"""Compressed bytes per chunk for the pyramid, measured on finished tiles: every level 0-5, chunk 128/256/512, per-band vs six-band stack, time-1 vs time-26. Output: stats/pyramid_chunk_bytes.json. Usage: uv run python src/landsat_mosaic/pyramid_chunk_bytes.py stats/pyramid_chunk_bytes.json"""
import sys, json, time
import numpy as np, zarr, icechunk
sys.path.insert(0, "src")
from landsat_mosaic import grid, init_store

repo = icechunk.Repository.open(icechunk.local_filesystem_storage(str(init_store.REPO_PATH)))
g = zarr.open_group(repo.readonly_session("main").store, mode="r")[init_store.GROUP]
years = list(init_store.YEARS)
ARR = init_store.BANDS + ["clear_count"]
import numcodecs
_cz = numcodecs.Zstd(level=3)
def _z(a): return len(_cz.encode(np.ascontiguousarray(a).tobytes()))

def coarsen(a, k):
    """nodata-aware block mean of a 2-D array, block k, rounded, same dtype."""
    if k == 1: return a
    H, W = a.shape
    b = a.reshape(H // k, k, W // k, k).astype(np.float64)
    n = (b > 0).sum(axis=(1, 3)); t = b.sum(axis=(1, 3))
    return np.where(n > 0, np.rint(t / np.maximum(n, 1)), 0).astype(a.dtype)

tiles = [(3, 3), (2, 4), (4, 2)]   # interior land tiles
yi = years.index(2016)
out = {}
for ty, tx in tiles:
    ys, xs = grid.tile_slices(ty, tx) if hasattr(grid, "tile_slices") else (slice(ty*4096,(ty+1)*4096), slice(tx*4096,(tx+1)*4096))
    t0 = time.time()
    planes = {a: g[a][yi, ys, xs] for a in ARR}
    print(f"tile r{ty:02d}c{tx:02d} read {time.time()-t0:.1f}s valid {(planes['clear_count']>0).mean():.3f}", flush=True)
    key = f"r{ty:02d}c{tx:02d}"
    out[key] = {}
    for lvl in range(0, 6):
        k = 2 ** lvl
        c = {a: coarsen(p, k) for a, p in planes.items()}
        n = 4096 // k
        rec = {}
        for cs in (128, 256, 512):
            if cs > n: continue
            # per-band chunks [1, cs, cs]: bytes per chunk, averaged over the tile
            per = {}
            for a in ARR:
                p = c[a]; sizes = []
                for i in range(0, n, cs):
                    for j in range(0, n, cs):
                        sizes.append(_z(p[i:i+cs, j:j+cs]))
                per[a] = float(np.mean(sizes))
            # band-as-dimension chunk [6, cs, cs], first chunk block only
            stack = np.stack([c[a][:cs, :cs] for a in init_store.BANDS])
            rec[cs] = {"per_band": per, "six_band_stack": _z(stack),
                       "raw_per_band": cs * cs * 2}
        out[key][lvl] = rec
    # time-26 vs time-1 at level 0, 512 chunk, red band, one chunk block
    t0 = time.time()
    red26 = g["red"][:, ys.start:ys.start+512, xs.start:xs.start+512]
    got = [i for i in range(26) if red26[i].any()]
    out[key]["time"] = {"years_present": len(got),
                        "t26_one_chunk": _z(red26[got]),
                        "t1_sum": int(sum(_z(red26[i]) for i in got))}
    print(f"  time test {time.time()-t0:.1f}s", flush=True)
json.dump(out, open(sys.argv[1], "w"), indent=1)
for key, d in out.items():
    print(key, "time:", d["time"])
    for lvl in range(6):
        for cs, r in d[lvl].items():
            pb = r["per_band"]
            print(f"  L{lvl} cs{cs:3d} refl mean {np.mean([pb[b] for b in init_store.BANDS])/1024:6.1f} KB "
                  f"ratio {r['raw_per_band']/np.mean([pb[b] for b in init_store.BANDS]):4.2f}  "
                  f"count {pb['clear_count']/1024:5.1f} KB  6stack {r['six_band_stack']/1024:6.1f} KB vs sum {sum(pb[b] for b in init_store.BANDS)/1024:6.1f} KB")
