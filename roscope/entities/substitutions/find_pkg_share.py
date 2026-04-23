# Copyright 2019 Open Source Robotics Foundation, Inc.
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
# - https://github.com/ros2/launch_ros/blob/rolling/launch_ros/launch_ros/substitutions/find_package.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""``$(find-pkg-share pkg)`` substitution — resolves a package share directory."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from roscope.entities.expose import expose_substitution
from roscope.entities.substitution import Substitution

if TYPE_CHECKING:
    from roscope.entities.state import LaunchContext

logger = logging.getLogger("roscope")


@expose_substitution("find-pkg-share")
class FindPackageShareSubstitution(Substitution):
    """Resolve ``$(find-pkg-share <pkg>)`` to the package's share directory.

    Always returns a real filesystem path — source path in preview mode,
    install path in non-preview mode.
    """

    def __init__(self, *, package: list[Substitution]) -> None:
        self.package = package

    @classmethod
    def parse(cls, args: list[Any]) -> tuple[type[FindPackageShareSubstitution], dict[str, Any]]:
        if not args:
            raise ValueError("$(find-pkg-share ...) requires a package argument")
        return cls, {"package": args[0] if isinstance(args[0], list) else [args[0]]}

    def perform(self, ctx: LaunchContext) -> str:
        from roscope.entities.helpers import resolve_substitutions_from_tokens

        state = ctx._state
        pkg = resolve_substitutions_from_tokens(self.package, ctx)
        state.track_package(pkg)
        return state.resolve_pkg_share(pkg)

    def __str__(self) -> str:
        return f"$(find-pkg-share {''.join(str(t) for t in self.package)})"


# ─── Python-shim substitution ────────────────────────────────────────────────


class FindPackageShare(Substitution):
    """Python shim for ``FindPackageShare`` — always returns a real path."""

    def __init__(self, package):
        self._package_subs = package

    def _resolve_name(self, context=None):
        """Resolve package name. Returns ``(name, is_fallback)``."""
        subs = self._package_subs
        if isinstance(subs, str):
            return subs, False
        if context is not None:
            result = context.perform_substitution(subs)
            if result is not None:
                return result, False
        return str(subs), True

    def perform(self, context, **kwargs):
        state = context._state
        pkg, is_fallback = self._resolve_name(context)
        if not is_fallback:
            state.track_package(pkg)
        return state.resolve_pkg_share(pkg)

    def __str__(self):
        pkg, _ = self._resolve_name(None)
        return f"$(find-pkg-share {pkg})"
