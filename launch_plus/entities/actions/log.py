"""Action handler for <log> element."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from launch_plus.entities.action import Action
from launch_plus.entities.expose import expose_action
from launch_plus.entities.parsing import _ActionParser
from launch_plus.parsers.entity import Entity


@expose_action("log")
class LogInfo(Action):
    """Tracks <log> — records a log message as a node entry."""

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        msg = parser.parse_substitution(entity.get_attr("message", optional=True) or "")
        return cls(message=msg)

    def __init__(self, message="", **kwargs):
        self._message = message
        self._resolved_message: str | None = None

    def execute(self, context) -> list:
        from launch_plus.entities.helpers import resolve_value

        msg = resolve_value(self._message, context) or ""
        resolved = LogInfo()
        resolved._resolved_message = msg
        return [resolved]

    def serialize_resolved(self) -> list[ET.Element]:
        msg = getattr(self, "_resolved_message", None)
        if msg is None:
            return []
        elem = ET.Element("log")
        elem.set("message", msg)
        return [elem]
