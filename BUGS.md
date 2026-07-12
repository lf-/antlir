# Antlir OSS build — broken parts

Tracking genuine OSS-build breakages (things wrong in the open-source release,
independent of the giant `generated` external cell for the CentOS RPM repos).

Legend: ✅ fixed in-tree · ⚠️ structural / needs decision · 🔎 investigating

## TL;DR

The two bugs that actually make the OSS build unusable for *any* image are
**#7** (feature-plugin libs land in `<unspecified_exec>`) and **#9** (the host
execution platform is missing the `may_run_local` marker). Both are fixed here,
and with them image-layer analysis resolves execution platforms and proceeds.
Also fixed: #1 (`package_style`), #2 (deb `export_file`), #4 (centos9/10
build-appliance facebook paths), #6 (`install` `fbcode//` exec_deps). Still open
and **not** pure-code bugs: #3 (the whole `antlir/distro/` tree is un-exported),
#5 (the default flavor makes the multi-GB `generated` RPM snapshot a hard
analysis dep + blows the inotify watch limit), #8 (the `nonempty` rust crate is
missing from the vendored `third-party/rust`).

Files changed by the fixes: `antlir/bzl/build_defs_impl.bzl`,
`antlir/antlir2/package_managers/deb/BUCK`, `antlir/antlir2/os/oses.bzl`,
`antlir/antlir2/features/install/install.bzl`,
`antlir/antlir2/features/defs.bzl`, `platforms/BUCK`, `platforms/defs.bzl` (new).

Reproduction used throughout (a generated-free image layer):
```python
# some_dir/BUCK
image.layer(
    name = "dirs-layer",
    force_flavor = "//antlir/antlir2/flavor:none",          # empty repo set, no `generated`
    build_appliance = "//flavor/centos9:build-appliance.prebuilt",  # tarball BA, no default flavor
    features = [feature.ensure_dirs_exist(dirs = "/foo")],
)
```
`buck2 audit execution-platform-resolution //some_dir:dirs-layer` exits 0 after
the fixes; the build then stops only at the vendoring/toolchain gaps (#8, no
`rustc`, S3-blocked build appliance).

---

## 1. ✅ `python_binary` forwards binary-only `package_style` to `python_library`

- **File:** `antlir/bzl/build_defs_impl.bzl` — `_python_binary`
- **Symptom:** `Found 'package_style' extra named parameter(s) for call to python_library`
  when evaluating e.g. `antlir/antlir2/features/install/tests/BUCK` (`true-py`).
- **Cause:** `_python_binary(**kwargs)` forwards *all* kwargs (including the
  binary-only `package_style`) to the underlying `python_library`, which the OSS
  prelude rejects (unknown attr).
- **Fix:** pop `package_style` out of the library kwargs and route it to the
  `python_binary` rule instead.

## 2. ✅ `deb` package manager loads `export_file` from Meta-internal cell

- **File:** `antlir/antlir2/package_managers/deb/BUCK:1`
- **Symptom:** `File not found: none//build_defs/export_files.bzl`
  (`@fbcode_macros//` is aliased to the empty `none` cell in OSS).
- **Cause:** load of `@fbcode_macros//build_defs:export_files.bzl` was not
  shimmed for OSS.
- **Fix:** load `export_file` from the antlir shim
  `//antlir/bzl:build_defs.bzl` instead.

## 3. ⚠️ `antlir/distro/...` directory is entirely missing from the OSS release

- **Referenced by real code (not oss-disabled):**
  - `antlir/antlir2/bzl/feature/feature.bzl:290` — `antlir//antlir/distro/transition:to-current-distro-platform`
  - `antlir/antlir2/features/install/install.bzl:142-147` — `antlir//antlir/distro/rpm:find-requires{,.py}`, `.../toolchain/python:pex-deps`, `xar-deps`
  - `antlir/bzl/build_defs_impl.bzl:43` — third-party cxx deps resolve to `//antlir/distro/deps/{project}:{rule}`
  - `ci/test_target_graph.bxl:10` — explicitly subtracts `//antlir/distro/...`
- **Symptom:** `ci/test_target_graph.bxl` fails: `Error listing dir 'antlir/distro' … No such file or directory`. Any `install`/`clone` of a buck-built binary that hits `distro_platform_deps` will fail analysis.
- **Confirmed:** directory does not exist upstream either
  (`facebookincubator/antlir` main, verified via GitHub API 404).
- **Impact:** installing **buck-built binaries** into images and the
  distro/target-platform transition are broken in OSS. Installing a plain
  **RPM** does not go through this path, so it is not blocking the RPM goal.
- **Decision needed:** either Meta needs to export `antlir/distro`, or we
  stub/generate the pieces we need. `ci/test_target_graph.bxl` also can't run
  as-written until the dir exists (buck2 errors on the missing recursive spec).

## 4. ✅ `oses.bzl` hardcodes Meta-only build-appliance paths for centos9/centos10

- **File:** `antlir/antlir2/os/oses.bzl` (centos9 & centos10 `_new_os`)
- **Symptom:** building **any** layer with the default OS hangs on
  `Waiting on antlir//antlir/antlir2/facebook/flavor/centos9 -- loading package file tree`
  then fails — `antlir/antlir2/facebook/...` does not exist in OSS.
- **Cause:** unlike the generic `_new_os` default (which uses
  `internal_external(fb=…, oss="antlir//flavor/{name}:build-appliance")`),
  the centos9/centos10 entries **override** `build_appliance` with a raw
  `select({...})` whose DEFAULT branch and `:corp` select-key both point at
  `antlir//antlir/antlir2/facebook/...` unconditionally — leaking Meta-internal
  targets into the OSS build.
- **Fix:** wrap the select in `internal_external(fb = select({...}), oss =
  "//flavor/centosN:build-appliance")`.

## 5. ⚠️ Default flavor makes the entire `generated` RPM snapshot a hard dep of every image

- **Files:** `flavor/centos{9,10}/BUCK` (`default_dnf_repo_set = "generated//snapshot/rpm/centosN:repos"`);
  consumed via `antlir/antlir2/bzl/flavor/defs.bzl` (`default_dnf_repo_set` is a
  non-optional `attrs.dep`).
- **Symptom:** analyzing the centos9 flavor — which every default-OS layer does —
  fetches the `generated` git external cell (hundreds of thousands of files).
- **Two hard consequences observed in this sandbox:**
  1. The fetch is enormous (multi-GB) and slow.
  2. buck2's `notify` file watcher (the non-watchman fallback the CI itself
     configures) then tries to watch every file in the cell and dies with
     **`OS file watch limit reached`** — the cell (~hundreds of k files) blows
     past `fs.inotify.max_user_watches`, which is unprivileged-unraisable.
- **Impact:** you cannot build even a trivial default-OS layer in an environment
  with a modest inotify limit without first fetching the whole CentOS snapshot.
  The generated-free path requires overriding `flavor`/`build_appliance` and
  using a repo set that doesn't reference `generated//` (e.g. `flavor:none`'s
  `empty-repo-set` + a local repo via `dnf_additional_repos`).

## 6. ✅ `install` feature declares `fbcode//` exec_deps (breaks every install in OSS)

- **File:** `antlir/antlir2/features/install/install.bzl` (`exec_deps`)
- **Symptom:** analysis of any `install` feature fails resolving
  `fbcode//antlir/antlir2/tools:debuginfo-splitter` /
  `fbcode//python/runtime/tools:recursive_mac_signer` — `fbcode` is aliased to
  the empty `none` cell, so this becomes `read_dir(/workspace/none)` → ENOENT.
- **Cause:** two exec_deps were left with the internal `fbcode//` cell prefix.
  `debuginfo-splitter` actually exists in-tree; `recursive_mac_signer` is
  Meta-only and is only consumed when building for macOS
  (`install.bzl` guards it with `expect(..., "when building for macOS")`, and
  the rule attr is `attrs.option(attrs.exec_dep(), default = None)`).
- **Fix:** point `_debuginfo_splitter` at `antlir//antlir/antlir2/tools:debuginfo-splitter`
  (the `antlir` cell root == fbcode's `antlir` subtree, so this label is valid
  both internally and in OSS); make `_mac_signer` `internal_external(fb=…, oss=None)`
  and only add it to the dict when set.
- **Note:** this was latent behind bug #7 (which fails first), but is a real
  independent breakage.

## 7. ✅ CRITICAL: feature-plugin libs land in `<unspecified_exec>` → every image layer fails analysis

- **File:** `antlir/antlir2/features/defs.bzl` — `_feature_plugin` rule.
- **Symptom (before fix):** analysis of **any** image layer that uses **any**
  feature (i.e. essentially all of them) fails:
  ```
  -> …/features/ensure_dir_exists:ensure_dir_exists (<unspecified_exec>)
  1: Error resolving configuration deps of `…:ensure_dir_exists.linked (<unspecified_exec>)`
  2: Error getting configuration node of `prelude//:none` within the `<unspecified_exec>` configuration
  3: Attempted to access the configuration data for the "unspecified_exec" platform.
  ```
- **Reproducible** with a bare `feature.ensure_dirs_exist` layer, **zero**
  `generated`/S3/RPM deps, and identically across public buck2 `2026-06-15`,
  `2026-07-01` (the pinned one), and `latest` — so it is **not** a version
  regression, but a property of the OSS setup + public buck2 binary.
- **Cause:** the layer references each feature plugin via `attrs.plugin_dep`, so
  the plugin (`feature_plugin` rule) is configured in the *target* config with
  **no execution platform** (`<unspecified_exec>`). `_feature_plugin.lib` was an
  `attrs.dep` → the `<name>.linked` **rust_library** it points at inherits that
  exec-less config, and a rust_library cannot be configured without an exec
  platform (its rustc toolchain, `prelude//:none` fat-platform marker, etc.
  all need exec config data). Meta's internal buck2 evidently tolerates this;
  the public buck2 release does not.
- **Fix:** make `_feature_plugin.lib` an **`attrs.exec_dep`**. This is also
  semantically correct: the plugin `.so` is `dlopen`'d by the host-side antlir2
  compiler, so it belongs on the **exec/host** platform, not the image's target
  platform. With this change the `unspecified_exec`/`prelude//:none` failure
  disappears and layer analysis proceeds.
- **Impact:** this is *the* blocker that makes the OSS build unusable for images
  out of the box — it fails before you ever reach flavors, the build appliance,
  or RPMs.

## 8. ⚠️ Missing vendored rust crate `nonempty` in `third-party/rust`

- **Symptom:** building the antlir2 compiler (`//antlir/antlir2/antlir2:antlir2`,
  an exec_dep of every layer) fails: `Unknown target 'nonempty' from package
  'antlir//third-party/rust'`.
- **Cause:** `antlir/antlir2/antlir2_vl/BUCK` and `antlir/antlir2/cad_stack/BUCK`
  depend on the `nonempty` crate (`deps = [… "nonempty" …]`), but it is not
  present in the reindeer-generated `third-party/rust/BUCK`. `cad_stack` is in
  the antlir2 compiler's dep graph, so this blocks building the compiler.
- **Fix (out of my scope here):** re-run reindeer / add `nonempty` (and its
  transitive deps) to the vendored `third-party/rust` set. This is part of the
  same third-party-vendoring gap family as the `generated` cell. (Couldn't do it
  in this sandbox: `static.crates.io` is also blocked by the proxy, so the crate
  source can't be fetched, and there's no `rustc` to compile it anyway.)
- This is the first blocker reached **after** #7 and #9 are fixed — i.e. exec
  resolution now works and the antlir2 compiler is the next thing to configure,
  and it fails purely because this crate is absent from the vendored set.

## 9. ✅ CRITICAL: execution platform `antlir//platforms:host` is missing the `may_run_local` marker → every layer's exec_deps land in `<unspecified_exec>`

- **Symptom (after fixing #7):** analysis of any image layer still failed, now on
  the layer's *own* exec_deps rather than the plugins:
  ```
  -> antlir//antlir/antlir2/antlir2:antlir2 (<unspecified_exec>)          # the compiler
  -> antlir//flavor/centos9:build-appliance (<unspecified_exec>)          # the BA
  … Error getting configuration node of `prelude//:none` / `prelude//os:macos`
    within the `<unspecified_exec>` configuration
  ```
  Every `attrs.exec_dep` on `layer_rule` (`antlir2`, `build_appliance`,
  `_analyze_feature`, …) was affected — the layer never resolved an exec platform.
- **Root cause:** antlir's own rules require a local-capable exec platform:
  - `antlir/antlir2/bzl/flavor/defs.bzl:60` → `exec_compatible_with = ["prelude//platforms:may_run_local"]`
  - `antlir/antlir2/bzl/platform.bzl:15` (`local_only_exec = True`) → same.

  But `platforms/BUCK` built the sole execution platform with the prelude
  `execution_platform` rule, which only unions the **cpu** and **os**
  ConfigurationInfos — it never adds the `may_run_local` (`prelude//platforms:runs_local`)
  constraint value. So the host exec platform did **not** satisfy
  `exec_compatible_with = [may_run_local]`; no exec platform was compatible; the
  layer resolved none; and all its exec_deps fell into `<unspecified_exec>`
  (where any `select` — surfacing as `prelude//:none`/`prelude//os:macos` — dies).
  Targets built *directly* (e.g. the plugin, the compiler) worked because they
  don't carry that `exec_compatible_with`, so the marker-less host platform was
  fine for them — which is exactly why it was easy to miss.
- **Fix:** add a small `platforms/defs.bzl` with an `execution_platform` rule
  that unions an arbitrary list of ConfigurationInfos (preserving the prelude's
  `exec_marker_constraint`), and in `platforms/BUCK` register `host` with
  `configurations = [host cpu, host os, prelude//platforms:may_run_local,
  prelude//platforms:may_run_remote]`. After this, exec-platform resolution
  succeeds (`buck2 audit execution-platform-resolution //…:<layer>` exits 0) and
  layer analysis proceeds to real build actions.
- **Impact:** #7 + #9 together are *the* reason the OSS build is unusable out of
  the box — with both fixed, image-layer analysis completes; the only things left
  are the environment/vendoring gaps below (rustc, S3 build appliance, the
  `generated` snapshot, the `nonempty` crate).

---

## Environment / sandbox blockers (not antlir bugs — for the harness owner)

These are properties of *this* sandbox, not the antlir code. They independently
prevent actually building/installing an RPM here, on top of #9:

1. **No `dotslash`** — the `./buck2` wrapper is a dotslash manifest and won't run.
   Worked around by parsing the manifest and downloading the buck2 binary directly.
2. **No `zstd`/`unzstd` binary, no root, no `pip`** — couldn't decompress the
   `.zst` buck2 build. Worked around via Node 22's `zlib.zstdDecompressSync`.
3. **No `watchman`** — buck2 needs a file watcher; used `file_watcher=notify`
   (as the CI does). Appended to `.buckconfig`.
4. **`fs.inotify.max_user_watches` = 32281, unraisable without root** — the
   `generated` cell alone has >42k files, so the `notify` watcher dies with
   `OS file watch limit reached` the moment `generated` is fetched (see #5).
5. **No `rustc`/`cargo`** — every image build compiles the antlir2 rust feature
   plugins + compiler; there is no rust toolchain here (the CI installs nightly
   via `dtolnay/rust-toolchain`).
6. **S3 egress is blocked** — the MITM proxy allows GitHub/ghcr but returns an
   empty reply for `*.s3.*.amazonaws.com` over HTTPS (and S3 denies plain HTTP by
   policy). The build appliance tarball and the RPM blobs are all S3-hosted, so
   they can't be fetched. Docker Hub and quay.io are also blocked; only
   github.com and ghcr.io are reachable.
7. **aarch64 host** — OSS antlir declares **x86_64 only** (`oses.bzl` oss arch
   list), the build appliance tarball and `generated` RPMs are x86_64, so even
   with network there's an arch mismatch (would need qemu-user emulation).

To actually reach "install an RPM" you'd want: an x86_64 runner, S3 (or a mirror)
allowlisted, a rust toolchain installed, and enough inotify headroom (or watchman)
— i.e. basically the CI's environment — plus a fix for #9.

---

## Notes on the `generated` external cell (out of scope, per request)

`generated//snapshot/rpm/{centos9,centos10}:repos` (referenced from
`flavor/centos{9,10}/BUCK` and `antlir/antlir2/features/rpm/tests/BUCK`) is the
huge auto-generated set of BUCK files mirroring the CentOS RPM repos. Avoided.
There is a **local, self-contained test RPM repo** at
`antlir/antlir2/features/rpm/tests/repo` that can install RPMs without the
generated cell — that's the intended path to the "install an RPM" goal.
