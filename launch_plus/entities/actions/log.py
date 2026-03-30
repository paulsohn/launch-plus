"""Action handler for <log> element."""

from __future__ import annotations

from launch_plus.entities.actions.base import _TrackedAction
from launch_plus.entities.expose import expose_action
from launch_plus.entities.helpers import _track_node
from launch_plus.entities.xml_resolver import _ActionParser
from launch_plus.parsers.entity import Entity


@expose_action("log")
class _LogAction(_TrackedAction):
    """Tracks <log> — records a log message as a node entry."""

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        msg = parser.parse_substitution(entity.get_attr("message", optional=True) or "")
        return cls(message=msg)

    def __init__(self, message="", **kwargs):
        self._message = message

    def execute(self, context) -> list | None:
        from launch_plus.entities.xml_resolver import resolve_value

        msg = resolve_value(self._message, context) or ""
        _track_node(
            context._state,
            {
                "package": "",
                "executable": "",
                "name": "",
                "namespace_stack": list(context._state.namespace_stack),
                "explicit_namespace": None,
                "parameters": {},
                "param_files": [],
                "remappings": [],
                "env": {},
                "kind": "log",
                "plugins": [],
                "target": None,
                "message": msg,
            },
        )
        return None
