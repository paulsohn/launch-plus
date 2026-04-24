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

"""``$(find-pkg-prefix pkg)`` substitution — resolves a package prefix directory."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from roscope.entities.expose import expose_substitution
from roscope.entities.substitution import Substitution

if TYPE_CHECKING:
    from roscope.entities.state import LaunchContext

logger = logging.getLogger(__name__)


@expose_substitution("find-pkg-prefix")
class FindPackagePrefixSubstitution(Substitution):
    """Resolve ``$(find-pkg-prefix <pkg>)`` to the package's install prefix.

    Official implementation (ament_index_python.packages.get_package_prefix) searches
    AMENT_PREFIX_PATH for ``<prefix>/share/ament_index/resource_index/packages/<pkg>``
    and returns the matching ``<prefix>``.  This works for both isolated install
    (``install/<pkg>/``) and merge-install (``install/``) layouts.

    Preview mode: error — source directories have no install prefix structure.
    Postbuild mode: AMENT index lookup; error if the package is not found.
    """

    def __init__(self, *, package: list[Substitution]) -> None:
        self.package = package

    @classmethod
    def parse(cls, args: list[Any]) -> tuple[type[FindPackagePrefixSubstitution], dict[str, Any]]:
        if not args:
            raise ValueError("$(find-pkg-prefix ...) requires a package argument")
        return cls, {"package": args[0] if isinstance(args[0], list) else [args[0]]}

    def perform(self, ctx: LaunchContext) -> str:
        from roscope.entities.helpers import resolve_substitutions_from_tokens

        state = ctx._state
        pkg = resolve_substitutions_from_tokens(self.package, ctx)
        state.track_package(pkg)

        if state.preview_mode:
            # In preview mode, packages are resolved to source workspace directories
            # which have no install prefix structure.  $(find-pkg-prefix) requires a
            # built install tree — it cannot be resolved statically from source.
            from roscope.entities.helpers import _current_file

            logger.error(
                "%s: $(find-pkg-prefix %s) is not supported in preview mode "
                "(source directories have no install prefix); "
                "build the workspace first or avoid this substitution in preview",
                _current_file(ctx),
                pkg,
            )
            raise LookupError(f"$(find-pkg-prefix {pkg}) unavailable in preview mode")

        # Postbuild mode: official ament_index_python behavior.
        # Iterate AMENT_PREFIX_PATH looking for the AMENT index marker file:
        #   <prefix>/share/ament_index/resource_index/packages/<pkg>
        # This works for both isolated and merge-install layouts.
        ament_prefix_path = os.environ.get("AMENT_PREFIX_PATH", "")
        for prefix_str in ament_prefix_path.split(":"):
            if not prefix_str:
                continue
            marker = (
                Path(prefix_str) / "share" / "ament_index" / "resource_index" / "packages" / pkg
            )
            if marker.exists():
                return prefix_str

        from roscope.entities.helpers import _current_file

        logger.error(
            "%s: $(find-pkg-prefix %s): package not found in AMENT index "
            "(AMENT_PREFIX_PATH=%r); source /opt/ros/<distro>/setup.bash",
            _current_file(ctx),
            pkg,
            ament_prefix_path or "<unset>",
        )
        raise LookupError(f"$(find-pkg-prefix {pkg}): not found in AMENT_PREFIX_PATH")

    def __str__(self) -> str:
        return f"$(find-pkg-prefix {''.join(str(t) for t in self.package)})"
