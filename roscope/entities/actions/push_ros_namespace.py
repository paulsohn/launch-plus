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
# - https://github.com/ros2/launch_ros/blob/rolling/launch_ros/launch_ros/actions/push_ros_namespace.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""Module for the `PushROSNamespace` action."""

from __future__ import annotations

import logging

from roscope.entities.action import Action
from roscope.entities.expose import expose_action
from roscope.entities.parsing import _ActionParser
from roscope.parsers.entity import Entity

logger = logging.getLogger("roscope")


@expose_action("push-ros-namespace")
class PushROSNamespace(Action):
    """
    Action that pushes the ros namespace.

    It's automatically popped when used inside a scoped `GroupAction`.
    There's no other way of popping it.
    """

    def __init__(self, namespace, **kwargs):
        super().__init__(**kwargs)
        self._namespace = namespace

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        """Return `PushROSNamespace` action and kwargs for constructing it."""
        _, kwargs = super().parse(entity, parser)
        kwargs["namespace"] = parser.parse_substitution(entity.get_attr("namespace"))
        return cls, kwargs

    def execute(self, context) -> list | None:
        from roscope.entities.helpers import _ros2_namespace_join, resolve_value

        ns = resolve_value(self._namespace, context)
        if ns:
            prev = context._launch_configurations.get("ros_namespace")
            context._launch_configurations["ros_namespace"] = _ros2_namespace_join(prev, ns)
        return None
