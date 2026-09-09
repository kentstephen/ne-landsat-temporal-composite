"""Copy the product to Source Coop with boto3, then diff the bucket against disk.

Credentials come from the source-coop CLI cache (`source-coop login` first)
and are re-read from it whenever botocore needs a refresh, so a re-login in
another terminal keeps a long run alive. If the CLI has no session, the
standard AWS chain is used instead: the AWS_ACCESS_KEY_ID,
AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN exports from the Source Coop web
UI, or a profile. AWS_ENDPOINT_URL, if set, overrides the proxy URL.
Writes go through the S3 API of the data.source.coop proxy (the CLI's
STS credentials are only valid there, not against AWS directly), where
the account is the bucket and the product the key prefix, with the
bucket-owner-full-control ACL Source Coop requires.

Each PATH is LOCAL[:REMOTE], a directory or file copied to
s3://ACCOUNT/PRODUCT/REMOTE (REMOTE defaults to the local basename). The
remote prefix is listed first and every key already there with the same
size is skipped, so a run that dies is rerun with the same arguments. At
the end the prefix is listed again and compared with disk: missing keys,
size mismatches and remote-only keys are printed, and the exit code is
nonzero unless nothing is missing or mismatched.

Usage:
  uv run python src/landsat_mosaic/upload.py --prefix ACCOUNT/PRODUCT \\
      README.md LICENSE data/pyramid_v1:pyramid_v1 supplemental \\
      data/newengland.icechunk:build.icechunk [--dry-run] [--workers 8]
  uv run python src/landsat_mosaic/upload.py --prefix ACCOUNT/PRODUCT --diff-only PATH...
  --delete-extra removes remote keys under a PATH's prefix that are not on
  disk (one DeleteObject per key). Off by default.
"""
import argparse
import hashlib
import json
import mimetypes
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.credentials import RefreshableCredentials
from botocore.session import get_session

ENDPOINT = "https://data.source.coop"   # proxy: bucket is the account, key is PRODUCT/...
REGION = "us-west-2"
ACL = "bucket-owner-full-control"
MULTIPART = 50 * 1024 * 1024
SKIP_NAMES = {".DS_Store"}


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ------------------------------------------------------------ credentials

def source_coop_creds() -> dict | None:
    """The cached STS credentials in botocore's refresh metadata shape, or
    None when the CLI is missing or has no session."""
    try:
        out = subprocess.run(["source-coop", "creds", "--format", "credential-process"],
                             capture_output=True, text=True)
    except FileNotFoundError:
        return None
    if out.returncode != 0:
        return None
    d = json.loads(out.stdout)
    return {"access_key": d["AccessKeyId"], "secret_key": d["SecretAccessKey"],
            "token": d["SessionToken"], "expiry_time": d["Expiration"]}


def client(workers: int):
    session = get_session()
    first = source_coop_creds()
    if first is not None:
        session._credentials = RefreshableCredentials.create_from_metadata(
            metadata=first, refresh_using=source_coop_creds, method="source-coop")
        log("credentials: source-coop CLI session")
    elif session.get_credentials() is not None:
        log(f"credentials: {session.get_credentials().method} (AWS chain)")
    else:
        sys.exit("no credentials: run `source-coop login`, or export the AWS_* variables from the web UI")
    cfg = Config(region_name=REGION, retries={"mode": "standard", "max_attempts": 10},
                 connect_timeout=10, read_timeout=60, max_pool_connections=max(32, 4 * workers),
                 s3={"addressing_style": "path"})
    endpoint = os.environ.get("AWS_ENDPOINT_URL", ENDPOINT)
    return boto3.Session(botocore_session=session).client("s3", endpoint_url=endpoint, config=cfg)


# ------------------------------------------------------------------- walk

def parse_path(spec: str) -> tuple[Path, str]:
    local, _, remote = spec.partition(":")
    p = Path(local)
    if not p.exists():
        sys.exit(f"{local} does not exist")
    return p, (remote or p.name).strip("/")


def local_files(root: Path, remote: str) -> dict[str, Path]:
    """key suffix (under the prefix) -> local path."""
    if root.is_file():
        return {remote: root}
    out = {}
    for dirpath, _, names in os.walk(root):
        for n in names:
            if n in SKIP_NAMES:
                continue
            f = Path(dirpath) / n
            out[f"{remote}/{f.relative_to(root).as_posix()}"] = f
    return out


def remote_files(s3, bucket: str, prefix: str, specs: list[tuple[Path, str]]) -> dict[str, tuple[int, str]]:
    """key suffix (under the prefix, so REMOTE/relpath) -> (size, etag), for
    every directory spec by listing and every file spec by a head."""
    out = {}
    for root, rem in specs:
        if root.is_dir():
            pages = s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{prefix}/{rem}/")
            for page in pages:
                for o in page.get("Contents", []):
                    out[o["Key"][len(prefix) + 1:]] = (o["Size"], o["ETag"].strip('"'))
        else:
            try:
                h = s3.head_object(Bucket=bucket, Key=f"{prefix}/{rem}")
                out[rem] = (h["ContentLength"], h["ETag"].strip('"'))
            except s3.exceptions.ClientError:
                pass
    return out


def md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ----------------------------------------------------------------- upload

class Progress:
    def __init__(self, n_files: int, n_bytes: int):
        self.n, self.total, self.files, self.done, self.t0 = n_files, n_bytes, 0, 0, time.time()
        self.lock, self.last = threading.Lock(), 0.0

    def add(self, nbytes: int) -> None:
        with self.lock:
            self.done += nbytes

    def file_done(self) -> None:
        with self.lock:
            self.files += 1
            now = time.time()
            if now - self.last > 30 or self.files == self.n:
                self.last = now
                mb = self.done / 2**20
                rate = mb / max(now - self.t0, 1e-9)
                left = (self.total / 2**20 - mb) / max(rate, 1e-9) / 60
                log(f"{self.files}/{self.n} files, {mb / 1024:.2f} of {self.total / 2**30:.2f} GiB, "
                    f"{rate:.1f} MiB/s, about {left:.0f} min left")


def upload_one(s3, bucket: str, key: str, path: Path, prog: Progress) -> None:
    ctype = mimetypes.guess_type(path.name)[0]
    extra = {"ACL": ACL}
    if path.suffix == ".md":
        extra["ContentType"] = "text/markdown"
    elif path.name == "zarr.json" or path.suffix == ".json":
        extra["ContentType"] = "application/json"
    elif ctype:
        extra["ContentType"] = ctype
    cfg = TransferConfig(multipart_threshold=MULTIPART, multipart_chunksize=MULTIPART,
                         max_concurrency=4, use_threads=True)
    s3.upload_file(str(path), bucket, key, ExtraArgs=extra, Config=cfg, Callback=prog.add)
    prog.file_done()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+", help="LOCAL[:REMOTE]")
    ap.add_argument("--prefix", required=True, help="ACCOUNT/PRODUCT")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true", help="list what would be uploaded")
    ap.add_argument("--diff-only", action="store_true", help="compare only, upload nothing")
    ap.add_argument("--md5", action="store_true", help="also compare md5 to ETag for single-part objects")
    ap.add_argument("--delete-extra", action="store_true")
    a = ap.parse_args()
    bucket, _, prefix = a.prefix.strip("/").partition("/")
    if not prefix:
        sys.exit("--prefix must be ACCOUNT/PRODUCT")
    specs = [parse_path(s) for s in a.paths]

    local: dict[str, Path] = {}
    for root, remote in specs:
        local.update(local_files(root, remote))
    total = sum(p.stat().st_size for p in local.values())
    log(f"{len(local)} local files, {total / 2**30:.2f} GiB, to {ENDPOINT}/{bucket}/{prefix}/")

    s3 = client(a.workers)
    remote = remote_files(s3, bucket, prefix, specs)
    log(f"{len(remote)} remote objects under the prefix")

    todo = {k: p for k, p in local.items()
            if k not in remote or remote[k][0] != p.stat().st_size}
    todo_bytes = sum(p.stat().st_size for p in todo.values())
    log(f"{len(todo)} files to upload ({todo_bytes / 2**30:.2f} GiB), "
        f"{len(local) - len(todo)} already present with the same size")
    if a.dry_run:
        for k in sorted(todo)[:40]:
            print(f"  {k}  {todo[k].stat().st_size}")
        if len(todo) > 40:
            print(f"  ... {len(todo) - 40} more")
        return
    if todo and not a.diff_only:
        prog = Progress(len(todo), todo_bytes)
        failed = []
        with ThreadPoolExecutor(a.workers) as pool:
            futs = {pool.submit(upload_one, s3, bucket, f"{prefix}/{k}", p, prog): k
                    for k, p in sorted(todo.items())}
            for f in as_completed(futs):
                try:
                    f.result()
                except Exception as e:
                    failed.append(futs[f])
                    log(f"FAILED {futs[f]}: {e!r}")
        log(f"uploaded {len(todo) - len(failed)} files, {len(failed)} failed")

    # final diff, from a fresh listing
    remote = remote_files(s3, bucket, prefix, specs)
    missing = sorted(k for k in local if k not in remote)
    mismatch = sorted(k for k in local if k in remote and remote[k][0] != local[k].stat().st_size)
    extra = sorted(k for k in remote if k not in local)
    bad_md5 = []
    if a.md5:
        for k, p in local.items():
            if k in remote and "-" not in remote[k][1] and remote[k][1] != md5(p):
                bad_md5.append(k)
    log(f"diff: {len(local)} local, {len(remote)} remote, {len(missing)} missing, "
        f"{len(mismatch)} size mismatch, {len(extra)} remote only"
        + (f", {len(bad_md5)} md5 mismatch" if a.md5 else ""))
    for name, keys in (("missing", missing), ("size mismatch", mismatch), ("md5 mismatch", bad_md5)):
        for k in keys[:20]:
            print(f"  {name}: {k}")
    for k in extra[:20]:
        print(f"  remote only: {k}")
    if extra and a.delete_extra:
        for k in extra:
            s3.delete_object(Bucket=bucket, Key=f"{prefix}/{k}")
        log(f"deleted {len(extra)} remote-only objects")
    if missing or mismatch or bad_md5:
        sys.exit("upload incomplete, rerun with the same arguments")
    log("remote matches disk")


if __name__ == "__main__":
    main()
