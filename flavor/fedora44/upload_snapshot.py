#!/usr/bin/env python3
"""Upload a snapshot's blobs to S3-compatible object storage.

Consumes the `upload.json` written by `import_snapshot.py` and pushes every
object under its content-addressed key (the sha256). Split out from the importer
on purpose: this is the only step that needs *write* credentials, so it belongs
in CI, never in a build sandbox.

Because keys are content-addressed the whole thing is idempotent -- it checks
what is already there and uploads only what is genuinely missing, so re-importing
a closure where three packages moved pushes three packages, not 88 MB.

Requires boto3 (`pip install boto3`). Credentials come from the usual chain
(`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, `~/.aws/credentials`, `--profile`).
For OCI these must be S3-compatible **Customer Secret Keys** (IAM -> User ->
Customer Secret Keys), NOT the OCI API signing keys.

Note the S3-compat endpoint is a *different* host from the public read URL baked
into packages.json:

    read (public):  https://<ns>.objectstorage.<region>.oci.customer-oci.com/n/<ns>/b/<bucket>/o/
    s3 endpoint:    https://<ns>.compat.objectstorage.<region>.oraclecloud.com

Usage:
    ./flavor/fedora44/upload_snapshot.py \\
        --bucket fedora.jade.fyi

    # see what would happen without touching anything
    ./flavor/fedora44/upload_snapshot.py --bucket ... --dry-run
"""

import argparse
import concurrent.futures
import hashlib
import json
import sys
import threading
from pathlib import Path

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover
    sys.exit("boto3 not found. `pip install boto3` (or run this from CI where it is installed).")

HERE = Path(__file__).resolve().parent

# botocore clients are cheap to make but not guaranteed thread-safe, so give
# each worker thread its own off a shared (thread-safe) Session.
_local = threading.local()


def get_client(args: argparse.Namespace):
    client = getattr(_local, "client", None)
    if client is None:
        session = boto3.session.Session(profile_name=args.profile, region_name=args.region)
        client = _local.client = session.client("s3")
    return client


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def exists(args: argparse.Namespace, key: str) -> bool:
    try:
        get_client(args).head_object(Bucket=args.bucket, Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def existing_keys(args: argparse.Namespace, keys: list[str]) -> set[str]:
    """Which of `keys` are already in the bucket?

    HEADs only the keys this manifest cares about, so cost is O(manifest) and
    independent of how large the bucket grows. (Enumerating the bucket instead
    would be O(bucket): the keys are content-addressed sha256s and therefore
    uniformly distributed, so no prefix narrows a listing to just our objects.)
    Cheap because boto3 pools connections -- these are plain HTTP round-trips,
    not a process spawn each.
    """
    found = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for key, ok in pool.map(lambda k: (k, exists(args, k)), keys):
            if ok:
                found.add(key)
    return found


def upload_one(args: argparse.Namespace, obj: dict, repo_dir: Path) -> str:
    path = repo_dir / obj["path"]
    if not path.is_file():
        raise FileNotFoundError(f"{path} (listed in upload.json but missing on disk)")

    # Guard against poisoning a content-addressed key with the wrong bytes: if
    # the object at key <sha> does not hash to <sha>, every future build fails
    # buck2's checksum verification, permanently, until someone notices. Cheap
    # insurance against a stale or half-rewritten working directory.
    if not args.no_verify:
        actual = sha256_of(path)
        if actual != obj["sha256"]:
            raise ValueError(
                f"sha256 mismatch -- manifest says {obj['sha256']}, "
                f"file is {actual}. Re-run import_snapshot.py."
            )

    if args.dry_run:
        return "would-upload"

    with path.open("rb") as body:
        get_client(args).put_object(
            Bucket=args.bucket,
            Key=obj["key"],
            Body=body,
            ContentType=obj.get("content_type", "application/octet-stream"),
        )
    return "uploaded"


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--bucket", required=True, help="destination bucket name")
    ap.add_argument(
        "--repo-dir",
        type=Path,
        default=HERE / "repo",
        help="snapshot repo dir containing upload.json and the blobs",
    )
    ap.add_argument("--profile", help="AWS credential profile")
    ap.add_argument("--region", help="AWS region")
    ap.add_argument("-j", "--jobs", type=int, default=8, help="parallel workers (default: 8)")
    ap.add_argument("-n", "--dry-run", action="store_true", help="report what would be uploaded")
    ap.add_argument(
        "--no-verify",
        action="store_true",
        help="skip re-hashing each file before upload (faster, less safe)",
    )
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    repo_dir = args.repo_dir.resolve()
    manifest = repo_dir / "upload.json"
    if not manifest.is_file():
        sys.exit(
            f"{manifest} not found. Run import_snapshot.py with --base-url first "
            "(local mode does not produce an upload manifest)."
        )

    objects = json.loads(manifest.read_text())["objects"]
    # Content-addressed keys dedupe naturally; identical blobs upload once.
    by_key = {o["key"]: o for o in objects}

    try:
        present = existing_keys(args, sorted(by_key))
    except ClientError as e:
        sys.exit(f"failed to query bucket {args.bucket}: {e}")

    todo = [o for k, o in sorted(by_key.items()) if k not in present]
    total_bytes = sum(o["size"] for o in todo)
    print(
        f"{len(by_key)} objects in manifest ({len(objects) - len(by_key)} duplicate keys), "
        f"{len(present)} already in bucket, {len(todo)} to upload ({total_bytes / 1e6:.1f} MB)"
    )
    if not todo:
        print("nothing to do")
        return

    failures = []
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(upload_one, args, o, repo_dir): o for o in todo}
        for fut in concurrent.futures.as_completed(futures):
            obj = futures[fut]
            try:
                status = fut.result()
                done += 1
                if done % 25 == 0 or done == len(todo):
                    print(f"  {done}/{len(todo)} {status}")
            except Exception as e:  # noqa: BLE001 - collect and report, keep going
                failures.append((obj["path"], str(e)))

    if failures:
        print(f"\n{len(failures)} FAILED:", file=sys.stderr)
        for path, err in failures[:10]:
            print(f"  {path}: {err}", file=sys.stderr)
        sys.exit(1)

    verb = "would upload" if args.dry_run else "uploaded"
    print(f"{verb} {len(todo)} objects ({total_bytes / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
