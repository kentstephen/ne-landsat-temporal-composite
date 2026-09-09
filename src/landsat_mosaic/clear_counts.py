"""Expected clear looks per WRS path/row per year, from the catalog alone.

Proxy for clear_count before any pixels are read: for each T1 scene in the
window, expected clear fraction = 1 - cloud_cover_land/100. Landsat 7 scenes
after the SLC failure (2003-05-31) are further scaled by 0.78 for the gap
stripes. Summing over scenes gives the expected number of clear looks at a
typical land pixel in that path/row.

Usage: uv run python src/landsat_mosaic/clear_counts.py
Writes stats/clear_counts.parquet and stats/clear_counts.png
"""
from pathlib import Path

import duckdb
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
BBOX = (-73.75, 40.95, -66.85, 47.5)
CC_MAX = 80

con = duckdb.connect()
con.execute(f"""
create view ne as
select id, datetime, year(datetime) y, month(datetime) m, platform,
  "landsat:wrs_path" p, "landsat:wrs_row" r,
  "landsat:cloud_cover_land" cc,
  bbox.xmin xmin, bbox.ymin ymin, bbox.xmax xmax, bbox.ymax ymax
from '{ROOT}/data/catalog/landsat-c2-l2_*.parquet'
where "landsat:collection_category"='T1'
  and bbox.xmax > {BBOX[0]} and bbox.xmin < {BBOX[2]}
  and bbox.ymax > {BBOX[1]} and bbox.ymin < {BBOX[3]}
  and year(datetime) between 2000 and 2025
""")

q = f"""
with s as (
  select *, 
    (1 - cc/100.0) * (case when platform='landsat-7' and datetime > '2003-05-31' then 0.78 else 1 end) w
  from ne where cc < {CC_MAX}
)
select p, r, y,
  count(*) filter (where m between 6 and 9) n_js,
  round(sum(w) filter (where m between 6 and 9), 1) clear_js,
  count(*) filter (where m between 5 and 10) n_mo,
  round(sum(w) filter (where m between 5 and 10), 1) clear_mo,
  round(avg(xmin+xmax)/2, 2) lon, round(avg(ymin+ymax)/2, 2) lat
from s group by 1,2,3 order by 1,2,3
"""
df = con.sql(q).df()
df.to_parquet(ROOT / "stats/clear_counts.parquet")

print("== path/rows in bbox ==")
print(df.groupby(["p", "r"])[["lon", "lat"]].first().to_string())

print("\n== region-wide, per year: median and min over path/rows of expected clear looks ==")
g = df.groupby("y").agg(
    scenes_js=("n_js", "sum"), med_clear_js=("clear_js", "median"), min_clear_js=("clear_js", "min"),
    scenes_mo=("n_mo", "sum"), med_clear_mo=("clear_mo", "median"), min_clear_mo=("clear_mo", "min"),
)
print(g.round(1).to_string())

print("\n== worst (path/row, year) cells, June-Sept ==")
print(df.nsmallest(20, "clear_js")[["p", "r", "y", "lon", "lat", "n_js", "clear_js", "n_mo", "clear_mo"]].to_string(index=False))

# heatmap: path/row (sorted west to east, north to south) x year
df["pr"] = df.p + "/" + df.r
prs = df.groupby("pr")[["lon", "lat"]].first().sort_index()
years = sorted(df.y.unique())
fig, axes = plt.subplots(1, 2, figsize=(16, 0.32 * len(prs) + 2), sharey=True)
for ax, col, title in zip(axes, ["clear_js", "clear_mo"], ["June to Sept", "May to Oct"]):
    piv = df.pivot(index="pr", columns="y", values=col).reindex(prs.index).reindex(columns=years)
    im = ax.imshow(piv.values, cmap="viridis", vmin=0, vmax=12, aspect="auto")
    ax.set_xticks(range(len(years)), years, rotation=90, fontsize=7)
    ax.set_yticks(range(len(prs)), piv.index, fontsize=7)
    ax.set_title(f"expected clear looks, {title}, cc_land < {CC_MAX}")
    for i in range(piv.shape[0]):
        for j in range(piv.shape[1]):
            v = piv.values[i, j]
            if np.isfinite(v):
                ax.text(j, i, f"{v:.0f}", ha="center", va="center", fontsize=5,
                        color="white" if v < 6 else "black")
fig.colorbar(im, ax=axes, shrink=0.6, label="expected clear looks (capped at 12)")
fig.savefig(ROOT / "stats/clear_counts.png", dpi=130, bbox_inches="tight")
print("\nwrote stats/clear_counts.png")
