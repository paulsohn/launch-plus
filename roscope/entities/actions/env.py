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

# Originally from (planned to split):
# - https://github.com/ros2/launch/blob/rolling/launch/launch/actions/set_environment_variable.py
# - https://github.com/ros2/launch/blob/rolling/launch/launch/actions/unset_environment_variable.py
# - https://github.com/ros2/launch_ros/blob/rolling/launch_ros/launch_ros/actions/push_ros_namespace.py
# - https://github.com/ros2/launch_ros/blob/rolling/launch_ros/launch_ros/actions/set_remap.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""Action handlers for environment and configuration elements.

Covers: <set_env>, <unset_env>, <push-ros-namespace>, <set_remap>.
"""

from __future__ import annotations

import logging
import os

from roscope.entities.action import Action
from roscope.entities.expose import expose_action
from roscope.entities.helpers import _current_file
from roscope.entities.parsing import _ActionParser
from roscope.parsers.entity import Entity

logger = logging.getLogger("roscope")


@expose_action("set_env")
class SetEnvironmentVariable(Action):
    """Tracks SetEnvironmentVariable / <set_env>."""

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
            logger.error(
                "%s: SetEnvironmentVariable: resolved name is empty or None — skipping",
                _current_file(context),
            )
            return None
        value = resolve_value(self._value, context) or ""
        context.environment[name] = value
        return None


@expose_action("unset_env")
class UnsetEnvironmentVariable(Action):
    """Tracks UnsetEnvironmentVariable / <unset_env>."""

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        _, kwargs = super().parse(entity, parser)
        kwargs["name"] = parser.parse_substitution(entity.get_attr("name", optional=True) or "")
        return cls, kwargs

    def __init__(self, name=None, **kwargs):
        super().__init__(**kwargs)
        self._name = name

    def execute(self, context) -> list | None:
        from roscope.entities.helpers import resolve_value

        name = resolve_value(self._name, context)
        if not name:
            logger.error(
                "%s: UnsetEnvironmentVariable: resolved name is empty or None — skipping",
                _current_file(context),
            )
            return None
        if name in os.environ:
            logger.error(
                "%s: unset_env: '%s' exists in the process env and cannot be unset. "
                'Use SetEnvironmentVariable(name="%s", value="") '
                "or a scoped group instead",
                _current_file(context),
                name,
                name,
            )
        elif name in context.environment:
            del context.environment[name]
        else:
            logger.error(
                "%s: unset_env: environment variable '%s' is not set", _current_file(context), name
            )
        return None


@expose_action("push-ros-namespace")
class PushRosNamespace(Action):
    """Tracks PushRosNamespace / <push-ros-namespace>.

    Matching official: computes cumulative namespace and stores in
    ``context.launch_configurations['ros_namespace']``.
    """

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        _, kwargs = super().parse(entity, parser)
        kwargs["namespace"] = parser.parse_substitution(
            entity.get_attr("namespace", optional=True) or ""
        )
        return cls, kwargs

    def __init__(self, namespace=None, **kwargs):
        super().__init__(**kwargs)
        self._namespace = namespace

    def execute(self, context) -> list | None:
        from roscope.entities.helpers import _ros2_namespace_join, resolve_value

        ns = resolve_value(self._namespace, context)
        if ns:
            prev = context._launch_configurations.get("ros_namespace")
            context._launch_configurations["ros_namespace"] = _ros2_namespace_join(prev, ns)
        return None


@expose_action("set_remap")
class SetRemap(Action):
    """Tracks SetRemap / <set_remap>.

    Matching official: appends to ``context.launch_configurations['ros_remaps']``.
    """

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        _, kwargs = super().parse(entity, parser)
        kwargs["src"] = parser.parse_substitution(entity.get_attr("from", optional=True) or "")
        kwargs["dst"] = parser.parse_substitution(entity.get_attr("to", optional=True) or "")
        return cls, kwargs

    def __init__(self, src="", dst="", **kwargs):
        super().__init__(**kwargs)
        self._src = src
        self._dst = dst

    def execute(self, context) -> list | None:
        from roscope.entities.helpers import resolve_value

        src = resolve_value(self._src, context) or ""
        dst = resolve_value(self._dst, context) or ""
        remaps = context._launch_configurations.get("ros_remaps", [])
        remaps.append((src, dst))
        context._launch_configurations["ros_remaps"] = remaps
        return None
