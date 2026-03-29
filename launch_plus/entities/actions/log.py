"""Action handler for <log> element."""

from __future__ import annotations

from launch_plus.entities.expose import expose_action
from launch_plus.parsers.entity import Entity
from launch_plus.resolver import (
    _ActionParser,
    _state,
)


@expose_action("log")
def _action_log(entity: Entity, parser: _ActionParser) -> None:
    msg = parser.resolve(entity.get_attr("message", optional=True) or "")
    parser.track_node(
        {
            "package": "",
            "executable": "",
            "name": "",
            "namespace_stack": list(_state.namespace_stack),
            "explicit_namespace": None,
            "parameters": {},
            "param_files": [],
            "remappings": [],
            "env": {},
            "kind": "log",
            "plugins": [],
            "target": None,
            "message": msg,
        }
    )
