"""Create the empty Icechunk repo and leafon group on the target grid.

Arrays [time, y, x]: blue, green, red, nir, swir1, swir2 as uint16
(Collection 2 scaling, 0 = nodata) and clear_count as uint8. Chunks
1 x 512 x 512 inside 1 x 4096 x 4096 shards, zstd. Coordinates time (years
as datetime64 Jan 1), y, x (pixel centers, degrees). Runs once; refuses to
touch a repo that already has the group.

Usage: uv run python src/landsat_mosaic/init_store.py
"""
import sys
from pathlib import Path

import icechunk
import numpy as np
import zarr
from zarr.codecs import BloscCodec, BytesCodec, ZstdCodec

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from landsat_mosaic import grid

REPO_PATH = grid.ROOT / "data/newengland.icechunk"
GROUP = "leafon"
YEARS = list(range(2000, 2026))
BANDS = ["blue", "green", "red", "nir", "swir1", "swir2"]
CHUNK = (1, 512, 512)
SHARD = (1, grid.TILE, grid.TILE)


def open_repo() -> icechunk.Repository:
    return icechunk.Repository.open_or_create(
        icechunk.local_filesystem_storage(str(REPO_PATH)))


def main() -> None:
    repo = open_repo()
    session = repo.writable_session("main")
    root = zarr.open_group(session.store, mode="a")
    if GROUP in root:
        sys.exit(f"{GROUP} already exists in {REPO_PATH}; not touching it")
    g = root.create_group(GROUP)
    g.attrs.update({
        "title": "New England leaf-on Landsat composites",
        "crs": "EPSG:4326", "resolution_deg": grid.RES,
        "bbox": [grid.WEST, grid.SOUTH, grid.EAST, grid.NORTH],
        "window": "May 1 to October 31 (fallback), June 1 to September 30 (preferred)",
        "scale": 0.0000275, "offset": -0.2, "nodata": 0,
        "source": "Landsat Collection 2 Level-2 Tier 1, Microsoft Planetary Computer",
    })
    shape = (len(YEARS), grid.HEIGHT, grid.WIDTH)
    codecs = [BytesCodec(), ZstdCodec(level=3)]
    for b in BANDS + ["clear_count"]:
        g.create_array(
            b, shape=shape, dtype="uint8" if b == "clear_count" else "uint16",
            chunks=CHUNK, shards=SHARD, fill_value=0,
            serializer=BytesCodec(), compressors=[ZstdCodec(level=3)],
            dimension_names=["time", "y", "x"],
        )
    t = g.create_array("time", shape=(len(YEARS),), dtype="int32",
                       dimension_names=["time"])
    t[:] = np.array(YEARS, dtype="int32")
    t.attrs["long_name"] = "year"
    y = g.create_array("y", shape=(grid.HEIGHT,), dtype="float64",
                       chunks=(grid.HEIGHT,), dimension_names=["y"])
    y[:] = grid.y_coords()
    x = g.create_array("x", shape=(grid.WIDTH,), dtype="float64",
                       chunks=(grid.WIDTH,), dimension_names=["x"])
    x[:] = grid.x_coords()
    for a, name in ((y, "latitude"), (x, "longitude")):
        a.attrs.update({"units": "degrees", "standard_name": name})
    snap = session.commit("init empty leafon group")
    print(f"committed {snap}")
    print(zarr.open_group(repo.readonly_session("main").store, mode="r")[GROUP].tree())


if __name__ == "__main__":
    main()
