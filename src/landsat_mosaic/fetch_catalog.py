"""Download the MPC stac-geoparquet snapshot of landsat-c2-l2, one file per year.

Only the newest part for each year is kept (2026 has several overlapping
snapshots). Free: the token is a public read SAS from the Planetary Computer.
"""
import re
import sys
from pathlib import Path

import adlfs
import planetary_computer as pc

OUT = Path(__file__).resolve().parents[2] / "data" / "catalog"


def main(start_year: int = 1997, only: int | None = None) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tok = pc.sas.get_token("pcstacitems", "items").token
    fs = adlfs.AzureBlobFileSystem(account_name="pcstacitems", credential=tok)
    newest: dict[int, str] = {}
    for f in sorted(fs.find("items/landsat-c2-l2.parquet")):
        m = re.search(r"part-\d+_(\d{4})-", f)
        year = int(m.group(1))
        newest[year] = f  # sorted, so last wins = latest snapshot
    for year, f in sorted(newest.items()):
        if year < start_year or (only and year != only):
            continue
        dst = OUT / f"landsat-c2-l2_{year}.parquet"
        if dst.exists():
            continue
        print("fetch", year, flush=True)
        fs.get(f, str(dst) + ".part")
        (dst.parent / (dst.name + ".part")).rename(dst)
    print("done")


if __name__ == "__main__":
    only = int(sys.argv[1]) if len(sys.argv) > 1 else None
    main(only=only)
