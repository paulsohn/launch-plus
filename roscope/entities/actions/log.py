# Copyright 2025 Open Source Robotics Foundation, Inc.
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
# - https://github.com/ros2/launch/blob/rolling/launch/launch/actions/log.py
# Modified for roscope project by Taeseung Sohn, 2026.

"""Action handler for <log> element."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from roscope.entities.action import Action
from roscope.entities.expose import expose_action
from roscope.entities.parsing import Parser
from roscope.parsers.entity import Entity


@expose_action("log")
class LogInfo(Action):
    """<log> — records a log message."""

    @classmethod
    def parse(cls, entity: Entity, parser: Parser):
        _, kwargs = super().parse(entity, parser)
        msg_raw = entity.get_attr("message", optional=True) or ""
        kwargs["message"] = parser.parse_substitution(msg_raw)
        return cls, kwargs

    def __init__(self, message="", **kwargs):
        super().__init__(**kwargs)
        self.message = message

    def execute(self, context) -> list:
        from roscope.entities.helpers import resolve_value

        msg = resolve_value(self.message, context) or ""
        return [LogInfo(message=msg)]

    def serialize_resolved(self) -> list[ET.Element]:
        if not self.message or not isinstance(self.message, str):
            return []
        elem = ET.Element("log")
        elem.set("message", self.message)
        return [elem]
