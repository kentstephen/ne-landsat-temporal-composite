"""Pilot: one small AOI, one year, several reducers, written to a local Icechunk repo.

Usage: uv run python src/landsat_mosaic/pilot.py <year> <lon> <lat> [half_size_deg] [m1-m2]

Optional m1-m2 restricts to a month window, e.g. 6-9 for leaf-on.

Loads every landsat-c2-l2 scene over the AOI for the year, masks with
QA_PIXEL, computes median / medoid / best-pixel (max NDVI) composites plus a
clear-observation count, writes them as groups in data/pilot.icechunk, and
saves quicklook PNGs in data/pilot_png/.
"""
import sys
import time
from pathlib import Path

import icechunk
import numpy as np
import odc.stac
import planetary_computer as pc
import pystac_client
import xarray as xr
from icechunk.xarray import to_icechunk

ROOT = Path(__file__).resolve().parents[2]
BANDS = ["blue", "green", "red", "nir08", "swir16", "swir22"]
OUT_NAMES = {"nir08": "nir", "swir16": "swir1", "swir22": "swir2"}

# QA_PIXEL bits (Collection 2)
FILL, DILATED, CIRRUS, CLOUD, SHADOW, SNOW = 0, 1, 2, 3, 4, 5


def clear_mask(qa: xr.DataArray, mask_snow: bool = False) -> xr.DataArray:
    bad = 0
    for b in (FILL, DILATED, CIRRUS, CLOUD, SHADOW) + ((SNOW,) if mask_snow else ()):
        bad |= 1 << b
    return (qa & bad) == 0


def load(year: int, bbox: tuple[float, float, float, float], months=(1, 12)) -> xr.Dataset:
    cat = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=pc.sign_inplace,
    )
    items = list(
        cat.search(
            collections=["landsat-c2-l2"],
            bbox=bbox,
            datetime=f"{year}-{months[0]:02d}-01/{year}-{months[1]:02d}-{31 if months[1] in (1,3,5,7,8,10,12) else 30:02d}",
            query={"landsat:collection_category": {"eq": "T1"}},
        ).items()
    )
    print(f"{len(items)} T1 scenes", flush=True)
    p = items[0].properties
    epsg = p.get("proj:epsg") or int(p["proj:code"].split(":")[1])
    ds = odc.stac.load(
        items,
        bands=BANDS + ["qa_pixel"],
        bbox=bbox,
        crs=f"EPSG:{epsg}",
        resolution=30,
        chunks={"x": 2048, "y": 2048},
        groupby="solar_day",
    )
    return ds


def reflectance(ds: xr.Dataset) -> xr.Dataset:
    """Scale C2 L2 SR to float reflectance; keep as float for the reducers."""
    sr = ds[BANDS].astype("float32") * 0.0000275 - 0.2
    return sr.where(clear_mask(ds.qa_pixel) & (ds.blue != 0))


def median(sr):
    return sr.median("time", skipna=True)


def medoid(sr):
    """Observation closest to the per-pixel median in band space."""
    med = sr.median("time", skipna=True)
    arr = sr.to_array("band")  # band,time,y,x
    dist = ((arr - med.to_array("band")) ** 2).sum("band")
    dist = dist.where(arr.notnull().all("band"))
    idx = dist.fillna(np.inf).argmin("time")
    picked = arr.isel(time=idx)
    picked = picked.where(dist.notnull().any("time"))
    return picked.to_dataset("band")


def best_pixel(sr):
    """Max NDVI observation. Greenest-pixel; classic, biased toward growing season."""
    ndvi = (sr.nir08 - sr.red) / (sr.nir08 + sr.red)
    idx = ndvi.fillna(-np.inf).argmax("time")
    picked = sr.isel(time=idx)
    return picked.where(ndvi.notnull().any("time"))


def to_int16(ds: xr.Dataset) -> xr.Dataset:
    out = ((ds + 0.2) / 0.0000275).round().fillna(0).astype("uint16")
    return out.rename(OUT_NAMES)


def quicklook(ds: xr.Dataset, count: xr.DataArray, name: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rgb = np.stack([ds[b].values for b in ("red", "green", "blue")], -1)
    rgb = np.clip((rgb.astype("float32") * 0.0000275 - 0.2) / 0.3, 0, 1)
    fig, ax = plt.subplots(1, 2, figsize=(12, 6))
    ax[0].imshow(rgb)
    ax[0].set_title(f"{name}: true color")
    im = ax[1].imshow(count.values, cmap="viridis")
    ax[1].set_title("clear observations")
    plt.colorbar(im, ax=ax[1], shrink=0.7)
    for a in ax:
        a.set_axis_off()
    d = ROOT / "data" / "pilot_png"
    d.mkdir(parents=True, exist_ok=True)
    fig.savefig(d / f"{name}.png", dpi=110, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    year = int(sys.argv[1])
    lon, lat = float(sys.argv[2]), float(sys.argv[3])
    h = float(sys.argv[4]) if len(sys.argv) > 4 else 0.15
    months = tuple(int(m) for m in sys.argv[5].split("-")) if len(sys.argv) > 5 else (1, 12)
    tag = f"{year}" if months == (1, 12) else f"{year}_m{months[0]}-{months[1]}"
    bbox = (lon - h, lat - h, lon + h, lat + h)

    t0 = time.time()
    ds = load(year, bbox, months)
    sr = reflectance(ds).compute()
    count = sr.blue.notnull().sum("time").astype("uint8").compute()
    print(f"loaded {dict(ds.sizes)} in {time.time()-t0:.0f}s", flush=True)

    repo = icechunk.Repository.open_or_create(
        icechunk.local_filesystem_storage(str(ROOT / "data" / "pilot.icechunk"))
    )
    for name, fn in [("median", median), ("medoid", medoid), ("best_pixel", best_pixel)]:
        t = time.time()
        comp = to_int16(fn(sr)).compute()
        comp = comp.drop_vars("time", errors="ignore")
        comp["clear_count"] = count
        comp = comp.expand_dims(time=[np.datetime64(f"{year}-01-01")])
        session = repo.writable_session("main")
        to_icechunk(comp.drop_vars("spatial_ref", errors="ignore"), session,
                    group=f"{name}/y{tag}", mode="w")
        session.commit(f"pilot {name} {year} {bbox}")
        quicklook(comp.isel(time=0), count, f"{name}_{tag}")
        print(f"{name}: {time.time()-t:.0f}s", flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
