# The store and the release

## Icechunk, locally

`init_store.py` created `data/newengland.icechunk` once, on 2026-09-04:
the `leafon` group with blue, green, red, nir, swir1, swir2 as uint16,
clear_count as uint8, shape [26, 26200, 27600], chunks 1 x 512 x 512 in
1 x 4096 x 4096 shards, zstd 3, coordinates time (years as datetime64
January 1), y and x (pixel centres in degrees). `store_source.py` added
source and pick_doy later, same chunks and shards.

Every tile-year is one commit on main, with the rule that built it in
the commit message; `pyramid.py` reads those messages to write the
ladder attrs. `tag.py NAME` tags main and is idempotent: an existing tag
at the same snapshot is a no-op, at a different snapshot an error,
because tags are immutable and a deleted tag name can never be reused.

Only one process writes. The local store is not safe for concurrent
commits, so `batch.py` is sequential, and every reader that ran beside
it (`stripe_score_store.py`, `check_rebuilt.py`, `validate_tile.py`,
`count_reducers.py`, `qa_level0.py`) opens a read-only session.

## Tags

Three tags ship in `build.icechunk/`:

    v3               own-year looks only, the fill rule everywhere: the composite before the ladder
    v4               the ladder in the 379 tile-years; level 0 of pyramid_v1 is this snapshot, pixel for pixel
    v4-provenance    v4 plus the source and pick_doy arrays; the snapshot pyramid_v1 was verified against

v3 is kept so that the pre-ladder pixels of the 379 tile-years stay
readable: `source` says which pixels were borrowed but not what was
there before. v1 (medoid, every look) and v2 (median in 2003 to 2012)
were expired.

## Garbage collection

`gc_store.py --keep TAGS` expires every snapshot older than the oldest
kept tag (icechunk keeps the root snapshot and the main tip regardless),
deletes the tags and branches that pointed only at expired snapshots,
and garbage collects the objects no surviving snapshot references. It
refuses to run while a writer is up, and `--dry-run` prints the same
numbers and deletes nothing. It was tested on scratch stores first:
kept tags still resolve, pixels and planes intact, old chunks gone.

The real run, 2026-09-09, `--keep v3,v4,v4-provenance`: 4 seconds, 1881
snapshots expired, 117.6 GB deleted (7162 chunks, 13160 manifests), 407
snapshots left on main. 240 GB to 130 GB. The v3 to v4 delta is bigger
than 379 tile-years of chunks because 2007 and the fixups were written
twice. `export_zarr.py --verify --ref v4-provenance` ran clean after,
and v3 still resolved (r01c04 2019 differs from v4 on 17.3 percent of
pixels, a ladder tile-year; r02c02 2024 on none).

## Source Coop

The bucket is `us-west-2.opendata.source.coop`, one prefix per product;
public reads go through the proxy at https://data.source.coop with GET,
HEAD, LIST, range requests and CORS, behind a Cloudflare cache (product
files 5 minutes, data 30 minutes, listings 1 minute). Writes go through
the proxy's S3 API with the credentials the `source-coop` CLI caches
after an OIDC login: the account is the bucket, the product is the key
prefix, addressing is path style, and every object carries the
bucket-owner-full-control ACL. Those STS credentials are valid only
against the proxy, not against AWS directly, which the first dry run
found out. The CLI session was one hour, not the twelve the docs
suggested; `upload.py` re-reads the cache whenever botocore needs a
refresh, so a second login in another terminal carries a long run
through. If the CLI has no session the standard AWS chain is used, for
the web UI's AWS_* exports.

Two proxy quirks from Colin Hill's CDL pipeline, both true here: no
batch DeleteObjects (a misleading NoSuchBucket comes back), so deletes
are per key; and README.md goes to the product root outside the store
prefix. boto3 is configured path style, retries mode "standard" with
max 10 (not "adaptive", which crawls after a few 5xx), connect 10 s,
read 60 s.

Hosted layout:

    README.md, LICENSE      the product README, CC0-1.0
    pyramid_v1/             the product
    build.icechunk/         the working store, tags v3, v4, v4-provenance
    supplemental/           tiger_states.parquet, hf435_wildlands.parquet, nhd_water_bodies.parquet, README.md

## Upload

`upload.py --prefix ACCOUNT/PRODUCT PATH...` walks each local path,
lists the remote prefix first and skips every key already there with
the same size, uploads the rest with retries and backoff (multipart
above 50 MB) on a thread pool, then lists the prefix again and prints
missing keys, size mismatches and remote-only keys, with a nonzero exit
until nothing is missing. `--diff-only` runs just the comparison;
`--delete-extra` removes remote-only keys under a path, per key, and is
off by default.

The run on 2026-09-09 was Stephen's, from his own terminal: 38,522
files, 261.7 GiB, at 88 to 98 MiB/s, about an hour. Seven PUTs dropped
with Cloudflare 520 (three store chunks, four pyramid chunks); the
rerun uploaded those seven and the final diff was clean, 38,522 local
equal to 38,522 remote, nothing missing, nothing mismatched, nothing
remote-only.

`remote_check.py --prefix ACCOUNT/PRODUCT` then opened the pyramid over
plain https with the consolidated metadata (1.3 s), checked the group
and level attrs against the local ones, read one 512 chunk per level
for three years (every band and the provenance planes) against
`data/pyramid_v1`, opened the store anonymously through the proxy,
checked the three tags, and read sample tile-year regions of
clear_count at v4-provenance against the local store. Clean.

One thing the release found about the proxy: Cloudflare answers 403 to
the stdlib Python urllib user agent (curl, requests, aiohttp and
obstore all get 200), so a reader that fetches a parquet with urllib
fails where a browser would not.

## The rule about credentials

The upload and the remote check are run by Stephen from his shell, with
his `source-coop login`. Nothing in this repo reads the CLI cache on its
own initiative or is run with credentials by anyone else.
