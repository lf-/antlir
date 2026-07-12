# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

load("@prelude//cfg/exec_platform:marker.bzl", "get_exec_platform_marker")

def _execution_platform_impl(ctx: AnalysisContext) -> list[Provider]:
    constraints = dict()
    for cfg in ctx.attrs.configurations:
        constraints.update(cfg[ConfigurationInfo].constraints)
    cfg = ConfigurationInfo(constraints = constraints, values = {})

    name = ctx.label.raw_target()
    platform = ExecutionPlatformInfo(
        label = name,
        configuration = cfg,
        executor_config = CommandExecutorConfig(
            local_enabled = True,
            remote_enabled = False,
            use_windows_path_separators = ctx.attrs.use_windows_path_separators,
        ),
    )

    return [
        DefaultInfo(),
        platform,
        PlatformInfo(label = str(name), configuration = cfg),
        ExecutionPlatformRegistrationInfo(
            platforms = [platform],
            exec_marker_constraint = get_exec_platform_marker(),
        ),
    ]

# Like prelude's `execution_platform`, but accepts an arbitrary list of
# ConfigurationInfo deps to union (cpu + os + the may_run_local / may_run_remote
# markers that antlir's own rules require via `exec_compatible_with`).
execution_platform = rule(
    impl = _execution_platform_impl,
    attrs = {
        "configurations": attrs.list(attrs.dep(providers = [ConfigurationInfo])),
        "use_windows_path_separators": attrs.bool(),
    },
)
