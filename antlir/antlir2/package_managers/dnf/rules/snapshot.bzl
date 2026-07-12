# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""
`rpm_snapshot()` -- declare a whole offline RPM snapshot from a JSON manifest.

antlir2's dnf model is fully offline: the resolver reads repodata built from
in-repo `rpm()` targets. Writing those out longhand is enormously verbose (a
dozen lines per package, times thousands of packages), so instead the importer
emits a single JSON manifest and the BUCK file is a fixed stub that does:

    load("@antlir//antlir/antlir2/package_managers/dnf/rules:snapshot.bzl", "rpm_snapshot")
    load(":packages.json", snapshot = "value")

    rpm_snapshot(snapshot = snapshot)

buck2 can `load()` a .json file directly, exposing the parsed document as the
symbol `value`. That keeps the BUCK file the same handful of lines whether the
snapshot has 10 packages or 10,000, and means the manifest is *data* -- one
committed artifact that is both the buck2 input and the human-readable lockfile,
rather than the same facts duplicated into generated starlark.

Two modes, chosen by whether the manifest sets `base_url`:

  * remote (base_url set) -- the .rpm blobs and their pre-generated xml chunks
    live in object storage under content-addressed keys (the sha256). Nothing
    heavy is committed; buck2 downloads and sha-verifies each one. Normal mode.
  * local (base_url null) -- everything is a file on disk next to the BUCK file.
    For hermetic/offline builds and for bootstrapping a snapshot that has not
    been uploaded yet.

Why the xml is supplied rather than computed: antlir's `makechunk` needs
`//third-party/python:createrepo-c`, a prebuilt wheel (cp312, x86_64 + aarch64).
Generating the chunks at import time sidesteps that entirely, so the build
never needs createrepo_c.
"""

load("//antlir/bzl:build_defs.bzl", "http_file")
load(":repo.bzl", "repo", "repo_set")
load(":rpm.bzl", "rpm")

def rpm_snapshot(
        *,
        snapshot,
        repo_set_name: str = "repos",
        visibility = None):
    """Expand a parsed snapshot manifest into a complete dnf repo.

    `snapshot` is the JSON document loaded via `load(":packages.json", "value")`.
    """
    repo_name = snapshot["repo_name"]

    # Absent/null base_url means the blobs are local files, not object storage.
    base_url = snapshot.get("base_url")

    # Provenance only (RepoInfo.base_url); dnf never fetches from it, since the
    # resolver is offline and sees the `location href` baked into each xml chunk.
    repo_base_url = snapshot.get("repo_base_url") or ""

    package_visibility = ["//{}:".format(native.package_name())]
    rpm_targets = []

    for pkg in snapshot["packages"]:
        fn = pkg["filename"]

        if base_url:
            # Content-addressed: the key IS the sha256, so re-importing an
            # unchanged package is a no-op and identical blobs dedupe.
            xml_target = fn + ".xml"
            http_file(
                name = xml_target,
                urls = [base_url + pkg["xml_sha256"]],
                sha256 = pkg["xml_sha256"],
                # Supplying the length lets buck2 form the full CAS digest
                # (hash:size) without a HEAD to discover Content-Length.
                size_bytes = pkg["xml_size"],
                visibility = package_visibility,
            )
            rpm_kwargs = {
                "size_bytes": pkg["rpm_size"],
                "url": base_url + pkg["rpm_sha256"],
                "xml": ":" + xml_target,
            }
        else:
            rpm_kwargs = {
                "rpm": fn,
                "xml": "xml/{}.json".format(fn),
            }

        rpm(
            name = fn,
            arch = pkg["arch"],
            epoch = pkg["epoch"],
            release = pkg["release"],
            rpm_name = pkg["name"],
            sha256 = pkg["rpm_sha256"],
            version = pkg["version"],
            visibility = package_visibility,
            **rpm_kwargs
        )
        rpm_targets.append(":" + fn)

    repo(
        name = repo_name,
        logical_id = repo_name,
        base_url = repo_base_url,
        rpms = rpm_targets,
        visibility = visibility or ["PUBLIC"],
    )

    repo_set(
        name = repo_set_name,
        repos = [":" + repo_name],
        visibility = visibility or ["PUBLIC"],
    )
