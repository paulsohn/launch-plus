"""Action handler for <executable> element."""

from __future__ import annotations

from launch_plus.entities.expose import expose_action
from launch_plus.parsers.entity import Entity
from launch_plus.resolver import (
    _ActionParser,
    _state,
)


@expose_action("executable")
def _action_executable(entity: Entity, parser: _ActionParser) -> None:
    if not parser.evaluate_condition(entity):
        return
    cmd = parser.resolve(entity.get_attr("cmd", optional=True) or "")
    name = parser.resolve_optional(entity.get_attr("name", optional=True))
    shell_raw = entity.get_attr("shell", optional=True)
    if shell_raw is None:
        shell = False
    elif isinstance(shell_raw, bool):
        shell = shell_raw
    else:
        shell = str(shell_raw).lower() in ("true", "1", "yes")
    parser.track_node(
        {
            "package": "",
            "executable": "",
            "name": name or "",
            "namespace_stack": list(_state.namespace_stack),
            "explicit_namespace": None,
            "parameters": {},
            "param_files": [],
            "remappings": [],
            "env": dict(_state.env),
            "kind": "executable",
            "plugins": [],
            "target": None,
            "cmd": cmd,
            "shell": shell,
        }
    )
