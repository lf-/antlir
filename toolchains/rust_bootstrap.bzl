# A drop-in replacement for prelude//toolchains:rust.bzl's system_rust_toolchain
# that additionally sets RUSTC_BOOTSTRAP=1 (and any extra rustc_env) on every
# rustc compile action.
#
# Why: many third-party crates (bytemuck, zerocopy, ppv-lite86, smallvec, ...)
# unconditionally opt into nightly `#![feature(...)]` gates behind cargo
# features that reindeer turns on. Our host toolchain is *stable* rustc
# (Fedora 1.96.1), so those gates are rejected (E0554) unless we open them with
# RUSTC_BOOTSTRAP=1 — the standard "pretend to be nightly" escape hatch. Rather
# than strip every nightly feature out of third-party/rust/BUCK by hand, we open
# the gate globally here.
#
# The stock system_rust_toolchain rule constructs RustToolchainInfo without a
# rustc_env, and the macro exposes no attr for it, so we can't just pass it in —
# hence this near-verbatim copy that threads rustc_env through. The main compile
# merges `toolchain_info.rustc_env` into the rustc action env (see prelude
# rust/build.bzl `process_env(compile_ctx, toolchain_info.rustc_env | ...)`).
#
# NOTE: a handful of crates (e.g. hashbrown's `nightly` feature, which
# specializes on `Copy` under min_specialization) fail for *semantic* reasons
# that RUSTC_BOOTSTRAP does not fix; those features are stripped in
# third-party/rust/BUCK instead.

load("@prelude//rust:rust_toolchain.bzl", "PanicRuntime", "RustToolchainInfo")

def _bootstrap_rust_toolchain_impl(ctx):
    return [
        DefaultInfo(),
        RustToolchainInfo(
            allow_lints = ctx.attrs.allow_lints,
            clippy_driver = RunInfo(args = ["clippy-driver"]),
            clippy_toml = ctx.attrs.clippy_toml[DefaultInfo].default_outputs[0] if ctx.attrs.clippy_toml else None,
            compiler = RunInfo(args = ["rustc"]),
            default_edition = ctx.attrs.default_edition,
            panic_runtime = PanicRuntime("unwind"),
            deny_lints = ctx.attrs.deny_lints,
            doctests = ctx.attrs.doctests,
            nightly_features = ctx.attrs.nightly_features,
            report_unused_deps = ctx.attrs.report_unused_deps,
            rustc_binary_flags = ctx.attrs.rustc_binary_flags,
            rustc_env = {"RUSTC_BOOTSTRAP": "1"} | ctx.attrs.rustc_env,
            rustc_flags = ctx.attrs.rustc_flags,
            rustc_target_triple = ctx.attrs.rustc_target_triple,
            rustc_test_flags = ctx.attrs.rustc_test_flags,
            rustdoc = RunInfo(args = ["rustdoc"]),
            rustdoc_flags = ctx.attrs.rustdoc_flags,
            warn_lints = ctx.attrs.warn_lints,
        ),
    ]

bootstrap_rust_toolchain = rule(
    impl = _bootstrap_rust_toolchain_impl,
    attrs = {
        "allow_lints": attrs.list(attrs.string(), default = []),
        "clippy_toml": attrs.option(attrs.dep(providers = [DefaultInfo]), default = None),
        "default_edition": attrs.option(attrs.string(), default = None),
        "deny_lints": attrs.list(attrs.string(), default = []),
        "doctests": attrs.bool(default = False),
        "nightly_features": attrs.bool(default = False),
        "report_unused_deps": attrs.bool(default = False),
        "rustc_binary_flags": attrs.list(attrs.arg(), default = []),
        "rustc_env": attrs.dict(attrs.string(), attrs.string(), default = {}),
        "rustc_flags": attrs.list(attrs.arg(), default = []),
        "rustc_target_triple": attrs.string(default = select({
            "ovr_config//cpu:arm64": "aarch64-unknown-linux-gnu",
            "ovr_config//cpu:x86_64": "x86_64-unknown-linux-gnu",
        })),
        "rustc_test_flags": attrs.list(attrs.arg(), default = []),
        "rustdoc_flags": attrs.list(attrs.arg(), default = []),
        "warn_lints": attrs.list(attrs.string(), default = []),
    },
    is_toolchain_rule = True,
)
