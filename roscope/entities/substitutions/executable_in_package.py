# Copyright 2018 Open Source Robotics Foundation, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Originally from:
# - https://github.com/ros2/launch_ros/blob/rolling/launch_ros/launch_ros/substitutions/executable_in_package.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""``$(exec-in-pkg executable package)`` substitution.

Mirrors ``launch_ros.substitutions.ExecutableInPackage``:

- **Preview mode**: raises ``LookupError`` — the install tree (libexec
  directory) is not available before ``colcon build``.
- **Post-build mode**: resolves the executable path from
  ``<package_prefix>/lib/<package>/<executable>`` using ``AMENT_PREFIX_PATH``,
  matching the official ``which``-style lookup.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any  # Any used in parse() return type

from roscope.entities.expose import expose_substitution
from roscope.entities.substitution import Substitution

if TYPE_CHECKING:
    from roscope.entities.launch_context import LaunchContext

logger = logging.getLogger("roscope")


@expose_substitution("exec-in-pkg")
class ExecutableInPackage(Substitution):
    """Resolve ``$(exec-in-pkg <executable> <package>)`` to an executable path.

    Official: ``ExecutableInPackage`` locates the executable in the package's
    ``lib/<package>`` directory (libexec).  Requires a built install tree.

    :raise LookupError: in preview mode (no install tree available).
    :raise LookupError: in post-build mode when the package or executable
        cannot be found in ``AMENT_PREFIX_PATH``.
    """

    def __init__(
        self,
        *,
        executable: list[Substitution] | None = None,
        package: list[Substitution] | None = None,
    ) -> None:
        self._executable: list[Substitution] = executable or []
        self._package: list[Substitution] = package or []

    @classmethod
    def parse(cls, args: list[Any]) -> tuple[type[ExecutableInPackage], dict[str, Any]]:
        """Parse ``$(exec-in-pkg <executable> <package>)``."""
        if not args or len(args) != 2:
            raise ValueError("$(exec-in-pkg ...) expects exactly 2 arguments: executable package")
        exe_arg = args[0] if isinstance(args[0], list) else [args[0]]
        pkg_arg = args[1] if isinstance(args[1], list) else [args[1]]
        return cls, {"executable": exe_arg, "package": pkg_arg}

    def perform(self, ctx: LaunchContext) -> str:
        from roscope.entities.helpers import _current_file, resolve_substitutions_from_tokens

        exe = resolve_substitutions_from_tokens(self._executable, ctx)
        pkg = resolve_substitutions_from_tokens(self._package, ctx)
        ctx._state.track_package(pkg)

        if ctx._state.preview_mode:
            logger.error(
                "%s: $(exec-in-pkg %s %s) requires a built install tree and cannot be "
                "resolved in preview mode; build the workspace first",
                _current_file(ctx),
                exe,
                pkg,
            )
            raise LookupError(f"$(exec-in-pkg {exe} {pkg}) unavailable in preview mode")

        # Post-build: find package prefix via AMENT index, then look up libexec.
        # Mirrors official FindPackagePrefix + which() logic.
        ament_prefix_path = os.environ.get("AMENT_PREFIX_PATH", "")
        package_prefix: str | None = None
        for prefix_str in ament_prefix_path.split(":"):
            if not prefix_str:
                continue
            marker = (
                Path(prefix_str) / "share" / "ament_index" / "resource_index" / "packages" / pkg
            )
            if marker.exists():
                package_prefix = prefix_str
                break

        if package_prefix is None:
            logger.error(
                "%s: $(exec-in-pkg %s %s): package '%s' not found in AMENT_PREFIX_PATH",
                _current_file(ctx),
                exe,
                pkg,
                pkg,
            )
            raise LookupError(f"$(exec-in-pkg {exe} {pkg}): package '{pkg}' not found")

        libexec_dir = os.path.join(package_prefix, "lib", pkg)
        if not os.path.isdir(libexec_dir):
            logger.error(
                "%s: $(exec-in-pkg %s %s): libexec directory '%s' does not exist",
                _current_file(ctx),
                exe,
                pkg,
                libexec_dir,
            )
            raise LookupError(
                f"$(exec-in-pkg {exe} {pkg}): libexec directory '{libexec_dir}' not found"
            )

        # which()-style search in libexec dir
        candidate = os.path.join(libexec_dir, exe)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

        logger.error(
            "%s: $(exec-in-pkg %s %s): executable '%s' not found in '%s'",
            _current_file(ctx),
            exe,
            pkg,
            exe,
            libexec_dir,
        )
        raise LookupError(
            f"$(exec-in-pkg {exe} {pkg}): executable '{exe}' not found in '{libexec_dir}'"
        )

    def __str__(self) -> str:
        exe = "".join(str(t) for t in self._executable)
        pkg = "".join(str(t) for t in self._package)
        return f"$(exec-in-pkg {exe} {pkg})"
