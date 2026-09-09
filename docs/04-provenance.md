# Provenance

The ladder (docs/03) puts observations from the year before or after
into 379 tile-years, and the store as built kept only clear_count, not
which look each pixel came from. Stephen asked whether v4 degraded the
imagery. On the stripe metrics, no. On the axis the score cannot see,
yes: a same-year Landsat 7 pick became a pick from a neighbouring year
on some share of pixels, and that share was not recoverable grid-wide.
The cheap proxy, the fraction of pixels that changed between v3 and v4
per rebuilt tile-year from the validator log, had a median of 0.10 and a
p75 of 0.23, and that is an upper bound.

The decision was to finish the build, recover the provenance after the
fact, and be upfront about it in the product README.

## The source pass

The medoid stores one real observation unaltered, so the pick can be
found again. `source_pass.py` reloads exactly the stack the build
loaded (`runner.job_items` plus `runner.neighbour_items`, through
`runner.load_stack`), rebuilds the ladder's valid mask with
`runner.valid_obs` called the way `batch.py` called it (min_clear 4,
fill_min_clear 3, fallback window), pushes each slot through the same
float32 path (`to_reflectance`, then back to uint16), and takes the
first valid slot whose six bands equal the stored six. The medoid's
argmin also takes the first of equal candidates, so ties resolve the
same way. As a check that the stack was reproduced, the ladder's count
is compared to the stored clear_count.

On the test tile-year (r01c05 2014, 125 scenes) the count matched the
store on 100.0000 percent of pixels and no stored pixel was unmatched.
Over the 379 tile-years the same held: `unmatched` 0.0 in every year,
`min_count_equal_nonzero` 1.0 (`stats/source_pass/summary.json`).

Output is a side store, `data/source_v4.zarr`, on the level 0 grid with
one shard per tile-year, so two workers on disjoint halves of the job
list (`--shard 0/2`, `--shard 1/2`) ran safely. Two planes:

    source     uint8   0 own-year look, not Landsat 7
                       1 look from the year before (the ladder's second rung)
                       2 look from the year after
                       3 own-year Landsat 7 look (third rung)
                       4 nodata in the store
                       5 stored value matched no valid look (0 everywhere)
                       255 not computed
    pick_doy   uint16  day of year of the picked look, in its own calendar year; 0 no pick

The split of the second rung into year-before and year-after came late
(2026-09-08): a cut showing a year early and a cut showing a year late
are different errors and the layer should tell them apart. `pick_doy`
came with it, because the year alone hides the phenology shift: on the
first sub-tile of r01c05 2014 the own-year picks had a median day of
year of 262, the 2013 picks 195 and the 2015 picks 249, so a borrowed
pixel there was on average six to nine weeks earlier in the season than
the own-year pixels around it.

Per year, share of the 379 tile-years' pixels by class:

    year   own     before  after   L7
    2003   0.951   0.021   0.015   0.011
    2004   0.876   0.047   0.065   0.012
    2005   0.985   0.002   0.002   0.012
    2006   0.963   0.017   0.007   0.012
    2007   0.895   0.040   0.052   0.012
    2008   0.940   0.017   0.022   0.013
    2009   0.941   0.014   0.026   0.012
    2010   0.849   0.091   0.037   0.013
    2011   0.961   0.021   0.000   0.013
    2012   0.998   0.000   0.000   0.002
    2013   0.848   0.000   0.149   0.004
    2014   0.897   0.050   0.052   0.001
    2015   0.902   0.008   0.073   0.002
    2016   0.952   0.019   0.018   0.001
    2017   0.905   0.018   0.063   0.000
    2018   0.978   0.015   0.008   0.000
    2019   0.848   0.084   0.063   0.000
    2020   0.914   0.039   0.038   0.000
    2021   0.939   0.021   0.040   0.000
    2022   0.996   0.001   0.003   0.000
    2023   0.992   0.000   0.000   0.005

Pooled: 92.9 percent own-year Landsat 5, 8 or 9, 2.6 percent the year
before, 3.4 percent the year after, 0.7 percent own-year Landsat 7.
2013 borrows only from 2014 (14.9 percent) because 2012 has nothing to
lend, and 2011 borrows only from 2010 for the same reason. 2012 reads
as almost all own-year because its own years are 2011 to 2013 by design;
the layer says it borrowed nothing while a third of it may be 2011 or
2013, and the product README carries that caveat in words.

The 314 v3 tile-years have a hidden class of their own: they borrowed no
year, but where the non-Landsat-7 looks fell short of four they took
Landsat 7 with gaps. The pass could run on them with the v3 rule and
give the Landsat 7 share, at about the same cost again. It has not.

## Into the store and the pyramid

`store_source.py` copies `source` and `pick_doy` from the side store
into the Icechunk store beside clear_count, one commit per year, reads
every plane back against the side store, and tags main `v4-provenance`.
The `v4` tag stays at the pixel snapshot because icechunk refuses to
reuse a deleted tag name. `source_pyramid.py` then writes three planes
into the pyramid:

    leafon/0/source            the class, level 0 only
    leafon/{0..7}/pick_doy     the picked look's day of year; levels 1-7 the nodata-aware block mean
    leafon/{0..7}/borrowed_pct uint8 0-100, the share of a block's valid pixels that are a
                               neighbour-year observation; 0 or 100 at level 0; 255 unknown
                               or outside the ladder tile-years

The levels are exact: borrowed and valid counts are summed 2x2 level to
level in uint32 and the percentage is the rounded ratio. Tile-years
outside the ladder list used their own year only, so they are 0
wherever clear_count is nonzero.

`pyramid.py --finalize` writes the ladder attrs beside the composite
attrs: `l7_ladder_tile_years` and `l7_ladder_tiles_by_year` (read from
the Icechunk commit messages on main, newest per tile-year, with
`data/l7fill_jobs.txt` as the fallback), `l7_ladder_fill_min_clear`,
`neighbour_span`, `l5_edge_rows` and a note.

## What borrowing costs

`holdout_test.py` measures the error of a borrowed pick where the own
year has plenty of clean looks. On a 2048 px window it loads the year
and its neighbours, takes as reference the medoid over every clear
own-year non-Landsat-7 look at pixels with at least five of them, then
starves those pixels to 0, 1 or 2 random own-year looks and composites
three ways: `own3`, three random own-year looks (the noise floor of a
right-year three-look medoid); `nbr`, the starved own looks plus every
neighbour-year look (the ladder's second rung); `v3`, the starved own
looks plus every own-year Landsat 7 look (the fill rule). Errors are
against the reference, split by whether the landscape changed between
the neighbour years (|dNDVI| between the two neighbour-year medoids
under 0.05 is stable, over 0.15 is changed).

Seven windows (`data/holdout_windows.txt`): Katahdin Woods and Waters
in 2006, 2010, 2016 and 2019, and Newbury, New Hampshire, in 2004, 2014
and 2020. Pooled, median |NDVI error| and the share of pixels with error
above 0.1:

    pick                                     stable            changed
    own3   3 own-year looks (noise floor)    0.013  (13%)      0.027  (30%)
    nbr    neighbour-year looks (the ladder) 0.033  (16%)      0.090  (47%)
    v3     own-year Landsat 7 (the fill)     0.041  (18%)      0.076  (43%)
    the neighbour year outright              0.035 / 0.036     0.118 / 0.162

On stable land a borrowed pick costs about the same as the Landsat 7
fill it replaced, roughly two and a half times the three-look noise
floor and small in absolute terms. Where the landscape changed between
the neighbour years the borrowed pick is worse than the fill (0.090
against 0.076 median) and far from taking the wrong year outright
(0.118 or 0.162). Per window the numbers are in `stats/holdout/`.

Two caveats the test cannot remove. It starves pixels that had plenty of
own-year looks, the cleaner and better-sampled places, while the ladder
fires where the year was cloudy and the neighbour looks may be atypical
too, so these are a lower bound on the real cost. And the ladder's pool
is biased toward the neighbour years by construction: on rung 2 a pixel
has 0 to 2 own looks in a pool of perhaps 20 neighbour looks, so the
medoid lands on a neighbour look even when an own-year look was nearly
as good. A rule that prefers an own-year look among near-median
candidates would cut the borrowed share without bringing the stripes
back. That would be a new method version.
