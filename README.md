# fedora44 aarch64 RPM snapshot

antlir2's dnf resolver is fully offline: it builds repodata from in-repo `rpm()`
targets rather than talking to a live mirror. This repo is that snapshot for the
fedora44 build appliance. It is a **separate git repo and a separate buck2 cell**
(`fedora44//`, declared in the main repo's `.buckconfig`) so its churn stays out
of the main history.

## What is tracked here

| file | tracked | what it is |
| --- | --- | --- |
| `packages.json` | yes | the whole snapshot: NVRAs, hashes, sizes, provenance |
| `BUCK` | yes | a fixed ~10-line stub; does not grow with the package count |
| `upload.json` | yes | exactly which objects to push to object storage |
| `*.rpm`, `xml/` | **no** | fetched from object storage, sha-verified by buck2 |

buck2 can `load()` a `.json` file directly, exposing the parsed document as the
symbol `value`:

```python
load(":packages.json", snapshot = "value")
```

so `packages.json` is simultaneously the build input *and* the human-readable
lockfile. There is no generated starlark restating the same facts, and `BUCK`
stays the same size whether the snapshot has 10 packages or 10,000. It is also
byte-identical between local and remote mode, since `base_url` lives in the JSON.

The blobs are deliberately *not* committed. They are ~16 KB/package of xml plus
the rpms themselves, and re-importing rewrites them, so committing would grow
this repo without bound.

Durability does not depend on Fedora mirrors: the `updates/` repo keeps only the
*latest* build of each package and prunes superseded ones within days-to-weeks,
so `dnf download` of a pinned NVRA rots quickly. Hosting the blobs ourselves
under content-addressed keys makes the snapshot reproducible indefinitely.

## Re-importing

The generator lives in the **main** repo at `flavor/fedora44/import_snapshot.py`
(it is source, not artifact). The arches to snapshot and the packages to fetch
are declared in `flavor/fedora44/config.toml`, so a re-import is config-driven
and needs no arguments. `build` needs `createrepo_c` — see the WHY in its
docstring — which is why the import runs in CI, not on the build host.

```bash
# 1. fetch the closure named in config.toml (one dnf run per arch, --forcearch)
./flavor/fedora44/import_snapshot.py download

# 2. generate packages.json + xml chunks + upload.json (base_url from config.toml)
./flavor/fedora44/import_snapshot.py build

# 3. upload the objects named in upload.json (needs write creds -> do this in
#    CI, never in a build sandbox). Keys are content-addressed, so re-imports
#    only push what is new: HEAD first and skip what exists.
./flavor/fedora44/upload_snapshot.py --bucket ... --endpoint-url ...
```

Add packages to `config.toml` as image features need them (`[packages].include`
for every arch, `[packages.<arch>].include` for arch-specific ones), then re-run
steps 1–2. The exact per-arch `dnf` commands are recorded under `generated_by`
in `packages.json`, so the provenance travels with the lockfile.

Moving bucket/CDN/domain is a one-line edit: `base_url` lives in `config.toml`
and is copied into the top of `packages.json`.

## Building without the bucket

`import_snapshot.py build --local` emits local `rpm=`/`xml=` references instead
of URLs, for offline or bootstrap builds. You need the `.rpm` files present (run
`download` first); the xml chunks are regenerated for you. Handy before the first
upload exists.
