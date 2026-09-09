"""Collect the Icechunk store down to the tags that ship.

Expires every snapshot older than the oldest kept tag (icechunk keeps the
root snapshot and the main tip regardless), deletes the tags and branches
that pointed only at expired snapshots, then garbage collects the objects
no surviving snapshot references. Refuses to run while a writer is up.
A dry run prints the same numbers and deletes nothing.

Tags cannot be recreated once deleted (icechunk refuses tag reuse), so the
kept tags are the final names.

Usage:
  uv run python src/landsat_mosaic/gc_store.py --keep v4,v4-provenance --dry-run
  uv run python src/landsat_mosaic/gc_store.py --keep v4,v4-provenance
"""
import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from landsat_mosaic import init_store

WRITERS = r"python.*(batch|runner|source_pass|store_source|init_store)\.py"


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def du(path: Path) -> str:
    return subprocess.run(["du", "-sh", str(path)], capture_output=True, text=True).stdout.split()[0]


def snapshot(repo, tag: str):
    return next(iter(repo.ancestry(tag=tag)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", required=True, help="comma separated tags to keep")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ignore-writers", action="store_true", help="tests on a scratch store only")
    a = ap.parse_args()
    keep = a.keep.split(",")
    running = subprocess.run(["pgrep", "-fl", WRITERS], capture_output=True, text=True).stdout.strip()
    if running and not a.ignore_writers:
        sys.exit(f"a writer is running, not collecting:\n{running}")
    repo = init_store.open_repo()
    tags = repo.list_tags()
    missing = [t for t in keep if t not in tags]
    if missing:
        sys.exit(f"tags to keep not in the store: {missing} (have {sorted(tags)})")
    kept = {t: snapshot(repo, t) for t in keep}
    for t, s in kept.items():
        log(f"keep {t} = {s.id} written {s.written_at:%Y-%m-%d %H:%M:%S} {s.message[:60]!r}")
    cut = min(s.written_at for s in kept.values())
    head = repo.lookup_branch("main")
    if head not in {s.id for s in kept.values()}:
        sys.exit(f"main ({head}) is not one of the kept tags; tag it or fix --keep")
    newer = [t for t in tags if t not in keep and snapshot(repo, t).written_at >= cut]
    if newer:
        sys.exit(f"tags not in --keep are newer than the cut and would survive: {newer}")
    before = len(list(repo.ancestry(branch="main")))
    log(f"{before} snapshots on main, tags {sorted(tags)}, branches {sorted(repo.list_branches())}, "
        f"{du(init_store.REPO_PATH)} on disk")
    log(f"expiring snapshots written before {cut:%Y-%m-%d %H:%M:%S.%f}")
    if a.dry_run:
        gone = [s for s in repo.ancestry(branch="main") if s.written_at < cut]
        log(f"dry run: {len(gone)} snapshots would expire, tags {sorted(set(tags) - set(keep))} deleted")
        summary = repo.garbage_collect(datetime.now(timezone.utc), dry_run=True)
        log(f"dry run gc (before expiry, so only already-unreferenced objects): {summary}")
        return
    expired = repo.expire_snapshots(cut, delete_expired_branches=True, delete_expired_tags=True)
    log(f"expired {len(expired)} snapshots; tags now {sorted(repo.list_tags())}, "
        f"branches {sorted(repo.list_branches())}")
    summary = repo.garbage_collect(datetime.now(timezone.utc))
    log(f"gc: {summary}")
    after = list(repo.ancestry(branch="main"))
    log(f"{len(after)} snapshots on main: {[(s.id, s.message[:40]) for s in after]}")
    log(f"{du(init_store.REPO_PATH)} on disk")
    for t in keep:
        assert repo.lookup_tag(t) == kept[t].id, f"tag {t} moved"
    log("kept tags still resolve; done")


if __name__ == "__main__":
    main()
