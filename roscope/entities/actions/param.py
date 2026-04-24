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

# Originally from (planned to split and refactor):
# - https://github.com/ros2/launch/blob/rolling/launch/launch/actions/set_launch_configuration.py
# - https://github.com/ros2/launch_ros/blob/rolling/launch_ros/launch_ros/actions/set_parameter.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""Parameter-related action handlers.

Covers: SetLaunchConfiguration / SetParameter.
"""

from __future__ import annotations

from roscope.entities.action import Action
from roscope.entities.expose import expose_action
from roscope.entities.parsing import _ActionParser
from roscope.parsers.entity import Entity


@expose_action("let")
class SetLaunchConfiguration(Action):
    """Implements SetLaunchConfiguration / <let>: updates launch_configurations."""

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        _, kwargs = super().parse(entity, parser)
        kwargs["name"] = entity.get_attr("name", optional=True) or ""
        kwargs["value"] = parser.parse_substitution(entity.get_attr("value", optional=True) or "")
        return cls, kwargs

    def __init__(self, name=None, value=None, **kwargs):
        super().__init__(**kwargs)
        self._name = name
        self._value = value

    def execute(self, context) -> list | None:
        from roscope.entities.helpers import resolve_value

        name = str(self._name) if self._name else ""
        if not name:
            return None
        value = resolve_value(self._value, context)
        resolved_value = str(value) if value is not None else ""
        # Set _launch_configurations — single source of truth for $(var name)
        if context is not None and hasattr(context, "_launch_configurations"):
            context._launch_configurations[name] = resolved_value
        return None


@expose_action("set_parameter")
class SetParameter(Action):
    """Mirrors launch_ros SetParameter / <set_parameter>."""

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        _, kwargs = super().parse(entity, parser)
        kwargs["name"] = parser.parse_substitution(entity.get_attr("name", optional=True) or "")
        kwargs["value"] = parser.parse_substitution(entity.get_attr("value", optional=True) or "")
        return cls, kwargs

    def __init__(self, name=None, value=None, **kwargs):
        super().__init__(**kwargs)
        self._name = name
        self._value = value

    def execute(self, context) -> list | None:
        from roscope.entities.helpers import resolve_value

        name = resolve_value(self._name, context)
        if not name:
            return None
        value: object = resolve_value(self._value, context)
        if isinstance(value, str):
            try:
                value = int(value)
            except (ValueError, TypeError):
                try:
                    value = float(value)
                except (ValueError, TypeError):
                    pass
        # Matching official SetParameter: write to launch_configurations['global_params']
        gp_list = context._launch_configurations.setdefault("global_params", [])
        gp_list.append((name, value))
        return None
