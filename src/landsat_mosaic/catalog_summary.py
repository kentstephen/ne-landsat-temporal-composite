"""Summarize the downloaded landsat-c2-l2 catalog: scenes and bytes per year."""
import duckdb

con = duckdb.connect()
con.execute("load spatial;")
con.execute("create view items as select * from 'data/catalog/landsat-c2-l2_*.parquet'")

print("== scenes per year by platform (T1 only) ==")
print(con.sql("""
select year(datetime) y,
  count(*) filter (where platform='landsat-5') l5,
  count(*) filter (where platform='landsat-7') l7,
  count(*) filter (where platform='landsat-8') l8,
  count(*) filter (where platform='landsat-9') l9,
  count(*) total,
  round(avg("landsat:cloud_cover_land"),1) mean_cc_land,
  count(*) filter (where "landsat:cloud_cover_land" < 80) usable_lt80
from items where "landsat:collection_category"='T1'
group by 1 order by 1"""))

print("== tier split ==")
print(con.sql("""select "landsat:collection_category" cat, count(*) from items group by 1"""))

print("== distinct path/rows with any T1 scene, by year ==")
print(con.sql("""
select year(datetime) y, count(distinct "landsat:wrs_path"||'_'||"landsat:wrs_row") pathrows
from items where "landsat:collection_category"='T1' group by 1 order by 1"""))
