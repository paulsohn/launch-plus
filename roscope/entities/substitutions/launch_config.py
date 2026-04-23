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
# - https://github.com/ros2/launch/blob/rolling/launch/launch/substitutions/launch_configuration.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""LaunchConfiguration — unified ``$(var name)`` / ``LaunchConfiguration("name")``.

In official ROS 2, both ``$(var x)`` in XML and ``LaunchConfiguration("x")``
in Python read from ``context.launch_configurations[x]``.  This module
provides the single implementation for both paths.
"""

from __future__ import annotations

import logging
from typing import Any

from roscope.entities.expose import expose_substitution
from roscope.entities.substitution import Substitution

logger = logging.getLogger("roscope")


@expose_substitution("var")
class LaunchConfiguration(Substitution):
    """Unified substitution: ``$(var name)`` (XML) and ``LaunchConfiguration("name")`` (Python).

    Reads from ``context.launch_configurations[name]`` at resolution time,
    matching the official ROS 2 ``LaunchConfiguration`` semantics.

    Construction:
    - Python shim: ``LaunchConfiguration("variable_name", default=...)``
    - XML parse: ``LaunchConfiguration.parse(args)`` → ``cls(variable_name=name_tokens)``
    """

    def __init__(self, variable_name=None, default=None, **kwargs):
        # variable_name can be:
        #   - str: from Python shim (e.g., LaunchConfiguration("my_var"))
        #   - list[Substitution]: from XML parse (e.g., [TextSubstitution("my_var")])
        self._name = variable_name
        self._default = default

    @classmethod
    def parse(cls, args: list[Any]) -> tuple[type[LaunchConfiguration], dict[str, Any]]:
        """Parse ``$(var name)`` from XML substitution syntax."""
        if not args:
            raise ValueError("$(var ...) requires a name argument")
        return cls, {"variable_name": args[0] if isinstance(args[0], list) else [args[0]]}

    def _resolve_name(self, context) -> str:
        """Resolve the variable name to a string."""
        name = self._name
        if isinstance(name, str):
            return name
        if isinstance(name, list):
            # list[Substitution] from XML parse — resolve tokens
            parts = []
            for t in name:
                if isinstance(t, Substitution):
                    result = t.perform(context)
                    parts.append(str(result) if result is not None else str(t))
                else:
                    parts.append(str(t))
            return "".join(parts)
        return str(name) if name is not None else ""

    def perform(self, context, **kwargs):
        """Resolve to the launch configuration value.

        Matching official ``LaunchConfiguration.perform()``:
        reads from ``context.launch_configurations[name]`` and returns the value.
        Values are always plain strings (DeclareLaunchArgument resolves defaults
        immediately at execute() time — no deferred resolution).
        """
        name = self._resolve_name(context)
        if context is not None:
            lc = getattr(context, "_launch_configurations", {})
            if name in lc:
                value = lc[name]
                return str(value) if value is not None else ""
        if self._default is not None:
            return str(self._default)
        # Not found
        logger.error("undefined variable: %s", name)
        return f"$(var {name})"

    def __str__(self):
        if isinstance(self._name, str):
            return self._name
        if isinstance(self._name, list):
            return f"$(var {''.join(str(t) for t in self._name)})"
        return f"$(var {self._name})"
