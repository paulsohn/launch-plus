"""Action handler for <executable> element."""

from __future__ import annotations

import launch_plus.resolver as _R
from launch_plus.entities.actions.base import _TrackedAction
from launch_plus.entities.expose import expose_action
from launch_plus.entities.state import _PackageNotFetchedError
from launch_plus.parsers.entity import Entity
from launch_plus.resolver import (
    _ActionParser,
    _state,
)


@expose_action("executable")
class _TrackedExecutable(_TrackedAction):
    """Tracks an ExecuteProcess so the walker can render it as <executable>."""

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser) -> None:
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

    def __init__(self, *, cmd=None, name=None, shell=False, **kwargs):
        if isinstance(cmd, list):
            self._cmd = cmd
        elif cmd is not None:
            self._cmd = [cmd]
        else:
            self._cmd = []
        self._name = name
        self._shell = bool(shell)
        self._idx = _R._track_node(
            {
                "package": "",
                "executable": "",
                "name": str(name) if name is not None and not hasattr(name, "perform") else "",
                "namespace_stack": [],
                "explicit_namespace": None,
                "parameters": {},
                "param_files": [],
                "remappings": [],
                "env": {},
                "kind": "executable",
                "plugins": [],
                "target": None,
                "cmd": "",
                "shell": self._shell,
            }
        )

    def execute(self, context) -> list | None:
        parts = []
        for part in self._cmd:
            raw_part = part
            if hasattr(part, "perform") and context is not None:
                try:
                    result = part.perform(context)
                    part = result if result is not None else raw_part
                except _PackageNotFetchedError:
                    raise
                except Exception:
                    part = str(raw_part)
            parts.append(str(part))
        cmd_str = " ".join(parts)
        name = self._name
        if hasattr(name, "perform") and context is not None:
            try:
                name = name.perform(context)
            except _PackageNotFetchedError:
                raise
            except Exception:
                name = str(name) if name is not None else ""
        name_str = str(name) if name is not None else ""
        _R._state.tracked["nodes"][self._idx]["cmd"] = cmd_str
        _R._state.tracked["nodes"][self._idx]["name"] = name_str
        _R._state.tracked["nodes"][self._idx]["shell"] = self._shell
        return None
