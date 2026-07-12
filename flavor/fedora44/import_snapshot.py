#!/usr/bin/env python3
"""Import a Fedora RPM snapshot into buck2 targets + an upload manifest.

This is the *generator*; it lives in the main repo. Its outputs are artifacts
and live in the snapshot repo (`flavor/fedora44/repo/` by default), which is a
separate git repo precisely so this churn stays out of the main history.

It has two subcommands, meant to run back-to-back in CI:

  download  resolve the transitive closure named in `config.toml` and fetch
            every .rpm, once per arch (dnf is invoked with --forcearch so a
            single host can snapshot both aarch64 and x86_64). Incremental:
            rpms already on disk are kept (dnf re-fetches nothing), and only
            those that have dropped out of the closure are pruned;

  build     turn that directory of .rpm files into the committed lockfile:

    1. generates the repodata xml chunk for each rpm (replicating antlir's
       `makechunk`) -- see WHY below;
    2. hashes and sizes every rpm and xml chunk;
    3. writes `packages.json` -- the resolved NVRA set, hashes and provenance.
       buck2 `load()`s this file directly (it exposes a parsed .json as the
       symbol `value`), so it is simultaneously the build input AND the
       human-readable lockfile, rather than the same facts duplicated into
       generated starlark. Committing it is what makes an import diffable
       ("these 3 packages moved") instead of an opaque wall of churn;
    4. writes `upload.json` -- exactly which objects to push to object storage,
       under content-addressed keys, so uploading is a dumb loop and re-imports
       only push what is new;
    5. writes the `BUCK` stub, which is a fixed handful of lines regardless of
       whether the snapshot has 10 packages or 10,000.

WHY the xml is pre-generated: antlir's `makechunk` imports
`//third-party/python:createrepo-c`, a prebuilt wheel pinned x86_64/cp310 that
will not run on e.g. aarch64/py3.14. Doing it here means the *build* never needs
createrepo_c -- only this import step does. (Building createrepo_c properly with
buck2 would remove even that; left as a follow-up.)

Upload is deliberately NOT done here: it needs write credentials, which should
live in CI, not in a build sandbox. Feed `upload.json` to `upload_snapshot.py`.

Usage:
    # in CI: fetch the closure, then generate the lockfile (base_url from config)
    ./flavor/fedora44/import_snapshot.py download
    ./flavor/fedora44/import_snapshot.py build

    # local: reference rpms/xml as files next to BUCK (offline/bootstrap)
    ./flavor/fedora44/import_snapshot.py build --local
"""

import argparse
import datetime
import hashlib
import json
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
GENERATOR = "flavor/fedora44/import_snapshot.py"
CONFIG = HERE / "config.toml"
# Stable, machine-independent destdir recorded in provenance (the real download
# uses an absolute --repo-dir, which would churn the lockfile across machines).
REPO_REL = "flavor/fedora44/repo"
RELEASEVER = "44"

# Fedora's stock release + updates repos, written out as a reposdir .repo file
# at download time. $releasever/$basearch are substituted by dnf from
# --releasever/--forcearch. We fetch the closure from these mirrors and then
# re-host it under content-addressed keys (see repo/README.md), so the import
# must define the repos itself -- the build host has no /etc/yum.repos.d, and
# pointing reposdir here also means we get exactly these repos regardless of
# what the host happens to have configured. gpgcheck is off: this step only
# *downloads*, and every blob is sha-pinned in packages.json and re-verified by
# buck2 at build time.
FEDORA_REPOS = """\
[fedora]
name=Fedora $releasever - $basearch
metalink=https://mirrors.fedoraproject.org/metalink?repo=fedora-$releasever&arch=$basearch
enabled=1
gpgcheck=0

[updates]
name=Fedora $releasever updates - $basearch
metalink=https://mirrors.fedoraproject.org/metalink?repo=updates-released-f$releasever&arch=$basearch
enabled=1
gpgcheck=0
"""


def load_config(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def arch_packages(config: dict) -> dict[str, list[str]]:
    """Map each configured arch to the full spec list to fetch for it.

    Layout in config.toml:

        [packages]
        include = [ ... ]     # fetched for every arch
        [packages.aarch64]
        include = [ ... ]     # extra specs, this arch only
        [packages.x86_64]
        include = []

    Every subtable under [packages] names an arch we snapshot; its spec list is
    the shared include plus that arch's own include.
    """
    section = config.get("packages", {})
    shared = section.get("include", [])
    arches = {name: sub for name, sub in section.items() if isinstance(sub, dict)}
    if not arches:
        sys.exit("config.toml defines no [packages.<arch>] tables; nothing to fetch")
    return {
        arch: sorted(set(shared) | set(sub.get("include", [])))
        for arch, sub in arches.items()
    }


def dnf_argv(
    dnf_bin: str,
    arch: str,
    packages: list[str],
    destdir,
    releasever: str,
    setopts: list[str] = (),
    *,
    url: bool = False,
) -> list[str]:
    """The dnf invocation for one arch.

    --releasever/--forcearch/--setopt are *global* options (they precede the
    `download` subcommand); this ordering works for both dnf5 and dnf4.
    --resolve/--alldeps pull the full transitive closure so the snapshot is
    self-contained. With url=True the closure is *resolved but not fetched* --
    dnf prints one rpm URL per line to stdout instead of downloading; cmd_download
    uses this to learn the exact set belonging in the snapshot (same --resolve
    --alldeps flags, so it matches the fetch set) before touching the disk.
    `setopts` carry the rootless cache redirects (see cmd_download); they are
    deliberately left out of the provenance recorded in packages.json, being an
    environment detail rather than part of the snapshot.
    """
    return [
        dnf_bin,
        f"--releasever={releasever}",
        f"--forcearch={arch}",
        *(f"--setopt={o}" for o in setopts),
        "download",
        "--resolve",
        "--alldeps",
        "--url" if url else f"--destdir={destdir}",
        *packages,
    ]


def find_dnf(explicit: str | None) -> str:
    if explicit:
        return explicit
    for cand in ("dnf5", "dnf"):
        found = shutil.which(cand)
        if found:
            return found
    sys.exit("neither dnf5 nor dnf found on PATH; install one or pass --dnf")


def hash_and_size(path: Path) -> tuple[str, int]:
    """sha256 + byte length, streamed so large rpms stay off the heap."""
    h = hashlib.sha256()
    n = 0
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def make_xml(cr, pkg, href: str, out_path: Path) -> None:
    """Replicate antlir's makechunk.py using the createrepo_c module."""
    # Stable output: package_from_rpm records the *file* mtime, which differs
    # per download and would churn the xml (and its sha) on every re-import.
    pkg.time_file = pkg.time_build
    pkg.location_href = href
    # Written compact and WITHOUT a trailing newline: these chunks are
    # content-addressed, so any cosmetic byte change rewrites every key and
    # forces a full re-upload.
    out_path.write_text(
        json.dumps(
            {
                "primary": cr.xml_dump_primary(pkg),
                "filelists": cr.xml_dump_filelists(pkg),
                "other": cr.xml_dump_other(pkg),
            },
            sort_keys=True,
        )
    )


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")


def write_buck(repo_dir: Path, rules_cell: str) -> None:
    """Write the BUCK stub.

    This is fixed-size: every package fact lives in packages.json, which buck2
    load()s directly. Rewritten each run only to keep it in sync with the
    generator; its content does not depend on the package set.
    """
    repo_dir.joinpath("BUCK").write_text(
        "\n".join(
            [
                f"# @generated by {GENERATOR} -- do not edit by hand; re-run the generator.",
                "# Everything package-specific lives in packages.json; buck2 load()s that",
                "# file directly, exposing the parsed document as the symbol `value`. So",
                "# this stub stays the same size no matter how large the snapshot grows.",
                f'load("@{rules_cell}//antlir/antlir2/package_managers/dnf/rules:snapshot.bzl", "rpm_snapshot")',
                'load(":packages.json", snapshot = "value")',
                "",
                'oncall("antlir")',
                "",
                "rpm_snapshot(snapshot = snapshot)",
            ]
        )
        + "\n"
    )


def cmd_download(args) -> None:
    config = load_config(args.config)
    per_arch = arch_packages(config)
    dnf_bin = find_dnf(args.dnf)

    repo_dir = args.repo_dir.resolve()
    repo_dir.mkdir(parents=True, exist_ok=True)

    # dnf writes repo metadata, logs and state under /var by default, which is
    # unwritable without root. Redirect all three into a writable cache dir so a
    # plain CI user can fetch. The two arch passes share the metadata cache.
    cachedir = args.cachedir.resolve()
    cachedir.mkdir(parents=True, exist_ok=True)

    # Define the Fedora repos ourselves (the host has none); reposdir=here also
    # shadows any host repos, so the closure is exactly fedora+updates.
    reposdir = cachedir / "repos"
    reposdir.mkdir(parents=True, exist_ok=True)
    (reposdir / "fedora.repo").write_text(FEDORA_REPOS)

    setopts = [
        f"reposdir={reposdir}",
        f"cachedir={cachedir}",
        f"logdir={cachedir / 'log'}",
        f"persistdir={cachedir / 'persist'}",
    ]

    # Resolve the transitive closure per arch WITHOUT fetching (dnf --url prints
    # one rpm URL per line to stdout). The union of basenames is the exact set of
    # .rpm that belongs in this snapshot. Knowing it up front lets us prune only
    # what has genuinely left the closure and keep everything else on disk; dnf's
    # own download then skips any rpm already sitting in --destdir, so a re-run
    # fetches only what actually changed instead of re-pulling the whole closure.
    # This also warms the metadata cache the fetch pass below reuses.
    resolved: set[str] = set()
    for arch, packages in sorted(per_arch.items()):
        argv = dnf_argv(dnf_bin, arch, packages, repo_dir, args.releasever, setopts, url=True)
        out = subprocess.run(argv, check=True, stdout=subprocess.PIPE, text=True).stdout
        resolved.update(
            line.rsplit("/", 1)[-1] for line in out.split() if line.endswith(".rpm")
        )
    if not resolved:
        sys.exit("dnf resolved an empty closure; refusing to touch the snapshot")

    # Drop rpms no longer in the closure (a package removed from config, or a
    # version superseded upstream) so the fetched set exactly reflects
    # config.toml. --no-clean keeps them, at the risk of a stale rpm sneaking
    # into the next build.
    on_disk = {p.name for p in repo_dir.glob("*.rpm")}
    stale = sorted(on_disk - resolved)
    if stale and not args.no_clean:
        for name in stale:
            repo_dir.joinpath(name).unlink()
        print(f"removed {len(stale)} stale .rpm no longer in the closure")

    fetched = len(resolved - on_disk)
    print(
        f"closure: {len(resolved)} rpms -- {fetched} to fetch, "
        f"{len(resolved) - fetched} already present"
    )

    for arch, packages in sorted(per_arch.items()):
        argv = dnf_argv(dnf_bin, arch, packages, repo_dir, args.releasever, setopts)
        print(f"\n== {arch}: {len(packages)} requested specs ==")
        print("  " + shlex.join(argv))
        subprocess.run(argv, check=True)

    total = len(list(repo_dir.glob("*.rpm")))
    print(f"\nfetched {total} rpms across {len(per_arch)} arches into {repo_dir}")
    print(f"next: {GENERATOR} build")


def cmd_build(args) -> None:
    try:
        import createrepo_c as cr
    except ImportError:
        sys.exit(
            "createrepo_c module not found. This is the build step, so it needs "
            "it (the antlir2 build does not). Get it from your distro's "
            "python3-createrepo_c, or `nix shell nixpkgs#createrepo_c`."
        )

    config = load_config(args.config) if args.config.exists() else {}

    # Resolve base_url: --local wins, then --base-url, then config.toml.
    if args.local:
        base_url = None
    else:
        base_url = args.base_url or config.get("base_url")
        if not base_url:
            sys.exit("no base_url: pass --base-url, set it in config.toml, or use --local")
        if not base_url.endswith("/"):
            sys.exit("base_url must end with '/'")

    repo_dir = args.repo_dir.resolve()
    xml_dir = repo_dir / "xml"
    xml_dir.mkdir(parents=True, exist_ok=True)

    rpms = sorted(repo_dir.glob("*.rpm"))
    if not rpms:
        sys.exit(f"no .rpm files found in {repo_dir}; run `{GENERATOR} download` first")

    packages, uploads = [], []
    for path in rpms:
        pkg = cr.package_from_rpm(str(path))
        epoch = int(pkg.epoch or 0)
        nevra = f"{pkg.name}-{epoch}:{pkg.version}-{pkg.release}.{pkg.arch}"

        rpm_sha, rpm_size = hash_and_size(path)
        # The href is what dnf sees through antlir's repo proxy. It is keyed by
        # pkgid, and is independent of the object-storage key.
        xml_path = xml_dir / f"{path.name}.json"
        make_xml(cr, pkg, f"Packages/{rpm_sha}/{nevra}.rpm", xml_path)
        xml_sha, xml_size = hash_and_size(xml_path)

        packages.append(
            {
                "filename": path.name,
                "nevra": nevra,
                "name": pkg.name,
                "epoch": epoch,
                "version": pkg.version,
                "release": pkg.release,
                "arch": pkg.arch,
                "rpm_sha256": rpm_sha,
                "rpm_size": rpm_size,
                "xml_sha256": xml_sha,
                "xml_size": xml_size,
            }
        )
        if not args.local:
            # `path` is repo-dir-relative and always posix-style: it is a
            # manifest key for the uploader, not a local filesystem path.
            uploads += [
                {
                    "key": rpm_sha,
                    "path": path.name,
                    "sha256": rpm_sha,
                    "size": rpm_size,
                    "content_type": "application/x-rpm",
                },
                {
                    "key": xml_sha,
                    "path": f"xml/{path.name}.json",
                    "sha256": xml_sha,
                    "size": xml_size,
                    "content_type": "application/json",
                },
            ]

    write_buck(repo_dir, args.rules_cell)

    # Provenance: the download step runs one dnf per arch, so this records the
    # whole set of commands, reconstructed from config.toml. `dnf` and a stable
    # relative destdir are used deliberately -- the real binary path and an
    # absolute --repo-dir would churn the lockfile across machines.
    commands = []
    if config:
        commands = [
            shlex.join(dnf_argv("dnf", arch, pkgs, REPO_REL, args.releasever))
            for arch, pkgs in sorted(arch_packages(config).items())
        ]

    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
    write_json(
        repo_dir / "packages.json",
        {
            "generated_by": {
                "generator": GENERATOR,
                "releasever": args.releasever,
                "commands": commands,
            },
            "imported_at": now,
            "repo_name": args.repo_name,
            # null => local mode (blobs are files on disk next to BUCK)
            "base_url": base_url,
            "packages": packages,
        },
    )

    upload_path = repo_dir / "upload.json"
    if args.local:
        upload_path.unlink(missing_ok=True)
    else:
        write_json(
            upload_path,
            {"generated_by": GENERATOR, "base_url": base_url, "objects": uploads},
        )

    mode = "local" if args.local else f"remote ({base_url})"
    print(f"wrote BUCK + packages.json for {len(packages)} packages [{mode}]")
    if not args.local:
        total = sum(o["size"] for o in uploads)
        print(f"wrote upload.json: {len(uploads)} objects, {total / 1e6:.1f} MB (pre-dedupe)")


def parse_args():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_common(p):
        p.add_argument(
            "--repo-dir",
            type=Path,
            default=HERE / "repo",
            help="snapshot repo dir holding the .rpm files; outputs are written here",
        )
        p.add_argument(
            "--config", type=Path, default=CONFIG, help="config.toml with arches + packages"
        )
        p.add_argument(
            "--releasever", default=RELEASEVER, help=f"Fedora release (default: {RELEASEVER})"
        )

    d = sub.add_parser("download", help="fetch the rpm closure via dnf, per config.toml")
    add_common(d)
    d.add_argument("--dnf", help="dnf binary (default: auto-detect dnf5, then dnf)")
    d.add_argument(
        "--cachedir",
        type=Path,
        default=HERE / ".dnf-cache",
        help="writable dir for dnf's metadata/log/state (keeps it out of /var, "
        "which needs root). Default: flavor/fedora44/.dnf-cache",
    )
    d.add_argument(
        "--no-clean",
        action="store_true",
        help="keep .rpm files that have fallen out of the resolved closure "
        "(default: prune them so repo/ matches config.toml exactly)",
    )
    d.set_defaults(func=cmd_download)

    b = sub.add_parser("build", help="generate packages.json + xml chunks + upload.json")
    add_common(b)
    b.add_argument(
        "--base-url",
        help="object-storage base URL, with trailing slash. Keys are appended as "
        "bare sha256 (content-addressed). Defaults to config.toml; omit with --local.",
    )
    b.add_argument(
        "--local",
        action="store_true",
        help="reference rpms/xml as local files instead of object storage",
    )
    b.add_argument("--repo-name", default="fedora44", help="dnf repo logical id")
    b.add_argument(
        "--rules-cell",
        default="antlir",
        help="buck2 cell holding the antlir dnf rules. The snapshot is its own "
        "cell, so the load() must be cell-qualified ('//' would resolve to the "
        "snapshot cell itself).",
    )
    b.set_defaults(func=cmd_build)

    return ap.parse_args()


def main() -> None:
    args = parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
