# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# OSS stub for the `to-current-distro-platform` transition.
#
# `antlir/antlir2/bzl/feature/feature.bzl` declares the `distro_platform_deps`
# attribute as `attrs.transition_dep(cfg = "antlir//antlir/distro/transition:
# to-current-distro-platform")`, so buck2 must be able to look up that target to
# configure *any* feature -- even when the dict is empty.
#
# Meta's internal implementation reconfigures those deps onto the image's
# "distro platform" (used when building buck-native binaries to install into an
# image). That whole `antlir/distro/...` tree is not part of the OSS release
# (see BUGS.md #3), and the OSS features never populate `distro_platform_deps`
# (install.bzl defaults `transition_to_distro_platform = "no"`, which routes
# `src` through the normal deps instead). So an identity transition is both
# sufficient and correct here: it satisfies the attr schema and is never
# actually exercised on a real dep.

def _to_current_distro_platform_impl(ctx: AnalysisContext) -> list[Provider]:
    def _impl(platform: PlatformInfo) -> PlatformInfo:
        # identity: leave the platform configuration untouched
        return platform

    return [
        DefaultInfo(),
        TransitionInfo(impl = _impl),
    ]

to_current_distro_platform = rule(
    impl = _to_current_distro_platform_impl,
    attrs = {},
    is_configuration_rule = True,
)
