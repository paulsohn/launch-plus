"""Action handler for <log> element."""

from __future__ import annotations

import launch_plus.resolver as _R
from launch_plus.entities.actions.base import _TrackedAction
from launch_plus.entities.expose import expose_action
from launch_plus.entities.xml_resolver import _ActionParser
from launch_plus.parsers.entity import Entity


@expose_action("log")
class _LogAction(_TrackedAction):
    """Tracks <log> — records a log message as a node entry."""

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        msg = parser.resolve(entity.get_attr("message", optional=True) or "")
        return cls(message=msg)

    def __init__(self, message="", **kwargs):
        self._message = message

    def execute(self, context) -> list | None:
        _R._track_node(
            {
                "package": "",
                "executable": "",
                "name": "",
                "namespace_stack": list(_R._state.namespace_stack),
                "explicit_namespace": None,
                "parameters": {},
                "param_files": [],
                "remappings": [],
                "env": {},
                "kind": "log",
                "plugins": [],
                "target": None,
                "message": self._message,
            }
        )
        return None
