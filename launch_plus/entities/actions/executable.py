"""Action handler for <executable> element."""

from __future__ import annotations

from launch_plus.entities.actions.base import _TrackedAction
from launch_plus.entities.expose import expose_action
from launch_plus.entities.parsing import _ActionParser
from launch_plus.entities.substitution import Substitution
from launch_plus.parsers.entity import Entity


def _parse_optional(parser: _ActionParser, text: str | None) -> list | None:
    """Parse an optional attribute to tokens, or return None."""
    if text is None:
        return None
    return parser.parse_substitution(text)


@expose_action("executable")
class _TrackedExecutable(_TrackedAction):
    """Tracks an ExecuteProcess so the walker can render it as <executable>."""

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        if not parser.evaluate_condition(entity):
            return None
        cmd_raw = entity.get_attr("cmd", optional=True) or ""
        name_raw = entity.get_attr("name", optional=True)
        shell_raw = entity.get_attr("shell", optional=True)
        if shell_raw is None:
            shell = False
        elif isinstance(shell_raw, bool):
            shell = shell_raw
        else:
            shell = str(shell_raw).lower() in ("true", "1", "yes")
        return cls(
            cmd=parser.parse_substitution(cmd_raw),
            name=_parse_optional(parser, name_raw),
            shell=shell,
            _xml_envs=parser.parse_envs(entity),
        )

    def __init__(self, *, cmd=None, name=None, shell=False, **kwargs):
        xml_envs = kwargs.pop("_xml_envs", None)
        if isinstance(cmd, list):
            self._cmd = cmd
        elif cmd is not None:
            self._cmd = [cmd]
        else:
            self._cmd = []
        self._name = name
        self._shell = bool(shell)
        self._xml_envs = xml_envs
        self._detailed = False
        self._idx = -1  # set lazily in execute()

    def _ensure_tracked(self, state) -> int:
        if self._idx >= 0:
            return int(self._idx)
        self._idx = state.track_node(
            {
                "package": "",
                "executable": "",
                "name": str(self._name) if isinstance(self._name, str) else "",
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
            },
        )
        return int(self._idx)

    def execute(self, context) -> list | None:
        state = context._state
        self._ensure_tracked(state)
        if not self._detailed:
            self._detailed = True
            if self._xml_envs is not None:
                self._resolve_xml_details(context)
            else:
                self._resolve_shim_details(context)
        return None

    def _resolve_shim_details(self, context) -> None:
        """Resolve Python shim path details."""
        parts = []
        for part in self._cmd:
            raw_part = part
            if isinstance(part, Substitution) and context is not None:
                try:
                    result = part.perform(context)
                    part = result if result is not None else raw_part
                except Exception:
                    part = str(raw_part)
            parts.append(str(part))
        cmd_str = " ".join(parts)
        name = self._name
        if isinstance(name, Substitution) and context is not None:
            try:
                name = name.perform(context)
            except Exception:
                name = str(name) if name is not None else ""
        name_str = str(name) if name is not None else ""
        context._state.tracked["nodes"][self._idx]["cmd"] = cmd_str
        context._state.tracked["nodes"][self._idx]["name"] = name_str
        context._state.tracked["nodes"][self._idx]["shell"] = self._shell

    def _resolve_xml_details(self, context) -> None:
        """Resolve XML-parsed token structures into the tracked node entry."""
        from launch_plus.entities.helpers import resolve_value

        entry = context._state.tracked["nodes"][self._idx]
        cmd = resolve_value(self._cmd, context) or ""
        name = resolve_value(self._name, context) or ""
        entry["cmd"] = cmd
        entry["name"] = name
        entry["shell"] = self._shell
        ros_ns = context._launch_configurations.get("ros_namespace")
        if ros_ns:
            entry["ros_namespace"] = ros_ns
        # Env
        from launch_plus.entities.node_resolution import _env_overrides

        env = _env_overrides(context)
        for k_tokens, v_tokens in self._xml_envs or []:
            env[resolve_value(k_tokens, context) or ""] = resolve_value(v_tokens, context) or ""
        entry["env"] = env
