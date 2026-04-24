# Copyright 2020 Open Source Robotics Foundation, Inc.
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
# - https://github.com/ros2/launch_ros/blob/rolling/launch_ros/launch_ros/actions/set_remap.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""Module for the `SetRemap` action."""

from __future__ import annotations

import logging

from roscope.entities.action import Action
from roscope.entities.expose import expose_action
from roscope.entities.helpers import resolve_value
from roscope.entities.parsing import _ActionParser
from roscope.parsers.entity import Entity

logger = logging.getLogger("roscope")


@expose_action("set_remap")
class SetRemap(Action):
    """
    Action that sets a remapping rule in the current context.

    This remapping rule will be passed to all the nodes launched in the same scope, overriding
    the ones specified in the `Node` action constructor.
    """

    def __init__(self, src="", dst="", **kwargs):
        """Create a SetRemap action."""
        super().__init__(**kwargs)
        self._src = src
        self._dst = dst

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        _, kwargs = super().parse(entity, parser)
        kwargs["src"] = parser.parse_substitution(entity.get_attr("from"))
        kwargs["dst"] = parser.parse_substitution(entity.get_attr("to"))
        return cls, kwargs

    def execute(self, context) -> list | None:
        src = resolve_value(self._src, context) or ""
        dst = resolve_value(self._dst, context) or ""
        global_remaps = context._launch_configurations.get("ros_remaps", [])
        global_remaps.append((src, dst))
        context._launch_configurations["ros_remaps"] = global_remaps
        return None
