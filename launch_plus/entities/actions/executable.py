"""Action handler for <executable> / ExecuteProcess.

Matching official ``launch.actions.ExecuteProcess``.
Method definition order follows the official implementation.
"""

from __future__ import annotations

import shlex

from launch_plus.entities.action import Action
from launch_plus.entities.expose import expose_action
from launch_plus.entities.parsing import _ActionParser
from launch_plus.entities.substitution import Substitution, TextSubstitution
from launch_plus.entities.utilities import normalize_to_list_of_substitutions, perform_substitutions
from launch_plus.parsers.entity import Entity


@expose_action("executable")
class ExecuteProcess(Action):
    """Tracks an ExecuteProcess / <executable>.

    Matching official: ``cmd`` is stored as ``list[list[Substitution]]``
    where each inner list represents one command argument.
    """

    def __init__(self, *, cmd=None, name=None, **kwargs):
        # Normalize cmd: list of argument lists, matching official Executable
        if cmd is None:
            self.__cmd: list[list[Substitution]] = []
        elif isinstance(cmd, list) and cmd and isinstance(cmd[0], list):
            # Already list[list[Substitution]] (from parse)
            self.__cmd = cmd
        else:
            # From Python shim: list of mixed str/Substitution items
            self.__cmd = [normalize_to_list_of_substitutions(x) for x in cmd]
        self.__name = normalize_to_list_of_substitutions(name) if name is not None else None
        self.__additional_env = kwargs.pop("additional_env", None)
        self._idx = -1

    @classmethod
    def _parse_cmdline(cls, cmd: str, parser: _ActionParser) -> list[list[Substitution]]:
        """Parse text apt for command line execution.

        Matching official ``ExecuteProcess._parse_cmdline``: splits on
        whitespace boundaries while preserving substitutions.
        """
        result_args: list[list[Substitution]] = []
        arg: list[Substitution] = []

        def _append_arg() -> None:
            nonlocal arg
            result_args.append(arg)
            arg = []

        for sub in parser.parse_substitution(cmd):
            if isinstance(sub, TextSubstitution):
                tokens = shlex.split(sub.text)
                if not tokens:
                    _append_arg()
                    continue
                if sub.text[0].isspace():  # noqa: SIM102 — matches official
                    if len(arg) != 0:
                        _append_arg()
                arg.append(TextSubstitution(text=tokens[0]))
                if len(tokens) > 1:
                    _append_arg()
                    arg.append(TextSubstitution(text=tokens[-1]))
                if len(tokens) > 2:
                    result_args.extend([TextSubstitution(text=x)] for x in tokens[1:-1])
                if sub.text[-1].isspace():
                    _append_arg()
            else:
                arg.append(sub)
        if arg:
            result_args.append(arg)
        return result_args

    @classmethod
    def parse(cls, entity: Entity, parser: _ActionParser):
        if not parser.evaluate_condition(entity):
            return None
        cmd_raw = entity.get_attr("cmd", optional=True) or ""
        name_raw = entity.get_attr("name", optional=True)
        return cls(
            cmd=cls._parse_cmdline(cmd_raw, parser),
            name=parser.parse_substitution(name_raw) if name_raw else None,
            additional_env=parser.parse_envs(entity),
        )

    def _ensure_tracked(self, state) -> int:
        if self._idx >= 0:
            return int(self._idx)
        self._idx = state.track_node(
            {
                "package": "",
                "executable": "",
                "name": "",
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
            },
        )
        return int(self._idx)

    def execute(self, context) -> list | None:
        from launch_plus.entities.helpers import env_overrides, resolve_value

        state = context._state
        self._ensure_tracked(state)

        entry = state.tracked["nodes"][self._idx]

        # Resolve cmd: each arg is a list[Substitution] → resolved string
        cmd_parts = [perform_substitutions(context, arg) for arg in self.__cmd]
        entry["cmd"] = " ".join(cmd_parts)

        if self.__name is not None:
            entry["name"] = perform_substitutions(context, self.__name)

        ros_ns = context._launch_configurations.get("ros_namespace")
        if ros_ns:
            entry["ros_namespace"] = ros_ns

        env = env_overrides(context)
        if self.__additional_env is not None:
            for k_tokens, v_tokens in self.__additional_env:
                env[resolve_value(k_tokens, context) or ""] = resolve_value(v_tokens, context) or ""
        entry["env"] = env
        return None
