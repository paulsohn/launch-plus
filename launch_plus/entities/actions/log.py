"""Action handler for <log> element."""

from __future__ import annotations

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

    def execute(self, context) -> list | None:
        from launch_plus.entities.helpers import resolve_value

        msg = resolve_value(self._message, context) or ""
        self._resolved_message = msg
        state = context._state
        state.track_node(
            {
                "package": "",
                "executable": "",
                "name": "",
                "ros_namespace": context._launch_configurations.get("ros_namespace"),
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
        self._include_chain = list(state.include_chain)
        state.resolved_actions.append(self)
        return None

    def serialize_resolved(self, indent: str = "  ") -> str | None:
        msg = getattr(self, "_resolved_message", None)
        if msg is None:
            return None
        return f'{indent}<log message="{self._esc(msg)}"/>\n'
