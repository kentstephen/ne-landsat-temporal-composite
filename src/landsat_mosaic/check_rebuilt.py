"""Score one year from the store and compare the blocks inside rebuilt tiles
against the v3 pyramid scores (stats/striping/<year>.json).
Read-only session beside the running batch. Nothing is written.
nir columns are gated (stripe_score.gated: 1.0 where cc_ratio < CC_ANCHOR);
the worst list ranks on the gated value and shows the raw one beside it.
usage: uv run python src/landsat_mosaic/check_rebuilt.py 2013 data/logs/build.nbr3.l8era.log"""
import sys, re, json, numpy as np, zarr
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from landsat_mosaic import grid, init_store, stripe_score, stripe_score_store as st
year=int(sys.argv[1]); log=open(sys.argv[2]).read()
tiles=set(re.findall(rf"^(r\d\dc\d\d) {year}: done in", log, re.M))
repo=init_store.open_repo(); g=zarr.open_group(repo.readonly_session("main").store, mode="r")[init_store.GROUP]
t=year-init_store.YEARS[0]; cc0=g["clear_count"][t]; nir1=st.coarsen(g["nir"][t],cc0); cc1=st.coarsen(cc0,cc0)
new={(b['by'],b['bx']):b for b in stripe_score.score_plane(nir1,cc1)}
B=stripe_score.BLOCK; refl=nir1*stripe_score.SCALE+stripe_score.OFFSET
def is_water(by,bx):
    blk=refl[by:by+B,bx:bx+B]; v=cc1[by:by+B,bx:bx+B]>0
    return bool(np.median(blk[v])<0.05) if v.any() else True
water={k:is_water(*k) for k in new}
import os
BASE=Path(os.environ.get('STRIPE_BASELINE', grid.ROOT / 'data/striping_v3'))  # v3 per-year block JSONs (git 98d4b45); stats/striping holds v4 now
old={(b['by'],b['bx']):b for b in json.load(open(BASE / f'{year}.json'))}
def tile_of(by,bx): return f"r{by*2//4096:02d}c{bx*2//4096:02d}"
inside=[k for k in new if k in old and tile_of(*k) in tiles and not water[k]]
inside_w=[k for k in new if k in old and tile_of(*k) in tiles and water[k]]
outside=[k for k in new if k in old and tile_of(*k) not in tiles and not water[k]]
def summ(keys,label):
    if not keys: print(label,'no blocks'); return
    G=stripe_score.gated; A=stripe_score.CC_ANCHOR
    o=np.array([[G(old[k]),old[k]['cc_ratio'],old[k]['mean_count']] for k in keys]); n=np.array([[G(new[k]),new[k]['cc_ratio'],new[k]['mean_count']] for k in keys])
    print(f"{label}: {len(keys)} blocks | gated nir p90 {np.percentile(o[:,0],90):.2f}->{np.percentile(n[:,0],90):.2f} max {o[:,0].max():.1f}->{n[:,0].max():.1f} blocks >2 {(o[:,0]>2).sum()}->{(n[:,0]>2).sum()} anchored (cc>={A:g}) {(o[:,1]>=A).sum()}->{(n[:,1]>=A).sum()} | cc median {np.median(o[:,1]):.1f}->{np.median(n[:,1]):.1f} blocks cc>5 {(o[:,1]>5).sum()}->{(n[:,1]>5).sum()} | count {o[:,2].mean():.1f}->{n[:,2].mean():.1f}")
print(f"{year}: {len(tiles)} rebuilt tiles in the log")
summ(inside,'inside rebuilt tiles, land'); summ(inside_w,'inside rebuilt tiles, water'); summ(outside,'outside land (should be unchanged)')
G=stripe_score.gated
worst=sorted(inside,key=lambda k:(-G(new[k]),-new[k]['nir_ratio']))[:5]
print('worst remaining inside, land (gated, raw, cc, was gated):', [(tile_of(*k), round(G(new[k]),2), round(new[k]['nir_ratio'],2), round(new[k]['cc_ratio'],1), 'was', round(G(old[k]),2)) for k in worst])
