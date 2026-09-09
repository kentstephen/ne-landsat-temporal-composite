# Landsat 7 striping

This is the problem that took most of the build. From June 2003 every
Landsat 7 scene has wedge-shaped gaps about a kilometre apart where the
scan line corrector failed. The gaps themselves are easy: they are
nodata and the mask drops them. The trouble is what they do to a
per-pixel reducer. Inside a gap zone a pixel has a different set of
clear looks than its neighbours a few hundred metres away, so adjacent
stripes are medoids of different dates, and the composite shows the gap
geometry as faint diagonal tone striping in the forest. clear_count
shows it hard.

The first build (v1, medoid, every look counted) had it in every year
that carried Landsat 7 looks, 2003 through 2023, strongest 2003 to 2012
when Landsat 7 was half the looks or more. It was first noticed in the
viewer and first mistaken for a reducer problem.

## Measuring it

`stripe_score.py` gives every 30 km block (512 px of pyramid level 1) a
number. The gaps are periodic lines, 12 to 28 px apart at level 1,
tilted a few degrees off east-west. The score takes the 2D power
spectrum of the block, finds the 15 degree angular sector with the most
power in that period band, and divides by the mean power at other
angles. Done on clear_count that is `cc_ratio`, which says how much
Landsat 7 was in the mix; done on NIR reflectance in the same sector it
is `nir_ratio`, which says whether the picture stripes. 1.0 is
isotropic texture. Years with no Landsat 7 sit at nir 1.05 to 1.10 and
cc 1.5 to 2.2.

A late fix to the score (2026-09-08): where the count plane has no
stripes (`cc_ratio` under 3, the `CC_ANCHOR`), the strongest sector is
arbitrary and `nir_ratio` reads terrain grain as striping. The lakes
and ridges of interior Maine run east-west at 0.7 to 1.7 km spacing,
exactly the gap band, and the r01c04 tile topped the worst list for
three years running with scores of 3 to 4 that did not move when the
tile was rebuilt. `nir_gated` is `nir_ratio` where `cc_ratio` is at
least 3 and 1.0 elsewhere: no count stripes, no stripe claim. The
summaries rank on the gated value and report land blocks (block median
NIR reflectance at least 0.05) separately from water, where a near-zero
denominator makes the ratio meaningless.

`stripe_score_store.py` runs the same score on level 0 read straight
from the Icechunk store, coarsened 2x, so a rebuild can be checked on
the years that have landed before the pyramid is refreshed.

## The median trial, and why it was wrong

The first read was that the medoid was the problem, since a median
blends dates and should hide which look a pixel came from.
`compare_reducers.py` on r04c01 for 2008 and 2011 showed the median
removing most of the visible tone striping and softening the picture
by 8 to 10 percent in forest high-pass texture, with the count striping
unchanged by construction. 2003 to 2012 were rebuilt as median (v2, 330
jobs, about 12 hours). Scored, the striping was still in every Landsat 7
year under either reducer. The reducer was never the cause; the look set
was.

## The fill rule (v3)

Landsat 7 looks count at a pixel only where Landsat 5, 8 and 9 together
give fewer than `min_clear` (4) clear looks. Where the other platforms
suffice, the gap pattern does not change the look set and the striping
goes. Where they do not, Landsat 7 fills in and the count still shows
the gap geometry. `stripe_trials.py` tried this against dropping Landsat
7 outright and against the median, on the worst block of six years, and
the fill variant kept the coverage that dropping lost.

2012 needed its own answer. Landsat 5 had stopped in late 2011 and
Landsat 8 launched in February 2013, so 2012 is Landsat 7 alone and the
fill rule would have nothing to fall back on. Its stack pools 2011 to
2013 (`YEAR_SPAN`), so a 2012 pixel may be a 2011 or 2013 observation.
Pooled, it has about ten clear looks per pixel and scores like a year
with no Landsat 7; on its own looks it had five and was the most striped
year.

The v3 rebuild ran 2003 to 2023, 693 jobs, 23.7 hours. Per year the
count striping fell to the clean-year level everywhere (cc median from
8 to 26 down to 1.8 to 2.2) and the NIR striping most of the way there:

    years        before the rule   v3 (fill rule)    clean years
    2003-2012    1.29 to 1.77      1.09 to 1.20      1.05 to 1.10
    2013-2023    1.15 to 1.36      1.07 to 1.15

The cost, by construction, is fewer looks per pixel where Landsat 7 was
excluded: mean clear count 8 to 10 down to 5 to 6 in 2003 to 2021. The
set of pixels with fewer than four looks was unchanged to four decimals
in every year but 2012 (a pixel is short after the rule if and only if
it was short before).

## The residual, and the ladder (v4)

After v3 a streak was still visible near Newbury, New Hampshire, in
2020 and across the decades. Loading the looks explained it: in 2020 the
western Landsat path over Newbury had one clean Landsat 8 pass all
summer and one half-clear, so Landsat 8 alone reached four looks on
only 41 percent of the window and the other 59 percent went to the
fill and striped. The rule had worked as written. The residual was
wherever a whole summer was cloudy for the non-Landsat-7 platforms, and
the seam followed the WRS path edge. Grid-wide, 17 to 96 blocks per
year (of about 460) still had `cc_ratio` above 5, and 379 of the 693
rebuilt tile-years contained such a block. That list is
`data/l7fill_jobs.txt`.

`fill_trials.py` tried four rules on a Newbury window for 2020 and 2014,
loading the years either side once:

    variant   rule                                              2020 nir   L7 / borrowed share
    fill4     the store: own non-L7 where >= 4, else every look    1.34      38% / 0
    fill3     same at threshold 3                                  1.24      28% / 0
    nbr4      own non-L7 >= 4; else own + neighbour non-L7 >= 4;   1.00       0 / 42%
              else every own look plus the neighbour looks
    nbr3      the same ladder at threshold 3                       0.92       0 / 31%

nbr3 was chosen. The ladder, per pixel:

1. the own-year Landsat 5, 8 and 9 looks, where they reach three;
2. else those plus the same platforms' looks from the year before and
   after, where together they reach three;
3. else every own-year look, Landsat 7 included, plus the neighbour
   looks.

It is `runner.valid_obs`, verified equal to the fill rule with
neighbours off and threshold 4, and equal to the trial's nbr3 on the
Newbury window. `runner.neighbour_items` loads only the non-Landsat-7
scenes of the years either side. The targeted rebuild was
`batch.py --force --fill-min-clear 3 --neighbours --jobs data/l7fill_jobs.txt`,
379 jobs. The other 314 tile-years of 2003 to 2023 keep the v3 rule,
threshold 4 and own year only. Which rule applies where is recorded per
tile-year in the Icechunk commit messages and, from there, in the
pyramid attrs (`l7_ladder_tile_years`).

The ladder's second rung is the one that changes what the product
claims. Each pixel is still one real observation, never a blend, but
in the 379 tile-years the observation may be from the year before or
after. That is what the provenance planes are for (docs/04).

## Two more stripe sources

Trialling nbr3 on the worst block of eleven years, the Landsat 8 era
fixed cleanly (2013 1.46 to 0.76, 2016 1.85 to 0.71, 2019 2.74 to 1.14)
but the 2004 to 2012 windows barely moved and 2023 not at all. Scoring
the clear masks scene by scene found two things the fill rule could not
see.

Landsat 5 bumper-mode edges. From 2002 Landsat 5 ran its scan mirror in
bumper mode and its east and west scene edges are combs, one tooth per
16-line swath, kilometres long. In a path overlap the look set
alternates swath by swath and the medoid stripes with it: `cc_ratio` of
19 to 30 on the own-year Landsat 5 count with no Landsat 7 in the set
at all. `runner.erode_l5_edges` erodes each Landsat 5 slot's
data-present mask by 25 rows (about 700 m) in `load_stack`. On the trial
windows 2006 went 1.45 to 0.96, 2004 2.56 to 1.12, 2009 1.65 to 0.66,
at no cost in mean count; pixels near those edges draw on the
neighbouring path.

Same-day Landsat 7 and 9. From 2022 the two image a tile on the same
day (docs/02), and `load_stack` had merged them into one slot labelled
by the last item, so the gaps sat inside "landsat-9" slots that the rule
treated as clean. Slots are now (solar day, platform). The 2023 r00c04
window went from 2.09 / 9.9 to 0.94 / 1.8.

Both fixes went into the runner before the v4 batches ran, so the 379
ladder tile-years carry them and the 314 v3 tile-years do not.

## What v4 measured

`stripe_score.py` on the finished v4 pyramid, gated, land blocks only,
v3 to v4. "anchored" is the number of land blocks with count stripes
(`cc_ratio` at least 3):

    year   anchored     gated p90     gated max   blocks > 2
    2003   89 -> 13     1.36 -> 1.00  3.8 -> 1.9   6 -> 0
    2004   90 -> 22     1.51 -> 1.00  5.2 -> 2.6  20 -> 5
    2007  104 -> 13     1.48 -> 1.00  4.6 -> 1.9  15 -> 0
    2011   70 -> 5      1.18 -> 1.00  3.1 -> 2.1   7 -> 1
    2013   64 -> 19     1.12 -> 1.00  3.2 -> 2.5   8 -> 2
    2019  106 -> 10     1.44 -> 1.00  5.8 -> 1.4  11 -> 0
    2023   37 -> 4      1.00 -> 1.00  3.4 -> 1.6   5 -> 0

The full table is in `stats/striping/summary.json` and the v3 baseline
in `data/striping_v3/`. The rebuild cut the anchored land blocks from 50
to 106 per rebuilt year to 3 to 34, and the gated p90 is 1.00 in every
year. Forty land blocks across all years still score above 2; 37 of
them are identical to v3 and sit in tile-years that were never on the
ladder list, at `cc_ratio` 3.0 to 5.0, where the anchor is marginal.
Three are in rebuilt tile-years (r01c03 2005 rose to 2.96, r01c02 2013
to 2.33, r03c03 2016 held at 2.32). 2000 to 2002 and 2024 to 2025 are
identical to v3 in every column.

Mid-build, `check_rebuilt.py` compared every year's rebuilt tiles
against the v3 scores as the tiles landed, and `validate_tile.py` gave
each tile-year an OK or FLAG line off the store within minutes of its
commit (docs/06). The one real seam the ladder made stronger is r00c05
2014, where the rung boundary follows a path-overlap edge: inside the
overlap the own-year Landsat 8 looks reach three, outside they fall to
rung 2 and mix 2013 and 2015 looks, and the two sides are composited
from different date sets. It fired on one tile of 379, so it stayed a
tile case rather than a rule case.
