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
# - https://github.com/ros2/launch/blob/rolling/launch/launch/actions/emit_event.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""Module for the EmitEvent action."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

from roscope.entities.action import Action
from roscope.entities.expose import expose_action

logger = logging.getLogger("roscope")


@expose_action("emit_event")
class EmitEvent(Action):
    """Action that emits an event when executed (TODO)."""

    def __init__(self, *, event, **kwargs):
        super().__init__(**kwargs)
        self.event = event

    # @classmethod
    # def parse(cls, entity: Entity, parser: _ActionParser):
    #     event = entity.get_attr("event", optional=True) or ""
    #     return cls(event=event)

    def execute(self, context) -> list:
        from roscope.entities.helpers import resolve_value

        event = resolve_value(self.event, context) or ""
        return [EmitEvent(event=event)]

    def serialize_resolved(self) -> list[ET.Element]:
        if not self.event:
            return []
        elem = ET.Element("emit_event")
        elem.set("event", self.event)
        return [elem]
