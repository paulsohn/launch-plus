"""Action handler for <log> element."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from launch_plus.entities.action import Action
from launch_plus.entities.expose import expose_action
from launch_plus.entities.parsing import _ActionParser
from launch_plus.parsers.entity import Entity


@expose_action("log")
class LogInfo(Action):
    """<log> — records a log message."""

    def __init__(self, message="", **kwargs):
        self.message = message

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        msg = parser.parse_substitution(entity.get_attr("message", optional=True) or "")
        return cls(message=msg)

    def execute(self, context) -> list:
        from launch_plus.entities.helpers import resolve_value

        msg = resolve_value(self.message, context) or ""
        return [LogInfo(message=msg)]

    def serialize_resolved(self) -> list[ET.Element]:
        if not self.message or not isinstance(self.message, str):
            return []
        elem = ET.Element("log")
        elem.set("message", self.message)
        return [elem]
