# Build-appliance bootstrap

An antlir2 **build appliance** (BA) is a distro root filesystem containing the
userspace tools the antlir2 compiler shells into when it builds/receives images
(`dnf` + its *dnf4* python bindings, `rpm`, `coreutils`, `mount`, `bash`) plus
the `/__antlir2__/…` scaffold dirs. antlir2 enters it as an isolated environment
(nspawn, or `unshare` rootless) and runs those tools from inside it, so image
builds don't depend on the developer's host.

Meta builds these in CI and publishes them on S3; the OSS `flavor/*/BUCK` files
then `http_archive` the tarball. That S3 bucket is gone and only ships x86_64,
so for anything else (e.g. an **aarch64 Fedora** BA) you bootstrap your own with
`build_appliance.py`, which needs nothing but the host's `dnf`.

The chicken-and-egg: antlir's own `flavor/<name>:build-appliance` layer is built
*using* a prebuilt BA. This script produces that seed `.prebuilt` tarball.

## Usage

```bash
# Fedora 44, host arch, using the host's repos:
sudo ./build_appliance.py --releasever 44 --distro fedora --out ./fedora44_ba.tar.zst
```

Run as **root**, or rootless in a user namespace **with a subuid/subgid range
mapped** (podman/mock style) — a single-uid `unshare -r` is *not* enough, because
the `filesystem` rpm chowns dirs to non-root gids (`mail`, `utmp`, …) during
unpack. The script itself performs the tmpfs/dev/state mount prep an installroot
needs; you just have to give it the privilege to chown.

Rootless example (requires setuid `newuidmap`/`newgidmap` + `/etc/subuid`):

```bash
podman unshare ./build_appliance.py --releasever 44 --out ./fedora44_ba.tar.zst
```

### Behind a TLS-terminating proxy

If dnf can't verify the mirror cert (`self-signed certificate in certificate
chain`), the proxy CA isn't in the system trust bundle dnf reads
(`/etc/pki/tls/certs/ca-bundle.crt`). Either register it once —

```bash
cp <proxy-ca>.crt /etc/pki/ca-trust/source/anchors/ && update-ca-trust
```

— or point dnf at it per-run:

```bash
./build_appliance.py … --dnf-setopt sslcacert=/etc/ssl/certs/<proxy-ca>.crt
```

## Wiring the tarball into a flavor

`build_appliance.py` prints the output's `sha256`. Point the flavor's BA source
at the file (drop the dead-S3 `http_archive` for a local `export_file`/`http_file`)
and update the hash — see `flavor/fedora44/BUCK`. The tarball itself is
`.gitignore`d; host the binary wherever you keep large artifacts.

## dnf4 vs dnf5 (Fedora gotcha)

antlir2's rpm driver (`antlir2_dnf_base.py`) does `import dnf; import hawkey` —
the **dnf4** python API. Fedora's default `dnf` is dnf5 (`libdnf5`, a different
API). The default package set pulls the dnf4 compat stack (`python3-dnf` &c.)
explicitly so the driver works; don't drop it.
