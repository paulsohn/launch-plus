"""Action handler for <log> element."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from roscope.entities.action import Action
from roscope.entities.expose import expose_action
from roscope.entities.parsing import _ActionParser
from roscope.parsers.entity import Entity


@expose_action("log")
class LogInfo(Action):
    """<log> — records a log message."""

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
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
