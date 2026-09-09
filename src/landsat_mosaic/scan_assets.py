"""Find corrupt Landsat assets on Planetary Computer before the batch trips on them.

Written for the 2022 incident: many 2022 scenes on Planetary Computer are
header-only blobs (12-14 KB), truncated blobs (offsets past Content-Length,
HTTP 416), or a QA_PIXEL without a CRS. Symptoms in the runner were
WarpOperationError "Chunk and warp failed", "got 0 bytes", and an
AssertionError on src.crs. Opening every asset and reading its last block
and a block in the middle finds all three cheaply (two range requests per
asset). Scanning 2023-2025 (10,626 assets) found nothing, so the damage is
confined to 2022.

Prints one line per problem asset and a BAD_IDS line per year. Paste the
ids into data/bad_scenes.txt; runner.bad_scenes() reads that file per job
(no restart needed) and job_items() drops those scenes.

Usage: uv run python src/landsat_mosaic/scan_assets.py 2022 [YEAR ...]
Needs data/jobs.parquet (planner.py) and data/catalog/ (fetch_catalog.py).
"""
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import planetary_computer as pc
import pyarrow.parquet as pq
import pystac
import rasterio
from rasterio.windows import Window
from stac_geoparquet.arrow import stac_table_to_items

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from landsat_mosaic import runner

warnings.simplefilter("ignore")
ROOT = Path(__file__).resolve().parents[2]
THREADS = 16
RETRIES = 3


def check(job):
    """(id, band, signed href) -> None if the asset opens and reads, else (id, band, why)."""
    iid, band, href = job
    err = "?"
    for _ in range(RETRIES):
        try:
            with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"):
                with rasterio.open("/vsicurl/" + href) as ds:
                    if ds.crs is None or ds.transform.is_identity:
                        return (iid, band, "NOCRS")
                    bh, bw = ds.block_shapes[0]
                    r0 = (ds.height - 1) // bh * bh
                    c0 = (ds.width - 1) // bw * bw
                    ds.read(1, window=Window(c0, r0, ds.width - c0, ds.height - r0))   # last block
                    ds.read(1, window=Window(ds.width // 2, ds.height // 2, 64, 64))   # middle
                    return None
        except Exception as e:
            err = repr(e)[:60]
    return (iid, band, "FAIL " + err)


def scan(year: int) -> set[str]:
    jobs = pd.read_parquet(ROOT / "data" / "jobs.parquet", columns=["tile", "year", "id"])
    ids = sorted(set(jobs[jobs.year == year].id))
    tab = pq.read_table(ROOT / "data" / "catalog" / f"landsat-c2-l2_{year}.parquet", filters=[("id", "in", ids)])
    items = [pystac.Item.from_dict(d) for d in stac_table_to_items(tab)]
    bands = runner.SRC_BANDS + ["qa_pixel"]
    work = [(it.id, b, pc.sign(it.assets[b].href)) for it in items for b in bands if b in it.assets]
    with ThreadPoolExecutor(THREADS) as ex:
        res = [r for r in ex.map(check, work) if r]
    bad = {r[0] for r in res}
    print(f"=== {year}: {len(items)} scenes, {len(work)} assets, {len(res)} problem assets, {len(bad)} scenes", flush=True)
    for r in sorted(res):
        print(r, flush=True)
    print("BAD_IDS", year, sorted(bad), flush=True)
    return bad


if __name__ == "__main__":
    for y in sys.argv[1:]:
        scan(int(y))
