#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""Bootstrap an antlir2 *build appliance* (BA) rootfs tarball from scratch.

A build appliance is nothing magic: it is a distro root filesystem containing
the userspace tools that the antlir2 compiler shells into when it builds/receives
images -- dnf (the *dnf4* python API), rpm, coreutils, `mount`, bash -- plus the
`/__antlir2__/...` scaffold directories antlir2 expects. antlir2 enters this
rootfs as an isolated environment (systemd-nspawn, or `unshare` in rootless mode)
and runs those tools from *inside* it, so image builds don't depend on whatever
happens to be installed on the developer's host.

Meta builds these once in CI and uploads them to S3; the OSS `flavor/*/BUCK`
files then `http_archive` the tarball. There is no committed recipe for making
the *first* BA from nothing -- this script is that recipe. It exists mainly to
bootstrap a BA for a distro/arch Meta doesn't publish (e.g. an aarch64 Fedora
BA), using only the host's `dnf`.

Chicken-and-egg note: antlir's own `flavor/<name>:build-appliance` layer is built
*using* a prebuilt BA (`build_appliance = ":build-appliance.prebuilt"`). This
script produces exactly that seed `.prebuilt` tarball so the buck build can then
refine it into the canonical BA.

Example (Fedora 44, aarch64, on a Fedora host):

    sudo ./build_appliance.py \\
        --releasever 44 \\
        --distro fedora \\
        --out /tmp/fedora44_ba.tar.zst

Run as root (or with a userns that maps root) so rpm scriptlets and file
ownership come out right. `--dnf-setopt sslcacert=/path/to/ca.crt` is forwarded
verbatim to dnf if you sit behind a TLS-terminating proxy.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# The tools antlir2 shells into from *inside* the BA. Mirrors the package set in
# antlir/antlir2/package_managers/dnf/build_appliance/BUCK (feature.rpms_install)
# plus the handful of coreutils/bash deps needed for a self-contained rootfs.
#
# IMPORTANT (Fedora): antlir2's rpm driver (antlir2_dnf_base.py) does
# `import dnf; import hawkey` -- that is the *dnf4* python API. Fedora's default
# `dnf` is dnf5, whose bindings are `libdnf5` and are a different API the driver
# does NOT speak. So we explicitly pull the dnf4 compat stack (python3-dnf &c).
DEFAULT_PACKAGES = [
    # dnf4 + its python bindings (the API antlir2_dnf_base.py imports)
    "python3-dnf",
    "python3-hawkey",
    "python3-libdnf",
    "python3-rpm",
    "dnf",
    # base userspace the compiler relies on being present in the BA
    "rpm",
    "coreutils",
    "util-linux",  # provides /bin/mount, referenced by dnf/build_appliance:features
    "bash",
    "glibc-minimal-langpack",
    "ca-certificates",
]

# Scaffold dirs antlir2 expects to exist in the BA. Mirrors
# antlir/antlir2/build_appliance/BUCK + dnf/build_appliance/BUCK
# (ensure_dirs_exist). antlir's `build_appliance:features` layer normally
# installs these when refining a BA, but the *seed* needs them too so that dnf
# can actually run inside it during that first refine build.
SCAFFOLD_DIRS = [
    "__antlir2__",
    "__antlir2__/root",
    "__antlir2__/build_appliance",
    "__antlir2__/working_directory",
    "__antlir2__/out",
    "__antlir2__/dnf",
    "__antlir2__/dnf/cache",
    "__antlir2__/dnf/repos",
]

# Path (inside the BA) of the dnf config antlir2_dnf_base.py reads. Kept in sync
# with antlir/antlir2/package_managers/dnf/build_appliance/dnf.antlir.conf.
ANTLIR_DNF_CONF_PATH = "__antlir2__/dnf/dnf.conf"
ANTLIR_DNF_CONF = """\
[main]
disableplugin=*
ignorearch=True
cachedir=/__antlir2__/dnf/cache
best=False
install_weak_deps=False
gpgcheck=True
localpkg_gpgcheck=True
assumeyes=True
reposdir=
varsdir=
protected_packages=
protect_running_kernel=False
"""


def run(cmd: list[str], **kw) -> None:
    print("+ " + " ".join(str(c) for c in cmd), file=sys.stderr)
    subprocess.run(cmd, check=True, **kw)


def _mount(*args: str) -> bool:
    """Best-effort mount; returns False instead of raising (some mounts are
    optional depending on how much privilege we have)."""
    try:
        subprocess.run(["mount", *args], check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def prep_installroot(root: Path, dnf_state: Path) -> None:
    """Prepare an installroot so rpm scriptlets/unpack succeed. Learned the hard
    way; all of this matters when running rootless in a user namespace, and none
    of it hurts when running as real root:

    - rpm scriptlets chroot into the installroot and write temp files to
      /var/tmp; if it's missing (or a rollback removes it) they fail with
      "No such file or directory". tmpfs over tmp/var/tmp keeps them present.
    - do NOT rbind /dev,/proc,/sys wholesale -- the `filesystem` package chowns
      those dirs during unpack and fails ("Device or resource busy") if they are
      mountpoints. Bind only the individual device *files* scriptlets use.
    - dnf5 writes repo state (the `countme` cookie) under /var/lib/dnf, which is
      not writable in a single-uid userns; redirect it to a writable dir.
    """
    for d in ("tmp", "var/tmp", "proc", "sys", "dev", "run", "etc"):
        (root / d).mkdir(parents=True, exist_ok=True)
    _mount("-t", "tmpfs", "tmpfs", str(root / "tmp"))
    _mount("-t", "tmpfs", "tmpfs", str(root / "var/tmp"))
    os.chmod(root / "tmp", 0o1777)
    os.chmod(root / "var/tmp", 0o1777)
    _mount("-t", "proc", "proc", str(root / "proc"))
    for dev in ("null", "zero", "full", "random", "urandom"):
        node = root / "dev" / dev
        node.touch(exist_ok=True)
        _mount("--bind", f"/dev/{dev}", str(node))
    # writable dnf state (host /var/lib/dnf is read-only under a userns)
    (dnf_state / "lib").mkdir(parents=True, exist_ok=True)
    _mount("--bind", str(dnf_state / "lib"), "/var/lib/dnf")


def teardown_mounts(root: Path) -> None:
    """Unmount everything prep_installroot set up (in reverse) before packaging,
    so the tarball captures clean empty dirs -- not a live /proc or the host's
    /dev/null -- and remove the device-node placeholders (antlir2 provides a real
    /dev at runtime)."""
    for dev in ("null", "zero", "full", "random", "urandom"):
        node = root / "dev" / dev
        subprocess.run(["umount", str(node)], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        node.unlink(missing_ok=True)
    for m in ("proc", "var/tmp", "tmp"):
        subprocess.run(["umount", str(root / m)], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def dnf_install(
    *,
    root: Path,
    releasever: str,
    forcearch: str | None,
    packages: list[str],
    repofrompath: list[str],
    setopts: list[str],
    use_host_config: bool,
    cachedir: Path,
) -> None:
    cmd = [
        "dnf",
        "-y",
        f"--installroot={root}",
        f"--releasever={releasever}",
        # antlir images are explicit about their contents; don't drag in
        # recommends (matches install_weak_deps=False in the antlir dnf.conf).
        "--setopt=install_weak_deps=False",
        # keep dnf's cache out of the host tree (writable when rootless)
        f"--setopt=cachedir={cachedir}",
    ]
    if use_host_config:
        # Reuse the host's /etc/yum.repos.d + imported GPG keys. Convenient when
        # bootstrapping a BA for the same distro the host runs.
        cmd.append("--use-host-config")
    if forcearch:
        cmd.append(f"--forcearch={forcearch}")
    for rfp in repofrompath:
        # "id,baseurl" -- lets you point at a specific mirror/snapshot without a
        # .repo file, and disable everything else for reproducibility.
        cmd.append(f"--repofrompath={rfp}")
    if repofrompath:
        cmd += ["--disablerepo=*"] + [
            f"--enablerepo={rfp.split(',', 1)[0]}" for rfp in repofrompath
        ]
    for opt in setopts:
        cmd.append(f"--setopt={opt}")
    cmd += ["install"] + packages
    run(cmd)


def write_scaffold(root: Path) -> None:
    for d in SCAFFOLD_DIRS:
        (root / d).mkdir(parents=True, exist_ok=True)
    conf = root / ANTLIR_DNF_CONF_PATH
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(ANTLIR_DNF_CONF)
    # antlir2_dnf_base.py reads /etc/dnf/dnf.conf; point it at the antlir conf,
    # matching dnf/build_appliance:features (ensure_file_symlink).
    etc_dnf = root / "etc/dnf"
    etc_dnf.mkdir(parents=True, exist_ok=True)
    link = etc_dnf / "dnf.conf"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to("/__antlir2__/dnf/dnf.conf")
    # empty versionlock, as installed by dnf/build_appliance:features
    (root / "__antlir2__/dnf/versionlock.json").write_text("{}\n")


def make_tarball(root: Path, out: Path, zstd_level: int) -> None:
    # Stream tar -> zstd so we never materialize the uncompressed tar. Ownership
    # is preserved (we run as root); numeric-owner keeps it host-uid-independent.
    out.parent.mkdir(parents=True, exist_ok=True)
    tar = subprocess.Popen(
        [
            "tar", "--numeric-owner", "--xattrs", "--acls",
            # belt-and-suspenders: never descend into pseudo-fs contents even if
            # a mount survived teardown (keeps the dir entries themselves).
            "--exclude=./proc/*", "--exclude=./sys/*", "--exclude=./dev/*",
            "--exclude=./tmp/*", "--exclude=./var/tmp/*",
            "-cf", "-", "-C", str(root), ".",
        ],
        stdout=subprocess.PIPE,
    )
    with open(out, "wb") as f:
        zstd = subprocess.Popen(
            ["zstd", f"-{zstd_level}", "-T0", "-c"],
            stdin=tar.stdout,
            stdout=f,
        )
    tar.stdout.close()  # let tar receive SIGPIPE if zstd dies
    if zstd.wait() != 0:
        raise SystemExit("zstd failed")
    if tar.wait() != 0:
        raise SystemExit("tar failed")


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--out", required=True, type=Path, help="output .tar.zst path")
    p.add_argument("--releasever", required=True, help="e.g. 44 (fedora) / 9 (centos)")
    p.add_argument(
        "--distro",
        default="fedora",
        help="informational label recorded in the manifest (default: fedora)",
    )
    p.add_argument(
        "--forcearch",
        default=None,
        help="target arch if cross-building (e.g. aarch64); omit to use host arch",
    )
    p.add_argument(
        "--package",
        action="append",
        default=[],
        dest="extra_packages",
        help="additional package to include (repeatable)",
    )
    p.add_argument(
        "--repofrompath",
        action="append",
        default=[],
        help="'id,baseurl' repo to use exclusively (repeatable); "
        "if omitted, uses --use-host-config",
    )
    p.add_argument(
        "--dnf-setopt",
        action="append",
        default=[],
        dest="setopts",
        help="raw dnf --setopt=KEY=VAL, forwarded verbatim (repeatable). "
        "e.g. sslcacert=/etc/ssl/certs/matchlock-ca.crt behind a TLS proxy",
    )
    p.add_argument("--zstd-level", type=int, default=19)
    p.add_argument(
        "--keep-root",
        action="store_true",
        help="don't delete the staged installroot after packaging (for inspection)",
    )
    args = p.parse_args()

    for tool in ("dnf", "tar", "zstd"):
        if shutil.which(tool) is None:
            print(f"error: required tool '{tool}' not found on PATH", file=sys.stderr)
            return 1

    if os.geteuid() != 0:
        print(
            "warning: not running as root; rpm scriptlets and file ownership in "
            "the BA will likely be wrong. Re-run under sudo (or a root userns).",
            file=sys.stderr,
        )

    packages = DEFAULT_PACKAGES + args.extra_packages
    use_host_config = not args.repofrompath

    staging = Path(tempfile.mkdtemp(prefix="antlir2-ba-"))
    root = staging / "root"
    root.mkdir()
    dnf_state = staging / "dnf-state"
    try:
        prep_installroot(root, dnf_state)
        dnf_install(
            root=root,
            releasever=args.releasever,
            forcearch=args.forcearch,
            packages=packages,
            repofrompath=args.repofrompath,
            setopts=args.setopts,
            use_host_config=use_host_config,
            cachedir=dnf_state / "cache",
        )
        write_scaffold(root)
        teardown_mounts(root)
        make_tarball(root, args.out, args.zstd_level)
    finally:
        if args.keep_root:
            print(f"staged installroot kept at: {root}", file=sys.stderr)
        else:
            shutil.rmtree(staging, ignore_errors=True)

    size = args.out.stat().st_size
    sha256 = subprocess.run(
        ["sha256sum", str(args.out)], capture_output=True, text=True, check=True
    ).stdout.split()[0]
    print(f"\nbuild appliance written: {args.out} ({size / 1e6:.1f} MB)")
    print(f"sha256: {sha256}")
    print(
        "\nWire it into flavor/<name>/BUCK by pointing the BA source at this file "
        "and updating the sha256 (see flavor/bootstrap/README.md)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
